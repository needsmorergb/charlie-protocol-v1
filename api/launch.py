"""Build the coin-creation transaction for a dev to sign. Never sign for them.

The launch door's server half. Three things it does, and one it never does:

* GET with `authority`, `name`, `symbol`, `uri`, `shares`: validates the split,
  including its transaction size, before taking a mint keypair (a
  pre-ground one from `CHARLIE_MINT_POOL`, whose address ends in the
  protocol's suffix; random when no pool is configured), builds pump's `create` with the dev as creator, signs it with the
  MINT (a key that is powerless the moment the instruction runs), simulates
  it against mainnet, and returns it for the dev's wallet to sign and send.
* GET with `status` and `mint`: whether that create transaction has landed
  and the bonding curve exists, so the page knows when step two may begin.
  Step two is `api/enroll.py`, unchanged: the same create-config-and-set-split
  transaction `/enroll` sends for a coin with no config.
* POST multipart with `file` and the text fields: uploads the image and the
  metadata JSON through pump's own IPFS endpoint, the one its UI uses, and
  returns the metadata URI `create` needs. The browser cannot call that
  endpoint itself (no CORS), so this relays it. Nothing is stored here.
* It never holds the dev's key, never signs as the dev, and never sends.

**Every transaction returned has already been simulated against mainnet and
came back with no error.** Same rule as `api/enroll.py`, for the same reason.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import traceback
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from indexer import enroll, launch, launchbuy, legs, mint_pool, relay  # noqa: E402
from indexer.base58 import decode, encode  # noqa: E402
from indexer.message import MessageError  # noqa: E402
from indexer.rpc import RpcClient, RpcError  # noqa: E402

BASE58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")

# pump's own metadata pinning endpoint: multipart `file` plus the text
# fields, answering `{"metadata": {...}, "metadataUri": "https://ipfs.io/ipfs/..."}`.
# Measured 2026-09-14 with an 8x8 PNG: HTTP 200 and a resolvable URI.
PUMP_IPFS = "https://pump.fun/api/ipfs"
MAX_IMAGE_BYTES = 4_000_000
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}

# The rent a launch costs the dev, measured 2026-09-14 from mainnet's own
# getMinimumBalanceForRentExemption at the sizes pump's `create` allocates
# (mint 82, curve token account 165, metadata 679, bonding curve 151) and the
# fee-share config (1024). Step one and step two, in lamports.
CREATE_RENT_LAMPORTS = 8_072_120
CONFIG_RENT_LAMPORTS = 5_852_160


def _address(value: str) -> str | None:
    if not value or len(value) < 32 or len(value) > 44 or any(c not in BASE58 for c in value):
        return None
    try:
        raw = decode(value)
    except Exception:
        return None
    return value if len(raw) == 32 and encode(raw) == value else None


def _endpoints():
    configured = os.environ.get("CHARLIE_RPC_URLS", "").strip()
    return [u.strip() for u in configured.split(",") if u.strip()] or None


def _rpc():
    return RpcClient(_endpoints()) if _endpoints() else RpcClient()


# -- multipart, both directions -----------------------------------------------------


def parse_multipart(body: bytes, content_type: str) -> tuple[dict[str, str], dict[str, tuple[str, str, bytes]]]:
    """`(fields, files)` from a multipart/form-data body. `files` maps the
    field name to `(filename, content_type, bytes)`. Written here because
    the stdlib's `cgi` is gone and nothing else should be imported for it."""
    marker = "boundary="
    if marker not in content_type:
        raise ValueError("multipart body without a boundary")
    boundary = content_type.split(marker, 1)[1].split(";")[0].strip().strip('"').encode()
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, str, bytes]] = {}
    for part in body.split(b"--" + boundary):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        head, _sep, data = part.partition(b"\r\n\r\n")
        disposition = ""
        ctype = "application/octet-stream"
        for line in head.decode("utf-8", "replace").split("\r\n"):
            lower = line.lower()
            if lower.startswith("content-disposition:"):
                disposition = line
            elif lower.startswith("content-type:"):
                ctype = line.split(":", 1)[1].strip()
        name = _disposition_value(disposition, "name")
        if name is None:
            continue
        filename = _disposition_value(disposition, "filename")
        if filename is not None:
            files[name] = (filename, ctype, data)
        else:
            fields[name] = data.decode("utf-8", "replace")
    return fields, files


def _disposition_value(disposition: str, key: str) -> str | None:
    for piece in disposition.split(";"):
        piece = piece.strip()
        if piece.startswith(key + "="):
            return piece[len(key) + 1:].strip('"')
    return None


def build_multipart(fields: dict[str, str], files: dict[str, tuple[str, str, bytes]]) -> tuple[bytes, str]:
    boundary = "charlie" + uuid.uuid4().hex
    out = bytearray()
    for name, value in fields.items():
        out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n".encode()
        out += value.encode("utf-8") + b"\r\n"
    for name, (filename, ctype, data) in files.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                f"filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n").encode()
        out += data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def pin_metadata(fields: dict[str, str], image: tuple[str, str, bytes], *, opener=None) -> dict:
    """Relay to pump's IPFS endpoint. Returns its JSON. `opener` is for tests."""
    body, ctype = build_multipart(fields, {"file": image})
    request = urllib.request.Request(PUMP_IPFS, body, {
        "Content-Type": ctype,
        "User-Agent": "Mozilla/5.0 (charlie-protocol launch door)",
        "Accept": "application/json",
    }, method="POST")
    open_ = opener or urllib.request.urlopen
    with open_(request, timeout=45) as response:
        return json.loads(response.read().decode("utf-8"))


# -- the handler ----------------------------------------------------------------------


class handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        query = parse_qs(urlparse(self.path).query)
        one = lambda k: (query.get(k) or [""])[0].strip()  # noqa: E731
        try:
            if one("status"):
                return self._status(one("status"), one("mint"))
            if not one("authority"):
                return self._describe()
            return self._build(one("authority"), one("name"), one("symbol"), one("uri"), one("shares"), one("buy"))
        except launch.LaunchError as exc:
            return self._fail(str(exc))
        except launchbuy.BuyError as exc:
            return self._fail(str(exc))
        except enroll.EnrollError as exc:
            return self._fail(str(exc))
        except mint_pool.PoolError as exc:
            # The operator's problem, not the creator's: say so, with 503.
            return self._fail(f"Launching is paused: {exc}", status=503)
        except MessageError as exc:
            return self._fail(f"This split does not fit a transaction: {exc}")
        except RpcError as exc:
            return self._fail(f"The chain answered an error: {exc}", status=502)
        except Exception:  # pragma: no cover - the last line of defence
            traceback.print_exc()
            return self._fail("Could not read the chain just now. Nothing was built.", status=502)

    def do_POST(self):  # noqa: N802
        if (parse_qs(urlparse(self.path).query).get("send") or [""])[0]:
            return self._relay()
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_IMAGE_BYTES + 64_000:
                return self._fail("The upload is empty or larger than 4 MB.")
            body = self.rfile.read(length)
            fields, files = parse_multipart(body, self.headers.get("Content-Type") or "")
            image = files.get("file")
            if image is None:
                return self._fail("Attach an image. pump shows it beside the coin everywhere.")
            filename, ctype, data = image
            if ctype not in IMAGE_TYPES:
                return self._fail("The image must be a PNG, JPEG, GIF or WebP.")
            if len(data) > MAX_IMAGE_BYTES:
                return self._fail("The image is larger than 4 MB.")
            name = (fields.get("name") or "").strip()
            symbol = (fields.get("symbol") or "").strip()
            # The same limits `create` will enforce, before anything is pinned.
            launch.validate_metadata(name, symbol, "https://placeholder.invalid/")
            forward = {
                "name": name,
                "symbol": symbol,
                "description": (fields.get("description") or "").strip()[:1000],
                "showName": "true",
            }
            for key in ("twitter", "telegram", "website"):
                value = (fields.get(key) or "").strip()
                if value:
                    forward[key] = value[:200]
            try:
                pinned = pin_metadata(forward, (filename or "image", ctype, data))
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                return self._fail(f"pump's metadata service did not answer ({exc}). Nothing was created; try again.", status=502)
            uri = (pinned or {}).get("metadataUri") or ""
            try:
                launch.validate_metadata(name, symbol, uri)
            except launch.LaunchError:
                return self._fail("pump's metadata service answered without a usable URI. Nothing was created; try again.", status=502)
            return self._send(200, {"metadataUri": uri, "metadata": pinned.get("metadata")})
        except launch.LaunchError as exc:
            return self._fail(str(exc))
        except Exception:  # pragma: no cover
            traceback.print_exc()
            return self._fail("The upload could not be read.", status=400)

    def _relay(self):
        """POST `?send=1` with JSON `{"transaction": base64, "signed": ...}`:
        send what the dev's wallet signed, and send it again when asked. The
        wallet's own send was seen to drop a split on mainnet with no way to
        resend it; see `indexer/relay.py`. `transaction` is what this door
        built; `signed` is whatever the wallet's `signTransaction` returned,
        in any of the shapes wallets use. The answer carries the fully signed
        transaction, so a resend posts that alone. Nothing is signed here."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 16_000:
                return self._fail("That is not a signed transaction.")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            built = base64.b64decode(body.get("transaction") or "", validate=True)
        except Exception:
            return self._fail("That is not a signed transaction.")
        try:
            transaction = relay.assemble(built, body["signed"]) if body.get("signed") else built
            signature = relay.send(_rpc(), transaction)
        except relay.RelayError as exc:
            return self._fail(f"Not sent: {exc}.")
        except RpcError as exc:
            # Not a verdict on the transaction: the page asks again.
            return self._fail(f"The chain did not take it just now: {exc}", status=502)
        return self._send(200, {"signature": signature, "sent": True,
                                "transaction": base64.b64encode(transaction).decode()})

    # -- the three GETs --

    def _describe(self):
        """What this door is, before a wallet connects. The page reads the
        toll from here rather than hardcoding it, like `/enroll` does."""
        return self._send(200, {
            "open": legs.TOLL_DESTINATION is not None,
            "toll": {"address": legs.TOLL_DESTINATION, "bps": enroll.TOLL_BPS},
            "limits": {"name_bytes": launch.MAX_NAME_BYTES, "symbol_bytes": launch.MAX_SYMBOL_BYTES,
                       "uri_bytes": launch.MAX_URI_BYTES, "image_bytes": MAX_IMAGE_BYTES},
            "rent_lamports": {"create": CREATE_RENT_LAMPORTS, "config": CONFIG_RENT_LAMPORTS},
            "steps": 2,
            # The pre-ground addresses: whether there is a pool, the mark its
            # addresses end in, and how many are left. A configured pool with
            # none left closes the door on the page before anyone fills the form.
            "mints": mint_pool.describe(_rpc()),
        })

    def _build(self, authority: str, name: str, symbol: str, uri: str, raw_shares: str, raw_buy: str = ""):
        dev = _address(authority)
        if dev is None:
            return self._fail("That is not a valid wallet address.")
        if legs.TOLL_DESTINATION is None:
            return self._fail("Launching is not open yet: the protocol's collection address has not been set.")
        meta = launch.validate_metadata(name, symbol, uri)
        shares = []
        if not raw_shares:
            return self._fail("Choose the fee split before building the coin.")
        for item in raw_shares.split(","):
            address, sep, bps = item.partition(":")
            if not sep or not bps.isascii() or not bps.isdecimal():
                return self._fail("Each split row needs an address and whole-number basis points.")
            shares.append(enroll.Share(address, int(bps)))
        launch.preflight(dev, shares, meta)
        # The account does not exist yet, so a full split simulation must wait.
        # Still reject impossible splits (including oversized messages) BEFORE
        # asking the creator to pay for the coin's accounts.
        rpc = _rpc()
        # A pre-ground mint whose address carries the protocol's suffix when
        # the pool is configured; a random one when it is not (local runs,
        # tests). A configured pool with nothing left refuses rather than
        # quietly launching a coin without the mark.
        pool = mint_pool.configured()
        mint = launch.new_mint() if pool is None else mint_pool.pick(pool, rpc)
        enroll.enrollment_message(mint.address, dev, shares, "11111111111111111111111111111111", create=True)
        # The dev's buy at launch, when asked for: priced from pump's global
        # (a new curve starts at its initial reserves) and bundled after
        # `create` so nobody trades before the dev holds tokens. Empty means
        # no buy, which is the page's default.
        buy = None
        extra = ()
        if raw_buy:
            lamports = launchbuy.parse_sol(raw_buy)
            global_, fee_config, accumulator_exists = launchbuy.observe(rpc, dev)
            quoted = launchbuy.quote(lamports, global_, fee_config, mint.address, dev)
            extra = launchbuy.instructions(mint.address, dev, global_, quoted, accumulator_exists=accumulator_exists)
            buy = launchbuy.describe(quoted)
        blockhash = rpc.call("getLatestBlockhash", [{"commitment": "finalized"}])["value"]["blockhash"]
        message = launch.create_message(mint.address, dev, meta, blockhash, extra=extra)
        transaction = launch.partially_signed(message, mint)
        encoded = base64.b64encode(transaction).decode()

        # The gate. A simulation that reports an error is a transaction the
        # dev must not be asked to sign.
        simulated = rpc.call("simulateTransaction", [encoded, {
            "encoding": "base64", "sigVerify": False,
            "replaceRecentBlockhash": True, "commitment": "processed",
        }])
        value = (simulated or {}).get("value") or {}
        if value.get("err") is not None:
            return self._fail(_explain(value), detail={"err": value.get("err"), "logs": (value.get("logs") or [])[-8:]})

        return self._send(200, {
            # base58 of the whole partially-signed transaction: signature
            # count (2), the dev's slot zeroed, the mint's signature, then the
            # message. Phantom parses `message` as a transaction and fills the
            # zero slot; the mint's signature is preserved.
            "signable": encode(transaction),
            "transaction": encoded,
            "mint": mint.address,
            "blockhash": blockhash,
            "simulated": True,
            "split_checked": True,
            # None when the dev asked for no buy. Otherwise what the page
            # shows: SOL in, the bound, tokens, share of supply, fees, and
            # that it lands before the split is set.
            "buy": buy,
            "units": value.get("unitsConsumed"),
            "name": meta.name, "symbol": meta.symbol, "uri": meta.uri,
            "rent_lamports": CREATE_RENT_LAMPORTS,
            # Step two is /enroll's own builder, for this mint, once the
            # create has landed. The page polls `status` until then.
            "next": f"/api/enroll?mint={mint.address}&authority={dev}",
        })

    def _status(self, signature: str, mint: str):
        if not signature or any(c not in BASE58 for c in signature) or len(signature) > 90:
            return self._fail("That is not a transaction signature.")
        mint_address = _address(mint)
        rpc = _rpc()
        statuses = rpc.call("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
        row = ((statuses or {}).get("value") or [None])[0]
        landed = bool(row) and row.get("confirmationStatus") in ("confirmed", "finalized") and row.get("err") is None
        failed = bool(row) and row.get("err") is not None
        curve = False
        if mint_address:
            account = rpc.call("getAccountInfo", [enroll.bonding_curve_address(mint_address), {"encoding": "base64"}])
            curve = bool((account or {}).get("value"))
        return self._send(200, {
            "signature": signature,
            "seen": bool(row),
            "confirmation": (row or {}).get("confirmationStatus"),
            "landed": landed,
            "failed": failed,
            "err": (row or {}).get("err"),
            "curve": curve,
            # Step two may start when the curve exists. The signature status
            # alone is not enough: a `processed` status can still be dropped.
            "ready": landed and curve,
        })

    # -- plumbing --

    def _fail(self, message: str, *, status: int = 400, detail=None):
        body = {"error": message}
        if detail:
            body["detail"] = detail
        return self._send(status, body)

    def _send(self, status: int, body: dict):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)


def _explain(value) -> str:
    """pump's refusal of a `create`, in words a dev can act on. Each branch
    names something seen in a real simulation while this was built."""
    logs = " ".join(value.get("logs") or [])
    if "already in use" in logs:
        # Seen once, with a deterministic test seed that somebody else had
        # also used as a mint. Random seeds make this a one-in-2^256 event;
        # the page simply asks again and gets a new mint.
        return "That mint address is already taken, which should never happen twice. Try again."
    if "NameTooLong" in logs or "SymbolTooLong" in logs or "UriTooLong" in logs:
        return "pump's metadata refused a field as too long. Shorten the name or ticker."
    if "Program 11111111111111111111111111111111 failed: custom program error: 0x1" in logs:
        sol = (CREATE_RENT_LAMPORTS + CONFIG_RENT_LAMPORTS) / 1e9
        return (f"The connected wallet holds too little SOL. Creating the coin and its "
                f"fee config costs about {sol:.3f} SOL of rent plus two network fees, "
                "paid by the wallet that signs. Top it up and try again.")
    return "pump refused to create this coin. Nothing was sent and nothing changed."

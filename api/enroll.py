"""Build the fee-split transaction for a dev to sign. Never sign it.

The authority is the dev's key. It stays in the dev's wallet, so this returns
an UNSIGNED transaction and the browser hands it to the wallet. Nothing here
holds a key, and nothing here sends.

**Every transaction returned has already been simulated against mainnet and
came back with no error.** A builder that hands a wallet a transaction it has
not run is asking the dev to pay a fee to discover a bug, and pump allows the
split to be changed exactly once -- so a wasted attempt is not merely a wasted
fee, it can be the coin's only chance.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from indexer import enroll, pump  # noqa: E402
from indexer.pump import DecodeError  # noqa: E402
from indexer.rpc import RpcClient  # noqa: E402

BASE58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def _address(value: str) -> str | None:
    from indexer.base58 import decode, encode
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


def _shares(raw: str):
    """`address:bps,address:bps`. Parsed strictly -- a split is money, and a
    field this page could not read must never be silently dropped from it.
    """
    rows = []
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        address, _sep, bps = chunk.partition(":")
        if not _sep:
            raise enroll.EnrollError(f"Could not read '{chunk}' as address:bps.")
        if _address(address.strip()) is None:
            raise enroll.EnrollError(f"{address.strip()} is not a valid Solana address.")
        try:
            value = int(bps)
        except ValueError:
            raise enroll.EnrollError(f"'{bps}' is not a whole number of bps.") from None
        rows.append(enroll.Share(address.strip(), value))
    return rows


class handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        query = parse_qs(urlparse(self.path).query)
        one = lambda k: (query.get(k) or [""])[0].strip()  # noqa: E731

        mint = _address(one("mint"))
        authority = _address(one("authority"))
        if mint is None:
            return self._fail("That is not a valid contract address.")
        if authority is None:
            return self._fail("That is not a valid wallet address.")

        try:
            rpc = RpcClient(_endpoints()) if _endpoints() else RpcClient()
            curve = pump.read_bonding_curve(rpc, mint)
            try:
                config = pump.read_sharing_config(rpc, curve)
            except DecodeError as exc:
                # "This coin has no fee split" and "we could not read this
                # coin's fee split" are different facts, and only one of them
                # is about the coin. A bare `except Exception` here reported
                # every transport failure as the first, and the page turned
                # that into advice -- the same mistake the verify page made,
                # which the rest of this module exists to avoid.
                #
                # The marker is what read_sharing_config raises when the
                # creator is an ordinary address, which is the ~95% case for
                # a fresh launch. Anything else is not an answer.
                if pump.NO_FEE_SPLIT_MARKER not in str(exc):
                    return self._fail(
                        "This coin has a fee-sharing config whose bytes did "
                        "not decode. That is a fact about the account, not "
                        "about your wallet, and nothing here can act on it.",
                        status=502,
                    )
                config = None

            if not one("shares"):
                # Inspection only: what the coin looks like today, so the page
                # can show the current split and say whether this wallet may
                # change it BEFORE anyone fills a form in.
                return self._send(200, {
                    "mint": mint,
                    "config": config.address if config else None,
                    "admin": config.admin if config else None,
                    "admin_revoked": bool(config.admin_revoked) if config else None,
                    "owns": enroll.owns(config, authority),
                    "current": [{"address": a, "bps": b} for a, b in (config.shareholders if config else ())],
                    "reason": None if config else "no_sharing_config",
                    # The create path. A coin with no config is enrolled by
                    # its CREATOR -- the bonding curve's creator field --
                    # who is the only key pump lets create one.
                    "creator": None if config else curve.creator,
                    "can_create": (config is None) and enroll.may_create(curve, authority),
                    # The protocol's fixed share, so the page can pin the row
                    # rather than hardcode it. None while unset, and the
                    # build path refuses in that case.
                    "toll": {"address": enroll.TOLL_DESTINATION, "bps": enroll.TOLL_BPS},
                    # Read from the bonding curve, and three-valued on
                    # purpose: absent is not off. A cashback coin routes its
                    # whole creator fee to traders, so every leg of every
                    # split is zero and enrolling spends the coin's one
                    # permanent change for nothing.
                    "cashback": curve.cashback,
                    "graduated": bool(curve.graduated),
                })

            shares = _shares(one("shares"))
            enroll.preflight(config, authority, shares, curve=curve)

            blockhash = rpc.call("getLatestBlockhash", [{"commitment": "finalized"}])
            blockhash = blockhash["value"]["blockhash"]
            # One signature either way. No config: create it and set the
            # split in the same transaction. Config: set the split.
            message = enroll.enrolment_message(
                mint, authority, shares, blockhash,
                create=config is None,
                current=[a for a, _bps in config.shareholders] if config else (),
            )
            unsigned = bytes([1]) + b"\x00" * 64 + message
            encoded = base64.b64encode(unsigned).decode()

            # The gate. A simulation that reports an error is a transaction the
            # dev must not be asked to sign, and its message is the program's
            # own words about why.
            simulated = rpc.call("simulateTransaction", [encoded, {
                "encoding": "base64", "sigVerify": False,
                "replaceRecentBlockhash": True, "commitment": "processed",
            }])
            value = (simulated or {}).get("value") or {}
            if value.get("err") is not None:
                return self._fail(
                    _explain(value),
                    detail={"err": value.get("err"), "logs": (value.get("logs") or [])[-6:]},
                )

            from indexer.base58 import encode as b58
            return self._send(200, {
                # base58 of the MESSAGE, which is what a wallet's
                # signAndSendTransaction request takes. The page needs no
                # bundled Solana library to sign, so it ships no third-party
                # code and there is no build step.
                "message": b58(message),
                "transaction": encoded,
                "blockhash": blockhash,
                "simulated": True,
                "units": value.get("unitsConsumed"),
                "creates_config": config is None,
                "summary": [{"address": s.address, "bps": s.bps} for s in shares],
            })
        except DecodeError as exc:
            # A fact about the ADDRESS, not about our infrastructure. Reporting
            # "could not read the chain" here would blame the tool for the
            # user's input -- the same mistake the verify page made, where a
            # coin with no fee split was rendered as a failed observation.
            return self._fail(
                "That address is not a pump.fun coin, so it has no fee split "
                "to set."
            )
        except enroll.EnrollError as exc:
            return self._fail(str(exc))
        except Exception:
            traceback.print_exc()
            return self._fail("Could not read the chain just now. Try again in a moment.", status=503)

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
    """The program's error, in words a dev can act on.

    Anchor codes are meaningless to a dev, and the raw log is a stack trace.
    Each of these was observed in a real mainnet simulation while this was
    built, not copied from a table.
    """
    logs = " ".join(value.get("logs") or [])
    if "FeeSharesAlreadyUpdated" in logs:
        return ("pump allows a coin's split to be changed once, and this coin has "
                "already used it. No key can change it again.")
    if "NotEnoughRemainingAccounts" in logs:
        return "The coin's current shareholders could not be read. Reload and try again."
    if "AccountNotInitialized" in logs:
        return "This coin is missing an account pump expects. It may not be a pump coin."
    if "NotAuthorized" in logs:
        # 6016, from create_fee_sharing_config: only the coin's creator may
        # create its config. Measured with a stranger as payer.
        return ("pump lets only the coin's creator create its fee-sharing config, "
                "and the connected wallet is not that key.")
    if "ConstraintHasOne" in logs or "Unauthorized" in logs:
        return "The connected wallet is not allowed to change this coin's split."
    return "pump refused this split. Nothing was sent and nothing changed."

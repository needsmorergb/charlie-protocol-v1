"""Build a buy of a pump coin for the buyer's wallet to sign. Never sign it.

`GET /api/buy?mint=<mint>&wallet=<buyer>&sol=<SOL>&slippage_bps=<bps>` reads
the coin, picks the venue (pump's bonding curve before graduation, its
PumpSwap pool after), quotes from the chain as it reads now, builds an
UNSIGNED legacy transaction with the buyer as fee payer, simulates it
against mainnet, and answers with it. `indexer/sitebuy.py` does the work.

The coin page signs with the wallet and POSTs the signed bytes to the door's
relay (`/api/launch?send=1`, `indexer/relay.py`), which checks every
signature before it sends. Nothing here holds a key, signs or sends.

**Every transaction returned has already been simulated against mainnet and
came back with no error.** A simulation that fails answers 422 with the
reason, and no transaction.
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

from indexer import sitebuy  # noqa: E402
from indexer.base58 import encode  # noqa: E402
from indexer.message import MessageError, unsigned_transaction  # noqa: E402
from indexer.pump import DecodeError  # noqa: E402
from indexer.rpc import RpcClient, RpcError  # noqa: E402


def _endpoints():
    configured = os.environ.get("CHARLIE_RPC_URLS", "").strip()
    return [u.strip() for u in configured.split(",") if u.strip()] or None


def _rpc():
    return RpcClient(_endpoints()) if _endpoints() else RpcClient()


def build(rpc, mint: str, wallet: str, sol: str, slippage: str) -> tuple[int, dict]:
    """`(status, body)` for one request. Split from the handler so the tests
    can drive it without a socket as well as with one."""
    try:
        mint = sitebuy.parse_address(mint, "coin")
        wallet = sitebuy.parse_address(wallet, "wallet")
        lamports = sitebuy.parse_sol(sol)
        slippage_bps = sitebuy.parse_slippage(slippage)
        buy = sitebuy.plan(rpc, mint, wallet, lamports, slippage_bps)
        blockhash = rpc.call("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]["blockhash"]
        transaction = unsigned_transaction(buy.message(blockhash))
    except sitebuy.BuyError as exc:
        return exc.status, {"error": str(exc)}
    except MessageError as exc:
        return 400, {"error": f"This buy does not fit a transaction: {exc}"}
    except DecodeError as exc:
        return 502, {"error": f"The chain answered with an account this page cannot read: {exc}"}
    except RpcError as exc:
        return 503, {"error": f"The chain did not answer just now ({exc}). Nothing was built; try again."}

    encoded = base64.b64encode(transaction).decode()
    # The gate. The blockhash was fetched a moment ago, so it is simulated
    # as it will be signed rather than with a replaced one.
    try:
        simulated = rpc.call("simulateTransaction", [encoded, {
            "encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": False, "commitment": "processed",
        }])
    except RpcError as exc:
        return 503, {"error": f"The chain did not answer the check just now ({exc}). Nothing was built; try again."}
    value = (simulated or {}).get("value") or {}
    checked = {"err": value.get("err"), "units": value.get("unitsConsumed")}
    if value.get("err") is not None:
        checked["logs"] = (value.get("logs") or [])[-8:]
        return 422, {"error": sitebuy.explain(value), "simulated": checked}

    body = buy.describe()
    body.update({
        "transaction": encoded,
        # base58 of the same bytes: what a wallet's `signTransaction` and
        # `signAndSendTransaction` take as `message`.
        "signable": encode(transaction),
        "blockhash": blockhash,
        "simulated": checked,
        "relay": "/api/launch?send=1",
    })
    return 200, body


class handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        query = parse_qs(urlparse(self.path).query)
        one = lambda k: (query.get(k) or [""])[0].strip()  # noqa: E731
        try:
            status, body = build(_rpc(), one("mint"), one("wallet"), one("sol"), one("slippage_bps"))
        except Exception:  # pragma: no cover - the last line of defence
            traceback.print_exc()
            status, body = 502, {"error": "Could not read the chain just now. Nothing was built."}
        return self._send(status, body)

    def _send(self, status: int, body: dict):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

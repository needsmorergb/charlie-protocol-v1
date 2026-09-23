"""Every enrolled coin, read off the chain, for the coin directory (`/api/coins`).

The directory page was a fixed list, so a coin launched a minute ago (a tag
launch, a /launch coin, an /enroll) never appeared. This asks pump's
fee-share program which configs pay the protocol (`enrolled.mints`), keeps
the ones that carry every leg an enrolled coin needs (the same rule the
PROTOCOL_SHARE check applies), and names each from its own Token Metadata
account. Three batched reads for any number of coins. Nothing here writes.

    GET /api/coins -> {"coins": [{"mint", "name", "symbol", "uri"}], "observed_at"}
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from indexer import enroll, enrolled, launch, legs, pump  # noqa: E402
from indexer.rpc import RpcClient  # noqa: E402

BATCH = 100
CACHE = "public, max-age=30, s-maxage=60, stale-while-revalidate=300"


def _batched(rpc, addresses: list[str]) -> list:
    out = []
    for at in range(0, len(addresses), BATCH):
        out.extend(rpc.accounts(addresses[at:at + BATCH]))
    return out


def is_enrolled(config: pump.SharingConfig, rate: int = legs.TOLL_BPS) -> bool:
    """Pays the protocol its rate and carries the other legs (invariants' rule)."""
    if config.share_of(legs.TOLL_DESTINATION) < rate:
        return False
    return not legs.missing_legs(config.shareholders, rate, allow_charlie_ops=True)


def directory(rpc) -> list[dict]:
    mints = enrolled.mints(rpc)
    configs = _batched(rpc, [enroll.sharing_config_address(m) for m in mints])
    metas = _batched(rpc, [launch.metadata_address(m) for m in mints])
    coins = []
    for mint, config_account, meta_account in zip(mints, configs, metas):
        try:
            config = pump.decode_sharing_config(enroll.sharing_config_address(mint), config_account)
        except Exception:  # noqa: BLE001 -- an unreadable config is not listed
            continue
        if config.mint != mint or not is_enrolled(config):
            continue
        try:
            import base64
            meta = launch.decode_metadata(base64.b64decode(meta_account["data"][0])) or {}
        except (TypeError, KeyError, IndexError, ValueError):
            meta = {}
        coins.append({"mint": mint, "name": meta.get("name") or "", "symbol": meta.get("symbol") or "",
                      "uri": meta.get("uri") or ""})
    return coins


def _endpoints():
    configured = os.environ.get("CHARLIE_RPC_URLS", "").strip()
    return [u.strip() for u in configured.split(",") if u.strip()] if configured else None


class handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        try:
            rpc = RpcClient(_endpoints()) if _endpoints() else RpcClient()
            body, status, cache = json.dumps({"coins": directory(rpc), "observed_at": int(time.time())}), 200, CACHE
        except Exception:  # noqa: BLE001 -- the page keeps its own list
            traceback.print_exc()
            body, status, cache = json.dumps({"error": "could not read the chain"}), 503, "no-store"
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

"""One LaunchLab platform-fee cycle: claim, end in SOL, forward it.

    python -m tools.platform_fee_keeper --wallet <fee wallet>            # simulate
    python -m tools.platform_fee_keeper --keypair fee.json --send        # sign and send
    python -m tools.platform_fee_keeper --quote usdc --pool <cpmm> ...   # tier 2

The SOL goes to the collection wallet (`legs.TOLL_DESTINATION`) in the same
transaction as the claim, unless --keep. The collection wallet's existing
$CHARLIE burn (`burn.yml`, deploy repository) spends it from there, so this
never buys anything itself. There is deliberately no flag that forwards
anywhere else: a keeper holding a key with a free destination is a keeper
that can be told to pay anyone.

Run by `.github/workflows/platform-fees.yml`. It lives in tools/ rather than
indexer/ because the deploy repository never runs it (tools/shared_sync.py).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from indexer import buyback, legs
from indexer import platform_fees as pf
from indexer.ed25519 import Keypair
from indexer.rpc import DEFAULT_ENDPOINTS, RpcClient

QUOTES = {"sol": pf.WSOL, "usdc": pf.USDC, "usdt": pf.USDT}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.platform_fee_keeper", description=__doc__.split("\n\n")[0])
    ap.add_argument("--quote", default="sol", help="sol, usdc, usdt, or a mint address (default sol)")
    ap.add_argument("--pool", help="the CPMM pool that trades a USDC/USDT quote against wSOL")
    ap.add_argument("--slippage-bps", type=int, default=pf.DEFAULT_SLIPPAGE_BPS)
    ap.add_argument("--keep", action="store_true", help="leave the SOL in the fee wallet instead of forwarding it")
    ap.add_argument("--keypair", help="the platform fee wallet's key; signs, and sends with --send")
    ap.add_argument("--wallet", help="the platform fee wallet's address, to build and simulate without a key")
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--rpc", help="comma-separated RPC URLs (default: env CHARLIE_RPC_URLS)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    keypair = Keypair.from_file(args.keypair) if args.keypair else None
    admin = keypair.address if keypair else (args.wallet or "").strip()
    if not admin:
        ap.error("give --keypair PATH (to sign and send) or --wallet ADDRESS (to simulate only)")
    if args.send and keypair is None:
        ap.error("--send needs --keypair")
    raw = args.rpc or os.environ.get("CHARLIE_RPC_URLS") or ""
    rpc = RpcClient(tuple(u.strip() for u in raw.split(",") if u.strip()) or DEFAULT_ENDPOINTS)

    try:
        out = pf.plan(rpc, admin, QUOTES.get(args.quote.lower(), args.quote), pool_key=args.pool,
                      slippage_bps=args.slippage_bps, forward_to=None if args.keep else legs.TOLL_DESTINATION)
        result = pf.execute(rpc, out, keypair, send=args.send) if out["instructions"] else {"sent": False}
    except (pf.PlatformFeeError, buyback.BuybackError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    report = {k: v for k, v in out.items() if k != "instructions"}
    report.update(result)
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        for note in out["notes"]:
            print(note)
        if result.get("error"):
            print(f"SIMULATION FAILED: {result['error']}")
            for line in result["simulation"]["logs_tail"]:
                print(f"  {line}")
        elif out["instructions"]:
            print(f"simulated: ok, {result['simulation']['units_consumed']} compute units")
            if result["sent"]:
                print(f"sent: {result['signature']} ({result.get('confirmation')})")
                print(f"      https://solscan.io/tx/{result['signature']}")
            else:
                print("not sent (pass --keypair and --send)")
    return 1 if result.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())

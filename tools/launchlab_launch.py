"""Launch a coin on the LaunchLab rail (mainnet), signed by the coin's dev.

    python -m tools.launchlab_launch --keypair dev.json --name NAME --symbol SYM \
        --uri https://.../metadata.json --ops-address <ADDRESS> [--raise-sol 85] \
        [--sol-burn-bps 4000 --own-burn-bps 4000 --ops-bps 2000] [--buy-sol 0] [--send]

One transaction: the program creates the coin on Raydium LaunchLab with the
coin's `ll_collect` address as its creator, writes its route (the dev's legs,
which must sum to 10,000 bps), and funds `ll_collect` with rent. A fresh mint
keypair is generated and co-signs; its secret is written next to the dev's
keypair as `<MINT>.json` so the mint can be proved later.

Simulated first; sent only with `--send`. Standard library only.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import struct
import sys
import time
from pathlib import Path

from indexer import message
from indexer.base58 import pubkey_bytes
from indexer.curve import find_program_address
from indexer.ed25519 import Keypair
from indexer.rpc import RpcClient
from tools.launchlab_mainnet_setup import (ATA, LAUNCHLAB, PLATFORM, PROGRAM, QUOTE_CFG, RAY_PLATFORM, SYSTEM, TOKEN,
                                           TOKEN_2022, WSOL)

COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"


def pda(seeds, program):
    return find_program_address(seeds, program)[0]


def ata(owner, mint, token_program):
    return pda([pubkey_bytes(owner), pubkey_bytes(token_program), pubkey_bytes(mint)], ATA)


def _str(s: str) -> bytes:
    raw = s.encode("utf-8")
    return struct.pack("<I", len(raw)) + raw


def launch_ix(dev: str, mint: str, name: str, symbol: str, uri: str, raise_lamports: int,
              sol_burn: int, own_burn: int, ops: int, ops_address: str, fund_collect: int,
              supply: int = 1_000_000_000_000_000, sell: int = 793_100_000_000_000) -> message.Instruction:
    route = pda([b"ll_route", pubkey_bytes(mint)], PROGRAM)
    collect = pda([b"ll_collect", pubkey_bytes(mint)], PROGRAM)
    pool = pda([b"pool", pubkey_bytes(mint), pubkey_bytes(WSOL)], LAUNCHLAB)
    gcfg = pda([b"global_config", pubkey_bytes(WSOL), bytes([0]), struct.pack("<H", 0)], LAUNCHLAB)
    data = (bytes([10, 6]) + _str(name) + _str(symbol) + _str(uri)
            + struct.pack("<QQQHHH", supply, sell, raise_lamports, sol_burn, own_burn, ops)
            + pubkey_bytes(ops_address) + struct.pack("<Q", fund_collect))
    metas = [
        (dev, True, True), (PLATFORM, False, False), (QUOTE_CFG, False, False), (route, False, True),
        (collect, False, True), (gcfg, False, False), (RAY_PLATFORM, False, False),
        (pda([b"vault_auth_seed"], LAUNCHLAB), False, False), (pool, False, True), (mint, True, True),
        (WSOL, False, False), (pda([b"pool_vault", pubkey_bytes(pool), pubkey_bytes(mint)], LAUNCHLAB), False, True),
        (pda([b"pool_vault", pubkey_bytes(pool), pubkey_bytes(WSOL)], LAUNCHLAB), False, True),
        (TOKEN_2022, False, False), (TOKEN, False, False), (SYSTEM, False, False),
        (pda([b"__event_authority"], LAUNCHLAB), False, False), (LAUNCHLAB, False, False),
        (ata(collect, WSOL, TOKEN), False, True), (ata(collect, mint, TOKEN_2022), False, True), (ATA, False, False),
    ]
    return (PROGRAM, metas, data)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--keypair", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--uri", required=True, help="metadata JSON URL")
    ap.add_argument("--ops-address", required=True, help="the dev's ops wallet; must already hold SOL (rent)")
    ap.add_argument("--raise-sol", type=float, default=85.0, help="SOL the curve raises before graduating")
    ap.add_argument("--sol-burn-bps", type=int, default=4000)
    ap.add_argument("--own-burn-bps", type=int, default=4000)
    ap.add_argument("--ops-bps", type=int, default=2000)
    ap.add_argument("--fund-collect", type=int, default=10_000_000, help="lamports for ll_collect; default 0.01 SOL")
    ap.add_argument("--rpc", default="https://api.mainnet-beta.solana.com")
    ap.add_argument("--send", action="store_true")
    args = ap.parse_args(argv)
    if args.sol_burn_bps + args.own_burn_bps + args.ops_bps != 10_000:
        raise SystemExit("the three legs must sum to 10,000 bps")

    with open(args.keypair) as f:
        dev = Keypair.from_secret_bytes(bytes(json.load(f)))
    mint = Keypair.from_seed(os.urandom(32))
    rpc = RpcClient(endpoints=[args.rpc])
    ix = launch_ix(dev.address, mint.address, args.name, args.symbol, args.uri, int(args.raise_sol * 1e9),
                   args.sol_burn_bps, args.own_burn_bps, args.ops_bps, args.ops_address, args.fund_collect)
    budget = (COMPUTE_BUDGET, [], bytes([2]) + struct.pack("<I", 400_000))
    blockhash = rpc.call("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]["blockhash"]
    msg = message.compile_legacy(dev.address, [budget, ix], blockhash)
    sim = rpc.call("simulateTransaction", [base64.b64encode(message.unsigned_transaction(msg)).decode(),
                                           {"encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True}])["value"]
    print(f"mint {mint.address}")
    if sim.get("err"):
        print("SIMULATION FAILED", json.dumps(sim["err"]))
        for line in (sim.get("logs") or [])[-10:]:
            print("   ", line)
        return 1
    print(f"simulated ok ({sim.get('unitsConsumed')} CU)")
    if not args.send:
        print("rerun with --send to launch (a new mint address is generated each run)")
        return 0
    keyfile = Path(args.keypair).with_name(f"{mint.address}.json")
    keyfile.write_text(json.dumps(list(mint.seed + mint.public)))
    # Signer order is the message's: the dev pays and comes first, then the mint.
    signed = message.signed_transaction(msg, [dev.sign(msg), mint.sign(msg)])
    sig = rpc.call("sendTransaction", [base64.b64encode(signed).decode(), {"encoding": "base64"}])
    for _ in range(60):
        status = rpc.call("getSignatureStatuses", [[sig]])["value"][0]
        if status and status.get("confirmationStatus") in ("confirmed", "finalized"):
            print(("FAILED " + json.dumps(status["err"])) if status.get("err") else f"launched: {sig}")
            return 1 if status.get("err") else 0
        time.sleep(1)
    print(f"sent {sig}; not confirmed within 60s, check it")
    return 1


if __name__ == "__main__":
    sys.exit(main())

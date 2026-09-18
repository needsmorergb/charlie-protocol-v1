"""Record the LaunchLab rail's devnet cranks from the chain, for `/launchlab`.

Every figure in the file this writes (`state/launchlab/devnet.json`) is read
off a landed transaction: lamport deltas of the addresses a crank pays, coins
burned from the transaction's own parsed `burn` instructions, compute units
and fee from its meta. Nothing is computed from the program's arithmetic and
nothing is modelled. A signature that does not resolve stops the run.

    python -m tools.launchlab_devnet [--rpc URL]

The signatures are the cranks run on 18 September 2026 and recorded in
LAUNCHLAB-RAIL.md section 8. Unlike `/flywheel`, nothing here is mocked:
LaunchLab, CPMM and the LP lock are Raydium's own devnet programs, the trades
that paid the fees were real swaps, and graduation was Raydium's migration bot.

Standard library only, like `indexer/`.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from indexer.rpc import RpcClient

OUT = Path(__file__).resolve().parents[1] / "state" / "launchlab" / "devnet.json"

PROGRAM = "4WgYTmPM9VHyFACSMdw4Bh9jkK9BETjWV5tNrDzuJWgX"
KEEPER = "Fdq7GiMhiitbHdjK1x7y6tLvSjwPYjNh9t4nibAzAq9V"  # also each route's ops address
NAMED = {
    "1nc1nerator11111111111111111111111111111111": "incinerator",
    "CJEC7BywJ4fgveTPRpPbF4NxSgAzZxs6Dgo9wk1mV5qL": "charlie_pool",
    KEEPER: "keeper_and_ops",
}
COINS = {
    "CA6NcWcELpoJWBeigPxSuShyGi84L7V5vFJvkYbryBrc": "on the curve",
    "BuX4dNXZDyhYfLWQyZoHpRDYrHWpHkEVFqxFDpRrgLbV": "graduated to CPMM",
}
RUNS = (
    ("crank_curve", "CA6NcWcELpoJWBeigPxSuShyGi84L7V5vFJvkYbryBrc",
     "pkhWM6jq371aiNs8K4fMPoRovSHq8UUYVSFP3dAE7awvMyRrkuG7U3oWw47e8RmJPHSs6LgAKJaKD9RGyUpjukX"),
    ("crank_amm_creator", "BuX4dNXZDyhYfLWQyZoHpRDYrHWpHkEVFqxFDpRrgLbV",
     "CcAeLt5XXRJsg1VpPUbrNathyrxaioCJMZ1mAiZEFp8FkixayoHxFpBwVFVcYjTaiX7FRrHav6ZXxp5gGvDZj9s"),
    ("crank_lp", "BuX4dNXZDyhYfLWQyZoHpRDYrHWpHkEVFqxFDpRrgLbV",
     "3Js81pDkDQ9pgkzKiQKkQ9VEoHxyS6KuC8XQ664Utjahu1LUdkwzDGzM9nSerhrCyJuzts9mY7Lx5NpHZNZAmdp"),
    ("crank_platform", None,
     "2kbrzESNQ36kmzVAn8gEjLa9novDofBxuJSPi3qDonWUKhBTL9uLGK5BMLeWqGkr1g99YThEGp27NvggqFyuXUSd"),
)


def _keys(tx: dict) -> list[str]:
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
    loaded = tx["meta"].get("loadedAddresses") or {}
    return keys + list(loaded.get("writable") or []) + list(loaded.get("readonly") or [])


def _burned(tx: dict, mint: str | None) -> int:
    total = 0
    for group in tx["meta"].get("innerInstructions") or []:
        for ix in group["instructions"]:
            parsed = ix.get("parsed")
            if isinstance(parsed, dict) and parsed.get("type") in ("burn", "burnChecked"):
                info = parsed["info"]
                if mint is None or info.get("mint") == mint:
                    amount = info.get("amount") or (info.get("tokenAmount") or {}).get("amount")
                    total += int(amount)
    return total


def record_run(tx: dict, crank: str, mint: str | None, signature: str) -> dict:
    if tx is None or tx["meta"].get("err"):
        raise SystemExit(f"{crank} {signature}: not a landed, successful transaction")
    keys = _keys(tx)
    pre, post = tx["meta"]["preBalances"], tx["meta"]["postBalances"]
    deltas = {name: 0 for name in NAMED.values()}
    for i, key in enumerate(keys):
        if key in NAMED:
            deltas[NAMED[key]] = post[i] - pre[i]
    fee = tx["meta"]["fee"]
    return {
        "crank": crank,
        "mint": mint,
        "signature": signature,
        "slot": tx["slot"],
        "block_time": tx.get("blockTime"),
        "compute_units": tx["meta"].get("computeUnitsConsumed"),
        "fee": fee,
        "incinerator": deltas["incinerator"],
        "charlie_pool": deltas["charlie_pool"],
        # The keeper is also each route's ops address on devnet; add its fee
        # back so the ops leg reads as what the program paid.
        "ops": deltas["keeper_and_ops"] + fee if crank != "crank_platform" else 0,
        "coins_burned": _burned(tx, mint) if mint else 0,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rpc", default="https://api.devnet.solana.com")
    args = ap.parse_args(argv)
    rpc = RpcClient(endpoints=[args.rpc])
    runs = [record_run(rpc.transaction(sig), crank, mint, sig) for crank, mint, sig in RUNS]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"cluster": "devnet", "program_id": PROGRAM, "coins": COINS, "runs": runs}, indent=2) + "\n")
    print(OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

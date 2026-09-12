"""The keeper: call `split` on a schedule, forever, and say what happened.

WHAT A KEEPER IS FOR. `split` is permissionless and takes no signer, so the
mechanism does not depend on anybody in particular running it -- but it does
depend on SOMEBODY running it, or fees sit in the vault indefinitely. This is
that somebody: a loop that calls `split` at an interval and logs each result.

WHY IT IS SAFE TO RUN UNATTENDED. Three properties, each measured rather
than assumed:

* **An idle call is a no-op, not a failure.** With nothing above the rent
  reserve the program logs `pending, not failed` and returns success. Three
  back-to-back calls on a near-empty vault each landed and left the residue
  untouched. So the loop never needs to know whether there is work.
* **It costs 5,000 lamports a call.** One SOL funds two hundred thousand
  splits; hourly for a year is about 0.044 SOL.
* **The caller is paid nothing.** No leg is payable to whoever calls, so a
  keeper cannot extract anything by calling more often, and there is no
  bounty for a griefer to farm.

WHAT IT DOES NOT DO. It does not buy or burn tokens. The buyback leg accrues
in `burn_vault` and spending it needs a swap against a live pool, which is
`indexer/buyback.py`'s job and needs mainnet. This keeper moves the split
only, and its log says so.

    python -m tools.keeper --keypair PATH --ops ADDRESS --interval 3600
    python -m tools.keeper --keypair PATH --ops ADDRESS --once

Use a dedicated keypair holding only what you mean to spend on fees. The
file is read once per call and never printed.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.flywheel_devnet import (
    RpcError,
    balance,
    load_keypair,
    minimum_balance,
    send,
    simulate,
    transaction_logs,
)
from tools.splitter_devnet import (
    CHARLIE_MINT,
    PROGRAM_ID,
    addresses,
    event_from_logs,
    ix_split,
)

# What the keeper wallet must keep to go on paying fees. Below this it stops
# and says so, rather than failing one call at a time until somebody notices.
MIN_KEEPER_LAMPORTS = 100_000

LOG = Path(__file__).resolve().parents[1] / "state" / "flywheel" / "keeper.jsonl"

_stop = False


def _handle_signal(signum, frame):  # pragma: no cover - signal path
    global _stop
    _stop = True
    print("\n  stopping after this cycle (signal received)")


def once(payer: str, seed: bytes, mint: str, a: dict, ops: str, *, dry_run: bool = False) -> dict:
    """One cycle: decide whether there is anything to split, then split it.

    The simulation first is not belt-and-braces, it is the gate the rest of
    this project uses: a transaction that simulates with an error is one
    this refuses to sign.
    """
    reserve = minimum_balance(0)
    vault = balance(a["vault"])
    pending = max(vault - reserve, 0)
    cycle = {
        "at": int(time.time()),
        "vault_lamports": vault,
        "pending_lamports": pending,
    }

    result = simulate([ix_split(mint, a, ops)], payer)
    if result.get("err"):
        cycle["outcome"] = "refused"
        cycle["error"] = json.dumps(result["err"])
        return cycle

    if dry_run:
        cycle["outcome"] = "dry_run"
        return cycle

    signature = send([ix_split(mint, a, ops)], payer, [seed])
    event = event_from_logs(transaction_logs(signature))
    cycle["signature"] = signature
    cycle["event"] = event
    # `l == 0` is the idle case and is a success, not a failure: the program
    # split nothing because there was nothing above the reserve.
    cycle["outcome"] = "idle" if not event or event["l"] == 0 else "split"
    return cycle


def describe(cycle: dict) -> str:
    stamp = time.strftime("%H:%M:%S", time.gmtime(cycle["at"]))
    outcome = cycle["outcome"]
    if outcome == "refused":
        return f"  {stamp}  REFUSED  {cycle['error']}"
    if outcome == "dry_run":
        return f"  {stamp}  dry run  {cycle['pending_lamports']:,} lamports pending"
    if outcome == "idle":
        return f"  {stamp}  idle     nothing above the reserve"
    event = cycle["event"]
    return (
        f"  {stamp}  split    {event['l']:>10,} -> "
        f"burn {event['sol_burn']:>9,} | buyback {event['buyback']:>9,} | "
        f"ops {event['ops']:>9,}   {cycle['signature'][:8]}..."
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the splitter on a schedule.")
    parser.add_argument("--keypair", required=True, type=Path)
    parser.add_argument("--ops", required=True,
                        help="the ops wallet the splitter's config names")
    parser.add_argument("--mint", default=CHARLIE_MINT)
    parser.add_argument("--interval", type=float, default=3600,
                        help="seconds between calls (default: hourly)")
    parser.add_argument("--cycles", type=int, default=0,
                        help="stop after N cycles; 0 runs until interrupted")
    parser.add_argument("--once", action="store_true", help="one cycle, then exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="simulate each cycle and sign nothing")
    parser.add_argument("--log", type=Path, default=LOG,
                        help="append one JSON object per cycle here")
    args = parser.parse_args(argv)

    payer, seed = load_keypair(args.keypair)
    a = addresses(args.mint)

    signal.signal(signal.SIGINT, _handle_signal)
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
    except (AttributeError, ValueError):  # pragma: no cover - platform dependent
        pass

    print(f"keeper    {payer}")
    print(f"program   {PROGRAM_ID} (devnet)")
    print(f"vault     {a['vault']}")
    print(f"interval  {args.interval:g}s"
          + ("  (one cycle)" if args.once else "")
          + ("  DRY RUN, signing nothing" if args.dry_run else ""))
    print("  a cycle that finds nothing is an idle success, not a failure\n")

    args.log.parent.mkdir(parents=True, exist_ok=True)
    limit = 1 if args.once else args.cycles
    count = 0

    while not _stop:
        remaining = balance(payer)
        if remaining < MIN_KEEPER_LAMPORTS and not args.dry_run:
            print(f"  keeper wallet down to {remaining:,} lamports "
                  f"(floor {MIN_KEEPER_LAMPORTS:,}); stopping rather than "
                  "failing a call at a time")
            return 1

        try:
            cycle = once(payer, seed, args.mint, a, args.ops, dry_run=args.dry_run)
        except RpcError as exc:
            # A transient RPC failure is not a reason to end the loop: the
            # next cycle re-reads the chain and the fees are still there.
            cycle = {"at": int(time.time()), "outcome": "error", "error": str(exc)}
            print(f"  {time.strftime('%H:%M:%S', time.gmtime())}  error    {exc}")
        else:
            print(describe(cycle))

        with args.log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(cycle) + "\n")

        count += 1
        if limit and count >= limit:
            break
        if _stop:
            break
        # Sleep in short slices so a signal is noticed promptly rather than
        # an hour later.
        deadline = time.time() + args.interval
        while time.time() < deadline and not _stop:
            time.sleep(min(1.0, deadline - time.time()))

    print(f"\n  {count} cycle(s); log at {args.log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

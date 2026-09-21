"""$CHARLIE bought and burned by the protocol's collection wallet: the record.

The flywheel's last leg. The collection wallet takes its row of every
enrolled coin's creator fee; `burn.yml` spends that on $CHARLIE and burns it
in the same transaction. This walks that wallet's own transactions and keeps
the ones that are exactly that: a burn of $CHARLIE whose authority is the
collection wallet, with a swap in the same transaction, that landed.

What the figure is NOT: $CHARLIE's total supply destroyed. Nearly all of that
is pump's one-time boost burn at migration, and the record withholds it
(BURN_SUPPLY). This counts only what the protocol's own wallet bought and
burned, every unit of it one transaction a reader can open.

The record is append-only and keyed by signature. A node that answers with a
short history (seen 2026-09-17: the same wallet listed as 6 transactions,
then 2) can therefore never shrink it; a run only ever adds.

Append-only is not the same as complete, and the difference is published
rather than assumed. A signature the node will not return, or history past
the one page the walk asks for, is a burn this record has never seen -- so
every run states whether its total is `exact` or `at least`, and why.

    python -m indexer.protocol_burns --out web
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass

from . import decode, legs
from .rpc import RpcClient

CHARLIE = "8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump"
DECIMALS = 6
FILENAME = "protocol-burns.json"

# What one `getSignaturesForAddress` answers with. The walk asks for a single
# page, newest first, so hitting this number means there is older history it
# did not look at.
SIGNATURE_PAGE = 1000


@dataclass(frozen=True)
class Coverage:
    """What the walk could not see, so the total can say which it is.

    Append-only storage keeps a row that was once read. It cannot make a
    claim about rows that were never read -- a signature the node would not
    return a transaction for this run, or history past the one page the walk
    asks for. Both understate the total, silently, and a total that can
    silently understate is exactly the figure this project does not publish
    without saying so.

    So the walk reports them and the record carries them: complete means the
    total is exact, and incomplete means it is a floor. The same shape
    `reconcile` uses when its burn walk has not finished.
    """

    unread: tuple = ()
    truncated: bool = False

    @property
    def complete(self) -> bool:
        return not self.unread and not self.truncated

    def caption(self) -> str:
        if self.complete:
            return "exact: every signature this wallet has was read"
        why = []
        if self.unread:
            why.append(f"{len(self.unread)} signature(s) could not be read this run")
        if self.truncated:
            why.append(f"the node answered with a full page of {SIGNATURE_PAGE}, so older history was not walked")
        return "at least: " + "; and ".join(why)


def burns_in(tx: dict, signature: str, wallet: str, mint: str = CHARLIE) -> list[dict]:
    """The rows one transaction contributes: none unless it landed, burned
    `mint` on `wallet`'s authority, and carried the swap beside the burn."""
    if not tx or (tx.get("meta") or {}).get("err") is not None:
        return []
    ours = [b for b in decode.find_burns(tx, mint) if b["authority"] == wallet]
    if not ours or not decode.find_swap_shaped(tx):
        return []
    return [{"signature": signature, "instruction_index": b["instruction_index"], "raw_amount": b["amount"],
             "slot": tx.get("slot"), "block_time": tx.get("blockTime")} for b in ours]


def merge(known: list[dict], found: list[dict]) -> list[dict]:
    """Append-only: a row already recorded is never replaced or dropped."""
    rows = {(r["signature"], r["instruction_index"]): r for r in found}
    rows.update({(r["signature"], r["instruction_index"]): r for r in known})
    return sorted(rows.values(), key=lambda r: (r.get("slot") or 0, r["signature"], r["instruction_index"]))


def ui(raw: int) -> str:
    return f"{raw // 10 ** DECIMALS:,}.{raw % 10 ** DECIMALS:0{DECIMALS}d}"


def record(rows: list[dict], wallet: str, now: float, coverage: Coverage | None = None) -> dict:
    coverage = coverage or Coverage()
    total = sum(r["raw_amount"] for r in rows)
    return {
        "schema": 1,
        "what": "$CHARLIE bought and burned by the protocol's collection wallet, one row per burn instruction",
        "is_not": "total supply destroyed; pump's boost burn at migration is not in this figure",
        "mint": CHARLIE, "wallet": wallet, "decimals": DECIMALS,
        "rule": "landed; burn authority == wallet; a swap in the same transaction",
        "read_at": int(now), "burns": rows, "raw_total": total, "total": ui(total),
        # What the total IS, not merely when it was read. A consumer that
        # ignores every other field here still cannot read an understated
        # number as an exact one.
        "complete": coverage.complete,
        "total_is": "exact" if coverage.complete else "at least",
        "coverage": coverage.caption(),
        "unread": list(coverage.unread),
        "truncated": coverage.truncated,
    }


def walk(rpc, wallet: str, known: list[dict]) -> tuple[list[dict], Coverage]:
    seen = {r["signature"] for r in known}
    found: list[dict] = []
    unread: list[str] = []
    entries = rpc.call("getSignaturesForAddress", [wallet, {"limit": SIGNATURE_PAGE}]) or []
    # A full page means the node had more to give. The rows beyond it were
    # never looked at, so this run cannot call its total exact.
    truncated = len(entries) >= SIGNATURE_PAGE
    for entry in entries:
        signature = entry["signature"]
        if signature in seen or entry.get("err") is not None:
            continue
        tx = rpc.call("getTransaction", [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
        if tx is None:
            # Unread is not "no burn": say so, count it, and leave it for the
            # next run. Counting is the half that was missing -- saying it in
            # a log line nobody reads did not stop the record claiming a total
            # it could not support.
            print(f"could not read {signature}; it will be tried again next run")
            unread.append(signature)
            continue
        found += burns_in(tx, signature, wallet)
    return merge(known, found), Coverage(tuple(unread), truncated)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", default="web")
    args = parser.parse_args(argv)
    wallet = legs.TOLL_DESTINATION
    path = os.path.join(args.out, FILENAME)
    known = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            known = json.load(fh)["burns"]
    rows, coverage = walk(RpcClient(), wallet, known)
    body = record(rows, wallet, time.time(), coverage)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(body, fh, indent=1)
        fh.write("\n")
    print(f"{len(rows)} burn(s) ({len(rows) - len(known)} new), "
          f"{body['total_is']} {body['total']} $CHARLIE -> {path}")
    print(f"coverage: {body['coverage']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

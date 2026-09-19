"""Append-only accounting for the shared launch-token buyback treasury.

The treasury is a shared wallet to avoid deploying a router for every coin.
This file therefore provides the missing invariant: a mint can be bought only
with the lamports credited to that mint.  Operators publish this JSONL file;
each credit and landed burn has a transaction signature, so the allocation is
auditable without trusting an aggregate wallet balance.
"""
from __future__ import annotations

import json
from pathlib import Path
from time import time

from . import legs

DEFAULT_LEDGER_PATH = Path("state/launch-buybacks.jsonl")


class LedgerError(ValueError):
    pass


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LedgerError(f"{path}:{number}: invalid JSON") from exc
        if row.get("kind") not in {"credit", "burn"} or not isinstance(row.get("mint"), str):
            raise LedgerError(f"{path}:{number}: not a launch buyback ledger row")
        rows.append(row)
    return rows


def summary(path: Path = DEFAULT_LEDGER_PATH) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    seen = set()
    for row in _read(path):
        key = (row["kind"], row.get("signature"))
        if key in seen:
            raise LedgerError(f"duplicate {row['kind']} signature {row.get('signature')}")
        seen.add(key)
        entry = out.setdefault(row["mint"], {"credited_lamports": 0, "spent_lamports": 0, "burned_raw": 0})
        if row["kind"] == "credit":
            entry["credited_lamports"] += int(row["lamports"])
        else:
            entry["spent_lamports"] += int(row["lamports"])
            entry["burned_raw"] += int(row.get("burned_raw", 0))
        if entry["spent_lamports"] > entry["credited_lamports"]:
            raise LedgerError(f"{row['mint']}: burns exceed credited treasury funds")
    for entry in out.values():
        entry["available_lamports"] = entry["credited_lamports"] - entry["spent_lamports"]
    return out


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")


def credit(mint: str, lamports: int, signature: str, *, path: Path = DEFAULT_LEDGER_PATH) -> None:
    if lamports <= 0 or not signature:
        raise LedgerError("a positive lamport amount and source signature are required")
    if any(row.get("signature") == signature for row in _read(path)):
        raise LedgerError(f"signature already recorded: {signature}")
    _append(path, {"at": int(time()), "kind": "credit", "mint": mint, "lamports": lamports,
                   "signature": signature, "treasury": legs.LAUNCH_BUYBACK_DESTINATION})


def record_burn(mint: str, lamports: int, burned_raw: int, signature: str, *, path: Path = DEFAULT_LEDGER_PATH) -> None:
    if lamports <= 0 or not signature:
        raise LedgerError("a positive spend and burn signature are required")
    available = summary(path).get(mint, {}).get("available_lamports", 0)
    if lamports > available:
        raise LedgerError(f"{mint}: attempted {lamports} lamports with only {available} credited")
    _append(path, {"at": int(time()), "kind": "burn", "mint": mint, "lamports": lamports,
                   "burned_raw": burned_raw, "signature": signature, "treasury": legs.LAUNCH_BUYBACK_DESTINATION})

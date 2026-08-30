"""Deterministic committed text export of the evidence store (D-03/D-04).

D-01 and D-03 conflict: a `.db` is rewritten wholesale on every write, so
committing the binary balloons git history. Resolution: SQLite is the
*working* store; what is committed is a deterministic text export generated
from it. "Deterministic" means: re-running the export against an unchanged
database produces a byte-identical file, so a diff shows only real changes.

`EXPORT_TABLES` is the only source of SQL identifiers this module
interpolates -- every entry is a literal tuple written here, never a
caller-supplied string. `tests/test_discipline.py` asserts this statically
for the whole package.
"""

from __future__ import annotations

import json
from pathlib import Path

DEFAULT_EXPORT_DIR = Path("state") / "evidence"

# (table_name, order_by) -- literal identifiers only. Extended as new tables
# are added to indexer/evidence.py.
EXPORT_TABLES: tuple[tuple[str, str], ...] = (
    ("inflow", "signature, destination"),
    ("opening_balance", "id"),
    ("scan_cursor", "endpoint, target, purpose"),
    ("initial_supply", "mint"),
    ("burn_event", "signature, mint, instruction_index"),
)


def export_table(conn, table: str, order_by: str, path: Path) -> None:
    cursor = conn.execute(f"SELECT * FROM {table} ORDER BY {order_by}")
    columns = [d[0] for d in cursor.description]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for row in cursor:
            record = dict(zip(columns, row))
            line = json.dumps(record, separators=(",", ":"), sort_keys=True)
            handle.write(line + "\n")


def export_all(evidence, out_dir=DEFAULT_EXPORT_DIR) -> list[Path]:
    out_dir = Path(out_dir)
    written = []
    for table, order_by in EXPORT_TABLES:
        path = out_dir / f"{table}.jsonl"
        export_table(evidence.connection, table, order_by, path)
        written.append(path)
    return written

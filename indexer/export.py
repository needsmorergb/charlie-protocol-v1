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
    ("discrepancy", "id"),
    ("submission", "repo, issue_number, attempted_at"),
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


def import_table(conn, table: str, path: Path) -> int:
    """Load one exported table back, replacing what is there.

    `INSERT OR IGNORE`, which is the store's own rule (`evidence.py`: every
    write is `INSERT OR IGNORE` keyed on the row's natural identity, never
    `INSERT OR REPLACE`). Loading the same export twice is therefore the same
    as loading it once -- the CI job that does this runs on a schedule and
    must not accumulate duplicate burn rows, which would inflate every figure
    derived from them.

    `OR REPLACE` would be idempotent too, and wrong. Every table here has a
    real primary key, so a replace does not add rows; it overwrites the ones
    already there with the exported version. The columns that would lose are
    exactly the ones the store documents as the single mutable field on an
    otherwise immutable row -- `submission.answered_at`, `submission.closed_at`,
    `burn_event.supply_after`. Loading a stale export in the deploy repository
    would blank `answered_at` and the next run would comment on and close an
    already-answered issue.

    Column names come from the exported record, and every one is checked
    against the table's own schema before it reaches SQL. A record naming a
    column the table does not have is a corrupt export, and a corrupt export
    must not be able to compose an identifier.
    """
    if not path.exists():
        return 0
    known = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    # Positional, in the table's own column order, so no column name from the
    # file ever reaches a SQL string -- `tests/test_discipline.py` allows the
    # table name and a run of `?`, and nothing else, into one of these.
    placeholders = ",".join("?" for _ in known)
    loaded = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        unknown = sorted(set(record) - set(known))
        if unknown:
            raise ValueError(
                f"{path}: record names {unknown}, which {table} does not have"
            )
        conn.execute(
            f"INSERT OR IGNORE INTO {table} VALUES ({placeholders})",
            [record.get(name) for name in known],
        )
        loaded += 1
    return loaded


def import_all(evidence, in_dir=DEFAULT_EXPORT_DIR) -> dict:
    """The committed export, loaded into a working store.

    The export is the record; the `.db` is a cache of it, and the deploy
    repository has no other way to hold one. Without this the landing page
    rendered there had no burns, no initial supply and no walk to read, so
    every counter came out "unknown" -- correct behaviour on an empty store,
    and a blank front page.
    """
    in_dir = Path(in_dir)
    loaded = {}
    for table, _order_by in EXPORT_TABLES:
        loaded[table] = import_table(evidence.connection, table, in_dir / f"{table}.jsonl")
    evidence.connection.commit()
    return loaded


def export_all(evidence, out_dir=DEFAULT_EXPORT_DIR) -> list[Path]:
    out_dir = Path(out_dir)
    written = []
    for table, order_by in EXPORT_TABLES:
        path = out_dir / f"{table}.jsonl"
        export_table(evidence.connection, table, order_by, path)
        written.append(path)
    return written

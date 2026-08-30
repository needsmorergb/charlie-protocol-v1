"""SQLite evidence store for recorded lamport movements (D-01).

This is the working store `SEAL_BALANCE` and `OPS_ROUTED` read from -- never
the other direction. A check that recomputed its answer from the chain every
time it ran would be unfalsifiable by construction: nothing stored here could
ever move it. See `tests/test_evidence.py`'s falsification tests for the
property this buys.

Append-only (ARCHITECTURE.md's rule, extended to SQL): every write is
`INSERT OR IGNORE` keyed on the row's natural identity, never `INSERT OR
REPLACE`. A re-scan re-observing the same signature must not rewrite a
recorded amount.

Coverage gap, stated rather than implied: the opening-balance mechanism
(`opening_balance` table, added by phase 1 plan 01's task 3) exists only for a
dedicated per-coin vault (D-06). Every SEAL destination in existence today is
the grandfathered shared address (`burn111...111`), so no opening balance is
recorded anywhere on live data until dedicated PDA vaults exist in phase 5
(D-07). The mechanism is built and tested; it is dormant.

Values that arrive from an untrusted RPC response -- mints, signatures,
destinations -- are never interpolated into SQL. Every query binds them with
`?` placeholders. The only SQL identifiers this module (or any other) is
allowed to interpolate are the literal table/column names in
`export.EXPORT_TABLES`; `tests/test_discipline.py` enforces this statically.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

DEFAULT_DB_PATH = Path("state") / "evidence.db"

SCHEMA_VERSION = 1


class Evidence:
    """A thin, context-manager-friendly wrapper over one SQLite database."""

    def __init__(self, path=DEFAULT_DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def __enter__(self) -> "Evidence":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    @property
    def connection(self) -> sqlite3.Connection:
        """The raw connection -- `export.py` reads through this, nothing else should."""
        return self._conn

    # -- schema -------------------------------------------------------
    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER NOT NULL,
                applied_at INTEGER NOT NULL
            )
            """
        )
        row = self._conn.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, int(time.time())),
            )

        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS inflow (
                signature        TEXT    NOT NULL,
                destination      TEXT    NOT NULL,
                mint             TEXT    NOT NULL,
                leg              TEXT    NOT NULL,
                lamports         INTEGER NOT NULL,
                block_time       INTEGER,
                slot             INTEGER NOT NULL,
                credit_ix_count  INTEGER NOT NULL DEFAULT 0,
                recorded_at      INTEGER NOT NULL,
                PRIMARY KEY (signature, destination)
            )
            """
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS inflow_destination ON inflow(destination)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS inflow_mint ON inflow(mint)")
        self._conn.commit()

    # -- inflow ---------------------------------------------------------
    def record_inflow(
        self,
        *,
        signature: str,
        destination: str,
        mint: str,
        leg: str,
        lamports: int,
        block_time: int | None,
        slot: int,
        credit_ix_count: int = 0,
        recorded_at: int | None = None,
    ) -> None:
        recorded_at = recorded_at if recorded_at is not None else int(time.time())
        self._conn.execute(
            """
            INSERT OR IGNORE INTO inflow
                (signature, destination, mint, leg, lamports, block_time, slot,
                 credit_ix_count, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signature,
                destination,
                mint,
                leg,
                int(lamports),
                block_time,
                int(slot),
                int(credit_ix_count),
                recorded_at,
            ),
        )
        self._conn.commit()

    def inflows_for(self, destination: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM inflow WHERE destination = ? ORDER BY signature",
            (destination,),
        ).fetchall()
        return [dict(row) for row in rows]

    def recorded_lamports(self, destination: str) -> int:
        """Sum of positive rows only -- an outflow must never net away a shortfall."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(lamports), 0) AS total FROM inflow "
            "WHERE destination = ? AND lamports > 0",
            (destination,),
        ).fetchone()
        return int(row["total"])

    def outflows_for(self, destination: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM inflow WHERE destination = ? AND lamports < 0 ORDER BY signature",
            (destination,),
        ).fetchall()
        return [dict(row) for row in rows]


def open_evidence(path=DEFAULT_DB_PATH) -> Evidence:
    return Evidence(path)

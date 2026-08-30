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

# scan_cursor's PK carries an endpoint dimension from the start (D-13, plan
# `01-01` task 4): coverage is tracked per endpoint, never per target alone,
# because which inflows get recorded currently depends on which endpoint
# answered. A single-endpoint caller (task 2) uses this sentinel and never
# has to think about the dimension; task 4's per-endpoint walk supplies the
# real endpoint URL instead.
DEFAULT_ENDPOINT_KEY = "*"


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

        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS opening_balance (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                destination        TEXT    NOT NULL,
                lamports           INTEGER NOT NULL,
                opening_signature  TEXT    NOT NULL,
                recorded_at        INTEGER NOT NULL,
                retired_at         INTEGER,
                superseded_by      INTEGER REFERENCES opening_balance(id)
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS opening_balance_destination ON opening_balance(destination)"
        )

        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scan_cursor (
                target             TEXT    NOT NULL,
                purpose            TEXT    NOT NULL,
                endpoint           TEXT    NOT NULL DEFAULT '*',
                last_signature     TEXT,
                oldest_signature   TEXT,
                backfill_complete  INTEGER NOT NULL DEFAULT 0,
                last_error         TEXT,
                updated_at         INTEGER NOT NULL,
                PRIMARY KEY (endpoint, target, purpose)
            )
            """
        )
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

    # -- scan_cursor ------------------------------------------------------
    def get_cursor(self, target: str, purpose: str, endpoint: str = DEFAULT_ENDPOINT_KEY) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM scan_cursor WHERE target = ? AND purpose = ? AND endpoint = ?",
            (target, purpose, endpoint),
        ).fetchone()
        return dict(row) if row else None

    def set_cursor(
        self,
        target: str,
        purpose: str,
        endpoint: str = DEFAULT_ENDPOINT_KEY,
        last_signature: str | None = None,
        oldest_signature: str | None = None,
        backfill_complete: int | None = None,
        last_error: str | None = None,
    ) -> None:
        """Update only the fields given -- an omitted field keeps its stored value."""
        existing = self.get_cursor(target, purpose, endpoint) or {}
        last_signature = last_signature if last_signature is not None else existing.get("last_signature")
        oldest_signature = (
            oldest_signature if oldest_signature is not None else existing.get("oldest_signature")
        )
        backfill_complete = (
            int(bool(backfill_complete))
            if backfill_complete is not None
            else int(existing.get("backfill_complete") or 0)
        )
        last_error = last_error if last_error is not None else existing.get("last_error")
        self._conn.execute(
            """
            INSERT INTO scan_cursor
                (target, purpose, endpoint, last_signature, oldest_signature,
                 backfill_complete, last_error, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (endpoint, target, purpose) DO UPDATE SET
                last_signature=excluded.last_signature,
                oldest_signature=excluded.oldest_signature,
                backfill_complete=excluded.backfill_complete,
                last_error=excluded.last_error,
                updated_at=excluded.updated_at
            """,
            (
                target,
                purpose,
                endpoint,
                last_signature,
                oldest_signature,
                backfill_complete,
                last_error,
                int(time.time()),
            ),
        )
        self._conn.commit()

    def is_backfill_complete(self, target: str, purpose: str) -> bool:
        """True once AT LEAST ONE endpoint has walked `target` to a short page (D-13).

        Never a global AND across endpoints: one endpoint reaching genesis is
        the unambiguous signal that the address's full history has been
        seen, even if a second, still-catching-up endpoint has not gotten
        there yet.
        """
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM scan_cursor WHERE target = ? AND purpose = ? AND backfill_complete = 1",
            (target, purpose),
        ).fetchone()
        return row["n"] > 0

    # -- opening_balance (EVID-02, dormant on live SEAL data -- see docstring) --
    def record_opening_balance(
        self,
        *,
        destination: str,
        lamports: int,
        opening_signature: str,
        recorded_at: int | None = None,
    ) -> int:
        """Record that history could not be walked all the way to zero for
        `destination`, and this is the balance it held at the earliest
        transaction we could see. Append-only: never call this to "correct"
        an existing row -- `retire_opening_balance` supersedes instead
        (D-08).
        """
        recorded_at = recorded_at if recorded_at is not None else int(time.time())
        cursor = self._conn.execute(
            """
            INSERT INTO opening_balance
                (destination, lamports, opening_signature, recorded_at)
            VALUES (?, ?, ?, ?)
            """,
            (destination, int(lamports), opening_signature, recorded_at),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def active_opening_balance(self, destination: str) -> dict | None:
        """The row whose `retired_at` is still null -- None if there is none."""
        row = self._conn.execute(
            "SELECT * FROM opening_balance WHERE destination = ? AND retired_at IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (destination,),
        ).fetchone()
        return dict(row) if row else None

    def retire_opening_balance(
        self,
        id: int,
        *,
        lamports: int,
        opening_signature: str,
        recorded_at: int | None = None,
    ) -> int:
        """Write the replacement as a NEW row and mark the old one superseded
        (D-08) -- the original admission stays readable, it is never edited
        or deleted.
        """
        recorded_at = recorded_at if recorded_at is not None else int(time.time())
        new_id = self.record_opening_balance(
            destination=self._conn.execute(
                "SELECT destination FROM opening_balance WHERE id = ?", (id,)
            ).fetchone()["destination"],
            lamports=lamports,
            opening_signature=opening_signature,
            recorded_at=recorded_at,
        )
        self._conn.execute(
            "UPDATE opening_balance SET retired_at = ?, superseded_by = ? WHERE id = ?",
            (recorded_at, new_id, id),
        )
        self._conn.commit()
        return new_id

    def cursor_endpoints(self, target: str, purpose: str) -> list[str]:
        """Every endpoint that has a recorded cursor for this target -- the
        observation's "how many endpoints agreed" field (D-13).
        """
        rows = self._conn.execute(
            "SELECT endpoint FROM scan_cursor WHERE target = ? AND purpose = ? ORDER BY endpoint",
            (target, purpose),
        ).fetchall()
        return [row["endpoint"] for row in rows]


def open_evidence(path=DEFAULT_DB_PATH) -> Evidence:
    return Evidence(path)

"""SQLite evidence store for recorded lamport movements (D-01).

This is the working store `SOL_BURN_BALANCE` and `OPS_ROUTED` read from -- never
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
dedicated per-coin vault (D-06). Every SOL burn destination in existence today is
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

import hashlib
import json
import sqlite3
import time
from pathlib import Path

# 03-02 Task 2: `record_submission`'s closed reason vocabulary is owned by
# `intake.py` (COV-03's disposition rules live there, beside `reason_for()`
# and the terminal/correctable/transient tuples), not re-declared here. This
# is the one place in the package where a lower-level store imports a
# higher-level module -- deliberate: the vocabulary has exactly one owner,
# and the store enforces the caller's contract structurally rather than
# trusting every call site to pass a valid value. No cycle results:
# `intake.py` never imports `evidence`.
from . import intake as _intake

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
            CREATE TABLE IF NOT EXISTS burn_event (
                signature           TEXT    NOT NULL,
                mint                TEXT    NOT NULL,
                instruction_index   INTEGER NOT NULL,
                tokens_burned       INTEGER NOT NULL,
                sol_spent           INTEGER,
                supply_after        INTEGER,
                protocol_attributed INTEGER NOT NULL DEFAULT 0,
                atomic              TEXT,
                source              TEXT    NOT NULL,
                block_time          INTEGER,
                slot                INTEGER NOT NULL,
                recorded_at         INTEGER NOT NULL,
                PRIMARY KEY (signature, mint, instruction_index)
            )
            """
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS burn_event_mint ON burn_event(mint)")

        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS initial_supply (
                mint               TEXT PRIMARY KEY,
                raw_supply         INTEGER,
                decimals           INTEGER NOT NULL,
                creation_signature TEXT,
                derived_at         INTEGER NOT NULL,
                unchecked_reason   TEXT
            )
            """
        )

        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS discrepancy (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                mint                   TEXT    NOT NULL,
                observed_at            INTEGER NOT NULL,
                initial_supply         INTEGER,
                live_supply            INTEGER NOT NULL,
                implied_total_burned   INTEGER,
                attributed_burned      INTEGER NOT NULL,
                attributed_boost       INTEGER NOT NULL DEFAULT 0,
                attributed_non_boost   INTEGER NOT NULL DEFAULT 0,
                residual               INTEGER,
                decimals               INTEGER NOT NULL,
                burn_cursor_signature  TEXT,
                walk_complete          INTEGER NOT NULL DEFAULT 0,
                note                   TEXT
            )
            """
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS discrepancy_mint ON discrepancy(mint)")

        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sharing_config (
                address            TEXT    NOT NULL,
                config_hash        TEXT    NOT NULL,
                mint               TEXT    NOT NULL,
                version            INTEGER NOT NULL,
                status             INTEGER NOT NULL,
                admin              TEXT    NOT NULL,
                admin_revoked      INTEGER NOT NULL,
                shareholder_count  INTEGER NOT NULL,
                shareholders       TEXT    NOT NULL,
                first_seen         INTEGER NOT NULL,
                last_seen          INTEGER NOT NULL,
                superseded_at      INTEGER,
                PRIMARY KEY (address, config_hash)
            )
            """
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS sharing_config_mint ON sharing_config(mint)")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS sharing_config_prospect "
            "ON sharing_config(superseded_at, admin_revoked, shareholder_count)"
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

        # -- submission (COV-02/COV-03, D-34's front door, 03-02 Task 2) ----
        # Append-only, `record_sharing_config`'s pattern applied to a
        # request's history rather than a config's byte-state: a re-attempt
        # of the same issue is a NEW row, never an edit -- the facts of an
        # attempt (mint, outcome, reason) are never rewritten. Only
        # `answered_at`/`closed_at` are ever filled after insert, exactly as
        # `opening_balance`'s retired/superseded columns are the one thing
        # that changes on an otherwise-immutable row.
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS submission (
                repo          TEXT    NOT NULL,
                issue_number  INTEGER NOT NULL,
                attempted_at  INTEGER NOT NULL,
                mint          TEXT,
                outcome       TEXT    NOT NULL,
                reason        TEXT,
                detail        TEXT,
                answered_at   INTEGER,
                closed_at     INTEGER,
                PRIMARY KEY (repo, issue_number, attempted_at)
            )
            """
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS submission_mint ON submission(mint)")
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
    ) -> bool:
        """Returns True iff this call inserted a NEW row -- False when the
        signature/destination pair was already recorded (idempotent union,
        D-13: two endpoints independently seeing the same transaction must
        not rewrite an earlier recorded amount, and the caller needs to know
        whether THIS endpoint's walk actually contributed anything new).
        """
        recorded_at = recorded_at if recorded_at is not None else int(time.time())
        cursor = self._conn.execute(
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
        return cursor.rowcount > 0

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

    # -- opening_balance (EVID-02, dormant on live SOL burn data -- see docstring) --
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

    # -- burn_event (EVID-06/D-09/D-10) -------------------------------------
    def record_burn_event(
        self,
        *,
        signature: str,
        mint: str,
        instruction_index: int,
        tokens_burned: int,
        source: str,
        slot: int,
        sol_spent: int | None = None,
        supply_after: int | None = None,
        protocol_attributed: int = 0,
        atomic: str | None = None,
        block_time: int | None = None,
        recorded_at: int | None = None,
    ) -> bool:
        """`INSERT OR IGNORE` on `(signature, mint, instruction_index)` -- a
        re-scan re-observing the same burn instruction must not rewrite a
        recorded amount (T-01-10). Returns True iff this call inserted a NEW
        row.
        """
        recorded_at = recorded_at if recorded_at is not None else int(time.time())
        cursor = self._conn.execute(
            """
            INSERT OR IGNORE INTO burn_event
                (signature, mint, instruction_index, tokens_burned, sol_spent,
                 supply_after, protocol_attributed, atomic, source, block_time,
                 slot, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signature,
                mint,
                int(instruction_index),
                int(tokens_burned),
                sol_spent,
                supply_after,
                int(bool(protocol_attributed)),
                atomic,
                source,
                block_time,
                int(slot),
                recorded_at,
            ),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def burns_for(self, mint: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM burn_event WHERE mint = ? ORDER BY signature, instruction_index",
            (mint,),
        ).fetchall()
        return [dict(row) for row in rows]

    def total_burned(self, mint: str) -> int:
        """D-09: every burn against the mint, by anyone -- the sum backing
        `supply_destroyed`.
        """
        row = self._conn.execute(
            "SELECT COALESCE(SUM(tokens_burned), 0) AS total FROM burn_event WHERE mint = ?",
            (mint,),
        ).fetchone()
        return int(row["total"])

    def fill_missing_supply_after(self, mint: str, supply: int) -> int:
        """Fills a still-null `supply_after` with the mint supply observed at
        this tick -- the one field a `burn_event` row may gain after being
        recorded, since `tokens_burned`/`sol_spent` are never touched again
        once written. Returns the number of rows updated.
        """
        cursor = self._conn.execute(
            "UPDATE burn_event SET supply_after = ? WHERE mint = ? AND supply_after IS NULL",
            (supply, mint),
        )
        self._conn.commit()
        return cursor.rowcount

    # -- initial_supply (EVID-07/08, cached once per mint, permanently) -----
    def record_initial_supply(
        self,
        *,
        mint: str,
        raw_supply: int | None,
        decimals: int,
        creation_signature: str | None = None,
        unchecked_reason: str | None = None,
        derived_at: int | None = None,
    ) -> dict:
        """Write once per mint. `raw_supply` is null exactly when
        `unchecked_reason` is set -- never both, never neither (EVID-08: a
        coin whose supply cannot be derived is recorded as such, with a
        reason, rather than silently omitted or defaulted).

        `INSERT OR IGNORE` on the `mint` primary key: the supply a coin was
        created with never changes, so a second call for an already-recorded
        mint is a no-op and returns the existing row -- this is what lets
        `derive_initial_supply` treat any cached row (even an UNCHECKED one)
        as the final answer rather than re-walking the chain.
        """
        if (raw_supply is None) == (unchecked_reason is None):
            raise ValueError("exactly one of raw_supply or unchecked_reason must be set")
        derived_at = derived_at if derived_at is not None else int(time.time())
        self._conn.execute(
            """
            INSERT OR IGNORE INTO initial_supply
                (mint, raw_supply, decimals, creation_signature, derived_at, unchecked_reason)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (mint, raw_supply, int(decimals), creation_signature, derived_at, unchecked_reason),
        )
        self._conn.commit()
        return self.initial_supply_for(mint)

    def initial_supply_for(self, mint: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM initial_supply WHERE mint = ?", (mint,)
        ).fetchone()
        return dict(row) if row else None

    # -- burn atomicity (EVID-09) -------------------------------------------
    def set_atomic(self, signature: str, mint: str, instruction_index: int, verdict: str) -> None:
        """The one permitted update to an existing `burn_event` row (T-01-18):
        writes the atomicity classification only. Changes no recorded fact --
        `tokens_burned`, `sol_spent`, `slot`, everything else -- only the
        verdict computed from them. Lets a later pass classify rows written
        before `BURN_ATOMIC` existed, via `unclassified_burns`.
        """
        self._conn.execute(
            "UPDATE burn_event SET atomic = ? WHERE signature = ? AND mint = ? AND instruction_index = ?",
            (verdict, signature, mint, int(instruction_index)),
        )
        self._conn.commit()

    def unclassified_burns(self, mint: str) -> list[dict]:
        """Rows whose atomicity has not yet been classified (`atomic IS
        NULL`) -- either written before this task existed, or recorded by a
        run that has not yet re-fetched the transaction to classify it.
        """
        rows = self._conn.execute(
            "SELECT * FROM burn_event WHERE mint = ? AND atomic IS NULL ORDER BY signature, instruction_index",
            (mint,),
        ).fetchall()
        return [dict(row) for row in rows]

    # -- discrepancy (EVID-10) -- append-only: a new observation is a new row --
    def record_discrepancy(
        self,
        *,
        mint: str,
        observed_at: int,
        live_supply: int,
        attributed_burned: int,
        decimals: int,
        initial_supply: int | None = None,
        implied_total_burned: int | None = None,
        attributed_boost: int = 0,
        attributed_non_boost: int = 0,
        residual: int | None = None,
        burn_cursor_signature: str | None = None,
        walk_complete: bool = False,
        note: str | None = None,
    ) -> int:
        """Record one reconciliation observation. Never `INSERT OR IGNORE`
        or `ON CONFLICT` -- there is no natural key to dedup on, because a
        second reading of a moving residual is a NEW fact, not a repeat of
        the first (EVID-10: the residual is correct as of an observation,
        not a one-time reconciliation). `autoincrement` id makes every call
        its own row.

        `attributed_boost`/`attributed_non_boost` split `attributed_burned`
        by `burn_event.source` -- what makes the residual explicable rather
        than mysterious (they always sum to `attributed_burned`).
        """
        cursor = self._conn.execute(
            """
            INSERT INTO discrepancy
                (mint, observed_at, initial_supply, live_supply, implied_total_burned,
                 attributed_burned, attributed_boost, attributed_non_boost, residual,
                 decimals, burn_cursor_signature, walk_complete, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mint,
                int(observed_at),
                int(initial_supply) if initial_supply is not None else None,
                int(live_supply),
                int(implied_total_burned) if implied_total_burned is not None else None,
                int(attributed_burned),
                int(attributed_boost),
                int(attributed_non_boost),
                int(residual) if residual is not None else None,
                int(decimals),
                burn_cursor_signature,
                int(bool(walk_complete)),
                note,
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def discrepancies_for(self, mint: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM discrepancy WHERE mint = ? ORDER BY id", (mint,)
        ).fetchall()
        return [dict(row) for row in rows]

    def cursor_progress(self, target: str, purpose: str) -> list[dict]:
        """Every `scan_cursor` row recorded for `target`/`purpose`, one per
        contributing endpoint, ordered by endpoint for stable output (WR-01).

        `get_cursor(target, purpose)` alone reads a single endpoint --
        defaulting to the single-endpoint sentinel `DEFAULT_ENDPOINT_KEY`,
        which `scan_inflows_all_endpoints()` (the only production scan path,
        D-13) never writes to. This is the read that matches how production
        actually writes cursors: one row per endpoint, under that endpoint's
        own identifier. Each row carries its `endpoint`, `oldest_signature`,
        `last_signature`, `backfill_complete` and `last_error` -- everything
        `_incomplete_walk_detail()` needs to report what each endpoint
        actually reached, rather than reading a key production never
        populates.
        """
        rows = self._conn.execute(
            "SELECT * FROM scan_cursor WHERE target = ? AND purpose = ? ORDER BY endpoint",
            (target, purpose),
        ).fetchall()
        return [dict(row) for row in rows]

    # -- sharing_config (COV-01/COV-02, plan 03-01) --------------------------
    # Raw facts only: no bps columns, no classification column. Both are
    # derived through `legs` against a `Registry` that changes in phase 5, and
    # a derived value in a fact table is a value that can go stale without
    # anything failing -- `legs.classify_split` is computed fresh every time
    # instead.
    def _config_hash(self, config) -> str:
        """A sha256 over the CANONICAL DECODED tuple -- never over the raw
        account bytes, which differ between a sliced (enumeration) and an
        unsliced (per-coin) read of the same account.
        """
        canonical = {
            "mint": config.mint,
            "version": int(config.version),
            "status": int(config.status),
            "admin": config.admin,
            "admin_revoked": bool(config.admin_revoked),
            "shareholders": [[who, int(bps)] for who, bps in config.shareholders],
        }
        return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode("utf-8")).hexdigest()

    def record_sharing_config(self, config, *, recorded_at: int | None = None) -> bool:
        """Append-only, `opening_balance`'s supersede pattern applied to a
        config's own byte-state: `INSERT OR IGNORE` the row keyed on
        `(address, config_hash)`, advance `last_seen` for that exact row,
        then mark every OTHER non-superseded row for this address
        superseded. A config whose shareholder list changed therefore leaves
        BOTH states in the store -- the change is evidence, never an
        overwrite, and the superseded row's own recorded fields are never
        touched. Returns True iff this call inserted a NEW row.
        """
        recorded_at = recorded_at if recorded_at is not None else int(time.time())
        config_hash = self._config_hash(config)
        shareholders_json = json.dumps(
            [[who, int(bps)] for who, bps in config.shareholders], separators=(",", ":")
        )
        cursor = self._conn.execute(
            """
            INSERT OR IGNORE INTO sharing_config
                (address, config_hash, mint, version, status, admin, admin_revoked,
                 shareholder_count, shareholders, first_seen, last_seen, superseded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                config.address,
                config_hash,
                config.mint,
                int(config.version),
                int(config.status),
                config.admin,
                int(bool(config.admin_revoked)),
                len(config.shareholders),
                shareholders_json,
                recorded_at,
                recorded_at,
            ),
        )
        inserted = cursor.rowcount > 0
        self._conn.execute(
            "UPDATE sharing_config SET last_seen = ? WHERE address = ? AND config_hash = ?",
            (recorded_at, config.address, config_hash),
        )
        self._conn.execute(
            "UPDATE sharing_config SET superseded_at = ? "
            "WHERE address = ? AND config_hash != ? AND superseded_at IS NULL",
            (recorded_at, config.address, config_hash),
        )
        self._conn.commit()
        return inserted

    def current_sharing_configs(
        self, *, admin_revoked: bool | None = None, min_shareholders: int | None = None
    ) -> list[dict]:
        """The non-superseded row per address, optionally filtered, ordered
        by mint for deterministic output.
        """
        clauses = ["superseded_at IS NULL"]
        params: list = []
        if admin_revoked is not None:
            clauses.append("admin_revoked = ?")
            params.append(int(bool(admin_revoked)))
        if min_shareholders is not None:
            clauses.append("shareholder_count >= ?")
            params.append(int(min_shareholders))
        query = "SELECT * FROM sharing_config WHERE " + " AND ".join(clauses) + " ORDER BY mint"
        rows = self._conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def sharing_config_for(self, mint: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM sharing_config WHERE mint = ? AND superseded_at IS NULL "
            "ORDER BY last_seen DESC LIMIT 1",
            (mint,),
        ).fetchone()
        return dict(row) if row else None

    def sharing_config_counts(self) -> dict:
        """The four population counts (03-CONTEXT.md's amendment table),
        computed by query every time -- never stored, because the enumerated
        set is live and growing.
        """
        row = self._conn.execute(
            """
            SELECT
                COUNT(*) AS enumerated,
                SUM(CASE WHEN shareholder_count > 1 THEN 1 ELSE 0 END) AS multi_shareholder,
                SUM(CASE WHEN admin_revoked = 1 THEN 1 ELSE 0 END) AS admin_revoked,
                SUM(CASE WHEN shareholder_count > 1 AND admin_revoked = 0 THEN 1 ELSE 0 END) AS prospects
            FROM sharing_config WHERE superseded_at IS NULL
            """
        ).fetchone()
        return {
            "enumerated": row["enumerated"] or 0,
            "multi_shareholder": row["multi_shareholder"] or 0,
            "admin_revoked": row["admin_revoked"] or 0,
            "prospects": row["prospects"] or 0,
        }

    # -- submission (COV-02/COV-03, 03-02 Task 2) ---------------------------
    OUTCOMES = ("observed", "failed")

    def record_submission(
        self,
        *,
        repo: str,
        issue_number: int,
        outcome: str,
        mint: str | None = None,
        reason: str | None = None,
        detail: str | None = None,
        attempted_at: int | None = None,
    ) -> None:
        """One row per attempt, never rewritten. `outcome` must be a member
        of `OUTCOMES`; `reason` -- required when `outcome` is `"failed"`,
        forbidden when it is `"observed"` -- must be a member of
        `intake.REASONS`. Raises `ValueError` for anything outside either
        closed vocabulary, so a reason this store cannot back fails loudly
        here rather than being recorded as a fact (the same discipline
        `record_initial_supply` already applies to its own exactly-one-of
        pair).
        """
        if outcome not in self.OUTCOMES:
            raise ValueError(f"{outcome!r} is not a member of Evidence.OUTCOMES {self.OUTCOMES}")
        if outcome == "failed" and reason is None:
            raise ValueError("a failed submission must carry a reason")
        if outcome == "observed" and reason is not None:
            raise ValueError("an observed submission must not carry a failure reason")
        if reason is not None and reason not in _intake.REASONS:
            raise ValueError(f"{reason!r} is not a member of intake.REASONS")

        attempted_at = attempted_at if attempted_at is not None else int(time.time())
        self._conn.execute(
            """
            INSERT INTO submission
                (repo, issue_number, attempted_at, mint, outcome, reason, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (repo, int(issue_number), int(attempted_at), mint, outcome, reason, detail),
        )
        self._conn.commit()

    # Four literal query strings rather than one built with an f-string --
    # `tests/test_discipline.py::TestSqlIdentifierAllowlist` flags any
    # non-literal fragment reaching `execute()`, and a WHERE clause built by
    # joining conditional pieces would be exactly that, even though every
    # piece here is a fixed literal itself.
    _SELECT_ALL = "SELECT * FROM submission ORDER BY repo, issue_number, attempted_at"
    _SELECT_BY_REPO = "SELECT * FROM submission WHERE repo = ? ORDER BY repo, issue_number, attempted_at"
    _SELECT_BY_MINT = "SELECT * FROM submission WHERE mint = ? ORDER BY repo, issue_number, attempted_at"
    _SELECT_BY_REPO_AND_MINT = (
        "SELECT * FROM submission WHERE repo = ? AND mint = ? ORDER BY repo, issue_number, attempted_at"
    )

    def submissions(self, *, repo: str | None = None, mint: str | None = None) -> list[dict]:
        if repo is not None and mint is not None:
            rows = self._conn.execute(self._SELECT_BY_REPO_AND_MINT, (repo, mint)).fetchall()
        elif repo is not None:
            rows = self._conn.execute(self._SELECT_BY_REPO, (repo,)).fetchall()
        elif mint is not None:
            rows = self._conn.execute(self._SELECT_BY_MINT, (mint,)).fetchall()
        else:
            rows = self._conn.execute(self._SELECT_ALL).fetchall()
        return [dict(row) for row in rows]

    def submission_counts(self) -> dict:
        """The two counts `coverage_statement()` (D-35) is fed: distinct
        mints ever observed through this store, and rows whose outcome was
        a failure. Never a count of issues or of attempts -- a mint
        resubmitted and re-observed several times is still one observed
        coin, exactly as `submission_counts`'s own docstring in the plan
        requires.
        """
        row = self._conn.execute(
            "SELECT COUNT(DISTINCT mint) AS observed FROM submission WHERE outcome = 'observed'"
        ).fetchone()
        failed_row = self._conn.execute(
            "SELECT COUNT(*) AS failed FROM submission WHERE outcome = 'failed'"
        ).fetchone()
        return {"observed": row["observed"] or 0, "failed": failed_row["failed"] or 0}

    def unanswered_submissions(self) -> list[dict]:
        """Every row not yet marked answered, oldest attempt first -- the
        reply step's own input. A transient-reason row is legitimately
        returned here forever until a *later* attempt for the same issue
        succeeds or terminates; the reply step itself decides to do nothing
        with a transient row rather than this query filtering it out, so
        `submissions()`'s full history and this view never disagree about
        what "unanswered" means.
        """
        rows = self._conn.execute(
            "SELECT * FROM submission WHERE answered_at IS NULL ORDER BY attempted_at"
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_answered(
        self,
        *,
        repo: str,
        issue_number: int,
        attempted_at: int,
        closed: bool,
        answered_at: int | None = None,
    ) -> None:
        """The one permitted update to a submission row (mirrors
        `set_atomic`'s one-column-only update): fills `answered_at`, and
        `closed_at` when `closed` is true. Never touches the facts of the
        attempt itself.
        """
        answered_at = answered_at if answered_at is not None else int(time.time())
        closed_at = answered_at if closed else None
        self._conn.execute(
            "UPDATE submission SET answered_at = ?, closed_at = ? "
            "WHERE repo = ? AND issue_number = ? AND attempted_at = ?",
            (answered_at, closed_at, repo, int(issue_number), int(attempted_at)),
        )
        self._conn.commit()

    def cursor_endpoints(self, target: str, purpose: str) -> list[str]:
        """Every endpoint that successfully walked at least one signature for
        this target -- D-13's "how many endpoints contributed" field. An
        endpoint that only ever recorded a `last_error` (it errored before
        seeing anything) does not count; a gap in coverage is stored, never
        inferred from silence, but it is also not counted as a contribution.
        """
        rows = self._conn.execute(
            "SELECT endpoint FROM scan_cursor WHERE target = ? AND purpose = ? "
            "AND last_signature IS NOT NULL ORDER BY endpoint",
            (target, purpose),
        ).fetchall()
        return [row["endpoint"] for row in rows]


def open_evidence(path=DEFAULT_DB_PATH) -> Evidence:
    return Evidence(path)

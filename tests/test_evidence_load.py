"""The committed export, loaded back into a working store.

The export has always been one-way: `state/evidence/*.jsonl` is the record,
`state/evidence.db` is a working cache, and the cache is not committed. That
was fine while every render happened on a machine that had done the walk.

It stopped being fine when the deploy repository started rendering the
landing page in CI. Measured, on a real run: its `state/evidence.db` holds
zero burn events, zero initial supplies and zero inflows, so five of the six
landing counters came back "unknown" even with the chain reachable. The
figures on the deployed page were not reproducible from either repository --
they had been generated somewhere else and copied.

So the export loads. These cover the properties that decide whether the
loaded store is the same store: every row, the same values, and loading twice
being the same as loading once.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from indexer.evidence import Evidence  # noqa: E402
from indexer.export import EXPORT_TABLES, export_all, import_all, import_table  # noqa: E402

COMMITTED = ROOT / "state" / "evidence"
MINT = "8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump"


def counts(evidence) -> dict:
    return {table: evidence.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table, _order in EXPORT_TABLES}


class LoadCase(unittest.TestCase):
    def store(self) -> Evidence:
        tmp = tempfile.mkdtemp()
        evidence = Evidence(Path(tmp) / "evidence.db")
        self.addCleanup(evidence.close)
        return evidence


class TestTheCommittedExportLoads(LoadCase):
    def test_the_repository_s_own_export_produces_a_populated_store(self):
        evidence = self.store()
        loaded = import_all(evidence, COMMITTED)
        self.assertGreater(loaded["burn_event"], 0, "the committed burn walk")
        self.assertEqual(loaded["initial_supply"], 1)
        self.assertEqual(counts(evidence)["burn_event"], loaded["burn_event"])

    def test_the_rows_are_the_rows_that_were_exported(self):
        """Not just the count. A positional insert that mismatched the column
        order would load exactly the right number of entirely wrong rows.
        """
        evidence = self.store()
        import_all(evidence, COMMITTED)
        exported = [json.loads(line) for line
                    in (COMMITTED / "initial_supply.jsonl").read_text().splitlines() if line.strip()]
        row = evidence.connection.execute(
            "SELECT * FROM initial_supply WHERE mint = ?", (exported[0]["mint"],)
        ).fetchone()
        columns = [d[0] for d in evidence.connection.execute(
            "SELECT * FROM initial_supply LIMIT 0").description]
        self.assertEqual(dict(zip(columns, row)), exported[0])

    def test_loading_twice_is_the_same_as_loading_once(self):
        """The job that does this runs on a schedule. Duplicated burn rows
        would inflate every figure derived from them, on the front page.
        """
        evidence = self.store()
        import_all(evidence, COMMITTED)
        first = counts(evidence)
        import_all(evidence, COMMITTED)
        self.assertEqual(counts(evidence), first)

    def test_a_round_trip_through_the_export_is_byte_identical(self):
        """Load it, export it again, and the files must not move. This is the
        property that makes the `.db` a cache of the export rather than a
        second, diverging record.
        """
        evidence = self.store()
        import_all(evidence, COMMITTED)
        with tempfile.TemporaryDirectory() as out:
            export_all(evidence, out)
            for table, _order in EXPORT_TABLES:
                with self.subTest(table=table):
                    self.assertEqual(
                        (Path(out) / f"{table}.jsonl").read_bytes(),
                        (COMMITTED / f"{table}.jsonl").read_bytes(),
                    )


class TestEveryTableTheStoreHasIsExported(LoadCase):
    """`EXPORT_TABLES` is a hand-written list, and the `.db` is no longer
    committed anywhere.

    So a table missing from the list is now data that does not survive a CI
    run at all -- written by one job and gone by the next. `sharing_config`
    was missing, and only had no rows yet by luck. Adding a table to
    `evidence.py` and forgetting the export must fail here rather than
    quietly lose what that table records.
    """

    # Not evidence, and deliberately not exported: `schema_version` is the
    # store's own migration marker and `sqlite_sequence` is SQLite's.
    NOT_EVIDENCE = {"schema_version", "sqlite_sequence"}

    def test_the_export_covers_the_whole_store(self):
        evidence = self.store()
        tables = {
            row[0] for row in evidence.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")
        } - self.NOT_EVIDENCE
        self.assertEqual({table for table, _order in EXPORT_TABLES}, tables)

    def test_every_exported_table_orders_by_columns_it_has(self):
        """The order is what makes the export deterministic, and an ORDER BY
        naming a column the table lost would raise only when that table next
        exported -- in CI, mid-run.
        """
        evidence = self.store()
        for table, order_by in EXPORT_TABLES:
            columns = {row[1] for row in
                       evidence.connection.execute(f"PRAGMA table_info({table})")}
            for column in (c.strip() for c in order_by.split(",")):
                with self.subTest(table=table, column=column):
                    self.assertIn(column, columns)


class TestALoadNeverRewritesARowTheStoreAlreadyHas(LoadCase):
    """`INSERT OR IGNORE`, which is `evidence.py`'s own rule for every write.

    `OR REPLACE` is equally idempotent -- every table has a real primary key,
    so neither adds rows -- and it silently overwrites what is there with the
    exported version. The columns that lose are exactly the ones the store
    documents as the single mutable field on an otherwise immutable row. In
    the deploy repository, loading a stale export would blank
    `submission.answered_at`, and the next run would comment on and close an
    issue it had already answered.

    `test_loading_twice_is_the_same_as_loading_once` cannot tell the two
    apart. Two of the three below can: with `OR REPLACE` restored,
    `test_an_answered_submission_is_not_reset_to_unanswered` and
    `test_the_statement_is_the_one_the_store_documents` fail.
    `test_the_row_count_is_unchanged_either_way` passes either way on
    purpose -- it is there to stop the reason for `OR IGNORE` being
    misremembered as the duplicate-rows one.
    """

    def _older_export(self, tmp: Path) -> Path:
        """The same rows, as they were before anything was filled in."""
        source = Path(tmp)
        (source / "submission.jsonl").write_text(json.dumps({
            "repo": "owner/repo", "issue_number": 7, "mint": MINT,
            "attempted_at": 100.0, "outcome": "observed", "reason": None,
            "detail": None, "answered_at": None, "closed_at": None,
        }) + "\n", encoding="utf-8")
        return source

    def test_an_answered_submission_is_not_reset_to_unanswered(self):
        evidence = self.store()
        with tempfile.TemporaryDirectory() as tmp:
            source = self._older_export(Path(tmp))
            import_table(evidence.connection, "submission", source / "submission.jsonl")
            evidence.connection.execute(
                "UPDATE submission SET answered_at = ?, closed_at = ? WHERE issue_number = ?",
                (200.0, 200.0, 7),
            )
            evidence.connection.commit()
            import_table(evidence.connection, "submission", source / "submission.jsonl")
        row = evidence.connection.execute(
            "SELECT answered_at, closed_at FROM submission WHERE issue_number = 7"
        ).fetchone()
        self.assertEqual((row["answered_at"], row["closed_at"]), (200.0, 200.0))

    def test_the_row_count_is_unchanged_either_way(self):
        """Stated so the reason for `OR IGNORE` is not mistaken for the
        duplicate-rows one: both are idempotent, and only one keeps the row.
        """
        evidence = self.store()
        with tempfile.TemporaryDirectory() as tmp:
            source = self._older_export(Path(tmp))
            import_table(evidence.connection, "submission", source / "submission.jsonl")
            import_table(evidence.connection, "submission", source / "submission.jsonl")
        self.assertEqual(
            evidence.connection.execute("SELECT COUNT(*) FROM submission").fetchone()[0], 1
        )

    def test_the_statement_is_the_one_the_store_documents(self):
        """Read from the source, because the defect is one word inside a
        string and a reviewer's eye slides over it.
        """
        source = (ROOT / "indexer" / "export.py").read_text(encoding="utf-8")
        statement = [line for line in source.splitlines() if "INSERT OR" in line and "f\"" in line]
        self.assertEqual(len(statement), 1, statement)
        self.assertIn("INSERT OR IGNORE", statement[0])


class TestWhatItRefuses(LoadCase):
    def test_an_absent_file_loads_nothing_rather_than_raising(self):
        """A table with no rows exports an empty file, and a fresh export
        directory may not have every file yet. Neither is corruption.
        """
        evidence = self.store()
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(import_table(evidence.connection, "burn_event",
                                          Path(empty) / "burn_event.jsonl"), 0)

    def test_a_record_naming_an_unknown_column_is_refused(self):
        """A corrupt or stale export must not be able to name a column. This
        is also what stops file content composing a SQL identifier: it never
        reaches the statement, which is positional.
        """
        evidence = self.store()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "initial_supply.jsonl"
            path.write_text(json.dumps({"mint": MINT, "drop_table": 1}) + "\n")
            with self.assertRaises(ValueError) as caught:
                import_table(evidence.connection, "initial_supply", path)
        self.assertIn("drop_table", str(caught.exception))

    def test_the_export_never_emits_a_json_boolean(self):
        """`true` becomes `1` on the way back through SQLite, so a boolean
        would not survive the round trip the class above pins. `export_all`
        does not write one -- SQLite has no boolean type and the store's
        integer columns come back as integers -- and this is what keeps that
        true, because the round-trip test cannot see a type that never
        appears in the committed files.
        """
        for table, _order in EXPORT_TABLES:
            path = COMMITTED / f"{table}.jsonl"
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                for key, value in json.loads(line).items():
                    with self.subTest(table=table, line=number, key=key):
                        self.assertNotIsInstance(value, bool)

    def test_blank_lines_are_skipped(self):
        evidence = self.store()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "initial_supply.jsonl"
            body = (COMMITTED / "initial_supply.jsonl").read_text()
            path.write_text("\n" + body + "\n\n")
            self.assertEqual(import_table(evidence.connection, "initial_supply", path), 1)


if __name__ == "__main__":
    unittest.main()

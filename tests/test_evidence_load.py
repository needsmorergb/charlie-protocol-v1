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

    def test_blank_lines_are_skipped(self):
        evidence = self.store()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "initial_supply.jsonl"
            body = (COMMITTED / "initial_supply.jsonl").read_text()
            path.write_text("\n" + body + "\n\n")
            self.assertEqual(import_table(evidence.connection, "initial_supply", path), 1)


if __name__ == "__main__":
    unittest.main()

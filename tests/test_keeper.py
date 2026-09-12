"""The keeper: that an idle cycle is a success, and that it cannot overspend.

A scheduled job that goes red when there is no work is a job somebody turns
off. The property that keeps this one green is in the PROGRAM -- `split`
returns Ok with nothing above the rent reserve -- and the keeper's job is to
report that as an idle success rather than an error. Both halves are checked
here, the program's half against the recorded devnet cycles.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import keeper  # noqa: E402

LOG = ROOT / "state" / "flywheel" / "keeper.jsonl"


def cycles() -> list:
    if not LOG.exists():
        return []
    return [json.loads(line) for line in LOG.read_text(encoding="utf-8").splitlines() if line.strip()]


class Describe(unittest.TestCase):
    """The log line a human reads. It has to distinguish idle from broken."""

    def test_an_idle_cycle_reads_as_idle_not_as_an_error(self):
        line = keeper.describe({"at": 0, "outcome": "idle", "pending_lamports": 0})
        self.assertIn("idle", line)
        self.assertNotIn("REFUSED", line)

    def test_a_refusal_is_loud(self):
        line = keeper.describe(
            {"at": 0, "outcome": "refused", "error": '{"InstructionError": []}'}
        )
        self.assertIn("REFUSED", line)

    def test_a_split_reports_all_three_legs(self):
        line = keeper.describe({
            "at": 0,
            "outcome": "split",
            "signature": "abcdefghijkl",
            "event": {"l": 1000, "sol_burn": 500, "buyback": 250, "ops": 250},
        })
        for figure in ("500", "250", "1,000"):
            self.assertIn(figure, line)

    def test_the_keeper_floor_leaves_room_for_more_than_one_call(self):
        """Stopping at a floor beats failing one call at a time until
        somebody notices. The floor must exceed a single fee."""
        self.assertGreater(keeper.MIN_KEEPER_LAMPORTS, 5_000)


class RecordedCycles(unittest.TestCase):
    def setUp(self):
        self.cycles = cycles()
        if not self.cycles:
            self.skipTest("the keeper has not run")

    def test_no_cycle_errored(self):
        """Including the ones that found nothing."""
        for cycle in self.cycles:
            self.assertIn(
                cycle["outcome"], ("split", "idle", "dry_run"), cycle
            )

    def test_idle_cycles_split_nothing_and_lost_nothing(self):
        for cycle in self.cycles:
            if cycle["outcome"] != "idle":
                continue
            self.assertEqual(cycle["pending_lamports"], 0)
            self.assertEqual((cycle.get("event") or {}).get("l", 0), 0)

    def test_every_split_cycle_divided_fifty_twentyfive_twentyfive(self):
        seen = 0
        for cycle in self.cycles:
            if cycle["outcome"] != "split":
                continue
            seen += 1
            event = cycle["event"]
            self.assertEqual(event["sol_burn"], event["l"] * 50 // 100)
            self.assertEqual(event["buyback"], event["l"] * 25 // 100)
            self.assertEqual(event["ops"], event["l"] * 25 // 100)
        if not seen:
            self.skipTest("no split cycle recorded yet")

    def test_a_split_cycle_had_something_to_split(self):
        for cycle in self.cycles:
            if cycle["outcome"] == "split":
                self.assertGreater(cycle["pending_lamports"], 0)


if __name__ == "__main__":
    unittest.main()

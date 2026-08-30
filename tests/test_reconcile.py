"""Offline tests for EVID-10: `indexer/reconcile.py`.

`python -m unittest discover -s tests -t tests -p "test_reconcile.py"`.

No network. RESEARCH.md Q8's exact chain-derived $CHARLIE figures are pinned
here as a regression vector, in raw units:

    initial_supply         1,000,000,000,000,000   (1,000,000,000.000000 UI)
    live_supply               956,384,474,035,955   (956,384,474.035955 UI)
    boost_burned               43,575,480,427,900   (43,575,480.427900 UI)
    implied_total_burned       43,615,525,964,045   (initial_supply - live_supply)
    residual                       40,045,536,145   (implied_total_burned - boost_burned)

"Two corrections this plan carries" (01-03-PLAN.md): 40,045.536145 is the
figure to explain -- all non-boost burns -- not a contradiction with any
earlier, differently-baselined estimate. And the residual is correct *as of
an observation*, never a one-time reconciliation.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer.evidence import Evidence
from indexer.pump import MintState, TOKEN_2022_PROGRAM
from indexer.reconcile import reconcile, record, render

CHARLIE = "8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump"

CHARLIE_INITIAL_SUPPLY = 1_000_000_000_000_000
CHARLIE_LIVE_SUPPLY = 956_384_474_035_955
CHARLIE_BOOST_BURNED = 43_575_480_427_900
CHARLIE_IMPLIED_TOTAL_BURNED = 43_615_525_964_045
CHARLIE_RESIDUAL = 40_045_536_145


def evidence_db(tmp_dir: str) -> Evidence:
    return Evidence(Path(tmp_dir) / "evidence.db")


def mint_state(supply: int, decimals: int = 6) -> MintState:
    return MintState(
        mint=CHARLIE, supply=supply, decimals=decimals,
        mint_authority=None, freeze_authority=None, program=TOKEN_2022_PROGRAM,
    )


def seed_boost_burn(evidence, mint, tokens_burned, *, index=0, signature="sig-boost"):
    evidence.record_burn_event(
        signature=signature, mint=mint, instruction_index=index,
        tokens_burned=tokens_burned, source="boost_buy_and_burn", slot=1, atomic="PASS",
    )


class TestReconcilePinnedCharlieFigures(unittest.TestCase):
    """RESEARCH.md Q8's exact figures, pinned as a regression vector."""

    def test_charlie_figures_reproduce_the_pinned_residual(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            evidence.record_initial_supply(mint=CHARLIE, raw_supply=CHARLIE_INITIAL_SUPPLY, decimals=6)
            seed_boost_burn(evidence, CHARLIE, CHARLIE_BOOST_BURNED)
            result = reconcile(evidence, CHARLIE, mint_state(CHARLIE_LIVE_SUPPLY), observed_at=1)
            evidence.close()

            self.assertEqual(result["initial_supply"], CHARLIE_INITIAL_SUPPLY)
            self.assertEqual(result["live_supply"], CHARLIE_LIVE_SUPPLY)
            self.assertEqual(result["implied_total_burned"], CHARLIE_IMPLIED_TOTAL_BURNED)
            self.assertEqual(result["attributed_burned"], CHARLIE_BOOST_BURNED)
            self.assertEqual(result["residual"], CHARLIE_RESIDUAL)
            self.assertEqual(result["attributed_burned_by_source"], {"boost_buy_and_burn": CHARLIE_BOOST_BURNED})


class TestReconcileReturnsExactIntegers(unittest.TestCase):
    def test_every_quantity_is_an_int(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            evidence.record_initial_supply(mint=CHARLIE, raw_supply=1_000, decimals=6)
            seed_boost_burn(evidence, CHARLIE, 100)
            result = reconcile(evidence, CHARLIE, mint_state(900), observed_at=1)
            evidence.close()

            for key in ("initial_supply", "live_supply", "implied_total_burned", "attributed_burned", "residual"):
                self.assertIsInstance(result[key], int, key)
                self.assertNotIsInstance(result[key], bool)


class TestReconcileAppendOnly(unittest.TestCase):
    def test_two_reconciliations_append_two_rows_neither_superseding(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            evidence.record_initial_supply(mint=CHARLIE, raw_supply=1_000, decimals=6)
            seed_boost_burn(evidence, CHARLIE, 100)

            first_result = reconcile(evidence, CHARLIE, mint_state(900), observed_at=1)
            record(evidence, first_result)
            second_result = reconcile(evidence, CHARLIE, mint_state(850), observed_at=2)
            record(evidence, second_result)

            rows = evidence.discrepancies_for(CHARLIE)
            evidence.close()

            self.assertEqual(len(rows), 2)
            self.assertNotEqual(rows[0]["residual"], rows[1]["residual"])
            self.assertEqual(rows[0]["live_supply"], 900)
            self.assertEqual(rows[1]["live_supply"], 850)
            for row in rows:
                self.assertIsNone(row.get("superseded_by"), "no such column -- neither row is ever superseded")


class TestReconcileCarriesTheObservation(unittest.TestCase):
    def test_carries_timestamp_cursor_and_walk_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            evidence.record_initial_supply(mint=CHARLIE, raw_supply=1_000, decimals=6)
            evidence.set_cursor(CHARLIE, "burn", last_signature="sig-latest", backfill_complete=1)
            result = reconcile(evidence, CHARLIE, mint_state(900), observed_at=42)
            evidence.close()

            self.assertEqual(result["observed_at"], 42)
            self.assertEqual(result["burn_cursor_signature"], "sig-latest")
            self.assertTrue(result["walk_complete"])


class TestReconcileUnderivableInitialSupply(unittest.TestCase):
    def test_no_residual_and_a_named_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            evidence.record_initial_supply(
                mint=CHARLIE, raw_supply=None, decimals=6,
                unchecked_reason="walked to exhaustion without finding CreateEvent",
            )
            result = reconcile(evidence, CHARLIE, mint_state(900), observed_at=1)
            evidence.close()

            self.assertIsNone(result["residual"])
            self.assertIsNone(result["implied_total_burned"])
            self.assertIn("CreateEvent", result["reason"])

    def test_no_initial_supply_row_at_all_still_returns_a_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            result = reconcile(evidence, CHARLIE, mint_state(900), observed_at=1)
            evidence.close()

            self.assertIsNone(result["residual"])
            self.assertIsNotNone(result["reason"])


class TestReconcileIncompleteWalk(unittest.TestCase):
    def test_residual_returned_but_flagged_not_yet_attributable(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            evidence.record_initial_supply(mint=CHARLIE, raw_supply=1_000, decimals=6)
            seed_boost_burn(evidence, CHARLIE, 100)
            # No cursor recorded at all -- is_backfill_complete is False by default.
            result = reconcile(evidence, CHARLIE, mint_state(900), observed_at=1)
            evidence.close()

            self.assertIsNotNone(result["residual"])
            self.assertFalse(result["walk_complete"])


class TestRenderIsIdempotent(unittest.TestCase):
    def test_rendering_the_same_stored_row_twice_is_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            evidence.record_initial_supply(mint=CHARLIE, raw_supply=1_000, decimals=6)
            seed_boost_burn(evidence, CHARLIE, 100)
            result = reconcile(evidence, CHARLIE, mint_state(900), observed_at=1)
            row = record(evidence, result)
            evidence.close()

            self.assertEqual(render(row), render(row))

    def test_render_of_underivable_row_states_the_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            evidence.record_initial_supply(mint=CHARLIE, raw_supply=None, decimals=6, unchecked_reason="no CreateEvent found")
            result = reconcile(evidence, CHARLIE, mint_state(900), observed_at=1)
            row = record(evidence, result)
            evidence.close()

            text = render(row)
            self.assertIn("no CreateEvent found", text)
            self.assertIn("not computable", text)


class TestRenderStatesThePinnedCharlieFigure(unittest.TestCase):
    def test_states_40045_536145_and_the_observation_framing_without_contradiction(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            evidence.record_initial_supply(mint=CHARLIE, raw_supply=CHARLIE_INITIAL_SUPPLY, decimals=6)
            seed_boost_burn(evidence, CHARLIE, CHARLIE_BOOST_BURNED)
            result = reconcile(evidence, CHARLIE, mint_state(CHARLIE_LIVE_SUPPLY), observed_at=1)
            row = record(evidence, result)
            evidence.close()

            text = render(row)
            self.assertIn("40,045.536145", text)
            self.assertIn("as of the observation", text.lower())
            self.assertIn("still falling", text)
            self.assertNotIn("contradict", text.lower())
            self.assertNotIn("discrepancy between", text.lower())

    def test_boost_and_non_boost_totals_are_separately_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            evidence.record_initial_supply(mint=CHARLIE, raw_supply=CHARLIE_INITIAL_SUPPLY, decimals=6)
            seed_boost_burn(evidence, CHARLIE, CHARLIE_BOOST_BURNED)
            evidence.record_burn_event(
                signature="sig-stranger", mint=CHARLIE, instruction_index=0,
                tokens_burned=500, source="spl_burn", slot=2, atomic="PASS",
            )
            result = reconcile(evidence, CHARLIE, mint_state(CHARLIE_LIVE_SUPPLY - 500), observed_at=1)
            row = record(evidence, result)
            evidence.close()

            self.assertEqual(row["attributed_boost"], CHARLIE_BOOST_BURNED)
            self.assertEqual(row["attributed_non_boost"], 500)
            text = render(row)
            self.assertIn(f"{CHARLIE_BOOST_BURNED:,}", text)
            self.assertIn("500", text)


if __name__ == "__main__":
    unittest.main()

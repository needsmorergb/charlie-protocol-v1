"""Burn campaigns: that a stated goal never becomes an unchecked figure.

`python -m unittest discover -s tests -t tests -p "test_campaign.py"`.

A campaign is the one surface here that carries something nobody measured --
a goal somebody declared. Two failure modes follow from that, and both are
pinned here rather than left to review:

1. **A campaign becomes a way to publish an ungated number.** Progress is
   computed from the same rows `SOL_BURN_TOTAL` and `SUPPLY_DESTROYED` are
   computed from, and obtained through `publish.Publisher`, so a coin whose
   totals are withheld must yield a campaign with NO progress figure -- not
   zero, and not the raw sum. The tests below drive a withheld observation
   and assert the value is absent and the blocking check is named.

2. **A campaign quietly becomes whatever happened.** Re-declaring must not
   retarget, a reached campaign must not revert, and every status change
   must leave an append-only event row. The id is derived from `(mint, name)`
   alone for exactly this reason: an earlier version hashed `created_at` too,
   so declaring the same campaign twice produced two campaigns with the same
   goal and different ids.

No network. Every observation is built here.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from indexer import campaign, invariants  # noqa: E402
from indexer.evidence import Evidence  # noqa: E402
from indexer.legs import Attribution, Split  # noqa: E402
from indexer.observe import Observation  # noqa: E402

MINT = "8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump"
SOL_BURN = "burn111111111111111111111111111111111111111"
SOL = campaign.LAMPORTS_PER_SOL


def sol_burn_split(address=SOL_BURN) -> Split:
    attribution = Attribution(
        address=address, bps=10_000, leg="sol_burn", reason="test fixture", keyless=True
    )
    return Split(sol_burn=10_000, burn=0, paid=0, attributions=(attribution,))


def passing_check(name: str, backs) -> invariants.Check:
    return invariants.Check(name, invariants.PASS, tuple(backs), "eq", "fixture")


def unchecked_check(name: str, backs) -> invariants.Check:
    return invariants.Check(name, invariants.UNCHECKED, tuple(backs), "eq", "the walk has not run")


def observation(*, checks, split=None, evidence_block=None) -> Observation:
    """An observation whose verdict is computed the same way production's is."""
    record = Observation(
        mint=MINT,
        observed_at=1.0,
        split=split if split is not None else sol_burn_split(),
        checks=tuple(checks),
        evidence=evidence_block,
    )
    record.verdict = invariants.apply_silence_rule(record.checks)
    return record


def publishable_sol() -> Observation:
    """Every figure backed by a passing check -- the green case."""
    return observation(
        checks=[passing_check("FIXTURE_ALL", invariants.FIGURES)],
        evidence_block={SOL_BURN: 0, "burn_total": 0},
    )


def withheld_sol() -> Observation:
    """SOL_BURN_TOTAL blocked by an UNCHECKED walk -- $CHARLIE's real state."""
    return observation(
        checks=[
            passing_check("FIXTURE_REST", (invariants.SPLIT, invariants.SUPPLY_DESTROYED)),
            unchecked_check("SOL_BURN_BALANCE", (invariants.SOL_BURN_TOTAL,)),
        ],
        evidence_block={SOL_BURN: 9_999 * SOL, "burn_total": 0},
    )


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.evidence = Evidence(Path(self.dir.name) / "evidence.db")
        self.addCleanup(self.evidence.close)

    def declare(self, **overrides):
        fields = dict(
            mint=MINT,
            name="Burn 5 SOL",
            trigger_type=campaign.TRIGGER_SOL_AMOUNT,
            trigger_value=5 * SOL,
            target_value=5 * SOL,
            asset=campaign.ASSET_SOL,
            created_at=1_000,
        )
        fields.update(overrides)
        return campaign.declare(self.evidence, **fields)

    def record_burn_inflow(self, lamports: int, *, signature: str, block_time: int):
        self.evidence.record_inflow(
            signature=signature,
            destination=SOL_BURN,
            mint=MINT,
            leg="sol_burn",
            lamports=lamports,
            block_time=block_time,
            slot=1,
        )


class TheStatedGoal(StoreCase):
    def test_declaring_stores_the_goal_and_opens_it_active(self):
        row = self.declare()
        self.assertEqual(row["status"], campaign.STATUS_ACTIVE)
        self.assertEqual(row["target_value"], 5 * SOL)
        self.assertEqual(row["mint"], MINT)

    def test_redeclaring_the_same_name_is_a_no_op_not_a_second_campaign(self):
        """The bug this pins: `campaign_id` once hashed `created_at`, so two
        declarations seconds apart produced two campaigns with one goal.
        """
        first = self.declare(created_at=1_000)
        second = self.declare(created_at=9_999)
        self.assertEqual(first["campaign_id"], second["campaign_id"])
        self.assertEqual(len(self.evidence.campaigns(mint=MINT)), 1)

    def test_redeclaring_cannot_retarget_an_existing_campaign(self):
        """A campaign short of its goal must not be quietly made smaller."""
        self.declare(target_value=5 * SOL)
        again = self.declare(target_value=1 * SOL)
        self.assertEqual(again["target_value"], 5 * SOL)

    def test_declaring_writes_an_append_only_event(self):
        row = self.declare()
        events = self.evidence.campaign_events(row["campaign_id"])
        self.assertEqual([e["event"] for e in events], ["declared"])

    def test_a_target_of_zero_is_refused(self):
        with self.assertRaises(campaign.CampaignError):
            self.declare(target_value=0)

    def test_sol_typed_as_lamports_is_refused(self):
        """The bug this pins, and it produced a FALSE PUBLIC CLAIM.

        `--target 5` meaning five SOL stored five LAMPORTS. Six lamports of
        dust then satisfied it, and a campaign named "Burn 5 SOL" reported
        its target reached having burned a billionth of the goal. The figure
        behind that was properly gated and correctly computed -- the gate
        cannot catch a target that was already wrong on the way in.
        """
        with self.assertRaises(campaign.CampaignError) as caught:
            self.declare(target_value=5)
        message = str(caught.exception)
        self.assertIn("LAMPORTS", message)
        self.assertIn("5000000000", message, "the refusal must show what 5 SOL actually is")

    def test_the_refusal_names_both_readings_of_the_number(self):
        """A refusal that only says "too small" leaves the user guessing which
        unit they got wrong."""
        with self.assertRaises(campaign.CampaignError) as caught:
            self.declare(target_value=3)
        message = str(caught.exception)
        self.assertIn("0.000000003", message)
        self.assertIn("3000000000", message)

    def test_a_deliberate_dust_target_is_allowed_explicitly(self):
        row = self.declare(target_value=5, allow_dust_target=True)
        self.assertEqual(row["target_value"], 5)

    def test_a_token_target_is_not_held_to_the_sol_floor(self):
        """Raw token units are not lamports -- a small token target is
        ordinary, and refusing it would be the check misfiring."""
        row = self.declare(asset=campaign.ASSET_TOKEN, target_value=5,
                           trigger_type=campaign.TRIGGER_TOKEN_AMOUNT, trigger_value=5)
        self.assertEqual(row["target_value"], 5)

    def test_a_real_sol_target_passes_the_floor(self):
        row = self.declare(target_value=5 * SOL)
        self.assertEqual(row["target_value"], 5 * SOL)

    def test_the_floor_itself_is_allowed(self):
        row = self.declare(target_value=campaign.MINIMUM_SANE_SOL_TARGET)
        self.assertEqual(row["target_value"], campaign.MINIMUM_SANE_SOL_TARGET)

    def test_a_threshold_trigger_without_a_threshold_is_refused(self):
        with self.assertRaises(campaign.CampaignError):
            self.declare(trigger_type=campaign.TRIGGER_SOL_AMOUNT, trigger_value=None)

    def test_a_schedule_without_a_window_is_refused(self):
        with self.assertRaises(campaign.CampaignError):
            self.declare(trigger_type=campaign.TRIGGER_SCHEDULE, starts_at=None, ends_at=None)

    def test_a_window_that_ends_before_it_starts_is_refused(self):
        with self.assertRaises(campaign.CampaignError):
            self.declare(
                trigger_type=campaign.TRIGGER_SCHEDULE, starts_at=5_000, ends_at=1_000
            )


class TriggersItRefuses(StoreCase):
    """The wishlist is refused with a reason, not silently accepted.

    Each of these needs a data source this protocol does not have. A campaign
    gated on one would read UNCHECKED forever while wearing a word that
    implies it can progress.
    """

    def test_every_refused_trigger_names_why(self):
        for trigger, reason in campaign.REFUSED_TRIGGERS.items():
            with self.subTest(trigger=trigger):
                self.assertGreater(len(reason), 40, trigger)
                with self.assertRaises(campaign.CampaignError) as caught:
                    self.declare(trigger_type=trigger)
                self.assertIn(reason[:30], str(caught.exception))

    def test_a_refused_trigger_is_not_in_the_supported_set(self):
        for trigger in campaign.REFUSED_TRIGGERS:
            with self.subTest(trigger=trigger):
                self.assertNotIn(trigger, campaign.TRIGGERS)

    def test_the_store_refuses_a_trigger_outside_the_vocabulary(self):
        """The store enforces the contract structurally, not just the engine."""
        with self.assertRaises(ValueError):
            self.evidence.record_campaign(
                campaign_id="x", mint=MINT, name="n", trigger_type="invented",
                target_value=1, asset=campaign.ASSET_SOL, status=campaign.STATUS_ACTIVE,
            )


class ProgressGoesThroughTheGate(StoreCase):
    """The load-bearing property: a campaign is never a bypass."""

    def test_a_withheld_figure_yields_no_progress_value(self):
        row = self.declare()
        self.record_burn_inflow(3 * SOL, signature="sig-1", block_time=2_000)
        progress = campaign.progress_of(row, withheld_sol(), self.evidence)
        self.assertIsNone(progress.value)
        self.assertIsNone(progress.percent)
        self.assertFalse(progress.reached)

    def test_a_withheld_figure_names_the_check_that_stopped_it(self):
        row = self.declare()
        progress = campaign.progress_of(row, withheld_sol(), self.evidence)
        self.assertTrue(progress.withheld_by)
        self.assertIn("SOL_BURN_BALANCE", [name for name, _s, _d in progress.withheld_by])

    def test_a_withheld_campaign_never_reaches_however_much_was_burned(self):
        """"We cannot say" is not "it happened" -- the burns here exceed the
        target many times over and the campaign still must not report reached.
        """
        row = self.declare(target_value=1 * SOL)
        self.record_burn_inflow(500 * SOL, signature="sig-big", block_time=2_000)
        progress = campaign.progress_of(row, withheld_sol(), self.evidence)
        self.assertFalse(progress.reached)
        self.assertIsNone(progress.value)

    def test_a_publishable_figure_yields_progress_with_its_backing_check(self):
        row = self.declare()
        self.record_burn_inflow(2 * SOL, signature="sig-1", block_time=2_000)
        progress = campaign.progress_of(row, publishable_sol(), self.evidence)
        self.assertEqual(progress.value, 2 * SOL)
        self.assertEqual(progress.withheld_by, ())
        self.assertTrue(progress.backed_by)

    def test_progress_is_never_stored_on_the_campaign_row(self):
        """A stored progress column is a number that goes stale in a table
        that looks authoritative."""
        row = self.declare()
        self.assertNotIn("progress", row)
        self.assertNotIn("current_progress", row)


class TheCountingWindow(StoreCase):
    """A campaign counts from when it was declared, not from the coin's birth."""

    def test_burns_before_the_campaign_do_not_count_toward_it(self):
        self.record_burn_inflow(4 * SOL, signature="sig-old", block_time=500)
        row = self.declare(created_at=1_000)
        progress = campaign.progress_of(row, publishable_sol(), self.evidence)
        self.assertEqual(progress.value, 0)

    def test_burns_after_the_campaign_count(self):
        row = self.declare(created_at=1_000)
        self.record_burn_inflow(4 * SOL, signature="sig-new", block_time=2_000)
        progress = campaign.progress_of(row, publishable_sol(), self.evidence)
        self.assertEqual(progress.value, 4 * SOL)

    def test_a_scheduled_campaign_counts_from_its_window_not_its_declaration(self):
        row = self.declare(
            trigger_type=campaign.TRIGGER_SCHEDULE,
            trigger_value=None,
            created_at=1_000,
            starts_at=3_000,
            ends_at=9_000,
        )
        self.record_burn_inflow(1 * SOL, signature="sig-early", block_time=2_000)
        self.record_burn_inflow(2 * SOL, signature="sig-inside", block_time=4_000)
        progress = campaign.progress_of(row, publishable_sol(), self.evidence)
        self.assertEqual(progress.value, 2 * SOL)

    def test_a_coin_with_no_sol_burn_destination_has_made_no_progress(self):
        """Not "every burn on the chain" -- an empty destination set is zero."""
        row = self.declare()
        self.record_burn_inflow(9 * SOL, signature="sig-1", block_time=2_000)
        ops_only = Split(
            sol_burn=0, burn=0, paid=10_000,
            attributions=(Attribution(
                address="9opsWallet1111111111111111111111111111111",
                bps=10_000, leg="paid", reason="fixture", keyless=False),),
        )
        record = observation(
            checks=[passing_check("FIXTURE_ALL", invariants.FIGURES)],
            split=ops_only,
            evidence_block={"burn_total": 0},
        )
        self.assertEqual(campaign.progress_of(row, record, self.evidence).value, 0)


class Lifecycle(StoreCase):
    def test_meeting_the_target_moves_it_to_reached(self):
        row = self.declare(target_value=2 * SOL)
        self.record_burn_inflow(2 * SOL, signature="sig-1", block_time=2_000)
        progress = campaign.progress_of(row, publishable_sol(), self.evidence)
        status = campaign.advance(row, progress, self.evidence, now=3_000)
        self.assertEqual(status, campaign.STATUS_REACHED)
        self.assertEqual(self.evidence.campaign(row["campaign_id"])["status"], campaign.STATUS_REACHED)

    def test_reached_is_terminal_and_never_reverts(self):
        row = self.declare(target_value=2 * SOL)
        self.record_burn_inflow(2 * SOL, signature="sig-1", block_time=2_000)
        campaign.advance(
            row, campaign.progress_of(row, publishable_sol(), self.evidence),
            self.evidence, now=3_000,
        )
        reached = self.evidence.campaign(row["campaign_id"])
        # A later evaluation, with a progress that no longer meets the target.
        behind = campaign.Progress(
            campaign_id=row["campaign_id"], target=99 * SOL, asset=campaign.ASSET_SOL, value=0
        )
        self.assertEqual(
            campaign.advance(reached, behind, self.evidence, now=9_000),
            campaign.STATUS_REACHED,
        )

    def test_a_closed_window_closes_the_campaign(self):
        row = self.declare(
            trigger_type=campaign.TRIGGER_SCHEDULE, trigger_value=None,
            created_at=1_000, starts_at=1_000, ends_at=5_000, target_value=50 * SOL,
        )
        progress = campaign.progress_of(row, publishable_sol(), self.evidence)
        self.assertEqual(
            campaign.advance(row, progress, self.evidence, now=6_000), campaign.STATUS_CLOSED
        )

    def test_a_withheld_progress_advances_nothing(self):
        row = self.declare(target_value=1 * SOL)
        self.record_burn_inflow(500 * SOL, signature="sig-1", block_time=2_000)
        progress = campaign.progress_of(row, withheld_sol(), self.evidence)
        self.assertEqual(
            campaign.advance(row, progress, self.evidence, now=3_000), campaign.STATUS_ACTIVE
        )

    def test_every_status_change_leaves_an_append_only_event(self):
        row = self.declare(target_value=2 * SOL)
        self.record_burn_inflow(2 * SOL, signature="sig-1", block_time=2_000)
        campaign.advance(
            row, campaign.progress_of(row, publishable_sol(), self.evidence),
            self.evidence, now=3_000,
        )
        events = [e["event"] for e in self.evidence.campaign_events(row["campaign_id"])]
        self.assertEqual(events, ["declared", campaign.STATUS_REACHED])

    def test_evaluate_returns_every_campaign_for_the_coin(self):
        self.declare(name="One", target_value=1 * SOL)
        self.declare(name="Two", target_value=2 * SOL)
        results = campaign.evaluate(MINT, publishable_sol(), self.evidence, now=3_000)
        self.assertEqual(len(results), 2)


class Formatting(unittest.TestCase):
    def test_a_withheld_amount_never_renders_as_zero(self):
        self.assertEqual(campaign.format_amount(None, campaign.ASSET_SOL), "not published")

    def test_sol_renders_in_sol(self):
        self.assertEqual(campaign.format_amount(SOL, campaign.ASSET_SOL), "1.000000 SOL")


if __name__ == "__main__":
    unittest.main()

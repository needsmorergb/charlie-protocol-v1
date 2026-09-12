"""`/flywheel` -- that it reports the run and cannot overstate it.

The page is the one surface on this site that shows real signatures for
something that is NOT mainnet, and the failure mode is specific: a reader
concludes the protocol is running in production, or that a figure here says
something about volume or price. Both are guarded here rather than by
remembering.

The other half is arithmetic. The page prints a percentage per leg, and the
whole claim is that it lands on exactly 25.00 for the toll. A renderer that
quietly rounded, or that summed the legs to something other than the fee,
would still produce a plausible-looking table -- so the numbers are checked
against the recorded run rather than eyeballed.
"""

from __future__ import annotations

import html
import json
import re
import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from indexer import flywheel_page, legs  # noqa: E402


def text_of(page: str) -> str:
    """The page's visible words, entities resolved -- what a reader sees."""
    body = page[page.index("<body>") :]
    stripped = re.sub(r"<[^>]+>", " ", body)
    return html.unescape(re.sub(r"\s+", " ", stripped))


class FlywheelRecord(unittest.TestCase):
    def test_the_recorded_run_exists_and_is_a_devnet_run(self):
        record = flywheel_page.load()
        self.assertIsNotNone(
            record, f"{flywheel_page.RECORD} is missing -- run tools.flywheel_devnet"
        )
        self.assertEqual(record["cluster"], "devnet")

    def test_the_legs_sum_to_the_fee_that_arrived(self):
        """Nothing is lost and nothing is invented."""
        totals = flywheel_page.load()["totals"]
        paid = sum(
            totals[leg] for leg in ("toll", "sol_burn", "own_burn", "ops", "remainder")
        )
        self.assertEqual(paid, totals["fee"])

    def test_the_toll_is_a_quarter_of_the_fee(self):
        totals = flywheel_page.load()["totals"]
        self.assertEqual(
            flywheel_page.share_percent(totals["toll"], totals["fee"]), "25.00"
        )

    def test_the_stored_toll_matches_the_projects_constant(self):
        """`legs.TOLL_BPS` and the toll the deployed program stored are the
        same number. If they ever diverge, one of them is describing a
        protocol that is not this one."""
        record = flywheel_page.load()
        self.assertEqual(record["toll_bps_on_chain"], legs.TOLL_BPS)
        self.assertEqual(record["toll_bps"], legs.TOLL_BPS)

    def test_the_devs_shares_fill_exactly_what_the_toll_leaves(self):
        route = flywheel_page.load()["route_on_chain"]
        total = route["sol_burn_bps"] + route["own_burn_bps"] + route["ops_bps"]
        self.assertEqual(total, 10_000 - legs.TOLL_BPS)

    def test_every_round_landed_with_a_signature(self):
        for entry in flywheel_page.load()["rounds"]:
            self.assertTrue(entry["distribute_signature"])
            self.assertTrue(entry["credit_signature"])

    def test_the_programs_own_event_agrees_with_the_arithmetic(self):
        """Each round records both what the program said and what the driver
        computed. They are checked at run time; this holds the recorded
        result, so a doctored file fails here."""
        for entry in flywheel_page.load()["rounds"]:
            event, computed = entry["event"], entry["legs"]
            for leg in ("toll", "sol_burn", "own_burn", "ops", "remainder"):
                self.assertEqual(event[leg], computed[leg], f"{leg} in round {entry['round']}")

    def test_the_attacks_were_refused_and_the_legal_change_was_not(self):
        """Three refusals and one acceptance. The acceptance is the control:
        without it, the refusals would only show that the instruction rejects
        everything."""
        cases = flywheel_page.load()["refusals"]
        self.assertGreaterEqual(len(cases), 4)
        for case in cases:
            if case.get("must_be_accepted"):
                self.assertFalse(case["refused"], case["case"])
            else:
                self.assertTrue(case["refused"], case["case"])

    def test_each_refusal_cites_its_own_reason(self):
        """A refusal for the wrong reason proves nothing. The bps case once
        came back `AccountAlreadyInitialized` -- refused, but by a check that
        was not the one being tested -- so every refusal carries the
        program's own log line and no two say the same thing."""
        refused = [c for c in flywheel_page.load()["refusals"] if c["refused"]]
        reasons = [c["log"] for c in refused]
        self.assertTrue(all(reasons), "a refusal with no program log")
        self.assertEqual(len(set(reasons)), len(reasons), "two refusals share a reason")


class Permissionless(unittest.TestCase):
    """`distribute` takes no signer, and the run has to show that with a
    caller who had no reason to call."""

    def setUp(self):
        self.entry = flywheel_page.load().get("permissionless")
        if not self.entry:
            self.skipTest("the run did not include a stranger-cranked round")

    def test_the_caller_was_neither_admin_nor_ops(self):
        self.assertFalse(self.entry["is_admin"])
        self.assertFalse(self.entry["is_ops"])

    def test_the_toll_moved_anyway(self):
        self.assertEqual(self.entry["charlie_pool_gained"], self.entry["event"]["toll"])
        self.assertGreater(self.entry["event"]["toll"], 0)

    def test_the_caller_was_paid_nothing_and_is_out_of_pocket(self):
        """A crank that pays its caller a tip is an extractable bounty: the
        legs would fund their own collection. The caller must end DOWN."""
        self.assertLess(self.entry["cranker_lamports_delta"], 0)


class FlywheelPage(unittest.TestCase):
    def setUp(self):
        self.page = flywheel_page.render(now=0)
        self.text = text_of(self.page)

    def test_it_says_devnet_and_says_a_step_is_mocked_before_any_figure(self):
        """The disclosure is not a footnote. It appears before the first
        table, because a reader who stops reading early must still have been
        told."""
        lowered = self.text.lower()
        self.assertIn("devnet", lowered)
        self.assertIn("mocked", lowered)
        disclosure = lowered.index("mocked")
        first_figure = lowered.index("where it went")
        self.assertLess(disclosure, first_figure)

    def test_it_names_what_is_mocked_specifically(self):
        """"Mocked" alone is not a disclosure; which step matters."""
        self.assertIn("pump does not exist on devnet", self.text)

    def test_it_carries_no_price_and_no_dollar_figure(self):
        """The rule `/dilution` runs under, for the same reason: a dollar
        figure attached to a fee-split argument reads as a projection of what
        a coin would earn, whatever the caption says."""
        self.assertNotIn("$", self.text.replace("$CHARLIE", ""))
        for word in ("USD", "market cap", "projected", "estimated"):
            self.assertNotIn(word.lower(), self.text.lower())

    def test_it_does_not_claim_mainnet_or_enrollment(self):
        lowered = self.text.lower()
        self.assertIn("it is not mainnet", lowered)
        self.assertNotIn("mainnet deployment", lowered)

    def test_it_says_the_program_is_still_upgradeable(self):
        """The absence-of-code guarantee is not yet a guarantee, and the page
        that shows the absences has to be the page that says so."""
        self.assertIn("upgradeable", self.text.lower())

    def test_the_percentages_on_the_page_are_the_recorded_ones(self):
        totals = flywheel_page.load()["totals"]
        for leg in ("toll", "sol_burn", "own_burn", "ops"):
            expected = flywheel_page.share_percent(totals[leg], totals["fee"])
            self.assertIn(f"{expected}%", self.text)

    def test_every_signature_in_the_record_is_linked(self):
        record = flywheel_page.load()
        for entry in record["rounds"]:
            self.assertIn(entry["distribute_signature"], self.page)

    def test_explorer_links_are_pinned_to_devnet(self):
        """A link that drops `cluster=devnet` resolves to mainnet, where
        none of these accounts exist -- so the page would send a reader to an
        empty address and look broken, or worse, look like a coincidence."""
        for match in re.finditer(r'href="(https://explorer\.solana\.com[^"]*)"', self.page):
            self.assertIn("cluster=devnet", match.group(1))

    def test_the_permissionless_round_is_on_the_page_when_it_ran(self):
        entry = flywheel_page.load().get("permissionless")
        if not entry:
            self.skipTest("the run did not include a stranger-cranked round")
        self.assertIn("takes no signer", self.text)
        self.assertIn(entry["distribute_signature"], self.page)
        # The claim that matters most on that section: the caller got nothing.
        self.assertIn("nothing", self.text)

    def test_the_program_id_on_the_page_is_the_one_that_ran(self):
        record = flywheel_page.load()
        self.assertIn(record["program_id"], self.page)

    def test_it_renders_nothing_rather_than_a_placeholder_without_a_run(self):
        """An unrun proof has nothing to show, and a page that says "coming
        soon" beside a real-looking table is the failure this avoids.

        `load()` answers None for a path that does not exist, and `render`
        refuses that rather than filling in zeros -- a table of zeros is a
        claim that a distribution moved nothing, which is not the same
        statement as "this has not been run".
        """
        self.assertIsNone(flywheel_page.load(ROOT / "state" / "flywheel" / "absent.json"))
        with unittest.mock.patch.object(flywheel_page, "load", return_value=None):
            with self.assertRaises(FileNotFoundError):
                flywheel_page.render(now=0)


if __name__ == "__main__":
    unittest.main()

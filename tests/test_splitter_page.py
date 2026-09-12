"""`/charlie-flywheel` -- that it reports the runs and cannot overstate them.

This page is the easiest one on the site to make dishonest, because it puts
simulated trading next to real transactions. Three ways that could go wrong,
all guarded here rather than by remembering:

1. A reader concludes $CHARLIE's fees really are split this way. They are
   not and cannot be -- its config is `admin_revoked`.
2. A reader concludes tokens were burned. None were: the buyback leg is
   funded and never spent.
3. A reader takes the simulated figures for measured ones. The page prints
   both and labels each.
"""

from __future__ import annotations

import html
import re
import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from indexer import splitter_page  # noqa: E402


def text_of(page: str) -> str:
    body = page[page.index("<body>") :]
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)))


class Records(unittest.TestCase):
    def test_the_splitter_run_exists_and_is_devnet(self):
        data = splitter_page.load_all()
        self.assertIsNotNone(data, "run tools.splitter_devnet first")
        self.assertEqual(data["splitter"]["cluster"], "devnet")

    def test_the_split_is_fifty_twentyfive_twentyfive(self):
        totals = splitter_page.load_all()["splitter"]["totals"]
        split = sum(totals[k] for k in ("sol_burn", "buyback", "ops", "remainder"))
        self.assertEqual(splitter_page.share_percent(totals["sol_burn"], split), "50.00")
        self.assertEqual(splitter_page.share_percent(totals["buyback"], split), "25.00")
        self.assertEqual(splitter_page.share_percent(totals["ops"], split), "25.00")

    def test_no_split_ever_pays_out_more_than_it_divided(self):
        """Per round, `l` in equals legs plus remainder out.

        NOT "the total paid is at most the fee sent this session", which was
        the first version of this test and was wrong: a round can legally
        split MORE than the fee that just arrived, because the vault still
        holds shares an earlier round deferred. The run on disk does exactly
        that -- round 1 divided 1,098,829 against an 800,000 fee, picking up
        298,829 of ops shares left waiting when the ops wallet was below its
        rent minimum. That carry-over is the remainder working as designed,
        so the invariant is per-round conservation, not a session total.
        """
        for entry in splitter_page.load_all()["splitter"]["rounds"]:
            with self.subTest(round=entry["round"]):
                legs = entry["legs"]
                out = sum(legs[k] for k in ("sol_burn", "buyback", "ops", "remainder"))
                self.assertEqual(out, entry["distributable"])

    def test_every_addressed_pda_derives_from_charlies_real_mint(self):
        splitter = splitter_page.load_all()["splitter"]
        self.assertTrue(splitter["mint_is_charlie"])
        self.assertEqual(splitter["config_on_chain"]["mint"], splitter["mint"])

    def test_the_attacks_were_refused(self):
        for case in splitter_page.load_all()["splitter"]["refusals"]:
            self.assertTrue(case["refused"], case["case"])

    def test_each_market_run_split_in_the_right_ratios(self):
        """The ratios must hold in every scenario, not only the rally."""
        for name, record in splitter_page.load_all()["markets"]:
            with self.subTest(scenario=name):
                legs, total = record["legs_real"], record["split_total_lamports"]
                self.assertEqual(splitter_page.share_percent(legs["sol_burn"], total), "50.00")
                self.assertEqual(splitter_page.share_percent(legs["buyback"], total), "25.00")
                self.assertEqual(splitter_page.share_percent(legs["ops"], total), "25.00")

    def test_a_falling_market_still_funded_every_leg(self):
        """pump charges the creator fee on sells too. If the dump run ever
        stops funding the legs, the mechanism being described has changed."""
        runs = dict(splitter_page.load_all()["markets"])
        if "dump" not in runs:
            self.skipTest("the dump scenario was not run")
        legs = runs["dump"]["legs_real"]
        for leg in ("sol_burn", "buyback", "ops"):
            self.assertGreater(legs[leg], 0, leg)

    def test_the_market_runs_are_labelled_simulated(self):
        for _name, record in splitter_page.load_all()["markets"]:
            self.assertTrue(record["market_is_simulated"])
            self.assertTrue(record["splitter_is_real"])


class Page(unittest.TestCase):
    def setUp(self):
        self.page = splitter_page.render(now=0)
        self.text = text_of(self.page)

    def test_it_says_charlie_cannot_do_this_before_any_figure(self):
        lowered = self.text.lower()
        self.assertIn("cannot actually do this", lowered)
        self.assertIn("admin_revoked", lowered)
        disclosure = lowered.index("cannot actually do this")
        first_figure = lowered.index("the split, as it landed")
        self.assertLess(disclosure, first_figure)

    def test_it_says_the_trading_is_simulated_and_the_split_is_real(self):
        self.assertIn("simulated", self.text.lower())
        self.assertIn("the split of those fees is real", self.text)

    def test_it_never_implies_a_token_was_burned(self):
        """The buyback leg is funded and unspent. The page must say so and
        must not claim a burn."""
        lowered = self.text.lower()
        self.assertIn("not yet spent", lowered)
        self.assertIn("not proven", lowered)
        for claim in ("tokens were burned", "tokens burned", "supply destroyed"):
            self.assertNotIn(claim, lowered)

    def test_it_carries_no_price_or_dollar_figure(self):
        self.assertNotIn("$", self.text.replace("$CHARLIE", ""))
        for word in ("usd", "market cap of", "projected", "forecast"):
            self.assertNotIn(word, self.text.lower())

    def test_it_says_the_vault_has_no_key(self):
        """The load-bearing property: off the curve, so the split is the
        only exit rather than the preferred one."""
        self.assertIn("off the ed25519 curve", self.text)
        self.assertIn("no private key can exist", self.text)

    def test_it_says_the_program_is_still_upgradeable(self):
        self.assertIn("upgradeable", self.text.lower())

    def test_it_does_not_confuse_itself_with_the_protocol_page(self):
        """`/flywheel` is a 25% toll. This is a 50/25/25 with none. A reader
        landing here must be able to tell them apart, and the link out has
        to name the other one."""
        self.assertIn("the protocol's own flywheel", self.text)
        self.assertNotIn("toll", self.text.lower())

    def test_every_landed_signature_is_linked(self):
        data = splitter_page.load_all()
        for _name, record in data["markets"]:
            for entry in record.get("settlements", []):
                self.assertIn(entry["split_signature"], self.page)

    def test_explorer_links_are_pinned_to_devnet(self):
        for match in re.finditer(r'href="(https://explorer\.solana\.com[^"]*)"', self.page):
            self.assertIn("cluster=devnet", match.group(1))

    def test_both_the_simulated_and_the_sent_figure_are_shown(self):
        """They differ by a scale factor. Printing only one would let the
        smaller read as the larger, or the larger as measured."""
        self.assertIn("simulated fee", self.text.lower())
        self.assertIn("sent", self.text.lower())

    def test_it_renders_nothing_rather_than_a_placeholder_without_a_run(self):
        with unittest.mock.patch.object(splitter_page, "load_all", return_value=None):
            with self.assertRaises(FileNotFoundError):
                splitter_page.render(now=0)


if __name__ == "__main__":
    unittest.main()

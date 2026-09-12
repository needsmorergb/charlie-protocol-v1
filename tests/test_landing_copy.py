"""The landing page's claim about what a dev can do today.

This page told visitors "no coin can enroll today" while its own hero linked
to a live enroller that builds, simulates and returns a real transaction. The
sentence outlived the thing it described, which is the failure this whole
project is built to make impossible, so the replacement is pinned by a test
rather than left to be noticed again.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import invariants, site  # noqa: E402


def _occurrences(haystack: str, needle: str):
    """Every index of `needle` in `haystack`."""
    start = 0
    while True:
        idx = haystack.find(needle, start)
        if idx == -1:
            return
        yield idx
        start = idx + 1


class TestLandingCopyMatchesReality(unittest.TestCase):
    def setUp(self):
        self.rendered = site._landing_soon()

    def test_it_no_longer_says_no_coin_can_enroll(self):
        self.assertNotIn("no coin can enroll", self.rendered)

    def test_it_says_enrolment_is_open_and_what_enrolled_means(self):
        # Enrollment is open, and enrolled means one thing: the coin's pump
        # config pays the protocol's wallet its share, which pump enforces.
        self.assertIn("Enrollment is open at /enroll", self.rendered)
        self.assertIn(f"{site.TOLL_BPS // 100}% of that fee", self.rendered)
        self.assertIn("pump enforces the config", self.rendered)
        self.assertIn("read from its config on the chain", self.rendered)

    def test_the_advertised_trade_figure_never_appears_without_its_fee(self):
        """The rate is quoted as a share of the trade because that is how the
        space quotes one, and that figure is only true against a creator fee
        pump sets and changes. Quoting it bare would be the one misleading
        way to say it, so the fee it is a share of is on the page with it."""
        self.assertIn(f"{site.TOLL_HEADLINE_PERCENT}% of every trade", self.rendered)
        self.assertIn("pump pays the coin&#x27;s creator a fee out of each one",
                      self.rendered)
        self.assertIn("bps of the trade at the tier a coin lands on when it graduates",
                      self.rendered)

    def test_it_does_not_claim_the_spending_is_on_chain(self):
        # The share is collected from pump and spent buying and burning
        # $CHARLIE; the chain shows the collecting, not the spending.
        self.assertIn("the chain shows the collecting, not the spending", self.rendered)

    def test_it_says_which_half_is_not(self):
        """The program is written and on devnet; no mainnet program exists.

        This assertion used to pin the phrase "does not exist yet", which
        went stale the day the program was written -- the same way "no coin
        can enroll" went stale, and for the same reason. So it pins the
        property instead: the page must say the deployment is devnet, and
        must say no mainnet program is deployed. Both halves matter. "Devnet"
        alone reads as deployed to someone skimming, and a reader holding the
        token is the one who pays for that reading.
        """
        self.assertIn("deployed to DEVNET, not to mainnet", self.rendered)
        self.assertIn("No mainnet program is deployed", self.rendered)
        # And it must never say the flat "deployed" without a cluster beside
        # it. Checked by requiring every occurrence of the word to sit within
        # a window that names one.
        for idx in _occurrences(self.rendered, "deployed"):
            window = self.rendered[max(0, idx - 90):idx + 90]
            self.assertTrue(
                "DEVNET" in window or "devnet" in window or "mainnet" in window,
                f"bare 'deployed' with no cluster named: ...{window}...",
            )

    def test_charlie_is_explained_as_a_spent_change_not_a_choice(self):
        # `revoke_fee_sharing_authority` answers 6023 DeprecatedInstruction for
        # every caller, so admin_revoked can now only mean the one permitted
        # update was used. Describing it as a deliberate revocation would be
        # a claim the chain no longer supports.
        self.assertIn("admin_revoked", self.rendered)
        self.assertIn("already used the single change", self.rendered)

    def test_it_still_names_no_figure(self):
        # The landing block is covered by the no-figure-names rule, and the
        # replacement copy has to keep obeying it.
        for name in invariants.FIGURES:
            self.assertNotIn(name, self.rendered, f"figure name {name!r} leaked into the landing copy")


if __name__ == "__main__":
    unittest.main()

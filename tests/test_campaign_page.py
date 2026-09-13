"""`/campaigns/<mint>` -- that a declared goal never reads as a measured one.

`python -m unittest discover -s tests -t tests -p "test_campaign_page.py"`.

This page puts a claim next to a check, which is the arrangement most likely
to mislead on the whole site. Three ways it could go wrong, guarded here
rather than by remembering:

1. **A withheld figure renders as zero.** A 0% bar reads as "nothing was
   burned"; the truth is "this protocol cannot say". The page must draw no
   bar at all and name the blocking check.
2. **A goal reads as a measurement.** The target is something somebody typed.
   It must never appear with the vocabulary the checked figures use.
3. **A projection appears.** No price, no dollar figure, no language implying
   a shortfall is temporary -- the rule /dilution and /flywheel run under.
"""

from __future__ import annotations

import html
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from indexer import campaign, campaign_page, invariants  # noqa: E402
from indexer.evidence import Evidence  # noqa: E402
from indexer.legs import Attribution, Split  # noqa: E402
from indexer.observe import Observation  # noqa: E402

MINT = "8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump"
SOL_BURN = "burn111111111111111111111111111111111111111"
SOL = campaign.LAMPORTS_PER_SOL


def text_of(page: str) -> str:
    body = page[page.index("<body>") :]
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)))


def sol_burn_split() -> Split:
    attribution = Attribution(
        address=SOL_BURN, bps=10_000, leg="sol_burn", reason="fixture", keyless=True
    )
    return Split(sol_burn=10_000, burn=0, paid=0, attributions=(attribution,))


def check(name: str, status: str, backs) -> invariants.Check:
    return invariants.Check(name, status, tuple(backs), "eq", "fixture detail")


def observation(*, checks) -> Observation:
    record = Observation(
        mint=MINT,
        observed_at=1.0,
        split=sol_burn_split(),
        checks=tuple(checks),
        evidence={SOL_BURN: 0, "burn_total": 0},
    )
    record.verdict = invariants.apply_silence_rule(record.checks)
    return record


def publishable() -> Observation:
    return observation(checks=[check("FIXTURE_ALL", invariants.PASS, invariants.FIGURES)])


def withheld() -> Observation:
    return observation(
        checks=[
            check("FIXTURE_REST", invariants.PASS,
                  (invariants.SPLIT, invariants.SUPPLY_DESTROYED)),
            check("SOL_BURN_BALANCE", invariants.UNCHECKED, (invariants.SOL_BURN_TOTAL,)),
        ]
    )


class PageCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.evidence = Evidence(Path(self.dir.name) / "evidence.db")
        self.addCleanup(self.evidence.close)

    def declare(self, **overrides):
        fields = dict(
            mint=MINT, name="Burn 5 SOL", trigger_type=campaign.TRIGGER_SOL_AMOUNT,
            trigger_value=5 * SOL, target_value=5 * SOL, asset=campaign.ASSET_SOL,
            created_at=1_000,
        )
        fields.update(overrides)
        return campaign.declare(self.evidence, **fields)

    def burn(self, lamports, *, signature, block_time=2_000):
        self.evidence.record_inflow(
            signature=signature, destination=SOL_BURN, mint=MINT, leg="sol_burn",
            lamports=lamports, block_time=block_time, slot=1,
        )

    def page(self, record, *, now=1_700_000_000):
        results = campaign.evaluate(MINT, record, self.evidence, now=now)
        return campaign_page.render(MINT, results, now=now)


class AWithheldFigureIsNotZero(PageCase):
    def test_no_bar_is_drawn_when_the_figure_is_withheld(self):
        self.declare()
        self.burn(3 * SOL, signature="sig-1")
        page = self.page(withheld())
        self.assertNotIn('class="bar"', page)

    def test_the_blocking_check_is_named_on_the_page(self):
        self.declare()
        page = self.page(withheld())
        self.assertIn("SOL_BURN_BALANCE", text_of(page))

    def test_it_says_no_figure_is_published_rather_than_showing_one(self):
        self.declare()
        self.burn(3 * SOL, signature="sig-1")
        words = text_of(self.page(withheld()))
        self.assertIn("No progress figure is published", words)
        self.assertIn("an absence of evidence is not a total", words)

    def test_a_withheld_campaign_never_shows_a_zero_percent_bar(self):
        """The specific misreading this page exists to prevent."""
        self.declare()
        page = self.page(withheld())
        self.assertNotIn("width:0.0%", page)
        self.assertNotIn("0.0%", page)

    def test_the_burned_amount_is_absent_even_though_it_is_in_the_store(self):
        """The store holds 3 SOL of burns; the check has not passed, so the
        page must not print it anywhere."""
        self.declare()
        self.burn(3 * SOL, signature="sig-1")
        self.assertNotIn("3.000000", text_of(self.page(withheld())))


class APublishableFigureIsShownWithItsCheck(PageCase):
    def test_the_bar_is_drawn(self):
        self.declare()
        self.burn(1 * SOL, signature="sig-1")
        self.assertIn('class="bar"', self.page(publishable()))

    def test_the_recorded_total_and_its_backing_check_both_appear(self):
        self.declare()
        self.burn(1 * SOL, signature="sig-1")
        words = text_of(self.page(publishable()))
        self.assertIn("1.000000 SOL", words)
        self.assertIn("backed by", words)

    def test_the_bar_never_exceeds_one_hundred_percent(self):
        self.declare(target_value=1 * SOL)
        self.burn(50 * SOL, signature="sig-big")
        page = self.page(publishable())
        widths = [float(w) for w in re.findall(r"width:([\d.]+)%", page)]
        self.assertTrue(widths)
        for width in widths:
            self.assertLessEqual(width, 100.0)


class TheGoalIsNeverAMeasurement(PageCase):
    def test_the_target_is_labelled_as_declared(self):
        self.declare()
        words = text_of(self.page(publishable()))
        self.assertIn("as declared", words)
        self.assertIn("not a measurement", words)

    def test_a_coin_with_no_campaigns_claims_nothing(self):
        words = text_of(self.page(publishable()))
        self.assertIn("declared no burn campaigns", words)

    def test_the_status_word_is_a_readers_word(self):
        self.declare(target_value=1 * SOL)
        self.burn(2 * SOL, signature="sig-1")
        self.assertIn("target reached", text_of(self.page(publishable())))


class WhatThePageMustNeverCarry(PageCase):
    """/dilution and /flywheel's rule, applied here."""

    FORBIDDEN = ("$", "USD", "market cap", "price target", "moon", "guaranteed")

    def test_no_price_or_dollar_figure_appears(self):
        self.declare()
        self.burn(2 * SOL, signature="sig-1")
        words = text_of(self.page(publishable()))
        for token in self.FORBIDDEN:
            with self.subTest(token=token):
                self.assertNotIn(token, words)

    def test_nothing_promises_the_target_will_be_met(self):
        self.declare()
        words = text_of(self.page(publishable())).lower()
        for token in ("will reach", "on track", "expected to"):
            with self.subTest(token=token):
                self.assertNotIn(token, words)

    def test_it_states_that_nothing_here_is_a_projection(self):
        self.declare()
        words = text_of(self.page(publishable()))
        self.assertIn("projection", words)


class TheArtifact(PageCase):
    def test_write_puts_the_page_under_campaigns(self):
        self.declare()
        results = campaign.evaluate(MINT, publishable(), self.evidence, now=1)
        out = Path(self.dir.name) / "web"
        path = campaign_page.write(MINT, results, out, now=1)
        self.assertTrue(path.exists())
        self.assertEqual(path.parent.name, campaign_page.CAMPAIGNS_DIRNAME)
        self.assertIn(MINT, path.name)

    def test_the_page_is_a_complete_document(self):
        self.declare()
        page = self.page(publishable())
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertIn("<title>", page)

    def test_the_mint_is_escaped_into_the_page(self):
        self.declare()
        self.assertIn(MINT, self.page(publishable()))


if __name__ == "__main__":
    unittest.main()

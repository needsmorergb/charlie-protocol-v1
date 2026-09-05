"""Offline tests for `indexer/site.py` -- the WEB-02/WEB-03 assertions the
generic `TestSilenceRuleSweep` (`tests/test_publication.py`) does not reach:
fixed figure-row order, the check-beside-figure contract's withheld path
naming every blocking check (not just the first), the three-state status
CSS classes, and HTML escaping.

`python -m unittest discover -s tests -t tests -p "test_site.py"` or
`python -m unittest tests.test_site` (both forms must work -- see the two
`sys.path.insert` calls below).

No network. Fixtures are reused from `tests/test_publication.py` (the same
sentinel constants and `build_observation()`) so a sentinel means the same
thing in both files.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import inspect
import re

from indexer import invariants, publish, pump, site
from indexer import legs as site_legs

ROOT = Path(__file__).resolve().parents[1]
from indexer.evidence import Evidence
from indexer.legs import Registry, split_of
from indexer.observe import Observation, observe

from test_indexer import CHARLIE, charlie_rpc  # noqa: E402
from test_publication import (  # noqa: E402
    SPENDABLE,
    build_all_blocked_sentinel_observation,
    build_all_publishable_sentinel_observation,
    build_observation,
    evidence_db,
    mint_state,
)
from test_publication import FULL_DETAIL_SURFACES  # noqa: E402

# The page route no longer points at a file. It points at the live function,
# which serves the committed page when one exists and observes the chain when
# it does not -- so a coin nobody has pre-generated still gets an answer.
VERCEL_JSON = Path(__file__).resolve().parents[1] / "vercel.json"
LIVE_ROUTE = "/api/verify"
LIVE_ROUTE_SUFFIX = "?mint=:mint"
JSON_ROUTE_SUFFIX = "?mint=:mint&format=json"


class TestFigureRowOrder(unittest.TestCase):
    def test_figure_rows_render_in_invariants_figures_order(self):
        observation = build_observation()
        rendered = site.render(observation)
        positions = [rendered.index(f'data-figure="{name}"') for name in invariants.FIGURES]
        self.assertEqual(positions, sorted(positions), "figure rows are out of invariants.FIGURES order")


class TestWithheldFigureRow(unittest.TestCase):
    def test_withheld_figure_row_renders_the_word_withheld_and_no_value(self):
        distinctive_balance = 87_654_321
        observation = build_observation(sol_burn_balance=distinctive_balance)
        rendered = site.render(observation)

        self.assertNotIn(invariants.SOL_BURN_TOTAL, observation.verdict.publishable)
        start = rendered.index('data-figure="sol_burn_total"')
        end = rendered.index("</div>", start)
        row = rendered[start:end]
        self.assertIn("withheld", row)
        self.assertNotIn(str(distinctive_balance), rendered)

    def test_withheld_figure_names_every_blocking_check_not_just_the_first(self):
        # SPENDABLE, because the check that blocks this figure only fails on a
        # destination SOL can come back from. A burn address passes it.
        observation = build_observation(sol_burn_address=SPENDABLE)
        reasons = observation.verdict.blocked[invariants.SOL_BURN_TOTAL]
        blocking_names = [name for name, _status, _detail in reasons]
        self.assertGreaterEqual(len(blocking_names), 2, "fixture must block sol_burn_total with 2+ checks")

        rendered = site.render(observation)
        start = rendered.index('data-figure="sol_burn_total"')
        end = rendered.index("</div>", start)
        row = rendered[start:end]
        for name in blocking_names:
            self.assertIn(name, row, f"blocking check {name!r} missing from the withheld sol_burn_total row")


class TestPublishableFigureRow(unittest.TestCase):
    def test_publishable_figure_row_names_its_backing_check(self):
        observation = build_observation()
        rendered = site.render(observation)
        publisher = publish.Publisher(observation)
        for name in invariants.FIGURES:
            if not publisher.verdict.may_publish(name):
                continue
            _value, backs = publisher.figure(name)
            self.assertTrue(backs, f"{name} publishable with no backing check names")
            start = rendered.index(f'data-figure="{name}"')
            end = rendered.index("</div>", start)
            row = rendered[start:end]
            for check_name in backs:
                self.assertIn(check_name, row, f"backing check {check_name!r} missing from {name}'s row")


class TestStatusClasses(unittest.TestCase):
    def test_fail_status_uses_a_fail_css_class_distinct_from_pass_and_unchecked(self):
        classes = {
            site._status_class(invariants.PASS),
            site._status_class(invariants.FAIL),
            site._status_class(invariants.UNCHECKED),
        }
        self.assertEqual(len(classes), 3, "the three statuses must map to three distinct CSS classes")
        for css_class in classes:
            self.assertIn(css_class, site._STYLE, f"{css_class!r} has no rule in _STYLE")

    def test_unrecognised_status_never_renders_the_pass_class(self):
        pass_class = site._status_class(invariants.PASS)
        self.assertEqual(site._status_class(""), site._status_class(None))
        self.assertEqual(site._status_class(None), site._status_class("WAT"))
        self.assertNotEqual(site._status_class("WAT"), pass_class)
        self.assertNotEqual(site._status_class(""), pass_class)
        self.assertNotEqual(site._status_class(None), pass_class)


class TestEscaping(unittest.TestCase):
    def test_detail_string_with_markup_is_escaped(self):
        # Task 1's render() carries only the header and the figures section
        # (the checks list -- where `check.detail` itself is shown -- lands
        # in 02-02); the interpolation site this task's render() actually
        # exercises for a check-derived string is the backing/blocking check
        # *name* in a figure row's "backed by" / "withheld by" text, which
        # goes through the exact same `esc()` call every other interpolated
        # value does. A markup-bearing check name exercises that same code
        # path a markup-bearing `detail` string would once 02-02 renders it.
        malicious_name = "<b>SOL_BURN_UNSPENDABLE</b> & friends"
        observation = build_observation()
        malicious_check = invariants.Check(
            name=malicious_name,
            status=invariants.FAIL,
            backs=(invariants.SOL_BURN_TOTAL,),
            equation="n/a",
            detail="n/a",
        )
        observation.checks = observation.checks + (malicious_check,)
        observation.verdict = invariants.apply_silence_rule(observation.checks)

        rendered = site.render(observation)
        self.assertNotIn(malicious_name, rendered, "raw markup leaked into the rendered page unescaped")
        self.assertIn("&lt;b&gt;SOL_BURN_UNSPENDABLE&lt;/b&gt; &amp; friends", rendered)


class TestBurnEvents(unittest.TestCase):
    """02-02 Task 1: `Observation.burn_events` -- the raw `evidence.burns_for(mint)`
    rows, carried as a plain non-figure field (never a member of
    `invariants.FIGURES`, never routed through `Publisher.figure()`).
    """

    def test_default_burn_events_is_an_empty_list_not_shared_between_instances(self):
        first = Observation(mint="m", observed_at=1.0)
        second = Observation(mint="n", observed_at=1.0)
        self.assertEqual(first.burn_events, [])
        self.assertIsNot(first.burn_events, second.burn_events)

    def test_burn_events_is_never_a_figure(self):
        self.assertNotIn("burn_events", invariants.FIGURES)

    def test_observe_without_evidence_leaves_burn_events_empty(self):
        record = observe(charlie_rpc(), CHARLIE)
        self.assertEqual(record.burn_events, [])

    def test_observe_with_evidence_populates_burn_events_from_burns_for(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            evidence.record_burn_event(
                signature="sig-boost-1", mint=CHARLIE, instruction_index=0,
                tokens_burned=1_500_000_000, source="boost_buy_and_burn", slot=1, block_time=100,
            )
            evidence.record_burn_event(
                signature="sig-boost-2", mint=CHARLIE, instruction_index=0,
                tokens_burned=2_500_000_000, source="boost_buy_and_burn", slot=2, block_time=200,
            )
            record = observe(charlie_rpc(), CHARLIE, evidence=evidence)
            expected_rows = evidence.burns_for(CHARLIE)
            evidence.close()

        self.assertEqual(len(record.burn_events), len(expected_rows))
        self.assertEqual(len(record.burn_events), 2)
        self.assertEqual(record.burn_events[0]["signature"], expected_rows[0]["signature"])

    def test_burns_for_is_called_exactly_once_in_observe_py(self):
        import ast

        source = Path(__file__).resolve().parents[1].joinpath("indexer", "observe.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = sum(
            1 for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "burns_for"
        )
        self.assertEqual(calls, 1, "burns_for() must be called exactly once and its result reused")


class TestFreshness(unittest.TestCase):
    """02-02 Task 2, D-18: the header's freshness block -- the observed-at
    stamp, the age computed at generation time, and the snapshot sentence.
    """

    def test_age_moves_with_generation_time_while_the_stamp_does_not(self):
        observation = Observation(mint="m", observed_at=1_000_000.0)
        rendered_soon = site.render(observation, now=1_000_000.0 + 3 * 86_400)
        rendered_later = site.render(observation, now=1_000_000.0 + 40 * 86_400)
        stamp = site._stamp(observation.observed_at)
        self.assertIn(stamp, rendered_soon)
        self.assertIn(stamp, rendered_later)
        self.assertNotEqual(rendered_soon, rendered_later)

    def test_freshness_is_a_pure_function_of_its_arguments(self):
        observation = Observation(mint="m", observed_at=1_000_000.0)
        first = site._freshness(observation, now=1_000_000.0 + 7_200)
        second = site._freshness(observation, now=1_000_000.0 + 7_200)
        self.assertEqual(first, second)

    def test_age_clamps_at_zero_never_a_negative_duration(self):
        self.assertNotIn("-", site._age(-500))
        self.assertNotIn("-", site._age(0))

    def test_snapshot_note_present_and_read_off_the_module(self):
        observation = Observation(mint="m", observed_at=1_000_000.0)
        rendered = site.render(observation, now=1_000_100.0)
        self.assertIn(site._SNAPSHOT_NOTE, rendered)

    def test_stamp_survives_the_page_level_error_branch(self):
        observation = Observation(mint="m", observed_at=1_000_000.0, error="RPC unavailable")
        rendered = site.render(observation, now=1_000_100.0)
        self.assertIn(site._stamp(observation.observed_at), rendered)
        self.assertIn("RPC unavailable", rendered)


class TestSolBurnFailureBanner(unittest.TestCase):
    """02-02 Task 2, PUB-03: the SOL burn Failure Banner -- unconditional on
    SOL_BURN_UNSPENDABLE: FAIL, above every other section, carrying the check's
    own `detail` verbatim, and naming no SOL burn total anywhere on the page.
    """

    def test_banner_present_with_the_check_detail_verbatim_for_a_fail_observation(self):
        observation = build_observation(sol_burn_address=SPENDABLE)
        check = next(c for c in observation.checks if c.name == "SOL_BURN_UNSPENDABLE")
        self.assertEqual(check.status, invariants.FAIL)

        rendered = site.render(observation, now=2.0)
        self.assertIn('data-banner="sol-burn-failure"', rendered)
        self.assertIn(check.detail, rendered)

    def test_banner_absent_for_a_pass_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            observation = build_all_publishable_sentinel_observation(evidence)
            evidence.close()

        check = next(c for c in observation.checks if c.name == "SOL_BURN_UNSPENDABLE")
        self.assertEqual(check.status, invariants.PASS)

        rendered = site.render(observation, now=2.0)
        self.assertNotIn('data-banner="sol-burn-failure"', rendered)

    def test_no_sol_burn_lamports_value_appears_anywhere_for_a_fail_observation(self):
        distinctive_balance = 91_234_567
        observation = build_observation(sol_burn_balance=distinctive_balance,
                                        sol_burn_address=SPENDABLE)
        rendered = site.render(observation, now=2.0)
        self.assertNotIn(str(distinctive_balance), rendered)

    def test_banner_renders_above_the_figures_section(self):
        observation = build_observation(sol_burn_address=SPENDABLE)
        rendered = site.render(observation, now=2.0)
        banner_pos = rendered.index('data-banner="sol-burn-failure"')
        figures_pos = rendered.index('id="figures"')
        self.assertLess(banner_pos, figures_pos)

    def test_freshness_renders_above_the_banner(self):
        observation = build_observation(sol_burn_address=SPENDABLE)
        rendered = site.render(observation, now=2.0)
        freshness_pos = rendered.index('class="freshness"')
        banner_pos = rendered.index('data-banner="sol-burn-failure"')
        self.assertLess(freshness_pos, banner_pos)

    def test_detail_over_400_characters_renders_in_full_with_no_ellipsis(self):
        long_detail = "x" * 450
        malicious_check = invariants.Check(
            name="SOL_BURN_UNSPENDABLE",
            status=invariants.FAIL,
            backs=(invariants.SOL_BURN_TOTAL,),
            equation="n/a",
            detail=long_detail,
        )
        observation = build_observation()
        observation.checks = tuple(
            c for c in observation.checks if c.name != "SOL_BURN_UNSPENDABLE"
        ) + (malicious_check,)
        observation.verdict = invariants.apply_silence_rule(observation.checks)

        rendered = site.render(observation, now=2.0)
        self.assertIn(long_detail, rendered)
        self.assertNotIn("…", rendered)  # horizontal ellipsis


class TestChecksList(unittest.TestCase):
    """02-02 Task 2: every check in `observation.checks` renders with its
    status badge and its full `detail` string -- never truncated.
    """

    def test_every_check_name_and_full_detail_appear(self):
        observation = build_observation()
        rendered = site.render(observation, now=2.0)
        for check in observation.checks:
            self.assertIn(check.name, rendered, f"{check.name} missing from checks list")
            self.assertIn(check.detail, rendered, f"{check.name}'s detail missing from checks list")

    def test_detail_string_with_a_literal_angle_bracket_is_escaped(self):
        # invariants.py's ops_routed() writes a literal `"> 0"` into `expected`
        # today (a live escaping path, not a hypothetical one -- flagged by
        # 02-01's own escaping test, which could not cover `detail` because
        # the checks list did not exist until this task).
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            observation = build_all_publishable_sentinel_observation(evidence)
            evidence.close()

        ops_check = next(c for c in observation.checks if c.name == "OPS_ROUTED")
        self.assertIn("> 0", ops_check.expected)

        rendered = site.render(observation, now=2.0)
        self.assertIn("&gt; 0", rendered)
        self.assertNotIn("expected " + ops_check.expected, rendered)


def _boost_rows(count: int, *, tokens_start=1_000_000, block_time_start=100, step=100):
    """Boost-source burn_event rows, shaped exactly like `evidence.burns_for()`'s
    output, distinct enough in `tokens_burned`/`block_time` that a hardcoded
    figure would fail the "not hardcoded" tests below.
    """
    return [
        {
            "signature": f"sig-boost-{i}-{'x' * 60}",
            "source": "boost_buy_and_burn",
            "tokens_burned": tokens_start + i * 1_000,
            "sol_spent": 10 + i,
            "block_time": block_time_start + i * step,
            "slot": i,
            "instruction_index": 0,
        }
        for i in range(count)
    ]


class TestBoostSummary(unittest.TestCase):
    def test_zero_rows_returns_zeroed_values(self):
        summary = site._boost_summary([])
        self.assertEqual(summary, {"count": 0, "tokens": 0, "lamports": 0, "window_seconds": 0})

    def test_non_boost_rows_are_excluded(self):
        rows = [{"signature": "s", "source": "spl_burn", "tokens_burned": 999, "block_time": 1}]
        summary = site._boost_summary(rows)
        self.assertEqual(summary["count"], 0)

    def test_summary_computed_from_rows_changing_rows_changes_the_result(self):
        first = site._boost_summary(_boost_rows(3, tokens_start=1_000, block_time_start=0, step=10))
        second = site._boost_summary(_boost_rows(3, tokens_start=99_999, block_time_start=500, step=50))
        self.assertNotEqual(first, second)


class TestTheBurnSection(unittest.TestCase):
    """02-02 Task 3: the boost-attribution figures are computed live from
    `observation.burn_events`, never hardcoded, and the transaction list
    handles zero/one/many/overflow per UI-SPEC's Overflow section.
    """

    def _observation_with_rows(self, rows):
        observation = build_observation()
        observation.burn_events = rows
        return observation

    def test_boost_figures_are_not_hardcoded_two_fixtures_render_differently(self):
        rendered_a = site.render(
            self._observation_with_rows(_boost_rows(2, tokens_start=1_000_000, block_time_start=0, step=50)),
            now=2.0,
        )
        rendered_b = site.render(
            self._observation_with_rows(_boost_rows(2, tokens_start=7_000_000, block_time_start=900, step=50)),
            now=2.0,
        )
        self.assertNotEqual(rendered_a, rendered_b)

    def test_more_rows_than_limit_shows_exactly_the_limit_and_a_remainder_count(self):
        rows = _boost_rows(site.TX_LINK_LIMIT + 5)
        rendered = site.render(self._observation_with_rows(rows), now=2.0)
        self.assertEqual(rendered.count(site.EXPLORER_TX_PREFIX), site.TX_LINK_LIMIT)
        self.assertIn("+ 5 more", rendered)

    def test_every_link_carries_the_full_signature_in_href_and_title(self):
        rows = _boost_rows(2)
        rendered = site.render(self._observation_with_rows(rows), now=2.0)
        for row in rows:
            self.assertIn(f'href="{site.EXPLORER_TX_PREFIX}{row["signature"]}"', rendered)
            self.assertIn(f'title="{row["signature"]}"', rendered)

    def test_one_row_uses_singular_wording(self):
        rendered = site.render(self._observation_with_rows(_boost_rows(1)), now=2.0)
        self.assertIn(" 1 transaction ", rendered)
        self.assertNotIn(" 1 transactions ", rendered)

    def test_several_rows_use_plural_wording(self):
        rendered = site.render(self._observation_with_rows(_boost_rows(3)), now=2.0)
        self.assertIn(" 3 transactions ", rendered)

    def test_zero_rows_renders_prose_not_an_empty_link_list(self):
        rendered = site.render(self._observation_with_rows([]), now=2.0)
        self.assertNotIn(site.EXPLORER_TX_PREFIX, rendered)
        self.assertIn("No burns are recorded", rendered)

    def test_burn_atomic_detail_rendered_verbatim_as_prose(self):
        observation = self._observation_with_rows(_boost_rows(1))
        atomic_check = next(c for c in observation.checks if c.name == "BURN_ATOMIC")
        rendered = site.render(observation, now=2.0)
        self.assertIn(atomic_check.detail, rendered)


class TestErrorBranchNoFigures(unittest.TestCase):
    def test_error_branch_has_no_figure_row_markers(self):
        observation = Observation(mint=CHARLIE, observed_at=1.0, error="RPC unavailable")
        rendered = site.render(observation, now=2.0)
        self.assertIn("RPC unavailable", rendered)
        self.assertNotIn("data-figure=", rendered)


class TestRisksSection(unittest.TestCase):
    def _risks(self, observation):
        rendered = site.render(observation, now=2.0)
        start = rendered.index('id="risks"')
        return rendered[start:rendered.index("</section>", start)]

    def test_six_entries_for_a_coin_burning_to_a_burn_address(self):
        # Four standing risks, the walk risk, and the generator-unverified
        # entry. Nothing about the SOL burn destination, because there is
        # nothing wrong with it.
        self.assertEqual(self._risks(build_observation()).count("<li"), 6)

    def test_a_seventh_appears_only_when_this_coin_fails_the_check(self):
        risks = self._risks(build_observation(sol_burn_address=SPENDABLE))
        self.assertEqual(risks.count("<li"), 7)
        self.assertIn("SOL_BURN_UNSPENDABLE fails", risks)

    def test_a_passing_coin_is_told_no_such_risk(self):
        """The entry was static, and said "fails permanently for this coin" on
        every page -- including pages whose own check row read PASS two
        sections above. A page that contradicts its own check is the defect
        this whole site exists to refuse.
        """
        self.assertNotIn("SOL_BURN_UNSPENDABLE fails", self._risks(build_observation()))

    def test_seventh_entry_carries_the_generator_unverified_constant_as_one_unit(self):
        observation = build_observation()
        rendered = site.render(observation, now=2.0)
        self.assertIn(site._GENERATOR_UNVERIFIED, rendered)
        self.assertIn(site.RISK_GENERATOR_ANCHOR, rendered)


class TestSingleScriptNoClockRead(unittest.TestCase):
    def test_exactly_one_script_element_with_no_src(self):
        observation = build_observation()
        rendered = site.render(observation, now=2.0)
        self.assertEqual(rendered.count("<script"), 1)
        start = rendered.index("<script")
        end = rendered.index("</script>")
        self.assertNotIn("src=", rendered[start:end])

    def test_copy_script_reads_no_clock(self):
        self.assertFalse(any(t in site._COPY_SCRIPT for t in ("Date", "getTime", "now(")))


FORBIDDEN_PHRASES = (
    "the SOL burn burned",
    "burned into the SOL burn",
    "burned and burned",
    "charlie protocol verified",
)


class TestForbiddenBurnLanguageAbsent(unittest.TestCase):
    def test_forbidden_burn_language_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            pass_observation = build_all_publishable_sentinel_observation(evidence)
            evidence.close()

        fail_observation = build_observation()

        for observation in (fail_observation, pass_observation):
            rendered = site.render(observation, now=2.0).lower()
            for phrase in FORBIDDEN_PHRASES:
                self.assertNotIn(phrase, rendered, f"forbidden phrase {phrase!r} found in web_page output")

    def test_forbidden_burn_language_absent_on_landing_page(self):
        # 02-03 Task 2: the same sweep, over render_landing, for both the
        # all-blocked and all-publishable sentinel fixtures.
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            pass_observation = build_all_publishable_sentinel_observation(evidence)
            evidence.close()

        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            fail_observation = build_all_blocked_sentinel_observation(evidence)
            evidence.close()

        for observation in (fail_observation, pass_observation):
            rendered = site.render_landing(observation, now=2.0).lower()
            for phrase in FORBIDDEN_PHRASES:
                self.assertNotIn(phrase, rendered, f"forbidden phrase {phrase!r} found in landing_page output")


class TestStdlibOnlyImport(unittest.TestCase):
    def test_site_imports_no_indexer_module_but_invariants_and_publish(self):
        import ast

        source = Path(__file__).resolve().parents[1].joinpath("indexer", "site.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        names = sorted(
            {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level for alias in node.names}
        )
        self.assertEqual(names, ["invariants", "publish"])


class TestArtifactName(unittest.TestCase):
    """02-03 Task 1, D-19: `_artifact_name` is the one place a `web/`
    artifact filename is constructed -- `write()`'s own paths resolve
    through it, and so must every in-page reference to either file.
    """

    def test_artifact_name_matches_writes_own_filenames(self):
        observation = build_observation()
        with tempfile.TemporaryDirectory() as tmp:
            html_path, json_path = site.write(observation, tmp)
        self.assertEqual(html_path.name, site._artifact_name(observation.mint, ".html"))
        self.assertEqual(json_path.name, site._artifact_name(observation.mint, ".json"))

    def test_write_builds_both_paths_through_the_shared_helper(self):
        import inspect

        source = inspect.getsource(site.write)
        self.assertEqual(source.count("_artifact_name"), 2, "write() must build both paths through _artifact_name (D-19)")

    def test_no_dated_or_latest_variant_of_the_helper_exists(self):
        # D-19 rejected a dated series / `latest` pointer -- a second naming
        # scheme is how that rejection would quietly come back.
        self.assertFalse(hasattr(site, "_dated_artifact_name"))
        self.assertFalse(hasattr(site, "_latest_artifact_name"))


class TestRawRecordLink(unittest.TestCase):
    """02-03 Task 1, WEB-06: the primary-CTA raw-JSON link, present in both
    the header and the closing raw-record section, its href derived from the
    same helper `write()` uses so the link and the file can never disagree.
    """

    def test_raw_record_link_href_matches_artifact_name(self):
        observation = build_observation()
        expected_href = site._artifact_name(observation.mint, ".json")
        link = site._raw_record_link(observation)
        self.assertIn(f'href="{expected_href}"', link)

    def test_primary_cta_copy_matches_ui_spec_verbatim(self):
        observation = build_observation()
        link = site._raw_record_link(observation)
        self.assertIn("View the raw observation JSON", link)

    def test_link_not_a_button(self):
        observation = build_observation()
        link = site._raw_record_link(observation)
        self.assertNotIn("<button", link)
        self.assertTrue(link.strip().startswith("<a "))

    def test_raw_record_link_appears_at_least_twice_header_and_closing_section(self):
        observation = build_observation()
        rendered = site.render(observation, now=2.0)
        expected_href = site._artifact_name(observation.mint, ".json")
        self.assertGreaterEqual(rendered.count(f'href="{expected_href}"'), 2)

    def test_json_filename_appears_at_least_twice_computed_from_shared_helper(self):
        observation = build_observation()
        rendered = site.render(observation, now=2.0)
        filename = site._artifact_name(observation.mint, ".json")
        self.assertGreaterEqual(rendered.count(filename), 2)


class TestHistoryNote(unittest.TestCase):
    """02-03 Task 1, D-19: the page names its own git history, rendering the
    `git log -p` command against its own filename -- computed, not pasted.
    """

    def test_history_command_present_for_own_filename(self):
        observation = Observation(mint="ZZTOP", observed_at=1_000_000.0)
        rendered = site.render(observation, now=1_000_100.0)
        self.assertIn("git log -p web/" + site._artifact_name("ZZTOP", ".html"), rendered)

    def test_history_command_is_specific_to_each_mint(self):
        first = Observation(mint="MINTONE", observed_at=1_000_000.0)
        second = Observation(mint="MINTTWO", observed_at=1_000_000.0)
        rendered_first = site.render(first, now=1_000_100.0)
        rendered_second = site.render(second, now=1_000_100.0)

        first_command = "git log -p web/" + site._artifact_name("MINTONE", ".html")
        second_command = "git log -p web/" + site._artifact_name("MINTTWO", ".html")

        self.assertIn(first_command, rendered_first)
        self.assertNotIn(second_command, rendered_first)
        self.assertIn(second_command, rendered_second)
        self.assertNotIn(first_command, rendered_second)

    def test_history_note_states_overwrite_and_no_latest_pointer(self):
        observation = build_observation()
        rendered = site.render(observation, now=2.0)
        self.assertIn("overwrites this page", rendered)
        self.assertIn("no dated series", rendered)
        self.assertIn("latest", rendered.lower())
        # No actual dated/latest artifact name is ever named -- only this
        # page's own two filenames appear anywhere in the document.
        own_html = site._artifact_name(observation.mint, ".html")
        own_json = site._artifact_name(observation.mint, ".json")
        for token in ("latest.html", "latest.json"):
            self.assertNotIn(token, rendered)
        self.assertNotEqual(own_html, "latest.html")
        self.assertNotEqual(own_json, "latest.json")


class TestRawRecordSection(unittest.TestCase):
    """02-03 Task 1, D-16: the closing section states what the raw record
    proves and what it does not, links the generator-unverified risk, and
    links the committed evidence export as the route to verify the
    transaction list.
    """

    def test_limit_statement_present(self):
        observation = build_observation()
        rendered = site.render(observation, now=2.0)
        # esc() turns the constant's apostrophes into entities on render --
        # compare the escaped form, the same transform every other
        # interpolated value in this module goes through.
        self.assertIn(site.esc(site._RECORD_LIMIT_STATEMENT), rendered)

    def test_closing_section_links_the_generator_unverified_risk(self):
        observation = build_observation()
        rendered = site.render(observation, now=2.0)
        self.assertIn(f'href="#{site.RISK_GENERATOR_ANCHOR}"', rendered)
        self.assertIn(f'id="{site.RISK_GENERATOR_ANCHOR}"', rendered)

    def test_page_links_the_evidence_export_directory(self):
        observation = build_observation()
        rendered = site.render(observation, now=2.0)
        self.assertIn(site.EVIDENCE_EXPORT_PATH, rendered)

    def test_no_second_json_producing_entry_point_was_added(self):
        json_producing = sorted(
            n for n in dir(site) if "json" in n.lower() and callable(getattr(site, n))
        )
        self.assertEqual(json_producing, ["record_json"])


class TestRecordJsonEqualsDurableRecord(unittest.TestCase):
    """02-03 Task 1: record_json's payload is exactly publish.durable_record(),
    for both the all-publishable and the all-blocked sentinel observations,
    and is byte-identical across two calls on an unchanged observation."""

    def test_record_json_equals_durable_record_for_all_publishable_sentinel(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            observation = build_all_publishable_sentinel_observation(evidence)
            evidence.close()
        self.assertEqual(json.loads(site.record_json(observation)), publish.durable_record(observation))

    def test_record_json_equals_durable_record_for_all_blocked_sentinel(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            observation = build_all_blocked_sentinel_observation(evidence)
            evidence.close()
        self.assertEqual(json.loads(site.record_json(observation)), publish.durable_record(observation))

    def test_record_json_byte_identical_across_two_calls(self):
        observation = build_observation()
        self.assertEqual(site.record_json(observation), site.record_json(observation))


class TestWrite(unittest.TestCase):
    def test_write_produces_a_sibling_html_json_pair_matching_the_durable_record(self):
        observation = build_observation()
        with tempfile.TemporaryDirectory() as tmp:
            html_path, json_path = site.write(observation, tmp)

            self.assertTrue(html_path.is_file())
            self.assertTrue(json_path.is_file())
            self.assertEqual(html_path.stem, json_path.stem)
            self.assertNotEqual(html_path.suffix, json_path.suffix)

            written_json = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(written_json, publish.durable_record(observation))

            first_pass = site.record_json(observation)
            second_pass = site.record_json(observation)
            self.assertEqual(first_pass, second_pass, "record_json must be byte-identical across runs")

    def test_write_still_produces_a_valid_json_artifact_when_every_figure_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            observation = build_all_blocked_sentinel_observation(evidence)
            evidence.close()

            out_dir = Path(tmp) / "out"
            html_path, json_path = site.write(observation, out_dir)

            self.assertTrue(json_path.is_file())
            written_json = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertIn("blocked", written_json)
            self.assertTrue(written_json["blocked"], "an all-blocked observation must carry a non-empty blocked map")
            self.assertTrue(html_path.is_file())


class TestWalkStateAndNonBoost(unittest.TestCase):
    """Two claims that were template constants and became false against live
    evidence the first time the burn walk ran to completion. Both are now
    computed; these tests exist so they cannot quietly become constants again.
    """

    class _Obs:
        def __init__(self, complete=False, events=None):
            self.burn_walk_complete = complete
            self.burn_events = events or []

    def test_walk_risk_states_complete_when_the_cursor_says_complete(self):
        text = site._walk_risk(self._Obs(complete=True))
        self.assertIn("complete and the residual survives it", text)
        self.assertNotIn("is incomplete", text)

    def test_walk_risk_states_incomplete_when_the_cursor_says_so(self):
        text = site._walk_risk(self._Obs(complete=False))
        self.assertIn("is incomplete", text)

    def test_walk_risk_never_asserts_completeness_the_observation_denies(self):
        """The regression: a page that says "complete" while the observation
        says otherwise is the page contradicting its own evidence."""
        for complete in (True, False):
            text = site._walk_risk(self._Obs(complete=complete))
            says_complete = "walk is complete" in text
            self.assertEqual(says_complete, complete, f"walk_complete={complete}")

    def test_the_static_risks_are_the_ones_true_of_every_coin(self):
        """D-20. Four standing risks, plus the walk risk and the
        generator-unverified entry, which are appended per observation. A
        risk that is only true of some coins does not belong in the tuple --
        that is how "SOL_BURN_UNSPENDABLE fails permanently for this coin"
        came to be printed on coins where it passed.
        """
        self.assertEqual(len(site._RISKS), 4)
        for risk in site._RISKS:
            with self.subTest(risk=risk):
                self.assertNotIn("this coin", risk)

    def test_non_boost_burns_are_stated_separately_and_computed(self):
        events = [
            {"source": "boost_buy_and_burn", "tokens_burned": 43_575_480_427_900},
            {"source": "spl_burn", "tokens_burned": 1_100_000_000},
        ]
        text = site._non_boost_sentence(events, 6)
        self.assertIn("1,100.000000", text)
        self.assertIn("1 recorded burn", text)

    def test_non_boost_sentence_says_so_when_there_are_none(self):
        events = [{"source": "boost_buy_and_burn", "tokens_burned": 5}]
        text = site._non_boost_sentence(events, 6)
        self.assertIn("No burn from any other source", text)

    def test_boost_sentence_no_longer_claims_every_token_ever_lost(self):
        """The superlative that went false: it was true only while boost was
        the sole recorded source, and a template cannot know that."""
        summary = site._boost_summary(
            [{"source": "boost_buy_and_burn", "tokens_burned": 5, "block_time": 1, "signature": "x"}]
        )
        self.assertNotIn("ever lost", site._boost_sentence(summary, 6))


# -- QT-01/QT-02/QT-03: the landing page (indexer/site.py's landing path) ---
def _landing_burn_rows():
    """29 boost rows + 1 non-boost row summing to the observed-values table
    in the plan -- 43,576,580.427900 tokens / 17.584506254 SOL across all 30
    rows, with the one non-boost row carrying 1,100.000000 tokens. Values
    are computed here from the plan's own totals, never pasted as a whole.
    """
    total_tokens = 43_576_580_427_900
    non_boost_tokens = 1_100_000_000
    boost_tokens_total = total_tokens - non_boost_tokens
    boost_lamports_total = 17_584_506_254
    n = 29
    base_tokens, remainder_tokens = divmod(boost_tokens_total, n)
    base_lamports, remainder_lamports = divmod(boost_lamports_total, n)
    rows = []
    for i in range(n):
        rows.append({
            "signature": f"sig-boost-{i}",
            "source": site.BOOST_SOURCE,
            "tokens_burned": base_tokens + (remainder_tokens if i == n - 1 else 0),
            "sol_spent": base_lamports + (remainder_lamports if i == n - 1 else 0),
            "block_time": 100 + i,
            "slot": i,
        })
    rows.append({
        "signature": "sig-handburn",
        "source": "spl_burn",
        "tokens_burned": non_boost_tokens,
        "sol_spent": None,
        "block_time": 500,
        "slot": 100,
    })
    return rows


def _counters_fixture(*, supply=956_383_374_035_955, initial_raw=1_000_000_000_000_000, burn_rows=None):
    burn_rows = _landing_burn_rows() if burn_rows is None else burn_rows
    observation = Observation(mint=CHARLIE, observed_at=1.0)
    observation.mint_state = mint_state(supply)
    initial_supply_row = {"raw_supply": initial_raw, "decimals": 6}
    observation.evidence = {"initial_supply": initial_supply_row}
    observation.burn_events = burn_rows
    # Real-shape BURN_SUPPLY check (walk complete, real expected/actual) --
    # `burned` is the evidence store's own running total (independent of
    # `burn_events`'s per-row sum, exactly like production), computed here
    # from the fixture's own rows so the check reads FAIL/PASS honestly
    # rather than resting on NO_CHECK/UNCHECKED.
    burned = sum(int(row.get("tokens_burned", 0)) for row in burn_rows)
    observation.checks = (
        invariants.burn_supply(observation.mint_state, initial_supply_row, burned, walk_complete=True),
    )
    observation.verdict = invariants.apply_silence_rule(observation.checks)
    return observation


class TestCounters(unittest.TestCase):
    def test_six_cells_in_fixed_order_with_expected_display_values(self):
        cells = site._counters(_counters_fixture())
        self.assertEqual(len(cells), 6)
        values = [c["value"] for c in cells]
        self.assertEqual(values[0], "956,383,374.035955")
        self.assertEqual(values[1], "1,000,000,000.000000")
        self.assertEqual(values[2], "30")
        self.assertEqual(values[3], "43,576,580.427900")
        self.assertEqual(values[4], "17.584506254")
        self.assertIn("1", values[5])
        self.assertIn("1,100.000000", values[5])
        for cell in cells:
            self.assertIn("label", cell)
            self.assertIn("source", cell)
            self.assertIn("raw", cell)

    def test_changing_fixture_inputs_changes_the_corresponding_cells(self):
        first = site._counters(_counters_fixture())
        second = site._counters(_counters_fixture(
            supply=1_234_567,
            initial_raw=9_999_999,
            burn_rows=_boost_rows(2, tokens_start=1, block_time_start=0, step=1),
        ))
        self.assertNotEqual(first, second)

    def test_reads_exactly_the_four_permitted_fields_never_burn_total(self):
        source = inspect.getsource(site._counters)
        self.assertNotIn("burn_total", source)
        self.assertNotIn("Publisher", source)


class TestSupplyProhibition(unittest.TestCase):
    def test_no_cell_raw_equals_the_prohibited_difference(self):
        observation = _counters_fixture()
        initial_raw = observation.evidence["initial_supply"]["raw_supply"]
        live_raw = observation.mint_state.supply
        prohibited_raw = initial_raw - live_raw
        for cell in site._counters(observation):
            self.assertNotEqual(cell["raw"], prohibited_raw)

    def test_prohibited_difference_absent_raw_and_display_from_rendered_page(self):
        observation = _counters_fixture()
        initial_raw = observation.evidence["initial_supply"]["raw_supply"]
        live_raw = observation.mint_state.supply
        prohibited_raw = initial_raw - live_raw
        decimals = observation.mint_state.decimals
        prohibited_display = f"{prohibited_raw / (10 ** decimals):,.{decimals}f}"

        rendered = site.render_landing(observation, now=2.0)
        self.assertNotIn(str(prohibited_raw), rendered)
        self.assertNotIn(prohibited_display, rendered)


class TestNoFiguresNameOnLandingPage(unittest.TestCase):
    def test_no_invariants_figures_name_appears(self):
        observation = _counters_fixture()
        rendered = site.render_landing(observation, now=2.0)
        for name in invariants.FIGURES:
            self.assertNotIn(name, rendered, f"invariants.FIGURES name {name!r} leaked into the landing page")

    def test_render_landing_source_reads_no_gated_figure(self):
        source = inspect.getsource(site.render_landing)
        self.assertNotIn("burn_total", source)
        self.assertNotIn("Publisher", source)


class TestBareObservationLanding(unittest.TestCase):
    def test_bare_observation_renders_without_raising_and_says_unknown(self):
        observation = Observation(mint="ZZTOP", observed_at=1_000_000.0)
        rendered = site.render_landing(observation)
        self.assertGreater(len(rendered), 0)
        self.assertIn(site._SNAPSHOT_NOTE, rendered)
        for cell in site._counters(observation):
            self.assertEqual(cell["value"], "unknown")
            self.assertIsNone(cell["raw"])
        self.assertGreaterEqual(rendered.count("unknown"), 6)


class TestRenderLandingSurfaceRegistration(unittest.TestCase):
    def test_render_landing_resolves_through_render_surface(self):
        observation = _counters_fixture()
        result = publish.render_surface("landing_page", observation)
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)

    def test_accepts_one_positional_and_keyword_only_now(self):
        observation = _counters_fixture()
        rendered = site.render_landing(observation, now=2.0)
        self.assertIsInstance(rendered, str)

    def test_landing_page_registered_in_surfaces(self):
        self.assertEqual(publish.SURFACES["landing_page"]["target"], "indexer.site:render_landing")
        self.assertEqual(publish.SURFACES["landing_page"]["input"], "observation")

    def test_landing_page_not_in_full_detail_surfaces(self):
        # QT-01/QT-03: FULL_DETAIL_SURFACES are required to show every
        # publishable figure -- the landing page is required to show none.
        # A decision, not an oversight.
        self.assertNotIn("landing_page", FULL_DETAIL_SURFACES)


class TestLandingEscaping(unittest.TestCase):
    def test_check_detail_with_markup_renders_escaped(self):
        observation = _counters_fixture()
        malicious_check = invariants.Check(
            name="BURN_SUPPLY",
            status=invariants.FAIL,
            backs=(invariants.BURN_TOTAL, invariants.SUPPLY_DESTROYED),
            equation="n/a",
            detail="<script>alert(1)</script>",
        )
        observation.checks = (malicious_check,)
        observation.verdict = invariants.apply_silence_rule(observation.checks)
        rendered = site.render_landing(observation, now=2.0)
        self.assertNotIn("<script>alert(1)</script>", rendered)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", rendered)


class TestSupplyRefusal(unittest.TestCase):
    def test_refusal_names_burn_supply_and_renders_its_own_live_fields(self):
        observation = _counters_fixture()
        check = observation.checks[0]
        rendered = site.render_landing(observation, now=2.0)
        self.assertIn(check.name, rendered)
        self.assertIn(check.detail, rendered)
        self.assertIn(site.esc(check.expected), rendered)
        self.assertIn(site.esc(check.actual), rendered)

    def test_refusal_present_even_with_no_burn_supply_check(self):
        observation = Observation(mint="ZZTOP", observed_at=1_000_000.0)
        rendered = site.render_landing(observation)
        self.assertIn('data-refusal="supply"', rendered)


class TestWriteLanding(unittest.TestCase):
    def test_write_landing_writes_index_html_and_returns_its_path(self):
        observation = _counters_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            path = site.write_landing(observation, tmp)
        self.assertEqual(path.name, site.LANDING_FILENAME)

    def test_write_landing_bytes_match_render_landing(self):
        observation = _counters_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            path = site.write_landing(observation, tmp)
            content = path.read_bytes()
        expected = site.render_landing(observation).encode("utf-8")
        self.assertEqual(content, expected)

    def test_write_still_has_two_artifact_name_occurrences(self):
        self.assertEqual(inspect.getsource(site.write).count("_artifact_name"), 2)

    def test_write_landing_has_no_artifact_name_occurrence(self):
        self.assertEqual(inspect.getsource(site.write_landing).count("_artifact_name"), 0)


class TestCoinUrl(unittest.TestCase):
    def test_coin_url_composes_the_route_prefix_and_artifact_name(self):
        self.assertEqual(
            site._coin_url("SOMEMINT", ".html"),
            site.COIN_ROUTE_PREFIX + site._artifact_name("SOMEMINT", ".html"),
        )

    def test_landing_links_use_coin_url(self):
        observation = _counters_fixture()
        rendered = site.render_landing(observation, now=2.0)
        # The page link is suffix-free (vercel.json's page rewrite is
        # deliberately suffix-free -- only the record rewrite carries a
        # literal .json); the record link keeps its .json.
        self.assertIn(f'href="{site._coin_url(observation.mint, "")}"', rendered)
        self.assertIn(f'href="{site._coin_url(observation.mint, ".json")}"', rendered)


# -- 02-03 Task 2: shared tokens, sourced counters, the withheld treatment --
class TestSharedTokens(unittest.TestCase):
    def test_tokens_is_a_substring_of_both_stylesheets(self):
        self.assertIn(site._TOKENS, site._STYLE)
        self.assertIn(site._TOKENS, site._LANDING_STYLE)

    def test_palette_declared_exactly_once_in_style(self):
        # The plan's own acceptance criterion asks for `_STYLE.count("--paper")
        # == 1`, but `body { background: var(--paper); }` (D-E: unchanged coin-
        # page CSS) also contains the substring "--paper" as a usage site, not
        # a second declaration -- an unavoidable collision with any bare
        # substring count. The declaration line itself is what "extracted, not
        # duplicated" actually means: exactly one `:root` block, never two
        # concatenated by an extraction mistake.
        self.assertEqual(site._STYLE.count("--paper: #FAF7F0;"), 1)


class TestCounterSourcesVisible(unittest.TestCase):
    def test_every_cell_source_string_appears_in_the_rendered_page(self):
        observation = _counters_fixture()
        rendered = site.render_landing(observation, now=2.0)
        for cell in site._counters(observation):
            self.assertIn(cell["source"], rendered, f"source {cell['source']!r} not visible on the landing page")


class TestLandingOneStyleZeroScript(unittest.TestCase):
    def test_exactly_one_style_and_zero_script_elements(self):
        observation = _counters_fixture()
        rendered = site.render_landing(observation, now=2.0)
        self.assertEqual(rendered.count("<style"), 1)
        self.assertEqual(rendered.count("<script"), 0)


class TestRefusalWithheldTreatment(unittest.TestCase):
    def test_refusal_wrapper_carries_the_unchecked_class(self):
        observation = _counters_fixture()
        rendered = site.render_landing(observation, now=2.0)
        marker = 'data-refusal="supply"'
        wrapper_start = rendered.rindex("<section", 0, rendered.index(marker))
        tag_end = rendered.index(">", wrapper_start)
        self.assertIn("status-unchecked", rendered[wrapper_start:tag_end])
        self.assertNotIn("status-pass", rendered[wrapper_start:tag_end])


class TestGeneratorUnverifiedOnLanding(unittest.TestCase):
    def test_generator_unverified_constant_appears_escaped(self):
        observation = _counters_fixture()
        rendered = site.render_landing(observation, now=2.0)
        self.assertIn(site.esc(site._GENERATOR_UNVERIFIED), rendered)


# -- 02-03 Task 3: vercel.json cross-checked against _artifact_name --------
VERCEL_JSON_PATH = Path(__file__).resolve().parents[1] / "vercel.json"


def _vercel_source_to_regex(vercel_source: str) -> re.Pattern:
    converted = re.sub(r":mint\(([^)]+)\)", r"(\1)", vercel_source)
    return re.compile("^" + converted + "$")


def _find_rule(rewrites, *, source_exact=None, source_prefix=None, destination_suffix=None):
    """Look a rewrite rule up by its own SOURCE (and/or destination shape),
    never by its position in the list -- a pinned test that has to change
    when a rule is added should change by looking the rule up differently,
    not by unpacking a fixed-length list.
    """
    matches = []
    for rule in rewrites:
        if source_exact is not None and rule["source"] != source_exact:
            continue
        if source_prefix is not None and not rule["source"].startswith(source_prefix):
            continue
        if destination_suffix is not None and not rule["destination"].endswith(destination_suffix):
            continue
        matches.append(rule)
    assert len(matches) == 1, f"expected exactly one matching rule, found {matches}"
    return matches[0]


class TestVercelJson(unittest.TestCase):
    """Rewritten for 03-01 Task 3: this class used to assert the rewrite
    list was exactly two long and unpack it into two names in three separate
    tests, which would fail four times over the moment a third rule exists.
    It now looks each rule up by its own source/destination shape via
    `_find_rule` and keeps every assertion it made about the two coin
    routes, plus the same assertions for `/verify/:mint` and `/coins`.
    """

    def _load(self):
        return json.loads(VERCEL_JSON_PATH.read_text(encoding="utf-8"))

    def _rules(self, data):
        rewrites = data["rewrites"]
        json_rewrite = _find_rule(rewrites, source_prefix=site.COIN_ROUTE_PREFIX, destination_suffix=JSON_ROUTE_SUFFIX)
        html_rewrite = _find_rule(
            rewrites, source_prefix=site.COIN_ROUTE_PREFIX, destination_suffix=LIVE_ROUTE_SUFFIX
        )
        verify_rewrite = _find_rule(rewrites, source_prefix="/verify/")
        coins_rewrite = _find_rule(rewrites, source_exact="/coins")
        return json_rewrite, html_rewrite, verify_rewrite, coins_rewrite

    def test_output_directory_no_build_step_no_clean_urls(self):
        data = self._load()
        self.assertEqual(data["outputDirectory"], "web")
        self.assertIsNone(data["buildCommand"])
        self.assertIsNone(data["framework"])
        self.assertIn(data.get("cleanUrls"), (None, False))

    def test_every_rewrite_is_one_of_the_five_this_project_declares(self):
        """Counted by NAME, not by length. The previous version asserted a
        bare count and broke four tests at once the moment a route was added;
        this fails only if a rule appears that nothing here accounts for.
        """
        data = self._load()
        sources = {r["source"] for r in data["rewrites"]}
        self.assertEqual(sources, {
            "/coins",
            "/enroll",
            "/verify",
            site.COIN_ROUTE_PREFIX + ":mint([1-9A-HJ-NP-Za-km-z]+).json",
            site.COIN_ROUTE_PREFIX + ":mint([1-9A-HJ-NP-Za-km-z]+)",
            "/verify/:mint([1-9A-HJ-NP-Za-km-z]+)",
        })

    def test_destinations_and_sources_built_from_artifact_name_and_route_prefix(self):
        data = self._load()
        json_rewrite, html_rewrite, verify_rewrite, coins_rewrite = self._rules(data)
        # The record goes through the function too. A live-rendered coin has
        # no committed .json, so the "View the raw observation JSON" link on
        # its own page pointed at a file that had never been written.
        self.assertEqual(json_rewrite["destination"], LIVE_ROUTE + "?mint=:mint&format=json")
        # The page route goes to the live function, NOT straight at a file.
        # A static destination 404s for every coin without a committed page,
        # which is almost every coin under the submit model; the function
        # serves the committed page when there is one and observes the chain
        # when there is not.
        self.assertEqual(html_rewrite["destination"], LIVE_ROUTE + "?mint=:mint")
        self.assertTrue(json_rewrite["source"].startswith(site.COIN_ROUTE_PREFIX))
        self.assertTrue(html_rewrite["source"].startswith(site.COIN_ROUTE_PREFIX))
        # D-22: /verify/:mint resolves to the SAME destination the coin-page
        # rule gives -- one artifact per coin, not two.
        self.assertEqual(verify_rewrite["destination"], LIVE_ROUTE + "?mint=:mint")
        self.assertEqual(verify_rewrite["destination"], html_rewrite["destination"])
        self.assertTrue(verify_rewrite["source"].startswith("/verify/"))
        self.assertEqual(coins_rewrite["destination"], "/" + site.INDEX_FILENAME_TEMPLATE.format(page=1))

    def test_mint_pattern_matches_real_mint_and_json_never_matches_html_pattern(self):
        data = self._load()
        json_rewrite, html_rewrite, verify_rewrite, coins_rewrite = self._rules(data)
        json_re = _vercel_source_to_regex(json_rewrite["source"])
        html_re = _vercel_source_to_regex(html_rewrite["source"])
        verify_re = _vercel_source_to_regex(verify_rewrite["source"])
        self.assertRegex(f"/coin/{CHARLIE}.json", json_re)
        self.assertRegex(f"/coin/{CHARLIE}", html_re)
        self.assertNotRegex(f"/coin/{CHARLIE}.json", html_re)
        self.assertRegex(f"/verify/{CHARLIE}", verify_re)
        # No character outside the base58 class matches any of the three
        # mint-parameterised rules.
        for regex in (json_re, html_re, verify_re):
            self.assertNotRegex("/coin/0OIl", regex)
            self.assertNotRegex("/verify/0OIl", regex)

    def test_landing_page_links_resolve_under_these_rewrites(self):
        data = self._load()
        json_rewrite, html_rewrite, verify_rewrite, coins_rewrite = self._rules(data)
        json_re = _vercel_source_to_regex(json_rewrite["source"])
        html_re = _vercel_source_to_regex(html_rewrite["source"])
        observation = _counters_fixture()
        self.assertRegex(site._coin_url(observation.mint, ".json"), json_re)
        self.assertRegex(site._coin_url(observation.mint, ""), html_re)

    def test_coins_rule_precedes_the_parameterised_rules(self):
        # Precedence stated rather than inferred: the literal /coins rule
        # must be tried before any rule carrying a :mint parameter.
        data = self._load()
        rewrites = data["rewrites"]
        coins_index = next(i for i, r in enumerate(rewrites) if r["source"] == "/coins")
        param_indices = [i for i, r in enumerate(rewrites) if ":mint" in r["source"]]
        self.assertTrue(param_indices)
        self.assertTrue(all(coins_index < i for i in param_indices))

    def test_no_planning_path_cited(self):
        self.assertNotIn(".planning", VERCEL_JSON_PATH.read_text(encoding="utf-8"))


class TestWebReadmeNoPlanningPath(unittest.TestCase):
    def test_no_planning_path_cited(self):
        path = Path(__file__).resolve().parents[1] / "web" / "README.md"
        self.assertNotIn(".planning", path.read_text(encoding="utf-8"))


# -- 03-01 Task 2: a coin page that is true about the coin it is about ------
# The reference coin's ticker, built by concatenation rather than as one
# literal, so a test checking for its ABSENCE cannot be satisfied by its own
# source text turning up in a naive grep.
_REFERENCE_TICKER = "$" + "CHARLIE"

OTHER_MINT_ONE = "So11111111111111111111111111111111111111112"
OTHER_MINT_TWO = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
OTHER_SHAREHOLDER = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


def _other_coin_observation(*, mint=OTHER_MINT_ONE, admin_revoked=True, burn_events=None):
    """A fully-shaped Observation for a coin that is NOT the reference coin --
    same construction as `test_publication.build_observation`, parameterised
    by mint and `admin_revoked` so Task 2's coin-correctness tests can render
    two different coins and compare.
    """
    registry = Registry(program_id=None, grandfathered_sol_burn=frozenset())
    config = type(
        "Cfg",
        (),
        {
            "mint": mint,
            "address": f"config-address-{mint}",
            "version": 2,
            "status": 1,
            "admin": "admin-address",
            "admin_revoked": admin_revoked,
            "shareholders": ((OTHER_SHAREHOLDER, 10_000),),
        },
    )()
    split = split_of(config, registry)
    record = Observation(mint=mint, observed_at=1.0)
    record.config = config
    record.graduated = True
    record.split = split
    record.mint_state = mint_state(900)
    record.burn_events = burn_events or []

    sol_burn_check = invariants.sol_burn_balance()
    ops_check = invariants.ops_routed(split)
    burn_check = invariants.burn_supply(record.mint_state)
    atomic_check = invariants.burn_atomic(mint, [], False)
    spend_check = invariants.burn_spend(split)
    record.checks = (
        invariants.config_mint(mint, config),
        invariants.split_sum(split),
        invariants.protocol_share(split),
        invariants.sol_burn_unspendable(split),
        sol_burn_check,
        burn_check,
        invariants.burn_irreversible(record.mint_state),
        atomic_check,
        spend_check,
        ops_check,
    )
    record.verdict = invariants.apply_silence_rule(record.checks)
    return record


def _enrolled_observation():
    """A coin whose on-chain split pays the protocol's wallet at its rate,
    with the rest to the incinerator and an ops wallet, admin_revoked."""
    from test_indexer import FakeRpc, bonding_curve, config_account, curve_account, mint_account
    # A mint of its own: the other-coin fixture already uses the wrapped-SOL
    # mint, and two rows for one mint cannot be told apart.
    mint = "JAMXU2JLraZ3RUhbgc3ttYPc18Kx4ojCnC56XR2zpump"
    config_addr = "Chx6EJ1QLRnhiyQHfpNNyiEWma8XPazbELPanPff4Nuj"
    accounts = {
        bonding_curve(mint): curve_account(config_addr),
        config_addr: config_account(
            mint,
            [(site_legs.TOLL_DESTINATION, 500),
             ("1nc1nerator11111111111111111111111111111111", 2000),
             ("9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM", 7500)],
            admin_revoked=True,
        ),
        mint: mint_account(1_000_000_000),
    }
    return observe(FakeRpc(accounts), mint, Registry(), now=1.0)


class TestCoinCorrectCopy(unittest.TestCase):
    def test_reference_ticker_absent_from_a_different_coins_page(self):
        rendered = site.render(_other_coin_observation(), now=2.0)
        self.assertNotIn(_REFERENCE_TICKER, rendered)

    def _enrolment_section(self, rendered: str) -> str:
        start = rendered.index('id="enrolment"')
        return rendered[start:rendered.index("</section>", start)]

    def test_a_coin_without_the_protocol_share_is_told_it_is_not_enrolled(self):
        """Enrolled means the on-chain split pays the protocol's wallet. This
        coin's does not, so it is not enrolled, and it is told how to be
        while its config can still change -- and that it cannot once the
        config is revoked."""
        revoked = self._enrolment_section(site.render(_other_coin_observation(admin_revoked=True), now=2.0))
        open_ = self._enrolment_section(site.render(_other_coin_observation(admin_revoked=False), now=2.0))
        for section in (revoked, open_):
            self.assertIn("Not enrolled", section)
            self.assertIn("does not pay the protocol", section)
        self.assertIn("cannot enrol", revoked)
        self.assertIn('href="/enroll"', open_)
        self.assertNotIn("cannot enrol", open_)

    def test_a_coin_paying_the_share_is_enrolled_and_says_pump_enforces_it(self):
        rendered = site.render(_enrolled_observation(), now=2.0)
        section = self._enrolment_section(rendered)
        self.assertIn("Enrolled in Charlie Protocol", section)
        self.assertIn(site_legs.TOLL_DESTINATION, section)
        self.assertIn("500 bps", section)
        self.assertIn("admin_revoked", section)
        self.assertIn("no key can alter this", section)

    def test_the_index_marks_enrolled_and_not_enrolled_rows(self):
        enrolled = publish.durable_record(_enrolled_observation())
        other = publish.durable_record(_other_coin_observation(admin_revoked=True))
        rows = site.index_rows([enrolled, other], known_pages=set())
        html = "".join(rows)
        self.assertEqual(html.count('class="index-enrolled"'), 1)
        self.assertEqual(html.count('class="index-not-enrolled"'), 1)
        # The marker sits on the right row, whatever order the rows come in.
        by_mint = {row[row.index('data-mint="') + 11:].split('"', 1)[0]: row for row in rows}
        self.assertIn('class="index-enrolled"', by_mint[enrolled["mint"]])
        self.assertIn('class="index-not-enrolled"', by_mint[other["mint"]])

    def test_non_revoked_coin_page_asserts_no_permanence_of_its_own_configuration(self):
        # "permanently destroyed" is a universal, true-of-every-coin claim in
        # the static How It Works table (the BURN leg's permitted claim) --
        # this checks only that nothing claims THIS coin's configuration
        # itself is permanent, which is false while it is reconfigurable.
        rendered = site.render(_other_coin_observation(admin_revoked=False), now=2.0)
        self.assertNotIn("only pump could ever reset it", rendered)
        self.assertNotIn("its configuration is admin_revoked", rendered)

    def test_quiet_section_text_is_identical_for_two_different_splits(self):
        obs_a = _other_coin_observation(mint=OTHER_MINT_ONE)
        obs_b = _other_coin_observation(mint=OTHER_MINT_ONE)
        # Force two different splits with the SAME burn history -- the quiet
        # section must make no claim that varies with a coin's bps.
        obs_a.split = split_of(
            type("Cfg", (), {"mint": OTHER_MINT_ONE, "shareholders": ((OTHER_SHAREHOLDER, 10_000),)})(),
            Registry(program_id=None, grandfathered_sol_burn=frozenset()),
        )
        obs_b.split = split_of(
            type("Cfg", (), {"mint": OTHER_MINT_ONE, "shareholders": ((OTHER_SHAREHOLDER, 1),)})(),
            Registry(program_id=None, grandfathered_sol_burn=frozenset()),
        )

        def quiet_html(observation):
            rendered = site.render(observation, now=2.0)
            start = rendered.index('id="quiet"')
            end = rendered.index("</section>", start)
            return rendered[start:end]

        self.assertEqual(quiet_html(obs_a), quiet_html(obs_b))

    def test_quiet_section_asserts_no_coin_specific_split(self):
        rendered = site.render(_other_coin_observation(), now=2.0)
        start = rendered.index('id="quiet"')
        end = rendered.index("</section>", start)
        quiet_section = rendered[start:end]
        self.assertNotIn("100%", quiet_section)
        self.assertIn("No protocol program is deployed", quiet_section)

    def test_two_mints_render_differing_coin_specific_sentences_and_neither_contains_the_others_mint(self):
        rendered_one = site.render(_other_coin_observation(mint=OTHER_MINT_ONE), now=2.0)
        rendered_two = site.render(_other_coin_observation(mint=OTHER_MINT_TWO), now=2.0)
        self.assertNotEqual(rendered_one, rendered_two)
        self.assertNotIn(OTHER_MINT_TWO, rendered_one)
        self.assertNotIn(OTHER_MINT_ONE, rendered_two)

    def test_sol_burn_failure_banner_never_asserts_permanence_for_a_reconfigurable_coin(self):
        obs = _other_coin_observation(admin_revoked=False)
        # Force SOL_BURN_UNSPENDABLE to FAIL so the banner renders.
        fail_check = invariants.Check(
            name="SOL_BURN_UNSPENDABLE", status=invariants.FAIL,
            backs=(invariants.SOL_BURN_TOTAL,), equation="n/a", detail="not program-derived",
        )
        obs.checks = tuple(c for c in obs.checks if c.name != "SOL_BURN_UNSPENDABLE") + (fail_check,)
        obs.verdict = invariants.apply_silence_rule(obs.checks)
        rendered = site.render(obs, now=2.0)
        self.assertIn('data-banner="sol-burn-failure"', rendered)
        start = rendered.index('data-banner="sol-burn-failure"')
        end = rendered.index("</section>", start)
        banner = rendered[start:end]
        self.assertNotIn("cannot be changed by anyone but pump", banner)
        self.assertIn("can still be changed by its own admin", banner)

    def test_no_recorded_burns_says_so_without_claiming_burns_never_walked(self):
        obs = _other_coin_observation(burn_events=[])
        rendered = site.render(obs, now=2.0)
        start = rendered.index('id="quiet"')
        end = rendered.index("</section>", start)
        quiet_section = rendered[start:end]
        self.assertIn("No burn is recorded against this coin's history", quiet_section)

        start = rendered.index('id="log"')
        end = rendered.index("</section>", start)
        log_section = rendered[start:end]
        self.assertIn("No burn is recorded against this mint yet", log_section)

    def test_forbidden_phrase_sweep_still_passes_over_both_sentinel_fixtures(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            pass_observation = build_all_publishable_sentinel_observation(evidence)
            evidence.close()
        fail_observation = build_observation()
        for observation in (fail_observation, pass_observation):
            rendered = site.render(observation, now=2.0).lower()
            for phrase in FORBIDDEN_PHRASES:
                self.assertNotIn(phrase, rendered)


class TestCoverageStatement(unittest.TestCase):
    """D-32 as revised by D-35. The sentence states counts it can back and
    refuses the two shapes that would smuggle back a denominator the project
    stopped measuring when the chain-wide sweep was cut.

    The figure-name constraint is enforced HERE, where the sentence is
    written, not only by the landing page's document-wide test: the moment
    this sentence renders only on the index, that test stops covering it and
    the constraint would silently lapse.
    """

    def test_no_invariants_figures_name_appears(self):
        sentence = site.coverage_statement({"observed": 12, "failed": 3}).lower()
        for name in invariants.FIGURES:
            with self.subTest(figure=name):
                self.assertNotIn(name, sentence)

    def test_is_computed_not_a_constant(self):
        a = site.coverage_statement({"observed": 12, "failed": 3})
        b = site.coverage_statement({"observed": 1})
        self.assertNotEqual(a, b)

    def test_states_the_counts_it_is_given(self):
        sentence = site.coverage_statement({"observed": 1234, "failed": 7})
        self.assertIn("1,234", sentence)
        self.assertIn("7", sentence)

    def test_missing_counts_read_as_zero_rather_than_raising(self):
        """A partial dict must not crash a render; an absent count is
        honestly zero, not an exception on a public surface.
        """
        self.assertIn("0", site.coverage_statement({}))

    def test_no_failed_clause_when_nothing_failed(self):
        self.assertNotIn("failed", site.coverage_statement({"observed": 5}))

    def test_never_claims_a_coin_was_submitted(self):
        """The count is coins with a committed record. The reference coin has
        one and nobody submitted it, so the sentence said "1 coin submitted
        and observed" on the live site while asserting a request that never
        happened. It states observation, which is what it can back.
        """
        for counts in ({"observed": 1}, {"observed": 9, "failed": 2}):
            with self.subTest(counts=counts):
                self.assertNotIn("submitted", site.coverage_statement(counts))

    def test_a_denominator_handed_in_is_never_rendered(self):
        """D-35, and the reason this test exists rather than a comment: the
        sweep is gone, so `enumerated` and `prospects` are numbers nobody
        measures any more. If a future edit routes them back into this
        sentence, no other test in the suite would notice — the page would
        simply start making a claim with nothing behind it.
        """
        sentence = site.coverage_statement(
            {"observed": 4, "enumerated": 603345, "multi_shareholder": 16871,
             "prospects": 2582, "measured": 99}
        )
        for smuggled in ("603,345", "603345", "16,871", "2,582", "99"):
            with self.subTest(value=smuggled):
                self.assertNotIn(smuggled, sentence)
        self.assertIn("4", sentence)

    def test_states_no_percentage_and_no_of_construction(self):
        """The two shapes that reintroduce a denominator without naming one:
        "N of M" and "X%".
        """
        for counts in ({"observed": 4}, {"observed": 4, "failed": 2}, {}):
            sentence = site.coverage_statement(counts)
            with self.subTest(counts=counts):
                self.assertNotIn("%", sentence)
                self.assertNotRegex(sentence, r"\d+\s+of\s+\d+")

    def test_says_plainly_that_it_is_not_a_census(self):
        """The sentence has to carry its own limit. Without this a reader
        counts the rows and reasonably concludes that is every coin.
        """
        self.assertIn("not a census", site.coverage_statement({"observed": 4}))


class TestSubmitIssueUrl(unittest.TestCase):
    """The pre-filled issue `/verify` links to must be recognised by the
    thing that reads the queue.

    `site` cannot import `intake` -- `intake` imports `site`, so it would be
    circular -- and duplicates the two markers as plain strings. That is the
    `EVIDENCE_EXPORT_PATH` pattern, and it has the same failure mode: if the
    copies drift, every submission made through this link is silently dropped
    as not-a-submission. This is the test that makes the duplication safe.
    """

    def test_markers_match_intakes(self):
        from indexer import intake
        self.assertEqual(site._SUBMISSION_MARKER, intake.SUBMISSION_MARKER)
        self.assertEqual(site._SUBMISSION_TITLE_PREFIX, intake.SUBMISSION_TITLE_PREFIX)

    def test_the_url_it_builds_round_trips_through_is_submission(self):
        from indexer import intake
        import urllib.parse
        query = urllib.parse.parse_qs(urllib.parse.urlparse(site.submit_issue_url()).query)
        issue = {"title": query["title"][0], "body": query["body"][0], "labels": []}
        self.assertTrue(intake.is_submission(issue))

    def test_sets_no_label(self):
        """GitHub drops a `labels` parameter for anyone without triage
        permission -- every stranger this queue exists for. Relying on one
        would lose exactly the submissions that matter.
        """
        self.assertNotIn("labels=", site.submit_issue_url())


class TestEnrollIsReachable(unittest.TestCase):
    """A page nobody can reach is not shipped. /verify was advertised in a
    post before its route existed once already; this is the same failure in
    the other direction -- a route that exists and nothing links to.
    """

    def test_the_landing_page_offers_it(self):
        h = site.render_landing(_counters_fixture(), now=1)
        self.assertIn('href="/enroll"', h)

    def test_the_verify_page_offers_it(self):
        self.assertIn('href="/enroll"', site.render_verify(now=1))

    def test_the_route_is_declared(self):
        data = json.loads(VERCEL_JSON.read_text(encoding="utf-8"))
        sources = {r["source"] for r in data["rewrites"]}
        self.assertIn("/enroll", sources)


class TestPasteBoxIsReachableAndStyled(unittest.TestCase):
    """The box is the product. Two ways it has already been broken: the
    landing page carried no route to it at all while a post was pointing
    traffic at exactly that, and it was added to a page whose stylesheet had
    never carried its rules, which renders it as bare unstyled inputs.
    """

    def _landing(self):
        return site.render_landing(_counters_fixture(), now=1)

    def test_the_landing_page_carries_the_box(self):
        h = self._landing()
        self.assertIn('action="/verify"', h)
        self.assertIn('name="mint"', h)

    def test_every_surface_that_shows_the_box_also_styles_it(self):
        for name in ("_STYLE", "_LANDING_STYLE", "_INDEX_STYLE"):
            with self.subTest(stylesheet=name):
                self.assertIn(".verify-form", getattr(site, name))

    def test_every_page_declares_a_viewport(self):
        """Without it a phone lays the page out near 980px and scales down,
        so every figure arrives too small to read.
        """
        pages = {
            "landing": self._landing(),
            "verify": site.render_verify(now=1),
            "not_found": site.render_not_found(now=1),
            "coin": site.render(build_observation()),
        }
        for name, html in pages.items():
            with self.subTest(page=name):
                self.assertIn('name="viewport"', html)
                self.assertIn("width=device-width", html)

    def test_every_page_carries_a_social_card(self):
        """A link posted without these renders as a bare URL and reads as a
        dead site. Posting a link is this site's primary route in.
        """
        pages = (self._landing(), site.render_verify(now=1), site.render(build_observation()))
        for i, html in enumerate(pages):
            with self.subTest(page=i):
                self.assertIn('property="og:image"', html)
                self.assertIn('name="twitter:card"', html)
                self.assertIn(site.META_IMAGE_SRC, html)


class TestVerifyPage(unittest.TestCase):
    def test_states_that_it_answers_for_any_coin(self):
        """It used to say the opposite, truthfully, before the live route
        existed. Once /verify started answering for any CA that sentence
        became a lie printed directly above a box that disproves it.
        """
        rendered = site.render_verify(now=1)
        self.assertIn("answers for any coin", rendered)
        self.assertNotIn("only answers for coins that have been measured", rendered)

    def test_says_what_submitting_is_actually_for(self):
        """Submitting no longer buys you an answer -- the live route gives one
        free. It buys a COMMITTED page backed by recorded evidence, which is a
        different and still-real thing.
        """
        rendered = site.render_verify(now=1)
        self.assertIn("committed", rendered)

    def test_links_the_submission_issue_and_the_index(self):
        rendered = site.render_verify(now=1)
        self.assertIn(site.submit_issue_url().replace("&", "&amp;"), rendered)
        self.assertIn(site.INDEX_FILENAME_TEMPLATE.format(page=1), rendered)

    def test_ships_no_script(self):
        self.assertNotIn("<script", site.render_verify(now=1))


class TestVerifyPasteBox(unittest.TestCase):
    """The route is only useful if a visitor can act on it.

    Before this, the page said "put a mint address after this URL" with no
    field to type into -- it asked people to hand-edit the address bar. The
    form is a plain GET; `vercel.json` redirects /verify?mint=... onto
    /verify/<mint>, so it works with no JavaScript at all and the visitor
    lands on the shareable address rather than a query string.
    """

    def test_has_a_form_and_an_input(self):
        h = site.render_verify(now=1)
        self.assertIn("<form", h)
        self.assertIn("<input", h)
        self.assertIn('method="get"', h)
        self.assertIn('action="/verify"', h)
        self.assertIn('name="mint"', h)

    def test_ships_no_script(self):
        self.assertNotIn("<script", site.render_verify(now=1))

    def test_input_accepts_only_base58(self):
        """A pasted CA is untrusted input. The browser-side pattern is
        convenience, not the boundary -- intake.validate_mint is -- but it
        stops the obvious paste mistakes before a round trip.
        """
        h = site.render_verify(now=1)
        self.assertIn("[1-9A-HJ-NP-Za-km-z]", h)

    def test_says_contract_address_not_only_mint(self):
        """A pump.fun user copies a thing labelled CA. A page that only says
        "mint address" makes them make that connection themselves.
        """
        h = site.render_verify(now=1).lower()
        self.assertIn("contract address", h)
        self.assertIn("ca", h)

    def test_worked_example_is_a_real_coin_when_one_exists(self):
        h = site.render_verify(now=1, example_mint="8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump")
        self.assertIn("8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump", h)

    def test_no_worked_example_rather_than_a_dead_one(self):
        """With nothing measured there is no example to give. Inventing one
        would point a visitor at a 404 on the page that exists to tell them
        what is measured.
        """
        h = site.render_verify(now=1)
        self.assertNotIn("Worked example", h)


class TestScanningPanel(unittest.TestCase):
    """The scanning gif on /verify.

    Its background is opaque near-black, so it sits inside a dark panel
    rather than being keyed to transparency: the art depicts a screen, and a
    keyed version would have eaten the outlines the way it would have on the
    hero sprite.
    """

    def test_present_and_sized_by_css_not_by_attribute(self):
        h = site.render_verify(now=1)
        self.assertIn(site.SCANNING_GIF_SRC, h)
        self.assertIn('class="scanner"', h)
        # intrinsic dimensions are declared so the box is reserved before the
        # image lands and the form below it does not jump
        w, hgt = site.SCANNING_GIF_INTRINSIC
        self.assertIn(f'width="{w}"', h)
        self.assertIn(f'height="{hgt}"', h)

    def test_does_not_displace_the_paste_box(self):
        h = site.render_verify(now=1)
        self.assertLess(h.index('class="scanner"'), h.index("<form"))
        self.assertIn('name="mint"', h)

    def test_still_ships_no_script(self):
        self.assertNotIn("<script", site.render_verify(now=1))


class TestLaunchModeAndResults(unittest.TestCase):
    """The two pump launch modes on the page, and the results chart.

    All three are observed facts about configuration -- what a coin IS, never
    how much moved -- so none may be a member of `invariants.FIGURES`.
    """

    def _obs(self, **kw):
        o = Observation(mint=CHARLIE, observed_at=1.0)
        o.checks = (
            invariants._check("A", invariants.PASS, [], "eq", "d"),
            invariants._check("B", invariants.FAIL, [], "eq", "d"),
            invariants._check("C", invariants.UNCHECKED, [], "eq", "d"),
            invariants._check("D", invariants.PASS, [], "eq", "d"),
        )
        for k, v in kw.items():
            setattr(o, k, v)
        return o

    def test_cashback_on_is_stated_plainly(self):
        h = site._launch_mode(self._obs(cashback=True))
        self.assertIn("Trader Cashback: on", h)

    def test_cashback_absent_is_unknown_not_off(self):
        """A curve predating the field has no byte to read. Absent is not
        off, and saying "off" would be an unbacked claim about a coin.
        """
        h = site._launch_mode(self._obs(cashback=None))
        self.assertIn("unknown", h)
        self.assertNotIn("Cashback: off", h)

    def test_charity_states_where_it_points_and_no_further(self):
        h = site._launch_mode(self._obs(
            cashback=False, charity_recipients=("Wallet1111",), donate_gg_fee_bps=1000))
        self.assertIn("Charity coin", h)
        self.assertIn("10%", h)
        self.assertIn("Wallet1111", h)
        self.assertIn("cannot tell you a charity received anything", h)

    def test_chart_counts_match_the_checks(self):
        h = site._results_chart(self._obs())
        self.assertIn("4 checks ran", h)
        self.assertIn("2 pass", h)
        self.assertIn("1 fail", h)
        self.assertIn("1 unchecked", h)

    def test_chart_states_every_count_in_text_too(self):
        """A chart nobody can read is decoration, and one that is the only
        place a number appears is worse.
        """
        h = site._results_chart(self._obs())
        self.assertIn("aria-label", h)
        self.assertNotIn("<script", h)

    def test_none_of_it_is_a_gated_figure(self):
        h = site._launch_mode(self._obs(cashback=True)) + site._results_chart(self._obs())
        for name in invariants.FIGURES:
            with self.subTest(figure=name):
                self.assertNotIn(name, h)


class TestNoFeeSplitPage(unittest.TestCase):
    """A coin whose creator is an ordinary wallet.

    Sampled against Dexscreener's trending Solana tokens on 2026-09-02: 12 of
    14 were in exactly this state, so it is the page MOST visitors see. It was
    rendering the failed-observation branch -- "No observation", "a tick that
    could not read the chain" -- which told them the tool was broken when the
    read had succeeded and had a definite answer.
    """

    def _obs(self):
        o = Observation(mint=CHARLIE, observed_at=1.0)
        o.error = (
            f"{CHARLIE}: its creator FZGxxhzHFDQMQqjjjkPNTzGpfbPWkYCXxqXgyRfijFuj "
            f"{pump.NO_FEE_SPLIT_MARKER} (it is an ordinary creator address). "
            "There is no split to report"
        )
        return o

    def test_marker_matches_the_message_pump_actually_raises(self):
        """site.py deliberately imports no chain decoder, so it carries its
        own copy of the phrase. This is what stops the copy from drifting
        away from the message pump raises and silently reverting every coin
        in this state back to the failure page.
        """
        self.assertEqual(site.NO_FEE_SPLIT_MARKER, pump.NO_FEE_SPLIT_MARKER)

    def test_states_the_finding_not_a_failure(self):
        h = site.render(self._obs())
        self.assertIn("does not split its creator fees", h)
        self.assertIn("not a failure here", h)

    def test_never_says_the_chain_could_not_be_read(self):
        """The exact wording that shipped, on the exact case it was wrong for."""
        h = site.render(self._obs())
        for wrong in ("No observation", "could not read the chain",
                      "failed observation"):
            with self.subTest(phrase=wrong):
                self.assertNotIn(wrong, h)

    def test_names_the_wallet_the_fee_goes_to(self):
        h = site.render(self._obs())
        self.assertIn("FZGxxhzHFDQMQqjjjkPNTzGpfbPWkYCXxqXgyRfijFuj", h)

    def test_offers_another_paste(self):
        h = site.render(self._obs())
        self.assertIn('action="/verify"', h)

    def test_a_real_read_failure_still_reads_as_one(self):
        """The other branch must survive. An RPC that never answered is not
        the same fact and must not borrow this page's reassuring voice.
        """
        o = Observation(mint=CHARLIE, observed_at=1.0)
        o.error = "connection reset by peer"
        h = site.render(o)
        self.assertIn("could not read the chain", h)
        self.assertNotIn("does not split its creator fees", h)


class TestNoSplitBreakdown(unittest.TestCase):
    """Saying a coin has no split states what is ABSENT. The page must also
    state what is happening, because that is the finding: every basis point of
    the creator fee goes to one ordinary wallet and none of it is burned.
    """

    def _obs(self, cashback=None):
        o = Observation(mint=CHARLIE, observed_at=1.0)
        o.error = f"{CHARLIE}: its creator FZGxx {pump.NO_FEE_SPLIT_MARKER} ..."
        o.error_kind = site.NO_SHARING_CONFIG
        o.creator = "FZGxxhzHFDQMQqjjjkPNTzGpfbPWkYCXxqXgyRfijFuj"
        o.cashback = cashback
        return o

    def test_shows_all_three_legs_including_the_zeroes(self):
        h = site.render(self._obs())
        for label in ("SOL burn", "Token burn", "To the creator"):
            with self.subTest(leg=label):
                self.assertIn(label, h)
        self.assertIn("100%", h)
        self.assertIn("0%", h)

    def test_says_plainly_that_nothing_is_burned(self):
        self.assertIn("Nothing is burned", site.render(self._obs()))

    def test_names_the_wallet_that_receives_all_of_it(self):
        h = site.render(self._obs())
        self.assertIn("FZGxxhzHFDQMQqjjjkPNTzGpfbPWkYCXxqXgyRfijFuj", h)
        self.assertIn("All of it goes to", h)

    def test_the_chart_is_readable_without_seeing_it(self):
        """Every share is in text as well as bar length, and the svg carries
        the same numbers for a screen reader.
        """
        h = site.render(self._obs())
        self.assertIn("to the creator 100 percent", h)

    def test_cashback_is_scoped_to_pump_not_folded_into_the_split(self):
        """Cashback returns part of pump's fee to traders. It is a different
        pool, and folding it in would make '100% to the creator' wrong.
        """
        on = site.render(self._obs(cashback=True))
        self.assertIn("Trader Cashback is on", on)
        self.assertIn("outside the creator fee", on)
        self.assertNotIn("Trader Cashback is on", site.render(self._obs(cashback=False)))

    def test_it_is_not_published_as_the_gated_split_figure(self):
        """CONFIG_MINT and SPLIT_SUM cannot run without a config, so `split`
        stays withheld. This is an observed fact about a destination.
        """
        h = site.render(self._obs())
        self.assertNotIn('data-figure="split"', h)

    def test_error_kind_drives_it_rather_than_the_error_wording(self):
        o = self._obs()
        o.error = "some other phrasing entirely"
        self.assertIn("Where the creator fee goes", site.render(o))


class TestNotFoundPage(unittest.TestCase):
    """A pasted CA for an unmeasured coin was reaching Vercel's own 404:
    "The page could not be found" and a request id. The visitor did exactly
    what the site told them to do, so that page has to explain the real
    reason and give them the next step.
    """

    def test_points_a_lost_visitor_at_the_paste_box(self):
        """Reachable now only for paths that are not a valid CA at all -- a
        typo, a stale link. It must not still say "this coin has not been
        measured", which would be a claim about a coin nobody named.
        """
        h = site.render_not_found()
        self.assertIn("Nothing at this address", h)
        self.assertIn("answers for any contract address", h)
        self.assertNotIn("not been measured", h)

    def test_carries_a_paste_box_that_needs_no_javascript(self):
        h = site.render_not_found()
        self.assertIn('method="get"', h)
        self.assertIn('action="/verify"', h)
        self.assertIn('name="mint"', h)
        self.assertNotIn("<script", h)

    def test_does_not_claim_to_know_which_coin(self):
        """Static file, no script: the mint is in the address bar and cannot
        reach the copy. Better to say nothing than to render a placeholder.
        """
        h = site.render_not_found()
        for guess in ("that coin's", "this mint", "{mint}", "%s"):
            with self.subTest(token=guess):
                self.assertNotIn(guess, h)

    def test_written_at_the_filename_vercel_serves(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = site.write_not_found(tmp)
            self.assertEqual(path.name, "404.html")
            self.assertTrue(path.exists())


class TestNoEmDashes(unittest.TestCase):
    """No em dash anywhere in rendered copy.

    It is the single most recognisable tell of machine-written text, and a
    project whose whole claim is "we measured this ourselves" cannot afford
    to read as generated. Colons, commas and full stops do the same work.
    """

    def test_no_em_dash_in_any_rendered_surface(self):
        source = inspect.getsource(site)
        for token in ("—", "&mdash;"):
            with self.subTest(token=token):
                self.assertNotIn(token, source)


class TestTheLoopIsReadableWithNothingRunning(unittest.TestCase):
    """The flywheel shipped to production with no test of any kind.

    It is a decorative ring with an animated rider, and the mechanism it
    illustrates is the one claim the whole site rests on. So the thing that
    has to hold is not that the circle draws: it is that a visitor who gets
    no animation, no images, or a screen reader still reads the mechanism.
    """

    def page(self):
        return site.render_landing(_counters_fixture(), now=2.0)

    def test_the_loop_is_on_the_landing_page(self):
        self.assertIn('id="flywheel"', self.page())

    def test_the_four_steps_are_prose_rather_than_labels_on_the_ring(self):
        page = self.page()
        for step in ("Trading pays a creator fee",
                     "The fee buys the token, and the token is burned",
                     "Buying is volume, and volume pays more fees",
                     "A share reaches Solana&#x27;s incinerator"):
            with self.subTest(step=step):
                self.assertIn(step, page)

    def test_the_ring_is_hidden_from_a_screen_reader(self):
        """A screen reader gets the ordered list. Read aloud, the ring is a
        circle, two images and a coin: no mechanism at all.
        """
        page = self.page()
        stage = page[page.index('class="fly-stage"'):]
        self.assertTrue(stage.startswith('class="fly-stage" aria-hidden="true"'), stage[:80])

    def test_reduced_motion_stops_every_animation_it_starts(self):
        """Three elements animate. A `prefers-reduced-motion` block that
        stopped two of them would still spin Charlie round the ring for a
        reader who asked the operating system for stillness.
        """
        page = self.page()
        # Every rule that starts an animation, so the list below cannot fall
        # behind a fourth one somebody adds.
        animated = set(re.findall(r"\.(fly-[a-z]+)[^{}]*\{[^{}]*animation:[^{}]*\}", page))
        self.assertEqual(animated, {"fly-path", "fly-orbit", "fly-rider"})

        blocks = [b for b in re.findall(
            r"@media \(prefers-reduced-motion: reduce\) \{(.*?)\n\}", page, re.S)
            if "fly-" in b]
        self.assertEqual(len(blocks), 1, "the loop's reduced-motion block")
        stilled, = blocks
        for name in sorted(animated):
            with self.subTest(rule=name):
                self.assertIn(f".{name}", stilled)
        self.assertIn("animation: none", stilled)

    def test_every_image_it_loads_is_one_the_site_already_ships(self):
        """The hub and the rider are `<img src>`. A renamed asset leaves a
        broken image on the front page and nothing else fails.
        """
        loop = site._flywheel()
        srcs = set(re.findall(r'<img[^>]*src="([^"]+)"', loop))
        self.assertEqual(srcs, {site.CHARLIE_SRC, site.INCINERATOR_SMOKE_SRC,
                                site.INCINERATOR_STACK_SRC})
        for src in srcs:
            with self.subTest(src=src):
                self.assertTrue((ROOT / "web" / src.lstrip("/")).exists(), src)

    def test_the_decorative_images_are_not_announced(self):
        """`alt=""` on all three, because the list beside them says it in
        words. An alt text here would read the mechanism out twice.
        """
        for tag in re.findall(r"<img[^>]*>", site._flywheel()):
            with self.subTest(tag=tag):
                self.assertIn('alt=""', tag)

    def test_the_loop_comes_before_the_counters_it_explains(self):
        # From <main> only: the stylesheet names the counter class long
        # before the body does.
        body = self.page()
        body = body[body.index("<main>"):]
        self.assertLess(body.index('id="flywheel"'), body.index('class="counter-value"'))


class TestNoCounterfactualReachesACoinPage(unittest.TestCase):
    """The page once printed what a coin's recorded burns WOULD have destroyed
    had that SOL gone to a burn instead of buying tokens.

    Nine tests covered the shape of that section and every one of them kept
    passing after the section stopped being rendered, because they called the
    renderer directly. So the guard now runs the page a visitor actually gets
    and asserts the counterfactual is absent from it -- and the renderer is
    deleted, so there is nothing left to call directly.
    """

    def _page(self):
        observation = Observation(mint=CHARLIE, observed_at=1.0)
        observation.burn_events = [{"sol_spent": 17_584_506_254}]
        observation.burn_walk_complete = True
        return site.render(observation)

    def test_the_renderer_is_gone_rather_than_merely_unreferenced(self):
        # Unreachable code with passing tests behind it is how the section
        # survived its own removal for a fortnight.
        self.assertFalse(hasattr(site, "_deflation"))

    def test_the_page_states_no_hypothetical(self):
        page = self._page()
        self.assertNotIn("This did not happen", page)
        self.assertNotIn('id="deflation"', page)

    def test_the_measured_burn_section_survives_the_deletion(self):
        """The counterfactual was derived from real recorded burns, and those
        burns have their own section. Removing the hypothetical must not take
        the measurement with it.
        """
        self.assertIn('id="the-burn"', self._page())


if __name__ == "__main__":
    unittest.main()

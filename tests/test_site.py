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

from indexer import invariants, publish, site
from indexer.evidence import Evidence
from indexer.observe import Observation, observe

from test_indexer import CHARLIE, charlie_rpc  # noqa: E402
from test_publication import (  # noqa: E402
    build_all_blocked_sentinel_observation,
    build_all_publishable_sentinel_observation,
    build_observation,
    evidence_db,
)


class TestFigureRowOrder(unittest.TestCase):
    def test_figure_rows_render_in_invariants_figures_order(self):
        observation = build_observation()
        rendered = site.render(observation)
        positions = [rendered.index(f'data-figure="{name}"') for name in invariants.FIGURES]
        self.assertEqual(positions, sorted(positions), "figure rows are out of invariants.FIGURES order")


class TestWithheldFigureRow(unittest.TestCase):
    def test_withheld_figure_row_renders_the_word_withheld_and_no_value(self):
        distinctive_balance = 87_654_321
        observation = build_observation(seal_balance=distinctive_balance)
        rendered = site.render(observation)

        self.assertNotIn(invariants.SEAL_TOTAL, observation.verdict.publishable)
        start = rendered.index('data-figure="seal_total"')
        end = rendered.index("</div>", start)
        row = rendered[start:end]
        self.assertIn("withheld", row)
        self.assertNotIn(str(distinctive_balance), rendered)

    def test_withheld_figure_names_every_blocking_check_not_just_the_first(self):
        observation = build_observation()
        reasons = observation.verdict.blocked[invariants.SEAL_TOTAL]
        blocking_names = [name for name, _status, _detail in reasons]
        self.assertGreaterEqual(len(blocking_names), 2, "fixture must block seal_total with 2+ checks")

        rendered = site.render(observation)
        start = rendered.index('data-figure="seal_total"')
        end = rendered.index("</div>", start)
        row = rendered[start:end]
        for name in blocking_names:
            self.assertIn(name, row, f"blocking check {name!r} missing from the withheld seal_total row")


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
        malicious_name = "<b>SEAL_UNSPENDABLE</b> & friends"
        observation = build_observation()
        malicious_check = invariants.Check(
            name=malicious_name,
            status=invariants.FAIL,
            backs=(invariants.SEAL_TOTAL,),
            equation="n/a",
            detail="n/a",
        )
        observation.checks = observation.checks + (malicious_check,)
        observation.verdict = invariants.apply_silence_rule(observation.checks)

        rendered = site.render(observation)
        self.assertNotIn(malicious_name, rendered, "raw markup leaked into the rendered page unescaped")
        self.assertIn("&lt;b&gt;SEAL_UNSPENDABLE&lt;/b&gt; &amp; friends", rendered)


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


class TestSealFailureBanner(unittest.TestCase):
    """02-02 Task 2, PUB-03: the Seal Failure Banner -- unconditional on
    SEAL_UNSPENDABLE: FAIL, above every other section, carrying the check's
    own `detail` verbatim, and naming no seal total anywhere on the page.
    """

    def test_banner_present_with_the_check_detail_verbatim_for_a_fail_observation(self):
        observation = build_observation()
        check = next(c for c in observation.checks if c.name == "SEAL_UNSPENDABLE")
        self.assertEqual(check.status, invariants.FAIL)

        rendered = site.render(observation, now=2.0)
        self.assertIn('data-banner="seal-failure"', rendered)
        self.assertIn(check.detail, rendered)

    def test_banner_absent_for_a_pass_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            observation = build_all_publishable_sentinel_observation(evidence)
            evidence.close()

        check = next(c for c in observation.checks if c.name == "SEAL_UNSPENDABLE")
        self.assertEqual(check.status, invariants.PASS)

        rendered = site.render(observation, now=2.0)
        self.assertNotIn('data-banner="seal-failure"', rendered)

    def test_no_seal_lamports_value_appears_anywhere_for_a_fail_observation(self):
        distinctive_balance = 91_234_567
        observation = build_observation(seal_balance=distinctive_balance)
        rendered = site.render(observation, now=2.0)
        self.assertNotIn(str(distinctive_balance), rendered)

    def test_banner_renders_above_the_figures_section(self):
        observation = build_observation()
        rendered = site.render(observation, now=2.0)
        banner_pos = rendered.index('data-banner="seal-failure"')
        figures_pos = rendered.index('id="figures"')
        self.assertLess(banner_pos, figures_pos)

    def test_freshness_renders_above_the_banner(self):
        observation = build_observation()
        rendered = site.render(observation, now=2.0)
        freshness_pos = rendered.index('class="freshness"')
        banner_pos = rendered.index('data-banner="seal-failure"')
        self.assertLess(freshness_pos, banner_pos)

    def test_detail_over_400_characters_renders_in_full_with_no_ellipsis(self):
        long_detail = "x" * 450
        malicious_check = invariants.Check(
            name="SEAL_UNSPENDABLE",
            status=invariants.FAIL,
            backs=(invariants.SEAL_TOTAL,),
            equation="n/a",
            detail=long_detail,
        )
        observation = build_observation()
        observation.checks = tuple(
            c for c in observation.checks if c.name != "SEAL_UNSPENDABLE"
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


if __name__ == "__main__":
    unittest.main()

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

from test_publication import (  # noqa: E402
    build_all_blocked_sentinel_observation,
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

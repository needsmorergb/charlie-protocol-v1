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
    def test_exactly_seven_risk_entries(self):
        observation = build_observation()
        rendered = site.render(observation, now=2.0)
        start = rendered.index('id="risks"')
        end = rendered.index("</section>", start)
        risks_html = rendered[start:end]
        self.assertEqual(risks_html.count("<li"), 7)

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
    "the seal burned",
    "burned into the seal",
    "sealed and burned",
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


if __name__ == "__main__":
    unittest.main()

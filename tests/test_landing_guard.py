"""A published page is never replaced by one that knows less than it did.

Two commands write over live artifacts, and both had the same hole.

`intake` regenerates `index.html` on every run, from the chain. The failure
mode this guards is quiet: when every chain read comes back empty, the
landing page still renders, still writes, and still commits -- with six
counters reading "unknown" where six live figures were. Nothing raises,
nothing fails, and the front door of the site goes blank.

Measured, not supposed: rendering the real committed evidence store with no
RPC reachable produced exactly that page, and the run reported success.

So `--require-counters` exists for the publishing job, and this drives the
command-line entry point rather than the renderer, because the refusal has
to happen before `write_landing` is called and after the observation is
built -- a window only the CLI can get wrong.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from indexer import cli, site  # noqa: E402
from indexer.observe import Observation  # noqa: E402

from test_indexer import CHARLIE  # noqa: E402
from test_site import _counters_fixture  # noqa: E402


class TestWhichCountersHaveNothingToShow(unittest.TestCase):
    def test_a_full_observation_is_missing_nothing(self):
        self.assertEqual(site.unknown_counters(_counters_fixture()), ())

    def test_an_observation_that_read_nothing_is_missing_all_six(self):
        blank = Observation(mint=CHARLIE, observed_at=1.0)
        missing = site.unknown_counters(blank)
        self.assertEqual(len(missing), 6)
        self.assertIn("Supply remaining", missing)

    def test_it_names_only_the_counters_that_are_absent(self):
        """A partial read is the dangerous one: five real figures and one
        "unknown" looks like a working page.
        """
        partial = _counters_fixture()
        partial.mint_state = None
        self.assertEqual(site.unknown_counters(partial), ("Supply remaining",))


class CliCase(unittest.TestCase):
    """Drives `cli.main` with the observation stubbed, because the thing being
    tested is what the CLI does with one, not how it builds one."""

    def run_site(self, observation, *flags):
        out = Path(tempfile.mkdtemp())
        argv = ["site", "--write", "--landing", "--out", str(out),
                "--evidence", str(out / "evidence.db"), CHARLIE, *flags]
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "observe", return_value=observation), \
             mock.patch.object(cli, "RpcClient"), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(argv)
        return code, out, stdout.getvalue(), stderr.getvalue()

    def blank(self):
        return Observation(mint=CHARLIE, observed_at=1.0)


class TestThePublishingJobRefusesABlankPage(CliCase):
    def test_it_exits_non_zero(self):
        code, _out, _o, _e = self.run_site(self.blank(), "--require-counters")
        self.assertEqual(code, 1)

    def test_it_writes_nothing_at_all(self):
        """Not just no index.html: the coin page is written first in the
        ordinary path, so a refusal placed after it would still leave a
        half-published run for the commit step to pick up.
        """
        _code, out, _o, _e = self.run_site(self.blank(), "--require-counters")
        written = sorted(p.name for p in out.iterdir() if p.suffix in (".html", ".json"))
        self.assertEqual(written, [])

    def test_it_says_which_counters_and_why(self):
        _code, _out, _o, err = self.run_site(self.blank(), "--require-counters")
        self.assertIn("Supply remaining", err)
        self.assertIn("did not come back", err)

    def test_a_complete_observation_is_published_as_usual(self):
        code, out, stdout, _e = self.run_site(_counters_fixture(), "--require-counters")
        self.assertEqual(code, 0)
        self.assertIn(site.LANDING_FILENAME, {p.name for p in out.iterdir()})
        self.assertIn("wrote", stdout)

    def test_without_the_flag_a_blank_page_still_writes(self):
        """Rendering offline is how the page is worked on. The refusal belongs
        to the job that publishes, not to everybody.
        """
        code, out, _o, _e = self.run_site(self.blank())
        self.assertEqual(code, 0)
        self.assertIn(site.LANDING_FILENAME, {p.name for p in out.iterdir()})


class TestStaleCoinPagesAreRefreshed(CliCase):
    """A committed coin page is a snapshot of a renderer that has since moved.

    Twice now that left a live page contradicting itself: a risk line reading
    "SOL_BURN_UNSPENDABLE fails permanently for this coin" printed above a
    check row on the same page reading PASS. Nothing regenerated a coin page
    unless its submission was measured again, and a submission is measured
    once. `--refresh` re-renders what is already on disk.
    """

    def out_with_a_page(self) -> Path:
        out = Path(tempfile.mkdtemp())
        observation = _counters_fixture()
        site.write(observation, out)
        return out

    def run_intake(self, out: Path, observation, *flags):
        argv = ["intake", "--repo", "owner/repo", "--out", str(out),
                "--evidence", str(out / "evidence.db"), *flags]
        stdout = io.StringIO()
        with mock.patch.object(cli.intake, "open_issues", return_value=[]), \
             mock.patch.object(cli, "observe", return_value=observation), \
             mock.patch.object(cli, "RpcClient"), \
             redirect_stdout(stdout):
            code = cli.main(argv)
        return code, stdout.getvalue()

    def test_a_committed_page_is_rendered_again(self):
        out = self.out_with_a_page()
        page = out / f"{CHARLIE}.html"
        page.write_text("a page an older renderer wrote", encoding="utf-8")
        code, output = self.run_intake(out, _counters_fixture(), "--refresh")
        self.assertEqual(code, 0)
        self.assertIn("refreshed", output)
        self.assertNotEqual(page.read_text(encoding="utf-8"),
                            "a page an older renderer wrote")

    def test_without_the_flag_nothing_on_disk_is_touched(self):
        out = self.out_with_a_page()
        page = out / f"{CHARLIE}.html"
        page.write_text("left alone", encoding="utf-8")
        self.run_intake(out, _counters_fixture())
        self.assertEqual(page.read_text(encoding="utf-8"), "left alone")

    def test_a_record_naming_a_path_is_skipped_rather_than_written(self):
        """A mint read off disk is not more trustworthy than one read off a
        GitHub issue: `site.write` composes a filename from it. The submission
        path has validated for exactly this since it was written; the refresh
        path went straight to the renderer, and `../PWNED` in a committed
        record wrote outside `--out`.
        """
        out = self.out_with_a_page()
        escape = Path(tempfile.mkdtemp())
        (out / "evil.json").write_text(
            json.dumps({"mint": f"../{escape.name}/escaped"}), encoding="utf-8")
        code, output = self.run_intake(out, _counters_fixture(), "--refresh")
        self.assertEqual(code, 0)
        self.assertIn("not one", output)
        # The directory the traversal aimed at, and only it: /tmp carries
        # everyone else's leavings.
        self.assertEqual(list(escape.iterdir()), [])

    def test_a_chain_that_could_not_be_read_leaves_the_page_alone(self):
        """The refresh must not turn a measured page into an error page
        because an endpoint was down for the minute the job ran.
        """
        out = self.out_with_a_page()
        page = out / f"{CHARLIE}.html"
        page.write_text("the good page", encoding="utf-8")
        failed = Observation(mint=CHARLIE, observed_at=1.0, error="RPC unavailable")
        code, output = self.run_intake(out, failed, "--refresh")
        self.assertEqual(code, 0)
        self.assertIn("kept", output)
        self.assertIn("RPC unavailable", output)
        self.assertEqual(page.read_text(encoding="utf-8"), "the good page")


class TestThePublishedCommandAsksForIt(unittest.TestCase):
    """The flag is worth nothing if the job that publishes does not pass it.

    An earlier version of this class checked that PUBLISHING.md mentioned the
    flags, which is prose about a job rather than the job. The job that
    publishes the deployed site lives in the OTHER repository, so it is
    checked when a checkout of it is on hand, and this repository's own
    publishing workflow is checked always.
    """

    def test_publishing_documents_the_flags(self):
        text = (ROOT / "PUBLISHING.md").read_text(encoding="utf-8")
        self.assertIn("--require-counters", text)
        self.assertIn("--refresh", text)

    def test_this_repository_s_publish_workflow_passes_them(self):
        workflow = (ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
        self.assertIn("--require-counters", workflow)
        self.assertIn("indexer refresh", workflow)

    @unittest.skipUnless(os.environ.get("CHARLIE_SITE_REPO"),
                         "set CHARLIE_SITE_REPO to check the deployed job itself")
    def test_the_deployed_job_passes_them(self):
        site_repo = Path(os.environ["CHARLIE_SITE_REPO"])
        workflow = (site_repo / ".github" / "workflows" / "intake.yml").read_text(encoding="utf-8")
        self.assertIn("--require-counters", workflow)
        self.assertIn("--refresh", workflow)
        # And loads the record before it renders anything from it.
        self.assertLess(workflow.index("indexer load"), workflow.index("--require-counters"))


if __name__ == "__main__":
    unittest.main()

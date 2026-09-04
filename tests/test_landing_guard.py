"""The landing page will not be replaced by a page that knows nothing.

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


class TestThePublishedCommandAsksForIt(unittest.TestCase):
    """The flag is worth nothing if the job that publishes does not pass it,
    and that job's command is documented here as the one to run."""

    def test_publishing_documents_the_flag(self):
        text = (ROOT / "PUBLISHING.md").read_text(encoding="utf-8")
        self.assertIn("--require-counters", text)


if __name__ == "__main__":
    unittest.main()

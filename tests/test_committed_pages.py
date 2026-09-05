"""The pages committed under `web/` are what the current renderer produces.

The gap this closes was found by watching it happen. `SOL_BURN_UNSPENDABLE`'s
equation was corrected in the source, and the deployed `web/` kept serving the
retracted one until a scheduled job happened to regenerate that page. Nothing
failed in between, in either repository, because nothing compares a committed
artifact against the code that writes it.

Only the pages that need no chain read are checked here -- `/verify`, `/404`
and `/enroll` are the same bytes on every machine, so a difference is always
staleness and never a fresh measurement. A coin page and the landing page are
observations; they are the publishing jobs' business, not this file's.

The generation timestamp is masked, and only that.
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from indexer import enroll_page, site  # noqa: E402
from indexer.cli import _index_inputs  # noqa: E402

# The one thing that legitimately differs between two renders of the same page.
TIMESTAMP = re.compile(r"generated at [^<]*")


def mask(page: str) -> str:
    return TIMESTAMP.sub("", page)


def static_pages(web: Path) -> dict:
    """Filename -> the bytes the current code would write there.

    `/verify` names a worked example chosen from the records on disk, exactly
    as `cli._write_index` chooses it, so this reads the same directory rather
    than pinning a mint that would outlive the record it points at.
    """
    records, _known = _index_inputs(web)
    example = records[0].get("mint") if records else None
    return {
        site.VERIFY_FILENAME: site.render_verify(example_mint=example),
        site.NOT_FOUND_FILENAME: site.render_not_found(),
        enroll_page.ENROLL_FILENAME: enroll_page.render(),
    }


class PagesCase(unittest.TestCase):
    def assertPagesAreCurrent(self, web: Path):
        self.assertTrue(web.is_dir(), f"{web} is not a directory")
        for filename, rendered in static_pages(web).items():
            with self.subTest(page=filename):
                committed = web / filename
                self.assertTrue(committed.exists(), f"{filename} is not committed")
                self.assertEqual(
                    mask(committed.read_text(encoding="utf-8")),
                    mask(rendered),
                    f"{committed} is not what the current renderer produces. "
                    f"Regenerate it: `python -m indexer refresh --out {web}`.",
                )


class TestThisRepositorysPages(PagesCase):
    def test_the_committed_static_pages_are_current(self):
        self.assertPagesAreCurrent(ROOT / "web")


SITE_REPO = os.environ.get("CHARLIE_SITE_REPO")


@unittest.skipUnless(SITE_REPO, "set CHARLIE_SITE_REPO to check the deployed pages too")
class TestTheDeployedPages(PagesCase):
    """The ones a visitor actually loads. The staleness that prompted this was
    on the deployed copy, and this repository's own pages were fine."""

    def test_the_deployed_static_pages_are_current(self):
        self.assertPagesAreCurrent(Path(SITE_REPO) / "web")


if __name__ == "__main__":
    unittest.main()

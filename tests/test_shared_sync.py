"""The list of files that must be identical in the deploy repository.

`tools/shared_sync.py` can say whether the two copies match. That is only
worth having if the list it checks is complete, and a hand-written list of
imports is exactly the thing that falls behind -- which is how the drift it
found got there in the first place.

So the list is not trusted here. It is recomputed from the import graph of
what the deploy repository actually runs (`python -m indexer`, and the two
serverless functions Vercel invokes) and compared. Add a module that
`indexer/site.py` imports and this fails until the list names it.
"""

from __future__ import annotations

import ast
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from indexer.export import EXPORT_TABLES  # noqa: E402
from tools import shared_sync  # noqa: E402

# What the deploy repository starts from: `python -m indexer <cmd>` in its
# GitHub Actions, and the two functions Vercel serves.
ENTRY_POINTS = ("indexer/__main__.py", "api/enroll.py", "api/verify.py")

# Shared but not reachable by import: Vercel reads the routing table, and the
# evidence export is the record the deployed CI loads its working store from.
# Derived from `EXPORT_TABLES` rather than listed, so a new table added there
# has to be copied too.
NON_PYTHON = ("vercel.json",) + tuple(
    f"state/evidence/{table}.jsonl" for table, _order in EXPORT_TABLES
)


def _module_paths(source: str, origin: Path) -> set[str]:
    """The repository-relative paths a file's imports resolve to.

    Handles both forms the code uses: `from indexer import site` inside the
    package (relative, `level` 1) and `from indexer.rpc import RpcClient`
    from the API functions (absolute).
    """
    found = set()

    def add(dotted: str) -> None:
        as_module = ROOT / (dotted.replace(".", "/") + ".py")
        as_package = ROOT / dotted.replace(".", "/") / "__init__.py"
        if as_module.exists():
            found.add(str(as_module.relative_to(ROOT)))
        elif as_package.exists():
            found.add(str(as_package.relative_to(ROOT)))

    tree = ast.parse(source)
    package = origin.parent.relative_to(ROOT).as_posix().replace("/", ".")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = package if node.level else (node.module or "")
            if node.level and node.module:
                base = f"{package}.{node.module}"
            if not base:
                continue
            add(base)
            for alias in node.names:
                add(f"{base}.{alias.name}")
    return found


def closure() -> set[str]:
    seen: set[str] = set()
    queue = list(ENTRY_POINTS)
    while queue:
        path = queue.pop()
        if path in seen:
            continue
        seen.add(path)
        origin = ROOT / path
        for found in _module_paths(origin.read_text(encoding="utf-8"), origin):
            if found not in seen:
                queue.append(found)
    return seen


class TestTheListIsComplete(unittest.TestCase):
    def test_it_is_exactly_what_the_deploy_repository_runs(self):
        expected = closure() | set(NON_PYTHON)
        self.assertEqual(set(shared_sync.SHARED), expected)

    def test_the_entry_points_are_themselves_shared(self):
        for path in ENTRY_POINTS:
            with self.subTest(path=path):
                self.assertIn(path, shared_sync.SHARED)

    def test_every_listed_file_exists_here(self):
        for path in shared_sync.SHARED:
            with self.subTest(path=path):
                self.assertTrue((ROOT / path).exists(), path)

    def test_the_list_is_sorted_and_has_no_repeats(self):
        # A diffable list. Two entries for one path would hide a deletion.
        self.assertEqual(list(shared_sync.SHARED), sorted(set(shared_sync.SHARED)))

    def test_no_test_or_tool_leaks_into_the_deployed_copy(self):
        """The deploy repository has no tests and its own tools. Something
        reaching them from an entry point would mean the split is wrong.
        """
        for path in shared_sync.SHARED:
            with self.subTest(path=path):
                self.assertFalse(path.startswith(("tests/", "tools/")), path)


class TestTheComparisonItself(unittest.TestCase):
    def test_a_matching_tree_reports_nothing_wrong(self):
        self.assertEqual(shared_sync.compare(lambda path: shared_sync.ours(path)), [])

    def test_a_changed_byte_is_reported_as_a_difference(self):
        def fetch(path):
            body = shared_sync.ours(path)
            return body + b"\n" if path == "indexer/site.py" else body

        wrong = shared_sync.compare(fetch)
        self.assertEqual([path for path, _why in wrong], ["indexer/site.py"])
        self.assertIn("differs", wrong[0][1])

    def test_an_absent_file_is_reported_as_missing(self):
        def fetch(path):
            return None if path == "api/enroll.py" else shared_sync.ours(path)

        wrong = shared_sync.compare(fetch)
        self.assertEqual(wrong, [("api/enroll.py", "missing from the deploy repository")])


SITE_REPO = os.environ.get("CHARLIE_SITE_REPO")


@unittest.skipUnless(SITE_REPO, "set CHARLIE_SITE_REPO to a deploy checkout to compare against it")
class TestAgainstARealCheckout(unittest.TestCase):
    """Opt-in, because the deploy repository is not present on every machine.

    CI compares against GitHub instead (`--against-branch`), which needs no
    checkout at all -- see the deploy repository's `sync` workflow.
    """

    def test_every_shared_file_matches(self):
        directory = Path(SITE_REPO).resolve()
        self.assertTrue(directory.is_dir(), f"{directory} is not a directory")
        wrong = shared_sync.compare(lambda path: shared_sync.theirs_local(directory, path))
        self.assertEqual(wrong, [], f"the deploy copy has drifted:\n{wrong}")


if __name__ == "__main__":
    unittest.main()

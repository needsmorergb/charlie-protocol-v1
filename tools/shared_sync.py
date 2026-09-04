"""The files `charlie-protocol-site` runs, which live here.

Two repositories hold the same code. This one has the tests and the specs;
`charlie-protocol-site` is what Vercel deploys, and it carries a copy of the
indexer and the API so its functions and its GitHub Actions can run. Nothing
generated the copy and nothing compared it, so keeping the two in step was a
matter of somebody remembering.

Somebody stopped remembering. Found by running this comparison for the first
time, against production:

  * `indexer/site.py` -- the deployed copy had an entire animated section
    (the flywheel, ~200 lines) that this repository had never seen, and had
    stopped rendering a section this repository still rendered. Every one of
    every test here passed against neither.
  * `indexer/invariants.py` -- the deployed copy had rewritten what
    SOL_BURN_UNSPENDABLE means (a burn address passes; program derivation is
    no longer demanded) and added an UNCHECKED branch to SOL_BURN_BALANCE.
    The tests here still asserted the retracted behaviour, and passed,
    because they were testing a file production does not run.

So: this module names the files that must be identical, and can say whether
they are.

    python tools/shared_sync.py --against ../charlie-protocol-site
    python tools/shared_sync.py --against-branch claude/my-branch
    python tools/shared_sync.py --copy-to ../charlie-protocol-site

`--against` compares a local checkout. `--against-branch` compares whatever
GitHub is serving for that branch of the deploy repo, which is what CI uses
and needs no checkout. `--copy-to` writes this repository's copies over the
other's, which is the only supported direction: this repository is the one
with the tests.

Reads and writes files. Nothing signs, nothing is sent to a chain.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DEPLOY_REPO = "needsmorergb/charlie-protocol-site"
RAW = "https://raw.githubusercontent.com/{repo}/{ref}/{path}"

# Every module the deployed repository imports, plus the two serverless
# functions, the routing table, and the static assets every page loads.
# `tests/test_shared_sync.py` recomputes the module list from the import graph
# and fails if it has fallen behind, so a new module is not silently left out
# of the copy.
#
# NOT the evidence record. `state/evidence/*.jsonl` was seeded there from here
# -- without it the deployed landing page had no walk to read and every
# counter came out "unknown" -- but the deploy repository is where intake runs
# in production, so it measures rows this repository has never seen and writes
# its own export on every run. Requiring the two to match would fail the
# moment production measured anything. Code is shared; what production
# measured belongs to production.
SHARED = (
    "api/enroll.py",
    "api/verify.py",
    "indexer/__init__.py",
    "indexer/__main__.py",
    "indexer/base58.py",
    "indexer/cli.py",
    "indexer/coverage.py",
    "indexer/curve.py",
    "indexer/decode.py",
    "indexer/enroll.py",
    "indexer/enroll_page.py",
    "indexer/evidence.py",
    "indexer/export.py",
    "indexer/intake.py",
    "indexer/invariants.py",
    "indexer/legs.py",
    "indexer/observe.py",
    "indexer/publish.py",
    "indexer/pump.py",
    "indexer/reconcile.py",
    "indexer/report.py",
    "indexer/rpc.py",
    "indexer/scan.py",
    "indexer/site.py",
    "indexer/store.py",
    "vercel.json",
    "web/assets/charlie-found.gif",
    "web/assets/charlie-scanning.gif",
    "web/assets/charlie.png",
    "web/assets/incinerator-smoke.png",
    "web/assets/incinerator-stack.png",
    "web/assets/meta-image.png",
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


class Missing(Exception):
    """A shared file is absent from THIS repository."""


def ours(path: str) -> bytes:
    target = ROOT / path
    if not target.exists():
        # Reported, never raised out of `compare`: in the deploy
        # repository's CI "ours" is the deploy checkout, and a file this list
        # has gained but that checkout has not is exactly the drift being
        # looked for. It used to come out as a FileNotFoundError traceback.
        raise Missing(path)
    return target.read_bytes()


def theirs_local(directory: Path, path: str) -> bytes | None:
    target = directory / path
    return target.read_bytes() if target.exists() else None


def theirs_remote(ref: str, path: str, *, repo: str = DEPLOY_REPO) -> bytes | None:
    url = RAW.format(repo=repo, ref=ref, path=path)
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def compare(fetch) -> list[tuple[str, str]]:
    """`(path, what is wrong)` for every shared file that is not identical."""
    wrong = []
    for path in SHARED:
        try:
            mine = ours(path)
        except Missing:
            wrong.append((path, "missing from this repository"))
            continue
        yours = fetch(path)
        if yours is None:
            wrong.append((path, "missing from the other repository"))
        elif yours != mine:
            wrong.append((path, f"differs (here {digest(mine)}, there {digest(yours)})"))
    return wrong


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--against", metavar="DIR", help="compare against a local checkout")
    group.add_argument("--against-branch", metavar="REF",
                       help=f"compare against a branch of {DEPLOY_REPO} on GitHub")
    group.add_argument("--copy-to", metavar="DIR", help="write our copies over another checkout")
    parser.add_argument("--repo", default=DEPLOY_REPO, help=f"default {DEPLOY_REPO}")
    args = parser.parse_args(argv)

    if args.copy_to:
        target = Path(args.copy_to).resolve()
        for path in SHARED:
            source = ROOT / path
            if not source.exists():
                print(f"MISSING {path}", file=sys.stderr)
                return 1
            destination = target / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            print(f"copied {path}")
        return 0

    if args.against:
        directory = Path(args.against).resolve()
        wrong = compare(lambda path: theirs_local(directory, path))
        where = str(directory)
    else:
        wrong = compare(lambda path: theirs_remote(args.against_branch, path, repo=args.repo))
        where = f"{args.repo}@{args.against_branch}"

    if not wrong:
        print(f"all {len(SHARED)} shared files match {where}")
        return 0

    print(f"{len(wrong)} of {len(SHARED)} shared files do not match {where}:", file=sys.stderr)
    for path, why in wrong:
        print(f"  {path}: {why}", file=sys.stderr)
    print("\nRun `python tools/shared_sync.py --copy-to <deploy checkout>` from this "
          "repository, then commit there.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

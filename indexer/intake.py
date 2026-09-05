"""D-34's front door: D-23's request queue, promoted from a fallback for "a
mint with no page yet" to the product's only intake.

A coin is measured because someone opened an issue asking for it. There is
no allowlist, no account, no approval step and no sweep on any automatic
path (D-33/D-36) -- the open issues on the public site repository ARE the
queue, and anyone can add to it.

The read half needs no credential at all: `open_issues()` fetches the public
GitHub API over `urllib`, unauthenticated, and anyone can run it and get the
same answer we do. Only the write half -- posting a comment, closing an
issue -- needs `gh` and its logged-in credential, and it lives behind an
explicit operator action (`answer()`), so nothing in the deployment ever
holds a token.

The security boundary this module exists to hold: a submitted mint is
untrusted text that becomes a filesystem path (through `site._artifact_name`),
an RPC parameter, and a `gh` process argument. `validate_mint()` decodes it
to exactly 32 bytes and returns the value RECONSTRUCTED from those bytes --
never the string a stranger typed -- so a path separator, a parent-directory
reference, or any other metacharacter cannot survive the round trip.

Standard library only. Every function here returns data rather than
printing it -- `cli.py` owns every printed line, which is what keeps this
module out of the surface registry's way.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import site
from .base58 import decode as base58_decode, encode as base58_encode, pubkey_bytes
from .curve import is_on_curve
from .observe import observe as observe_coin
from .scan import scan_burns

USER_AGENT = "charlie-protocol-indexer/0.1"

GITHUB_API_ROOT = "https://api.github.com"
GH_EXECUTABLE = "gh"
VERIFY_ROUTE_PREFIX = "/verify/"
DEFAULT_REPO = "needsmorergb/charlie-protocol-site"

# -- bounds (T-03-03: denial of service via an unbounded queue) ------------
MAX_ISSUE_RESPONSE_BYTES = 2_000_000
MAX_LIVENESS_RESPONSE_BYTES = 4_096
MAX_SUBMISSION_BODY_CHARS = 8_000
MAX_CANDIDATE_TOKENS = 64
DEFAULT_ISSUE_PAGE_LIMIT = 50
DEFAULT_RUN_LIMIT = 5

# -- what counts as a submission (T-03-08: not label-only) -----------------
# GitHub ignores a `labels` parameter on an issue-creation URL for anyone
# without triage permission on the repository, so a label-only rule would
# silently drop every submission from an actual stranger -- the only kind
# this queue exists for. The label stays as the operator's manual escape
# hatch for triaging a freeform request into the queue by hand.
SUBMISSION_MARKER = "<!-- charlie-protocol:submission -->"
SUBMISSION_TITLE_PREFIX = "[coverage]"
SUBMISSION_LABEL = "coverage-request"

_TOKEN_SPLIT_RE = re.compile(r"[\s,;:'\"()\[\]{}<>|`*_#/\\]+")


class InvalidMint(ValueError):
    """A submitted string is not a mint this project can act on.

    `reason` is always a member of `REASONS` -- the vocabulary
    `record_submission` (03-02 Task 2) requires.
    """

    def __init__(self, message: str, *, reason: str):
        super().__init__(message)
        self.reason = reason


# -- COV-03's closed reason vocabulary, and its three dispositions ---------
# Terminal: the chain answered, and the answer is a fact about the coin. The
# issue is answered plainly and closed.
REASON_NOT_PUMP_COIN = "not_pump_coin"
REASON_NO_SHARING_CONFIG = "no_sharing_config"

# Correctable: the submitter can fix it by editing their own issue. Answered
# with what is wrong; left open, because closing it would read as an answer
# when nothing was measured.
REASON_NOT_BASE58 = "not_base58"
REASON_WRONG_LENGTH = "wrong_length"
REASON_OFF_CURVE = "off_curve"
REASON_AMBIGUOUS_MINT = "ambiguous_mint"
REASON_NO_MINT_FOUND = "no_mint_found"

# Transient: our side failed. No comment is posted, the issue is left open,
# and the row records the reason so the next run attempts it again --
# `scan.py`'s cursor-never-advances-past-a-failure property, expressed here
# as the absence of a completion record.
REASON_MINT_DECODE_FAILED = "mint_decode_failed"
REASON_RPC_UNAVAILABLE = "rpc_unavailable"
REASON_RPC_ERROR = "rpc_error"
REASON_CONFIG_MISMATCH = "config_mismatch"

TERMINAL = (REASON_NOT_PUMP_COIN, REASON_NO_SHARING_CONFIG)
CORRECTABLE = (
    REASON_NOT_BASE58,
    REASON_WRONG_LENGTH,
    REASON_OFF_CURVE,
    REASON_AMBIGUOUS_MINT,
    REASON_NO_MINT_FOUND,
)
TRANSIENT = (
    REASON_MINT_DECODE_FAILED,
    REASON_RPC_UNAVAILABLE,
    REASON_RPC_ERROR,
    REASON_CONFIG_MISMATCH,
)

# The closed vocabulary itself: `record_submission` (03-02 Task 2) raises
# for anything outside it. TERMINAL/CORRECTABLE/TRANSIENT partition it
# exactly -- a test asserts the partition, not just the membership.
REASONS = TERMINAL + CORRECTABLE + TRANSIENT


def reason_for(error) -> str:
    """The one mapping from a validation/observation failure to a member of
    `REASONS` -- never derived by matching text in an error message.

    `error` is either an `InvalidMint` (validation failed before a chain
    read was attempted) or an `observe.Observation` whose `error_kind` is
    set (a chain read failed). Raises `ValueError` if the resulting value is
    not a member of `REASONS`, so a reason this store cannot back fails
    loudly here rather than being recorded as a fact.
    """
    if isinstance(error, InvalidMint):
        candidate = error.reason
    else:
        candidate = getattr(error, "error_kind", None)
    if candidate not in REASONS:
        raise ValueError(f"{candidate!r} is not a member of intake.REASONS")
    return candidate


def validate_mint(text: str) -> str:
    """The security boundary. Decodes `text` with `base58.decode`, requires
    exactly 32 bytes via `base58.pubkey_bytes`, requires `curve.is_on_curve`
    to hold, and returns `base58.encode` of those bytes -- the value that
    flows onward to a filename, an RPC parameter and a process argument is
    one WE constructed from bytes, never the string a stranger typed.

    Raises `InvalidMint` for anything else: a character outside the base58
    alphabet (which is also what rejects a path separator, a parent-
    directory reference, or any other metacharacter -- none of them are
    valid base58 characters), a value that does not decode to exactly 32
    bytes, or a 32-byte value that is not on the ed25519 curve (an ordinary
    wallet or mint IS on-curve; only a program-derived address is not, and a
    submitted mint can never be one).
    """
    if not isinstance(text, str) or not text:
        raise InvalidMint("empty mint", reason=REASON_NOT_BASE58)
    try:
        raw = base58_decode(text)
    except ValueError as exc:
        raise InvalidMint(f"{text!r} is not valid base58: {exc}", reason=REASON_NOT_BASE58) from None
    try:
        raw = pubkey_bytes(raw)
    except ValueError as exc:
        raise InvalidMint(f"{text!r}: {exc}", reason=REASON_WRONG_LENGTH) from None
    if not is_on_curve(raw):
        raise InvalidMint(f"{text!r} is off the ed25519 curve -- not an ordinary address", reason=REASON_OFF_CURVE) from None
    rebuilt = base58_encode(raw)
    if rebuilt != text:
        # Provably unreachable for this base58 implementation (decode() is
        # injective over the strings that yield exactly 32 bytes -- see
        # 03-02-SUMMARY.md), kept anyway as the property the plan requires
        # and as a guard against a future change to base58.py loosening
        # that injectivity.
        raise InvalidMint(
            f"{text!r} is not the canonical encoding of its own bytes", reason=REASON_NOT_BASE58
        ) from None
    return rebuilt


def _label_names(issue: dict) -> set:
    names = set()
    for label in issue.get("labels") or []:
        if isinstance(label, dict):
            names.add(label.get("name"))
        else:
            names.add(label)
    return names


def is_submission(issue: dict) -> bool:
    """Marker in the body, OR title prefix, OR the operator's label -- never
    the label alone (T-03-08).
    """
    body = issue.get("body") or ""
    title = issue.get("title") or ""
    return (
        SUBMISSION_MARKER in body
        or title.startswith(SUBMISSION_TITLE_PREFIX)
        or SUBMISSION_LABEL in _label_names(issue)
    )


def submitted_mint(issue: dict) -> str:
    """The single valid mint named in `issue`'s title or body.

    The body is truncated to a bounded number of characters before
    scanning -- a hostile body is unbounded, and a bounded read is the only
    thing that makes the scan's cost predictable (T-03-03). Every candidate
    token is run through `validate_mint`; a token that fails is simply not a
    mint, not an error. Exactly one distinct valid mint is the answer.
    """
    title = issue.get("title") or ""
    body = (issue.get("body") or "")[:MAX_SUBMISSION_BODY_CHARS]
    text = f"{title}\n{body}"
    tokens = [token for token in _TOKEN_SPLIT_RE.split(text) if token][:MAX_CANDIDATE_TOKENS]

    found: list[str] = []
    for token in tokens:
        try:
            mint = validate_mint(token)
        except InvalidMint:
            continue
        if mint not in found:
            found.append(mint)

    if not found:
        raise InvalidMint(
            "no valid mint found in the issue title or body", reason=REASON_NO_MINT_FOUND
        )
    if len(found) > 1:
        raise InvalidMint(
            f"the issue names {len(found)} distinct valid mints ({', '.join(found)}) -- "
            "one coin per issue is the rule",
            reason=REASON_AMBIGUOUS_MINT,
        )
    return found[0]


def open_issues(repo: str, *, opener=urllib.request.urlopen, limit: int = DEFAULT_ISSUE_PAGE_LIMIT) -> list:
    """The public issues endpoint for `repo`, unauthenticated, over
    `urllib`. Reads at most a bounded number of bytes, drops pull requests
    (the issues endpoint returns them too, and a pull request is not a
    submission), and returns an empty list -- never raises -- when the body
    is not JSON or the request fails: an unreachable queue is a run with
    nothing to do, not a crash.

    `opener` is a parameter so tests drive this with a stub and never touch
    the network.
    """
    url = f"{GITHUB_API_ROOT}/repos/{repo}/issues?state=open&per_page={int(limit)}"
    request = urllib.request.Request(
        url,
        headers={
            "user-agent": USER_AGENT,
            "accept": "application/vnd.github+json",
        },
        method="GET",
    )
    try:
        with opener(request, timeout=15) as response:
            raw = response.read(MAX_ISSUE_RESPONSE_BYTES)
        payload = json.loads(raw.decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return []

    if not isinstance(payload, list):
        return []

    issues = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        if "pull_request" in entry:
            continue
        issues.append(
            {
                "number": entry.get("number"),
                "title": entry.get("title"),
                "body": entry.get("body"),
                "html_url": entry.get("html_url"),
                "labels": entry.get("labels") or [],
            }
        )
    return issues


def verdict_url(mint: str, site_url: str) -> str:
    """Composes the deployed site's verify route onto the validated mint and
    nothing else.
    """
    return site_url.rstrip("/") + VERIFY_ROUTE_PREFIX + mint


def _response_status(response) -> int | None:
    status = getattr(response, "status", None)
    if status is not None:
        return status
    getcode = getattr(response, "getcode", None)
    return getcode() if callable(getcode) else None


def is_live(url: str, *, opener=urllib.request.urlopen) -> bool:
    """True only on a 200. Catches every exception and answers False rather
    than raising -- a link this project publishes is checked exactly as
    hard as a figure it publishes (T-03-09).
    """
    try:
        request = urllib.request.Request(url, headers={"user-agent": USER_AGENT}, method="GET")
        with opener(request, timeout=15) as response:
            response.read(MAX_LIVENESS_RESPONSE_BYTES)
            return _response_status(response) == 200
    except Exception:
        return False


# -- the reply: the one place a GitHub write happens -----------------------
_SUCCESS_COMMENT_TEMPLATE = (
    "Measured: {mint}\n\n"
    "Verdict: {verdict_url}\n\n"
    "This comment was posted automatically by Charlie Protocol's intake run. "
    "The page above is a committed, recomputable record -- not a live answer."
)


def comment_body(mint: str, verdict_url: str) -> str:
    """Built from a literal template plus the validated mint plus the
    verdict URL and nothing else: no environment value, no local path and
    no endpoint list may reach a public comment (T-03-05).
    """
    return _SUCCESS_COMMENT_TEMPLATE.format(mint=mint, verdict_url=verdict_url)


def answer(repo: str, issue_number, *, body: str, close: bool, runner=subprocess.run) -> None:
    """The one place a GitHub write happens. Argument list only, never a
    shell string (T-03-02); the executable is resolved through
    `shutil.which` first (the Windows shim trap CLAUDE.md already records);
    the issue number is coerced to an integer before it becomes an argument,
    so whatever odd text a stranger's issue payload carried at that
    position can never reach argv unchanged.
    """
    gh_path = shutil.which(GH_EXECUTABLE)
    if gh_path is None:
        raise RuntimeError("gh executable not found on PATH -- cannot answer a submission")
    issue_number = int(issue_number)

    comment_args = [gh_path, "issue", "comment", str(issue_number), "--repo", repo, "--body", body]
    runner(comment_args, check=True)

    if close:
        close_args = [gh_path, "issue", "close", str(issue_number), "--repo", repo]
        runner(close_args, check=True)


@dataclass
class Outcome:
    """One issue's result from a `run()` call. `reason` is a member of
    `REASONS` when `observed` is False, and `None` when it is True.
    """

    issue_number: int | None
    issue_url: str | None
    mint: str | None
    observed: bool
    reason: str | None = None
    verdict_url: str | None = None


# Wall-clock budget for one submission's burn walk. A submission queue runs
# on a schedule with a job timeout; an unbounded walk is not a slow answer, it
# is no answer for every coin behind it. 45s walks a long way and still lets a
# five-submission run finish inside a few minutes.
DEFAULT_SCAN_SECONDS = 45.0


def run(
    issues,
    rpc,
    registry,
    evidence,
    out_dir,
    *,
    repo: str = DEFAULT_REPO,
    site_url: str | None = None,
    limit: int = DEFAULT_RUN_LIMIT,
    scan_seconds: float = DEFAULT_SCAN_SECONDS,
    now=None,
) -> list:
    """Measures, writes and records (03-02 Task 2's three verbs). Submissions
    only (T-03-08's rule -- an issue that is none of the three markers is
    left completely alone), oldest first, up to `limit` attempted. An issue
    over the cap gets no row, no comment and no attempt at all (T-03-03) --
    it is simply absent from both `attempted` and the returned outcomes; a
    cap is not a skip, and recording a row for every unreached issue would
    bury the failures COV-03 exists to make visible.

    Two issues naming the same mint cost one observation; both outcomes
    point at the same verdict, and BOTH still get their own submission row
    -- the row tracks a request (repo, issue, attempt time), not a coin.

    A submitted coin is a coin with a reader (D-31): `site.write()` -- both
    the page and the record -- is called only on success.

    `evidence.record_submission()` is called once per attempted issue, for
    every disposition, when `evidence` is not `None` -- the append-only row
    COV-03 requires. `evidence=None` (used by callers that only want the
    in-memory outcomes, e.g. a dry run) skips recording entirely rather than
    raising, matching `observe()`'s own convention for an absent evidence
    handle.
    """
    submissions = [issue for issue in issues if is_submission(issue)]
    submissions.sort(key=lambda issue: issue.get("number") or 0)
    attempted = submissions[: max(0, limit)]
    attempt_time = now() if callable(now) else (now if now is not None else time.time())

    outcomes: list[Outcome] = []
    by_mint: dict[str, Outcome] = {}

    def _record(*, number, mint, ok, reason) -> None:
        if evidence is None:
            return
        evidence.record_submission(
            repo=repo,
            issue_number=number,
            attempted_at=attempt_time,
            mint=mint,
            outcome="observed" if ok else "failed",
            reason=None if ok else reason,
        )

    for issue in attempted:
        number = issue.get("number")
        url = issue.get("html_url")

        try:
            mint = submitted_mint(issue)
        except InvalidMint as exc:
            reason = reason_for(exc)
            _record(number=number, mint=None, ok=False, reason=reason)
            outcomes.append(Outcome(issue_number=number, issue_url=url, mint=None, observed=False, reason=reason))
            continue

        if mint in by_mint:
            prior = by_mint[mint]
            _record(number=number, mint=mint, ok=prior.observed, reason=prior.reason)
            outcomes.append(
                Outcome(
                    issue_number=number,
                    issue_url=url,
                    mint=mint,
                    observed=prior.observed,
                    reason=prior.reason,
                    verdict_url=prior.verdict_url,
                )
            )
            continue

        observed, reason, url_for_verdict = measure(
            rpc, mint, registry, evidence, out_dir,
            attempt_time=attempt_time, scan_seconds=scan_seconds, site_url=site_url,
        )
        _record(number=number, mint=mint, ok=observed, reason=reason)
        outcome = Outcome(
            issue_number=number,
            issue_url=url,
            mint=mint,
            observed=observed,
            reason=reason,
            verdict_url=url_for_verdict,
        )

        by_mint[mint] = outcome
        outcomes.append(outcome)

    return outcomes


def measure(rpc, mint: str, registry, evidence, out_dir, *, attempt_time, scan_seconds: float = DEFAULT_SCAN_SECONDS,
            site_url: str | None = None) -> tuple[bool, str | None, str | None]:
    """One coin: walk its burns, observe it, write its page. Answers
    `(observed, reason, verdict_url)`; `reason` is a member of `REASONS`
    when not observed. Shared by the issue queue and `enrolled.index_new`,
    so a coin measured because its dev signed for it on the chain is
    measured exactly as one measured because a stranger asked.

    D-37: find what the coin actually DOES before reading what it is
    configured to do. `scan_burns` walks the mint's own signature history
    and classifies each burn as `boost_buy_and_burn` or `spl_burn`, which
    is the only measurement here that differs between two coins -- a split
    alone reads `ops 10000` for every coin that has not enrolled, because
    attribution matches our PDA and PROGRAM_ID is None. Supply falling is
    observable whoever caused it.

    BEFORE `observe_coin`, not after: `observe()` reads
    `evidence.burns_for()` and `evidence.total_burned()` to compute
    BURN_SUPPLY and BURN_ATOMIC. Scanning afterwards would publish a page
    whose burn checks describe the run before this one.

    A scan failure does NOT abort the measurement. `scan_burns` records its
    own `last_error` against the mint's burn cursor, and a partial walk
    leaves BURN_SUPPLY reading UNCHECKED rather than wrong -- which is the
    outcome the silence rule wants. Answering "we could not finish looking"
    beats answering with a total we cannot stand behind.
    """
    if evidence is not None:
        try:
            # Bounded by wall clock, not just pages. Without this a coin
            # with real trading history walks thousands of transactions
            # and the run never returns -- one busy coin would stall
            # every other submission behind it in the queue. A walk cut
            # short stays incomplete, so its figures stay withheld and
            # the next run resumes from the same cursor.
            scan_burns(rpc, evidence, mint, deadline=time.monotonic() + scan_seconds)
        except Exception:
            pass

    record = observe_coin(rpc, mint, registry, now=attempt_time, evidence=evidence)
    if record.error_kind is not None:
        return False, reason_for(record), None
    site.write(record, out_dir)
    return True, None, (verdict_url(mint, site_url) if site_url else None)


# -- the reply: separate from the run, so a link is never cited before it --
# -- is reachable (T-03-09) --------------------------------------------
_TERMINAL_COMMENT_TEMPLATES = {
    REASON_NOT_PUMP_COIN: (
        "The address in this issue does not decode as a pump.fun bonding curve -- it is "
        "not a pump coin, so this protocol has nothing to measure here. If that ever "
        "changes, open a new issue."
    ),
    REASON_NO_SHARING_CONFIG: (
        "This coin's creator is an ordinary wallet, not a fee-sharing config -- there is "
        "no split to measure today. This coin could gain a sharing config later; a new "
        "issue will measure it if it does."
    ),
}

_CORRECTABLE_COMMENT_TEMPLATES = {
    REASON_NOT_BASE58: (
        "The text in this issue does not contain a valid base58 mint address. Edit the "
        "issue to include the coin's mint address and it will be picked up on the next run."
    ),
    REASON_WRONG_LENGTH: (
        "The value found does not decode to a 32-byte address, so it cannot be a mint. "
        "Edit the issue to include the coin's mint address and it will be picked up on "
        "the next run."
    ),
    REASON_OFF_CURVE: (
        "The value found is a program-derived address, not an ordinary mint address. Edit "
        "the issue to include the coin's mint address and it will be picked up on the "
        "next run."
    ),
    REASON_AMBIGUOUS_MINT: (
        "This issue names more than one valid mint address. One coin per issue -- edit "
        "the issue to name exactly one, and it will be picked up on the next run."
    ),
    REASON_NO_MINT_FOUND: (
        "No mint address was found in this issue's title or body. Edit the issue to "
        "include the coin's mint address and it will be picked up on the next run."
    ),
}


def _terminal_comment(reason: str, mint: str | None) -> str:
    return _TERMINAL_COMMENT_TEMPLATES[reason]


def _correctable_comment(reason: str) -> str:
    return _CORRECTABLE_COMMENT_TEMPLATES[reason]


def reply(evidence, *, site_url: str, repo: str = DEFAULT_REPO, runner=subprocess.run, opener=urllib.request.urlopen) -> list:
    """The separate write step D-23/T-03-09 require. Reads
    `evidence.unanswered_submissions()` and, for every row:

    - `outcome == "observed"`: fetches the verdict URL and refuses to close
      the issue when it does not answer -- a dead link published as an
      answer is the same defect as a figure with no passing check behind
      it. A row whose link is not (yet) live is left unanswered for a later
      call.
    - `reason` is TERMINAL: answered plainly and closed.
    - `reason` is CORRECTABLE: answered with what to fix; left open.
    - `reason` is TRANSIENT (or unrecognised): never answered here at all --
      the next `run()` retries the underlying issue, which is still open.

    Returns one result dict per row acted on or considered, for the caller
    to report.
    """
    results = []
    for row in evidence.unanswered_submissions():
        row_repo = row.get("repo") or repo
        issue_number = row["issue_number"]
        attempted_at = row["attempted_at"]
        outcome = row.get("outcome")
        reason = row.get("reason")

        if outcome == "observed":
            mint = row["mint"]
            url = verdict_url(mint, site_url)
            if not is_live(url, opener=opener):
                results.append({"issue_number": issue_number, "answered": False, "why": "verdict_not_live"})
                continue
            answer(row_repo, issue_number, body=comment_body(mint, url), close=True, runner=runner)
            evidence.mark_answered(repo=row_repo, issue_number=issue_number, attempted_at=attempted_at, closed=True)
            results.append({"issue_number": issue_number, "answered": True, "closed": True})
        elif reason in TERMINAL:
            answer(row_repo, issue_number, body=_terminal_comment(reason, row.get("mint")), close=True, runner=runner)
            evidence.mark_answered(repo=row_repo, issue_number=issue_number, attempted_at=attempted_at, closed=True)
            results.append({"issue_number": issue_number, "answered": True, "closed": True})
        elif reason in CORRECTABLE:
            answer(row_repo, issue_number, body=_correctable_comment(reason), close=False, runner=runner)
            evidence.mark_answered(repo=row_repo, issue_number=issue_number, attempted_at=attempted_at, closed=False)
            results.append({"issue_number": issue_number, "answered": True, "closed": False})
        else:
            # TRANSIENT: our side failed. Never answered; the issue stays
            # open and the next run() attempts it again.
            results.append({"issue_number": issue_number, "answered": False, "why": "transient"})
    return results

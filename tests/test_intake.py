"""Offline tests for `indexer/intake.py` -- D-34's front door.

`python -m unittest discover -s tests -t tests -p "test_intake.py"`.

No network. Every account fed to `observe()` here is built byte by byte,
mirroring `tests/test_indexer.py`'s style, so a pump layout change shows up
as a failing decode test rather than as a wrong published number. Every
GitHub interaction (`open_issues`, `is_live`, `answer`) is driven through a
stub opener/runner -- this file never touches the network and never spawns
`gh`.
"""

from __future__ import annotations

import ast
import base64
import inspect
import json
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import cli, coverage, intake
from indexer import observe as observe_module
from indexer.base58 import decode, encode, pubkey_bytes
from indexer.evidence import Evidence
from indexer.legs import Registry
from indexer.observe import observe
from indexer.pump import (
    DISC_BONDING_CURVE,
    DISC_SHARING_CONFIG,
    PUMP_FEE_SHARE_PROGRAM,
    PUMP_PROGRAM,
    TOKEN_PROGRAM,
)
from indexer.rpc import RpcError, RpcUnavailable

CHARLIE = "8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump"
CHARLIE_CONFIG = "8cUvP3q3KqcKMT6rEowN55ZepafYLFLwY2vijETRK3E4"
CHARLIE_CURVE = "7VxCTsEknMC9ofXsddPM8piaGorGrMR8FQnDFjsQ7bjx"
BURN_VANITY = "burn111111111111111111111111111111111111111"
INCINERATOR = "1nc1nerator11111111111111111111111111111111"
ADMIN = "2CFywHXDPjDK2iRQsb95vnjgncDUZeQKJ6MceJ4ALpdc"
WALLET = "So11111111111111111111111111111111111111112"
SYSTEM_PROGRAM = "11111111111111111111111111111111"


# -- fixtures (mirrors tests/test_indexer.py's style) -----------------------
def account(data: bytes, owner: str, lamports: int = 1_000_000) -> dict:
    return {
        "owner": owner,
        "lamports": lamports,
        "data": [base64.b64encode(data).decode(), "base64"],
    }


def curve_account(creator: str, graduated: bool = True) -> dict:
    data = DISC_BONDING_CURVE + bytes(40) + bytes([1 if graduated else 0]) + pubkey_bytes(creator)
    return account(data, PUMP_PROGRAM)


def config_account(mint: str, holders, admin_revoked: bool = True, admin: str = ADMIN) -> dict:
    data = bytearray(DISC_SHARING_CONFIG)
    data += bytes([255, 2, 1])
    data += pubkey_bytes(mint)
    data += pubkey_bytes(admin)
    data += bytes([1 if admin_revoked else 0])
    data += len(holders).to_bytes(4, "little")
    for address, bps in holders:
        data += pubkey_bytes(address) + bps.to_bytes(2, "little")
    data += bytes(1024 - len(data))
    return account(bytes(data), PUMP_FEE_SHARE_PROGRAM)


def mint_account(supply: int, decimals: int = 6) -> dict:
    data = bytearray()
    data += (0).to_bytes(4, "little")
    data += bytes(32)
    data += supply.to_bytes(8, "little")
    data += bytes([decimals, 1])
    data += (0).to_bytes(4, "little")
    data += bytes(32)
    return account(bytes(data), TOKEN_PROGRAM)


class FakeRpc:
    def __init__(self, accounts: dict, balances: dict | None = None):
        self._accounts = accounts
        self._balances = balances or {}

    def accounts(self, addresses):
        return [self._accounts.get(address) for address in addresses]

    def balance(self, address):
        return self._balances.get(address, 0)


class RaisingRpc:
    """Every read raises the same exception -- the "every endpoint failed"
    and "the node answered and refused" shapes.
    """

    def __init__(self, exc: Exception):
        self._exc = exc

    def accounts(self, addresses):
        raise self._exc

    def balance(self, address):
        raise self._exc


class PartialFailureRpc:
    """`accounts()` succeeds; `balance()` fails for exactly one address --
    the late SOL-burn-balance-read failure path.
    """

    def __init__(self, accounts: dict, raise_balance_for: str):
        self._accounts = accounts
        self._raise_balance_for = raise_balance_for

    def accounts(self, addresses):
        return [self._accounts.get(address) for address in addresses]

    def balance(self, address):
        if address == self._raise_balance_for:
            raise RuntimeError("balance read failed")
        return 0


def charlie_accounts(overrides: dict | None = None) -> dict:
    accounts = {
        CHARLIE_CURVE: curve_account(CHARLIE_CONFIG),
        CHARLIE_CONFIG: config_account(CHARLIE, [(BURN_VANITY, 10_000)]),
        CHARLIE: mint_account(956_384_474_035_955),
    }
    accounts.update(overrides or {})
    return accounts


class FakeResponse:
    """A stand-in for `http.client.HTTPResponse` -- context-manager protocol
    plus `.read()` and `.status`.
    """

    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=-1):
        return self._body


class RaisingOpener:
    def __call__(self, request, timeout=None):
        raise OSError("connection refused")


# -- validate_mint: the security boundary -----------------------------------
class TestValidateMint(unittest.TestCase):
    def test_accepts_a_real_mint_and_returns_it(self):
        self.assertEqual(intake.validate_mint(CHARLIE), CHARLIE)

    def test_returned_value_is_reconstructed_from_bytes_not_the_input_object(self):
        result = intake.validate_mint(CHARLIE)
        self.assertEqual(result, encode(decode(CHARLIE)))
        self.assertIsNot(result, CHARLIE)

    def test_rejects_every_traversal_and_malformed_shape(self):
        thirty_one_bytes = "1" * 31          # decodes to 31 zero bytes
        thirty_three_bytes = "1" * 33        # decodes to 33 zero bytes
        cases = [
            "a/b",                            # forward slash
            "a\\b",                           # backslash
            "../../etc/passwd",               # parent-directory reference
            "",                                # empty
            thirty_one_bytes,
            thirty_three_bytes,
            "0" + CHARLIE[1:],                 # zero -- base58 omits it
            "O" + CHARLIE[1:],                 # capital O
            "I" + CHARLIE[1:],                 # capital I
            "l" + CHARLIE[1:],                 # lowercase l
            INCINERATOR,                        # off-curve, program-derived
        ]
        for candidate in cases:
            with self.subTest(candidate=candidate):
                with self.assertRaises(intake.InvalidMint) as ctx:
                    intake.validate_mint(candidate)
                self.assertIsNotNone(ctx.exception.reason)
                self.assertIn(ctx.exception.reason, intake.REASONS)

    def test_off_curve_value_names_the_off_curve_reason(self):
        with self.assertRaises(intake.InvalidMint) as ctx:
            intake.validate_mint(INCINERATOR)
        self.assertEqual(ctx.exception.reason, intake.REASON_OFF_CURVE)

    def test_wrong_length_values_name_the_wrong_length_reason(self):
        for candidate in ("1" * 31, "1" * 33):
            with self.subTest(candidate=candidate):
                with self.assertRaises(intake.InvalidMint) as ctx:
                    intake.validate_mint(candidate)
                self.assertEqual(ctx.exception.reason, intake.REASON_WRONG_LENGTH)


# -- is_submission: marker OR title prefix OR label, never label alone -----
class TestIsSubmission(unittest.TestCase):
    def test_marker_in_body_is_a_submission(self):
        issue = {"title": "random bug", "body": f"hello\n{intake.SUBMISSION_MARKER}\nmint here", "labels": []}
        self.assertTrue(intake.is_submission(issue))

    def test_title_prefix_is_a_submission(self):
        issue = {"title": f"{intake.SUBMISSION_TITLE_PREFIX} please check this coin", "body": "", "labels": []}
        self.assertTrue(intake.is_submission(issue))

    def test_label_alone_is_a_submission(self):
        issue = {"title": "random", "body": "", "labels": [{"name": intake.SUBMISSION_LABEL}]}
        self.assertTrue(intake.is_submission(issue))

    def test_none_of_the_three_is_not_a_submission(self):
        issue = {"title": "bug: it crashes on launch", "body": "steps to reproduce...", "labels": [{"name": "bug"}]}
        self.assertFalse(intake.is_submission(issue))


# -- submitted_mint -----------------------------------------------------
class TestSubmittedMint(unittest.TestCase):
    def test_single_valid_mint_is_returned(self):
        issue = {"title": "[coverage] check this", "body": f"please check {CHARLIE} thanks"}
        self.assertEqual(intake.submitted_mint(issue), CHARLIE)

    def test_two_distinct_mints_raise_ambiguous(self):
        issue = {"title": "x", "body": f"{CHARLIE} or maybe {WALLET}"}
        with self.assertRaises(intake.InvalidMint) as ctx:
            intake.submitted_mint(issue)
        self.assertEqual(ctx.exception.reason, intake.REASON_AMBIGUOUS_MINT)

    def test_no_mint_raises_absent(self):
        issue = {"title": "please check my coin", "body": "no mint here, sorry"}
        with self.assertRaises(intake.InvalidMint) as ctx:
            intake.submitted_mint(issue)
        self.assertEqual(ctx.exception.reason, intake.REASON_NO_MINT_FOUND)

    def test_the_same_mint_repeated_is_not_ambiguous(self):
        issue = {"title": "x", "body": f"{CHARLIE} and again {CHARLIE}"}
        self.assertEqual(intake.submitted_mint(issue), CHARLIE)


# -- open_issues ----------------------------------------------------------
class TestOpenIssues(unittest.TestCase):
    def test_drops_pull_requests_and_extracts_named_fields(self):
        payload = json.dumps(
            [
                {"number": 1, "title": "a", "body": "b", "html_url": "u1", "labels": []},
                {"number": 2, "title": "a PR", "body": "b", "html_url": "u2", "labels": [], "pull_request": {}},
            ]
        ).encode("utf-8")

        captured_request = {}

        def opener(request, timeout=None):
            captured_request["request"] = request
            return FakeResponse(payload)

        issues = intake.open_issues("owner/repo", opener=opener)
        self.assertEqual([i["number"] for i in issues], [1])
        headers = {k.lower(): v for k, v in captured_request["request"].headers.items()}
        self.assertNotIn("authorization", headers)

    def test_non_json_body_returns_empty_list_rather_than_raising(self):
        def opener(request, timeout=None):
            return FakeResponse(b"not json at all")

        self.assertEqual(intake.open_issues("owner/repo", opener=opener), [])

    def test_a_failing_opener_returns_empty_list(self):
        self.assertEqual(intake.open_issues("owner/repo", opener=RaisingOpener()), [])

    def test_non_list_payload_returns_empty_list(self):
        def opener(request, timeout=None):
            return FakeResponse(json.dumps({"message": "not found"}).encode("utf-8"))

        self.assertEqual(intake.open_issues("owner/repo", opener=opener), [])


# -- verdict_url / is_live --------------------------------------------------
class TestVerdictUrl(unittest.TestCase):
    def test_composes_site_url_and_mint_only(self):
        self.assertEqual(
            intake.verdict_url(CHARLIE, "https://charlieprotocol.fun"),
            f"https://charlieprotocol.fun/verify/{CHARLIE}",
        )

    def test_trailing_slash_on_site_url_is_handled(self):
        self.assertEqual(
            intake.verdict_url(CHARLIE, "https://charlieprotocol.fun/"),
            f"https://charlieprotocol.fun/verify/{CHARLIE}",
        )


class TestIsLive(unittest.TestCase):
    def test_true_on_200(self):
        def opener(request, timeout=None):
            return FakeResponse(b"", status=200)

        self.assertTrue(intake.is_live("https://example/verify/x", opener=opener))

    def test_false_on_404(self):
        def opener(request, timeout=None):
            return FakeResponse(b"", status=404)

        self.assertFalse(intake.is_live("https://example/verify/x", opener=opener))

    def test_false_when_opener_raises(self):
        self.assertFalse(intake.is_live("https://example/verify/x", opener=RaisingOpener()))


# -- answer: the one place a GitHub write happens ---------------------------
class FakeRunner:
    def __init__(self):
        self.calls: list = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0)


class TestAnswer(unittest.TestCase):
    def test_passes_a_list_never_a_shell_string_with_coerced_issue_number(self):
        runner = FakeRunner()
        with unittest.mock.patch("indexer.intake.shutil.which", return_value="/usr/bin/gh"):
            intake.answer("owner/repo", "42", body="hello", close=True, runner=runner)

        self.assertEqual(len(runner.calls), 2)  # comment, then close
        for args, kwargs in runner.calls:
            self.assertIsInstance(args, list)
            self.assertNotEqual(kwargs.get("shell"), True)
            self.assertIn("42", args)
            self.assertNotIn(42, args)  # every argv element is a string

    def test_close_false_only_comments(self):
        runner = FakeRunner()
        with unittest.mock.patch("indexer.intake.shutil.which", return_value="/usr/bin/gh"):
            intake.answer("owner/repo", 42, body="hello", close=False, runner=runner)
        self.assertEqual(len(runner.calls), 1)

    def test_missing_gh_executable_raises_rather_than_running_anything(self):
        runner = FakeRunner()
        with unittest.mock.patch("indexer.intake.shutil.which", return_value=None):
            with self.assertRaises(RuntimeError):
                intake.answer("owner/repo", 1, body="x", close=False, runner=runner)
        self.assertEqual(runner.calls, [])


class TestCommentBody(unittest.TestCase):
    def test_matches_the_template_exactly(self):
        url = f"https://charlieprotocol.fun/verify/{CHARLIE}"
        body = intake.comment_body(CHARLIE, url)
        self.assertIn(CHARLIE, body)
        self.assertIn(url, body)
        self.assertEqual(body, intake._SUCCESS_COMMENT_TEMPLATE.format(mint=CHARLIE, verdict_url=url))


# -- observe()'s error_kind (COV-03) ----------------------------------------
class TestObserveErrorKind(unittest.TestCase):
    def test_bonding_curve_that_does_not_decode_names_not_a_pump_coin(self):
        record = observe(FakeRpc({}), CHARLIE, now=1.0)
        self.assertEqual(record.error_kind, "not_pump_coin")

    def test_ordinary_creator_address_names_no_sharing_config(self):
        accounts = {
            CHARLIE_CURVE: curve_account(WALLET),
            WALLET: account(bytes(64), SYSTEM_PROGRAM),
        }
        record = observe(FakeRpc(accounts), CHARLIE, now=1.0)
        self.assertEqual(record.error_kind, "no_sharing_config")

    def test_every_endpoint_failing_names_rpc_unavailable(self):
        record = observe(RaisingRpc(RpcUnavailable("all endpoints failed")), CHARLIE, now=1.0)
        self.assertEqual(record.error_kind, "rpc_unavailable")

    def test_node_level_error_names_rpc_error(self):
        record = observe(RaisingRpc(RpcError(32000, "custom program error", "getMultipleAccounts")), CHARLIE, now=1.0)
        self.assertEqual(record.error_kind, "rpc_error")

    def test_late_sol_burn_balance_failure_keeps_error_kind_unset(self):
        accounts = charlie_accounts()
        record = observe(PartialFailureRpc(accounts, BURN_VANITY), CHARLIE, now=1.0)
        self.assertIsNotNone(record.error)
        self.assertIsNone(record.error_kind)
        self.assertTrue(record.checks)  # a full record was still produced

    def test_every_error_kind_maps_through_reason_for(self):
        for candidate_rpc, expected in (
            (FakeRpc({}), "not_pump_coin"),
            (RaisingRpc(RpcUnavailable("x")), "rpc_unavailable"),
            (RaisingRpc(RpcError(1, "x", "y")), "rpc_error"),
        ):
            record = observe(candidate_rpc, CHARLIE, now=1.0)
            with self.subTest(expected=expected):
                self.assertEqual(intake.reason_for(record), expected)
                self.assertIn(intake.reason_for(record), intake.REASONS)


# -- reason_for: closed vocabulary, no string matching ----------------------
class TestReasonFor(unittest.TestCase):
    def test_rejects_a_value_outside_the_vocabulary(self):
        record = observe(FakeRpc({}), CHARLIE, now=1.0)
        record.error_kind = "not_a_real_reason"
        with self.assertRaises(ValueError):
            intake.reason_for(record)

    def test_invalid_mint_reason_passes_through(self):
        exc = intake.InvalidMint("x", reason=intake.REASON_OFF_CURVE)
        self.assertEqual(intake.reason_for(exc), intake.REASON_OFF_CURVE)

    def test_reasons_partition_into_exactly_three_dispositions(self):
        self.assertEqual(set(intake.REASONS), set(intake.TERMINAL) | set(intake.CORRECTABLE) | set(intake.TRANSIENT))
        self.assertEqual(len(intake.REASONS), len(intake.TERMINAL) + len(intake.CORRECTABLE) + len(intake.TRANSIENT))


# -- run(): the thin path proven end to end ---------------------------------
class TestRun(unittest.TestCase):
    def test_one_valid_submission_produces_one_observed_outcome_and_writes_both_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            issue = {
                "number": 5,
                "title": f"{intake.SUBMISSION_TITLE_PREFIX} please check",
                "body": f"mint: {CHARLIE}",
                "html_url": "https://github.com/x/y/issues/5",
                "labels": [],
            }
            rpc = FakeRpc(charlie_accounts(), balances={BURN_VANITY: 178_734_302_038})
            outcomes = intake.run([issue], rpc, Registry(), None, out_dir, site_url="https://charlieprotocol.fun")

            self.assertEqual(len(outcomes), 1)
            outcome = outcomes[0]
            self.assertTrue(outcome.observed)
            self.assertEqual(outcome.mint, CHARLIE)
            self.assertEqual(outcome.issue_number, 5)
            self.assertEqual(outcome.verdict_url, f"https://charlieprotocol.fun/verify/{CHARLIE}")
            self.assertTrue((out_dir / f"{CHARLIE}.html").exists())
            self.assertTrue((out_dir / f"{CHARLIE}.json").exists())

    def test_traversal_mint_fails_validation_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            issue = {
                "number": 9,
                "title": f"{intake.SUBMISSION_TITLE_PREFIX} check",
                "body": "../../etc/passwd",
                "html_url": "u",
                "labels": [],
            }
            outcomes = intake.run([issue], FakeRpc({}), Registry(), None, out_dir)

            self.assertEqual(len(outcomes), 1)
            self.assertFalse(outcomes[0].observed)
            self.assertIsNotNone(outcomes[0].reason)
            self.assertIn(outcomes[0].reason, intake.REASONS)
            self.assertEqual(list(out_dir.iterdir()), [])

    def test_non_submission_issues_are_left_completely_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            issue = {"number": 1, "title": "bug report", "body": "no marker here", "labels": [], "html_url": "u"}
            outcomes = intake.run([issue], FakeRpc({}), Registry(), None, Path(tmp))
            self.assertEqual(outcomes, [])

    def test_per_run_cap_leaves_the_rest_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            issues = [
                {
                    "number": n,
                    "title": intake.SUBMISSION_TITLE_PREFIX,
                    "body": "nothing valid here",
                    "html_url": "u",
                    "labels": [],
                }
                for n in range(1, 6)
            ]
            outcomes = intake.run(issues, FakeRpc({}), Registry(), None, Path(tmp), limit=2)
            self.assertEqual(len(outcomes), 2)
            self.assertEqual([o.issue_number for o in outcomes], [1, 2])

    def test_duplicate_mint_across_two_issues_observes_once(self):
        calls = {"n": 0}
        real_accounts = charlie_accounts()

        class CountingRpc(FakeRpc):
            def accounts(self, addresses):
                calls["n"] += 1
                return super().accounts(addresses)

        with tempfile.TemporaryDirectory() as tmp:
            issues = [
                {"number": 1, "title": intake.SUBMISSION_TITLE_PREFIX, "body": CHARLIE, "html_url": "u1", "labels": []},
                {"number": 2, "title": intake.SUBMISSION_TITLE_PREFIX, "body": CHARLIE, "html_url": "u2", "labels": []},
            ]
            outcomes = intake.run(issues, CountingRpc(real_accounts), Registry(), None, Path(tmp))
            self.assertEqual(len(outcomes), 2)
            self.assertTrue(all(o.observed for o in outcomes))
            self.assertEqual(outcomes[0].verdict_url, outcomes[1].verdict_url)
            # observe() calls .accounts() four times per coin: curve, config,
            # mint, and D-40's batched recipient-kind read. The number matters
            # less than the multiple -- duplicate collapse means exactly ONE
            # coin's worth of reads, not two, so this must never be 8.
            self.assertEqual(calls["n"], 4)


# -- D-36: the dormant sweep stays unreferenced ------------------------------
# Driven from `indexer.coverage`'s own attribute names, never a written list
# -- if a name is ever added to that module, it becomes forbidden here too
# without anyone having to remember to update this test.
def _coverage_public_names() -> list:
    return sorted(
        name
        for name, value in vars(coverage).items()
        if not name.startswith("_") and getattr(value, "__module__", None) == "indexer.coverage"
    )


def _references_coverage(source: str, forbidden_names) -> bool:
    """True iff `source` imports the `coverage` module, imports one of its
    names directly, or references `coverage.<anything>` -- an AST check
    rather than a raw substring search, since `intake.py` legitimately
    contains the string "coverage" inside submission-marker literals
    (`"[coverage] ..."`) that have nothing to do with the dormant module.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "coverage" or module.endswith(".coverage"):
                return True
            if any(alias.name in forbidden_names for alias in node.names):
                return True
        elif isinstance(node, ast.Import):
            if any(alias.name.split(".")[-1] == "coverage" for alias in node.names):
                return True
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "coverage":
            return True
        elif isinstance(node, ast.Name) and node.id in forbidden_names:
            return True
    return False


class TestDormantSweepStaysUnreferenced(unittest.TestCase):
    def test_intake_module_never_references_the_dormant_sweep(self):
        names = _coverage_public_names()
        self.assertTrue(names, "sanity: indexer.coverage should define at least one public name")
        self.assertFalse(_references_coverage(inspect.getsource(intake), names))

    def test_cli_intake_handler_never_references_the_dormant_sweep(self):
        names = _coverage_public_names()
        self.assertFalse(_references_coverage(inspect.getsource(cli._intake), names))


def evidence_db(tmp_dir) -> Evidence:
    return Evidence(Path(tmp_dir) / "evidence.db")


# -- run() records every attempted issue to the evidence store (Task 2) -----
class TestRunRecordsSubmissions(unittest.TestCase):
    def test_cap_records_exactly_the_attempted_rows_and_nothing_for_the_rest(self):
        issues = [
            {"number": n, "title": intake.SUBMISSION_TITLE_PREFIX, "body": "nothing valid here", "html_url": "u", "labels": []}
            for n in range(1, 6)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            try:
                outcomes = intake.run(issues, FakeRpc({}), Registry(), evidence, Path(tmp), limit=2)
                rows = evidence.submissions(repo=intake.DEFAULT_REPO)
            finally:
                evidence.close()
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["issue_number"] for r in rows}, {1, 2})
        for row in rows:
            self.assertEqual(row["outcome"], "failed")
            self.assertIn(row["reason"], intake.REASONS)

    def test_duplicate_mint_across_two_issues_gets_two_rows_pointing_at_the_same_verdict(self):
        issues = [
            {"number": 1, "title": intake.SUBMISSION_TITLE_PREFIX, "body": CHARLIE, "html_url": "u1", "labels": []},
            {"number": 2, "title": intake.SUBMISSION_TITLE_PREFIX, "body": CHARLIE, "html_url": "u2", "labels": []},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            try:
                rpc = FakeRpc(charlie_accounts(), balances={BURN_VANITY: 178_734_302_038})
                outcomes = intake.run(issues, rpc, Registry(), evidence, Path(tmp), site_url="https://x.example")
                rows = evidence.submissions(repo=intake.DEFAULT_REPO)
            finally:
                evidence.close()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["outcome"] == "observed" and r["mint"] == CHARLIE for r in rows))
        self.assertEqual(outcomes[0].verdict_url, outcomes[1].verdict_url)

    def test_a_terminal_failure_is_recorded_and_no_page_is_written(self):
        accounts = {
            "7VxCTsEknMC9ofXsddPM8piaGorGrMR8FQnDFjsQ7bjx": curve_account(WALLET),
            WALLET: account(bytes(64), SYSTEM_PROGRAM),
        }
        issue = {"number": 1, "title": intake.SUBMISSION_TITLE_PREFIX, "body": CHARLIE, "html_url": "u", "labels": []}
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            try:
                intake.run([issue], FakeRpc(accounts), Registry(), evidence, Path(tmp))
                rows = evidence.submissions(repo=intake.DEFAULT_REPO)
            finally:
                evidence.close()
            self.assertEqual(list(Path(tmp).glob("*.html")), [])
        self.assertEqual(rows[0]["outcome"], "failed")
        self.assertEqual(rows[0]["reason"], intake.REASON_NO_SHARING_CONFIG)
        self.assertIn(rows[0]["reason"], intake.TERMINAL)

    def test_evidence_none_skips_recording_without_raising(self):
        issue = {"number": 1, "title": intake.SUBMISSION_TITLE_PREFIX, "body": "no mint", "html_url": "u", "labels": []}
        with tempfile.TemporaryDirectory() as tmp:
            outcomes = intake.run([issue], FakeRpc({}), Registry(), None, Path(tmp))
        self.assertEqual(len(outcomes), 1)


class TestRunNeverInvokesTheReply(unittest.TestCase):
    def test_run_source_never_calls_answer_or_reply(self):
        source = inspect.getsource(intake.run)
        self.assertNotIn("answer(", source)
        self.assertNotIn("reply(", source)


# -- reply(): the separate write step, driven from the three dispositions --
class TestReply(unittest.TestCase):
    def _evidence_with(self, tmp, rows):
        evidence = evidence_db(tmp)
        for row in rows:
            evidence.record_submission(**row)
        return evidence

    def test_transient_rows_are_never_answered(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = self._evidence_with(
                tmp,
                [dict(repo="o/r", issue_number=1, attempted_at=1, outcome="failed", reason=intake.REASON_RPC_UNAVAILABLE)],
            )
            runner = FakeRunner()
            with unittest.mock.patch("indexer.intake.shutil.which", return_value="/usr/bin/gh"):
                results = intake.reply(evidence, site_url="https://x.example", repo="o/r", runner=runner)
            unanswered = evidence.unanswered_submissions()
            evidence.close()
        self.assertEqual(runner.calls, [])
        self.assertFalse(results[0]["answered"])
        self.assertEqual(len(unanswered), 1)

    def test_every_terminal_reason_is_answered_and_closed(self):
        for reason in intake.TERMINAL:
            with self.subTest(reason=reason):
                with tempfile.TemporaryDirectory() as tmp:
                    evidence = self._evidence_with(
                        tmp, [dict(repo="o/r", issue_number=1, attempted_at=1, outcome="failed", reason=reason)]
                    )
                    runner = FakeRunner()
                    with unittest.mock.patch("indexer.intake.shutil.which", return_value="/usr/bin/gh"):
                        intake.reply(evidence, site_url="https://x.example", repo="o/r", runner=runner)
                    row = evidence.submissions(repo="o/r")[0]
                    evidence.close()
                self.assertEqual(len(runner.calls), 2)  # comment + close
                self.assertIsNotNone(row["answered_at"])
                self.assertIsNotNone(row["closed_at"])

    def test_every_correctable_reason_is_answered_and_left_open(self):
        for reason in intake.CORRECTABLE:
            with self.subTest(reason=reason):
                with tempfile.TemporaryDirectory() as tmp:
                    evidence = self._evidence_with(
                        tmp, [dict(repo="o/r", issue_number=1, attempted_at=1, outcome="failed", reason=reason)]
                    )
                    runner = FakeRunner()
                    with unittest.mock.patch("indexer.intake.shutil.which", return_value="/usr/bin/gh"):
                        intake.reply(evidence, site_url="https://x.example", repo="o/r", runner=runner)
                    row = evidence.submissions(repo="o/r")[0]
                    evidence.close()
                self.assertEqual(len(runner.calls), 1)  # comment only, no close
                self.assertIsNotNone(row["answered_at"])
                self.assertIsNone(row["closed_at"])

    def test_observed_row_whose_link_is_not_live_is_left_unanswered(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = self._evidence_with(
                tmp, [dict(repo="o/r", issue_number=1, attempted_at=1, outcome="observed", mint=CHARLIE)]
            )
            runner = FakeRunner()

            def dead_opener(request, timeout=None):
                return FakeResponse(b"", status=404)

            with unittest.mock.patch("indexer.intake.shutil.which", return_value="/usr/bin/gh"):
                results = intake.reply(evidence, site_url="https://x.example", repo="o/r", runner=runner, opener=dead_opener)
            unanswered = evidence.unanswered_submissions()
            evidence.close()
        self.assertEqual(runner.calls, [])
        self.assertFalse(results[0]["answered"])
        self.assertEqual(len(unanswered), 1)

    def test_observed_row_whose_link_is_live_is_answered_and_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = self._evidence_with(
                tmp, [dict(repo="o/r", issue_number=1, attempted_at=1, outcome="observed", mint=CHARLIE)]
            )
            runner = FakeRunner()

            def live_opener(request, timeout=None):
                return FakeResponse(b"", status=200)

            with unittest.mock.patch("indexer.intake.shutil.which", return_value="/usr/bin/gh"):
                intake.reply(evidence, site_url="https://x.example", repo="o/r", runner=runner, opener=live_opener)
            row = evidence.submissions(repo="o/r")[0]
            evidence.close()
        self.assertEqual(len(runner.calls), 2)
        self.assertIsNotNone(row["answered_at"])
        self.assertIsNotNone(row["closed_at"])
        body = runner.calls[0][0][runner.calls[0][0].index("--body") + 1]
        self.assertIn(CHARLIE, body)
        self.assertIn("https://x.example/verify/" + CHARLIE, body)


# -- COV-01, closed by evidence rather than by a sentence (Task 3) ---------
# `observe()` has always taken a mint and a registry and consulted no list
# of permitted coins; what never existed is a test that would fail if that
# stopped being true. This walks the SOURCE of the two modules on the
# observation path and asserts no identifier resembling an allowlist, an
# enrollment gate or a consent table is ever DEFINED or REFERENCED as a name
# -- not a raw substring search over the whole file, which would trip on
# this project's own prose (`intake.py`'s docstring already says "no
# allowlist" -- that sentence must not fail this test).
_GATING_SUBSTRINGS = (
    "allowlist",
    "allow_list",
    "whitelist",
    "enrolled",
    "enrollment",
    "consenttable",
    "consent_table",
    "approvalrequired",
    "approval_required",
    "isapproved",
    "is_approved",
    "permittedmints",
    "permitted_mints",
)


def _bound_identifiers(tree: ast.AST) -> set:
    """Every name this module actually DEFINES or REFERENCES as code --
    function/class names, assignment targets, arguments, attribute
    accesses -- never a string or comment.
    """
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


class TestCOV01ObservationPathHasNoGate(unittest.TestCase):
    def test_no_gating_identifier_is_defined_or_referenced(self):
        for module_name, module in (("indexer.observe", observe_module), ("indexer.intake", intake)):
            tree = ast.parse(inspect.getsource(module))
            identifiers = {name.lower() for name in _bound_identifiers(tree)}
            for gate in _GATING_SUBSTRINGS:
                with self.subTest(module=module_name, gate=gate):
                    offenders = [ident for ident in identifiers if gate in ident]
                    self.assertEqual(offenders, [], f"{module_name} binds {offenders} resembling {gate!r}")

    def test_observe_signature_carries_no_gating_parameter(self):
        """`observe()` takes a mint and nothing else that could gate it --
        asserted against the live signature, not a comment claiming it.
        """
        params = set(inspect.signature(observe).parameters)
        self.assertEqual(params, {"rpc", "mint", "registry", "now", "evidence", "config"})


if __name__ == "__main__":
    unittest.main()


class TestBurnScanIsWiredIntoIntake(unittest.TestCase):
    """D-37. The scan that finds what a coin actually DOES.

    A split alone reads `ops 10000` for every coin that has not enrolled --
    attribution only matches our own PDA and `PROGRAM_ID` is `None` -- so the
    burn walk is the only measurement here that distinguishes two coins.
    These tests pin that it runs, that it runs FIRST, and that its failure
    cannot take the submission down with it.
    """

    def _issue(self):
        return {"number": 1, "title": intake.SUBMISSION_TITLE_PREFIX,
                "body": CHARLIE, "html_url": "u", "labels": []}

    def test_the_scan_runs_for_a_submitted_mint(self):
        calls = []
        original = intake.scan_burns
        intake.scan_burns = lambda rpc, evidence, mint, **kw: calls.append(mint)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                evidence = evidence_db(tmp)
                try:
                    rpc = FakeRpc(charlie_accounts(), balances={BURN_VANITY: 178_734_302_038})
                    intake.run([self._issue()], rpc, Registry(), evidence, Path(tmp))
                finally:
                    evidence.close()
        finally:
            intake.scan_burns = original
        self.assertEqual(calls, [CHARLIE])

    def test_the_scan_runs_before_the_observation(self):
        """`observe()` reads the evidence store to compute BURN_SUPPLY and
        BURN_ATOMIC. Scanning afterwards would publish a page whose burn
        checks describe the run before this one -- correct-looking, and a
        version behind.
        """
        order = []
        original_scan, original_observe = intake.scan_burns, intake.observe_coin
        intake.scan_burns = lambda rpc, evidence, mint, **kw: order.append("scan")
        def _observe(*a, **kw):
            order.append("observe")
            return original_observe(*a, **kw)
        intake.observe_coin = _observe
        try:
            with tempfile.TemporaryDirectory() as tmp:
                evidence = evidence_db(tmp)
                try:
                    rpc = FakeRpc(charlie_accounts(), balances={BURN_VANITY: 178_734_302_038})
                    intake.run([self._issue()], rpc, Registry(), evidence, Path(tmp))
                finally:
                    evidence.close()
        finally:
            intake.scan_burns, intake.observe_coin = original_scan, original_observe
        self.assertEqual(order, ["scan", "observe"])

    def test_a_failing_scan_does_not_abort_the_submission(self):
        """The submission still produces a verdict. A partial walk leaves
        BURN_SUPPLY reading UNCHECKED rather than wrong, which is what the
        silence rule wants -- 'we could not finish looking' beats a total
        nobody can stand behind.
        """
        original = intake.scan_burns
        def _boom(*a, **kw):
            raise RuntimeError("the node refused")
        intake.scan_burns = _boom
        try:
            with tempfile.TemporaryDirectory() as tmp:
                evidence = evidence_db(tmp)
                try:
                    rpc = FakeRpc(charlie_accounts(), balances={BURN_VANITY: 178_734_302_038})
                    outcomes = intake.run([self._issue()], rpc, Registry(), evidence, Path(tmp))
                finally:
                    evidence.close()
        finally:
            intake.scan_burns = original
        self.assertEqual(len(outcomes), 1)
        self.assertTrue(outcomes[0].observed)

    def test_no_scan_without_an_evidence_store(self):
        """A dry run passes `evidence=None`; there is nowhere to record a
        burn, so walking signatures would be network cost for nothing.
        """
        calls = []
        original = intake.scan_burns
        intake.scan_burns = lambda *a, **kw: calls.append(1)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                rpc = FakeRpc(charlie_accounts(), balances={BURN_VANITY: 178_734_302_038})
                intake.run([self._issue()], rpc, Registry(), None, Path(tmp))
        finally:
            intake.scan_burns = original
        self.assertEqual(calls, [])

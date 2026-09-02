"""Offline tests for the evidence store, the inflow scan, and the export.

`python -m unittest discover -s tests -t tests -p "test_evidence.py"`.

No network. Every RPC response fed to `scan_inflows` is a literal dict built
here, shaped exactly as RESEARCH.md Q2 describes a real `getTransaction`
response, so a shape change surfaces as a failing test rather than a wrong
published number.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import intake, invariants
from indexer.evidence import Evidence
from indexer.export import export_all
from indexer.legs import Attribution, GRANDFATHERED_SOL_BURN, Split
from indexer.observe import Observation
from indexer.scan import account_keys, balance_delta, fetch_new_signatures, scan_inflows

MINT = "8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump"
SOL_BURN = "burn111111111111111111111111111111111111111"
OTHER = "So11111111111111111111111111111111111111112"
DERIVED_VAULT = "3vaU1t11111111111111111111111111111111111"  # not grandfathered -- comparator "=="


def sol_burn_split(address) -> Split:
    attribution = Attribution(address=address, bps=10_000, leg="sol_burn", reason="test fixture", keyless=True)
    return Split(sol_burn=10_000, burn=0, paid=0, attributions=(attribution,))


# -- fixtures ---------------------------------------------------------------
def make_tx(account_deltas: dict, signature: str, slot: int = 100, block_time: int = 1_000,
            err=None, extra_instructions=None) -> dict:
    """A jsonParsed transaction shaped like RESEARCH.md Q2's mainnet response.

    `account_deltas` maps address -> (pre_balance, post_balance), in the exact
    order they should appear in `accountKeys` (index-aligned with the balance
    arrays, as the real RPC guarantees).
    """
    keys = list(account_deltas.keys())
    account_keys_field = [
        {"pubkey": key, "signer": i == 0, "writable": True, "source": "transaction"}
        for i, key in enumerate(keys)
    ]
    return {
        "transaction": {
            "message": {
                "accountKeys": account_keys_field,
                "instructions": extra_instructions or [],
            }
        },
        "meta": {
            "err": err,
            "preBalances": [pre for pre, _post in account_deltas.values()],
            "postBalances": [post for _pre, post in account_deltas.values()],
            "innerInstructions": [],
        },
        "blockTime": block_time,
        "slot": slot,
    }


class FakeScanRpc:
    """A fake `RpcClient` exposing only what `scan.py` needs."""

    def __init__(self, signatures, transactions, balances=None):
        self._signatures = signatures
        self._transactions = transactions
        self._balances = balances or {}

    def signatures_for_address(self, address, before=None, until=None, limit=1000):
        return list(self._signatures)

    def transaction(self, signature):
        return self._transactions.get(signature)

    def balance(self, address):
        return self._balances.get(address, 0)


def evidence_db(tmp_dir: str) -> Evidence:
    return Evidence(Path(tmp_dir) / "evidence.db")


# -- scan_inflows -------------------------------------------------------
class TestScanInflows(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_one_credited_destination_writes_exactly_one_row(self):
        tx = make_tx({SOL_BURN: (1_000, 1_500), OTHER: (500, 500)}, signature="sig-1")
        rpc = FakeScanRpc(
            signatures=[{"signature": "sig-1", "err": None, "slot": 10, "blockTime": 111}],
            transactions={"sig-1": tx},
        )
        evidence = evidence_db(self.tmp.name)
        scan_inflows(rpc, evidence, MINT, {SOL_BURN}, leg_of=lambda _d: "sol_burn", target=SOL_BURN, pages=1)
        rows = evidence.inflows_for(SOL_BURN)
        evidence.close()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["signature"], "sig-1")
        self.assertEqual(rows[0]["destination"], SOL_BURN)
        self.assertEqual(rows[0]["lamports"], 500)

    def test_credit_ix_count_is_populated(self):
        tx = make_tx(
            {SOL_BURN: (0, 500)},
            signature="sig-1",
            extra_instructions=[
                {
                    "parsed": {
                        "type": "transfer",
                        "info": {"source": OTHER, "destination": SOL_BURN, "lamports": 500},
                    },
                    "programId": "11111111111111111111111111111111",
                }
            ],
        )
        rpc = FakeScanRpc(
            signatures=[{"signature": "sig-1", "err": None, "slot": 1, "blockTime": 1}],
            transactions={"sig-1": tx},
        )
        evidence = evidence_db(self.tmp.name)
        scan_inflows(rpc, evidence, MINT, {SOL_BURN}, leg_of=lambda _d: "sol_burn", target=SOL_BURN, pages=1)
        rows = evidence.inflows_for(SOL_BURN)
        evidence.close()
        self.assertEqual(rows[0]["credit_ix_count"], 1)

    def test_signature_dedup_key_ignores_a_repeat_scan(self):
        tx = make_tx({SOL_BURN: (0, 500)}, signature="sig-1")
        rpc = FakeScanRpc(
            signatures=[{"signature": "sig-1", "err": None, "slot": 1, "blockTime": 1}],
            transactions={"sig-1": tx},
        )
        evidence = evidence_db(self.tmp.name)
        scan_inflows(rpc, evidence, MINT, {SOL_BURN}, leg_of=lambda _d: "sol_burn", target=SOL_BURN, pages=1)
        scan_inflows(rpc, evidence, MINT, {SOL_BURN}, leg_of=lambda _d: "sol_burn", target=SOL_BURN, pages=1)
        rows = evidence.inflows_for(SOL_BURN)
        evidence.close()
        self.assertEqual(len(rows), 1)


# -- balance_delta / account_keys -----------------------------------------
class TestBalanceDelta(unittest.TestCase):
    def test_positive_delta(self):
        tx = make_tx({SOL_BURN: (1_000, 1_500)}, signature="sig-1")
        self.assertEqual(balance_delta(tx, SOL_BURN), 500)

    def test_absent_address_is_none(self):
        tx = make_tx({SOL_BURN: (1_000, 1_500)}, signature="sig-1")
        self.assertIsNone(balance_delta(tx, OTHER))

    def test_short_balance_arrays_are_none_not_indexerror(self):
        tx = make_tx({SOL_BURN: (1_000, 1_500), OTHER: (0, 0)}, signature="sig-1")
        tx["meta"]["postBalances"] = tx["meta"]["postBalances"][:1]
        self.assertIsNone(balance_delta(tx, OTHER))

    def test_account_keys_merges_loaded_addresses(self):
        tx = make_tx({SOL_BURN: (0, 0)}, signature="sig-1")
        tx["meta"]["loadedAddresses"] = {"writable": [OTHER], "readonly": []}
        self.assertEqual(account_keys(tx), [SOL_BURN, OTHER])


# -- SOL_BURN_BALANCE: the tracer's actual claim -------------------------------
class TestSolBurnBalanceCheck(unittest.TestCase):
    def test_pass_for_a_stored_inflow_matching_the_live_balance(self):
        with tempfile.TemporaryDirectory() as tmp:
            tx = make_tx({SOL_BURN: (0, 500)}, signature="sig-1")
            rpc = FakeScanRpc(
                signatures=[{"signature": "sig-1", "err": None, "slot": 1, "blockTime": 1}],
                transactions={"sig-1": tx},
                balances={SOL_BURN: 500},
            )
            evidence = evidence_db(tmp)
            scan_inflows(rpc, evidence, MINT, {SOL_BURN}, leg_of=lambda _d: "sol_burn", target=SOL_BURN, pages=1)
            recorded = evidence.recorded_lamports(SOL_BURN)
            evidence.close()

            check = invariants.sol_burn_balance(
                destination=SOL_BURN, recorded=recorded, vault_balance=rpc.balance(SOL_BURN)
            )
            self.assertEqual(check.status, invariants.PASS)
            self.assertNotEqual(check.status, invariants.UNCHECKED)

    def test_fail_for_a_stored_inflow_not_matching_the_live_balance(self):
        with tempfile.TemporaryDirectory() as tmp:
            tx = make_tx({SOL_BURN: (0, 500)}, signature="sig-1")
            rpc = FakeScanRpc(
                signatures=[{"signature": "sig-1", "err": None, "slot": 1, "blockTime": 1}],
                transactions={"sig-1": tx},
                balances={SOL_BURN: 999},
            )
            evidence = evidence_db(tmp)
            scan_inflows(rpc, evidence, MINT, {SOL_BURN}, leg_of=lambda _d: "sol_burn", target=SOL_BURN, pages=1)
            recorded = evidence.recorded_lamports(SOL_BURN)
            evidence.close()

            check = invariants.sol_burn_balance(
                destination=SOL_BURN, recorded=recorded, vault_balance=rpc.balance(SOL_BURN)
            )
            self.assertEqual(check.status, invariants.FAIL)
            self.assertEqual(check.expected, "500")
            self.assertEqual(check.actual, "999")

    def test_unchecked_survives_for_a_caller_that_passes_nothing(self):
        check = invariants.sol_burn_balance()
        self.assertEqual(check.status, invariants.UNCHECKED)
        self.assertIn("inflow recording is not built", check.detail)


# -- deterministic export --------------------------------------------------
class TestExport(unittest.TestCase):
    def test_two_exports_of_an_unchanged_database_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            evidence.record_inflow(
                signature="sig-1", destination=SOL_BURN, mint=MINT, leg="sol_burn",
                lamports=500, block_time=111, slot=10, credit_ix_count=1,
            )
            evidence.record_inflow(
                signature="sig-2", destination=SOL_BURN, mint=MINT, leg="sol_burn",
                lamports=300, block_time=112, slot=11,
            )
            out1, out2 = Path(tmp) / "out1", Path(tmp) / "out2"
            export_all(evidence, out1)
            export_all(evidence, out2)
            evidence.close()

            self.assertEqual(
                (out1 / "inflow.jsonl").read_bytes(),
                (out2 / "inflow.jsonl").read_bytes(),
            )


# -- submission table (COV-02/COV-03, 03-02 Task 2) --------------------------
class TestSubmissionTable(unittest.TestCase):
    def test_two_attempts_for_the_same_issue_leave_two_unrewritten_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            evidence.record_submission(
                repo="o/r", issue_number=7, attempted_at=1, outcome="failed",
                reason=intake.REASON_RPC_UNAVAILABLE,
            )
            evidence.record_submission(
                repo="o/r", issue_number=7, attempted_at=2, outcome="observed", mint=MINT,
            )
            rows = evidence.submissions(repo="o/r")
            evidence.close()

            self.assertEqual(len(rows), 2)
            first = next(r for r in rows if r["attempted_at"] == 1)
            self.assertEqual(first["reason"], intake.REASON_RPC_UNAVAILABLE)
            self.assertIsNone(first["mint"])

    def test_record_submission_rejects_a_reason_outside_the_vocabulary(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            try:
                with self.assertRaises(ValueError):
                    evidence.record_submission(
                        repo="o/r", issue_number=1, outcome="failed", reason="not_a_real_reason"
                    )
            finally:
                evidence.close()

    def test_record_submission_rejects_every_reason_string_outside_the_vocabulary(self):
        """Driven from `intake.REASONS` itself, not a hand-picked example."""
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            try:
                for bogus in ("sweep", "enumerate", "", "OBSERVED", "not_pump_coinX"):
                    if bogus in intake.REASONS:
                        continue  # sanity: never test a value that is actually valid
                    with self.subTest(bogus=bogus):
                        with self.assertRaises(ValueError):
                            evidence.record_submission(repo="o/r", issue_number=1, outcome="failed", reason=bogus)
            finally:
                evidence.close()

    def test_every_member_of_reasons_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            try:
                for n, reason in enumerate(intake.REASONS):
                    evidence.record_submission(repo="o/r", issue_number=n, outcome="failed", reason=reason, attempted_at=n)
                self.assertEqual(len(evidence.submissions(repo="o/r")), len(intake.REASONS))
            finally:
                evidence.close()

    def test_submission_counts_counts_distinct_mints_and_failed_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            evidence.record_submission(repo="o/r", issue_number=1, attempted_at=1, outcome="observed", mint=MINT)
            evidence.record_submission(repo="o/r", issue_number=2, attempted_at=2, outcome="observed", mint=MINT)  # resubmission, same mint
            evidence.record_submission(
                repo="o/r", issue_number=3, attempted_at=3, outcome="failed", reason=intake.REASON_NO_MINT_FOUND
            )
            counts = evidence.submission_counts()
            evidence.close()
            self.assertEqual(counts, {"observed": 1, "failed": 1})

    def test_unanswered_submissions_excludes_rows_marked_answered(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            evidence.record_submission(repo="o/r", issue_number=1, attempted_at=1, outcome="observed", mint=MINT)
            evidence.record_submission(
                repo="o/r", issue_number=2, attempted_at=2, outcome="failed", reason=intake.REASON_NO_MINT_FOUND
            )
            self.assertEqual(len(evidence.unanswered_submissions()), 2)
            evidence.mark_answered(repo="o/r", issue_number=1, attempted_at=1, closed=True)
            remaining = evidence.unanswered_submissions()
            evidence.close()
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["issue_number"], 2)

    def test_mark_answered_never_touches_the_facts_of_the_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            evidence.record_submission(repo="o/r", issue_number=1, attempted_at=1, outcome="observed", mint=MINT)
            evidence.mark_answered(repo="o/r", issue_number=1, attempted_at=1, closed=False)
            row = evidence.submissions(repo="o/r")[0]
            evidence.close()
            self.assertEqual(row["mint"], MINT)
            self.assertEqual(row["outcome"], "observed")
            self.assertIsNotNone(row["answered_at"])
            self.assertIsNone(row["closed_at"])

    def test_submission_is_registered_in_export_tables(self):
        from indexer.export import EXPORT_TABLES

        names = [table for table, _order in EXPORT_TABLES]
        self.assertIn("submission", names)

    def test_two_exports_of_an_unchanged_database_with_submissions_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            evidence.record_submission(repo="o/r", issue_number=1, attempted_at=1, outcome="observed", mint=MINT)
            out1, out2 = Path(tmp) / "out1", Path(tmp) / "out2"
            export_all(evidence, out1)
            export_all(evidence, out2)
            evidence.close()
            self.assertEqual(
                (out1 / "submission.jsonl").read_bytes(),
                (out2 / "submission.jsonl").read_bytes(),
            )


# -- schema discipline: old records are never read as zero -----------------
class TestSchemaDiscipline(unittest.TestCase):
    def test_no_evidence_key_when_no_handle_was_consulted(self):
        record = Observation(mint=MINT, observed_at=1.0)
        self.assertNotIn("evidence", record.as_dict())

    def test_evidence_key_present_and_correct_when_a_handle_was_used(self):
        """01-04, PUB-01: restated against the gated shape -- a missing
        `evidence` total means the backing figure was withheld, never that
        the quantity is zero. `SUPPLY_DESTROYED`'s `burn_total` is present
        only while a check backs it, and the record names the withholding
        the moment that check fails.
        """
        passing = (invariants.Check("SUPPLY_CHECK", invariants.PASS, (invariants.SUPPLY_DESTROYED,), "x==y", "ok"),)
        record = Observation(mint=MINT, observed_at=1.0, evidence={"burn_total": 500})
        record.checks = passing
        record.verdict = invariants.apply_silence_rule(passing)
        as_dict = record.as_dict()
        self.assertEqual(as_dict["evidence"]["burn_total"], 500)
        self.assertIn(invariants.SUPPLY_DESTROYED, as_dict["backed_by"])
        self.assertIn("SUPPLY_CHECK", as_dict["backed_by"][invariants.SUPPLY_DESTROYED])

        failing = (invariants.Check("SUPPLY_CHECK", invariants.FAIL, (invariants.SUPPLY_DESTROYED,), "x==y", "bad"),)
        blocked_record = Observation(mint=MINT, observed_at=1.0, evidence={"burn_total": 500})
        blocked_record.checks = failing
        blocked_record.verdict = invariants.apply_silence_rule(failing)
        blocked_dict = blocked_record.as_dict()
        self.assertNotIn("evidence", blocked_dict)
        self.assertIn(invariants.SUPPLY_DESTROYED, blocked_dict["blocked"])
        self.assertEqual(blocked_dict["blocked"][invariants.SUPPLY_DESTROYED][0]["check"], "SUPPLY_CHECK")

    def test_schema_bumped_to_three(self):
        self.assertEqual(Observation(mint=MINT, observed_at=1.0).schema, 3)


# -- git ignores the working store, not the committed export --------------
class TestGitIgnore(unittest.TestCase):
    def test_db_ignored_export_is_not(self):
        repo_root = Path(__file__).resolve().parents[1]
        try:
            db = subprocess.run(
                ["git", "check-ignore", "state/evidence.db"],
                cwd=repo_root, capture_output=True, text=True,
            )
            export = subprocess.run(
                ["git", "check-ignore", "state/evidence/inflow.jsonl"],
                cwd=repo_root, capture_output=True, text=True,
            )
        except FileNotFoundError:
            self.skipTest("git is not available in this environment")
        self.assertEqual(db.returncode, 0, "state/*.db must be gitignored")
        self.assertEqual(export.returncode, 1, "state/evidence/*.jsonl must NOT be gitignored")


# -- falsification: the named deliverable (EVID-03) -----------------------
class TestFalsification(unittest.TestCase):
    """Corrupting the inflow log must turn a passing SOL_BURN_BALANCE red, with
    the live balance held fixed -- proof the check reads the store rather
    than recomputing independently of it.
    """

    def _passing_state(self, tmp) -> Evidence:
        evidence = evidence_db(tmp)
        evidence.record_inflow(
            signature="sig-1", destination=DERIVED_VAULT, mint=MINT, leg="sol_burn",
            lamports=500, block_time=1, slot=1,
        )
        evidence.record_inflow(
            signature="sig-2", destination=DERIVED_VAULT, mint=MINT, leg="sol_burn",
            lamports=300, block_time=2, slot=2,
        )
        evidence.set_cursor(
            DERIVED_VAULT, "inflow", last_signature="sig-2",
            oldest_signature="sig-1", backfill_complete=1,
        )
        return evidence

    def test_deleting_a_row_flips_pass_to_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = self._passing_state(tmp)
            split = sol_burn_split(DERIVED_VAULT)
            balances = {DERIVED_VAULT: 800}

            before = invariants.sol_burn_balance(split=split, evidence=evidence, balances=balances)
            self.assertEqual(before.status, invariants.PASS)

            evidence.connection.execute("DELETE FROM inflow WHERE signature = ?", ("sig-2",))
            evidence.connection.commit()

            after = invariants.sol_burn_balance(split=split, evidence=evidence, balances=balances)
            evidence.close()
            self.assertEqual(after.status, invariants.FAIL)

    def test_mutating_a_lamports_value_flips_pass_to_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = self._passing_state(tmp)
            split = sol_burn_split(DERIVED_VAULT)
            balances = {DERIVED_VAULT: 800}

            before = invariants.sol_burn_balance(split=split, evidence=evidence, balances=balances)
            self.assertEqual(before.status, invariants.PASS)

            evidence.connection.execute(
                "UPDATE inflow SET lamports = ? WHERE signature = ?", (301, "sig-2")
            )
            evidence.connection.commit()

            after = invariants.sol_burn_balance(split=split, evidence=evidence, balances=balances)
            evidence.close()
            self.assertEqual(after.status, invariants.FAIL)


# -- opening balances (EVID-02, D-05, D-06, D-07, D-08) --------------------
class TestOpeningBalance(unittest.TestCase):
    def test_recorded_when_walk_exhausts_with_a_nonzero_pre_balance(self):
        with tempfile.TemporaryDirectory() as tmp:
            tx = make_tx({DERIVED_VAULT: (1_000, 1_500)}, signature="sig-1")
            rpc = FakeScanRpc(
                signatures=[{"signature": "sig-1", "err": None, "slot": 1, "blockTime": 1}],
                transactions={"sig-1": tx},
            )
            evidence = evidence_db(tmp)
            scan_inflows(
                rpc, evidence, MINT, {DERIVED_VAULT}, leg_of=lambda _d: "sol_burn",
                target=DERIVED_VAULT, pages=1, grandfathered=GRANDFATHERED_SOL_BURN,
            )
            opening = evidence.active_opening_balance(DERIVED_VAULT)
            evidence.close()

            self.assertIsNotNone(opening)
            self.assertEqual(opening["lamports"], 1_000)
            self.assertEqual(opening["opening_signature"], "sig-1")

    def test_never_recorded_for_the_grandfathered_address(self):
        with tempfile.TemporaryDirectory() as tmp:
            tx = make_tx({SOL_BURN: (1_000, 1_500)}, signature="sig-1")
            rpc = FakeScanRpc(
                signatures=[{"signature": "sig-1", "err": None, "slot": 1, "blockTime": 1}],
                transactions={"sig-1": tx},
            )
            evidence = evidence_db(tmp)
            scan_inflows(
                rpc, evidence, MINT, {SOL_BURN}, leg_of=lambda _d: "sol_burn",
                target=SOL_BURN, pages=1, grandfathered=GRANDFATHERED_SOL_BURN,
            )
            opening = evidence.active_opening_balance(SOL_BURN)
            evidence.close()

            self.assertIsNone(opening)

    def test_retiring_leaves_the_original_row_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            original_id = evidence.record_opening_balance(
                destination=DERIVED_VAULT, lamports=1_000, opening_signature="sig-1"
            )
            new_id = evidence.retire_opening_balance(
                original_id, lamports=1_200, opening_signature="sig-0-earlier"
            )
            original = evidence.connection.execute(
                "SELECT * FROM opening_balance WHERE id = ?", (original_id,)
            ).fetchone()
            replacement = evidence.active_opening_balance(DERIVED_VAULT)
            evidence.close()

            self.assertIsNotNone(original)
            self.assertEqual(original["retired_at"] and True, True)
            self.assertEqual(original["superseded_by"], new_id)
            self.assertEqual(original["lamports"], 1_000)  # unedited
            self.assertEqual(replacement["id"], new_id)
            self.assertEqual(replacement["lamports"], 1_200)

    def test_active_opening_balance_publishes_no_reconciled_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            evidence.set_cursor(DERIVED_VAULT, "inflow", backfill_complete=1)
            evidence.record_opening_balance(
                destination=DERIVED_VAULT, lamports=1_000, opening_signature="sig-0"
            )
            evidence.record_inflow(
                signature="sig-1", destination=DERIVED_VAULT, mint=MINT, leg="sol_burn",
                lamports=500, block_time=1, slot=1,
            )
            split = sol_burn_split(DERIVED_VAULT)
            check = invariants.sol_burn_balance(
                split=split, evidence=evidence, balances={DERIVED_VAULT: 1_500}
            )
            evidence.close()

            self.assertEqual(check.status, invariants.UNCHECKED)
            self.assertNotEqual(check.status, invariants.PASS)
            self.assertIn("no total is published", check.detail)


if __name__ == "__main__":
    unittest.main()

"""Offline tests for the multi-destination scan, the per-destination
comparator, OPS_ROUTED, and the backfill-completeness gate.

`python -m unittest discover -s tests -t tests -p "test_scan.py"`.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import invariants, legs
from indexer.evidence import Evidence
from indexer.legs import Attribution, GRANDFATHERED_SOL_BURN, Registry, Split
from indexer.scan import scan_inflows, scan_inflows_all_endpoints

MINT = "8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump"
GRANDFATHERED = "burn111111111111111111111111111111111111111"
PROGRAM = "Charr1eProtoco11111111111111111111111111111"
SHAREHOLDER_A = "So11111111111111111111111111111111111111112"
SHAREHOLDER_B = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
OPS_WALLET = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


def make_tx(account_deltas: dict, err=None) -> dict:
    keys = list(account_deltas.keys())
    account_keys_field = [
        {"pubkey": key, "signer": i == 0, "writable": True, "source": "transaction"}
        for i, key in enumerate(keys)
    ]
    return {
        "transaction": {"message": {"accountKeys": account_keys_field, "instructions": []}},
        "meta": {
            "err": err,
            "preBalances": [pre for pre, _post in account_deltas.values()],
            "postBalances": [post for _pre, post in account_deltas.values()],
            "innerInstructions": [],
        },
        "blockTime": 1_000,
        "slot": 10,
    }


class FakeScanRpc:
    """Supports multi-page `signatures_for_address` via a list of pages."""

    def __init__(self, pages, transactions, balances=None, unfetchable=frozenset()):
        self._pages = list(pages)  # list of pages, newest-first, each newest-first within
        self._call_count = 0
        self._transactions = transactions
        self._balances = balances or {}
        self._unfetchable = unfetchable

    def signatures_for_address(self, address, before=None, until=None, limit=1000):
        if self._call_count >= len(self._pages):
            return []
        page = self._pages[self._call_count]
        self._call_count += 1
        if until:
            page = list(page)
            for i, entry in enumerate(page):
                if entry["signature"] == until:
                    page = page[:i]
                    break
        return page

    def transaction(self, signature):
        if signature in self._unfetchable:
            return None
        return self._transactions.get(signature)

    def balance(self, address):
        return self._balances.get(address, 0)


def evidence_db(tmp_dir: str) -> Evidence:
    return Evidence(Path(tmp_dir) / "evidence.db")


def split_with(attributions) -> Split:
    sol_burn = sum(a.bps for a in attributions if a.leg == "sol_burn")
    burn = sum(a.bps for a in attributions if a.leg == "burn")
    paid = sum(a.bps for a in attributions if a.leg == "paid")
    return Split(sol_burn=sol_burn, burn=burn, paid=paid, attributions=tuple(attributions))


def sol_burn_attr(address, bps=10_000):
    return Attribution(address=address, bps=bps, leg="sol_burn", reason="test fixture", keyless=True)


def ops_attr(address, bps=10_000):
    return Attribution(address=address, bps=bps, leg="paid", reason="test fixture", keyless=False)


# -- multi-destination writes ------------------------------------------
class TestMultiDestination(unittest.TestCase):
    def test_one_transaction_crediting_three_destinations_writes_three_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            tx = make_tx({GRANDFATHERED: (0, 100), SHAREHOLDER_A: (0, 200), SHAREHOLDER_B: (0, 300)})
            rpc = FakeScanRpc(
                pages=[[{"signature": "sig-1", "err": None, "slot": 1, "blockTime": 1}]],
                transactions={"sig-1": tx},
            )
            destinations = {GRANDFATHERED, SHAREHOLDER_A, SHAREHOLDER_B}
            evidence = evidence_db(tmp)
            scan_inflows(rpc, evidence, MINT, destinations, leg_of=lambda _d: "sol_burn",
                          target=GRANDFATHERED, pages=1)
            rows = [row for d in destinations for row in evidence.inflows_for(d)]
            evidence.close()

            self.assertEqual(len(rows), 3)
            self.assertTrue(all(row["signature"] == "sig-1" for row in rows))
            self.assertEqual({row["destination"] for row in rows}, destinations)

    def test_failed_transaction_writes_no_rows_but_cursor_advances(self):
        with tempfile.TemporaryDirectory() as tmp:
            rpc = FakeScanRpc(
                pages=[[{"signature": "sig-1", "err": {"InstructionError": [0, {}]},
                         "slot": 1, "blockTime": 1}]],
                transactions={},  # never fetched -- failed sigs are skipped before fetch
            )
            evidence = evidence_db(tmp)
            newest, oldest, complete = scan_inflows(
                rpc, evidence, MINT, {GRANDFATHERED}, leg_of=lambda _d: "sol_burn",
                target=GRANDFATHERED, pages=1,
            )
            rows = evidence.inflows_for(GRANDFATHERED)
            evidence.close()

            self.assertEqual(rows, [])
            self.assertEqual(newest, "sig-1")
            self.assertTrue(complete)  # the one-signature page was short -> exhausted

    def test_unfetchable_transaction_leaves_cursor_where_it_was(self):
        with tempfile.TemporaryDirectory() as tmp:
            rpc = FakeScanRpc(
                pages=[[{"signature": "sig-1", "err": None, "slot": 1, "blockTime": 1}]],
                transactions={},
                unfetchable={"sig-1"},
            )
            evidence = evidence_db(tmp)
            newest, oldest, complete = scan_inflows(
                rpc, evidence, MINT, {GRANDFATHERED}, leg_of=lambda _d: "sol_burn",
                target=GRANDFATHERED, pages=1,
            )
            evidence.close()

            self.assertIsNone(newest)
            self.assertIsNone(oldest)
            self.assertFalse(complete)

    def test_negative_delta_is_stored_and_excluded_from_recorded_sum(self):
        with tempfile.TemporaryDirectory() as tmp:
            credit_tx = make_tx({GRANDFATHERED: (0, 1_000)})
            debit_tx = make_tx({GRANDFATHERED: (1_000, 400)})
            rpc = FakeScanRpc(
                pages=[[
                    {"signature": "sig-credit", "err": None, "slot": 1, "blockTime": 1},
                    {"signature": "sig-debit", "err": None, "slot": 2, "blockTime": 2},
                ]],
                transactions={"sig-credit": credit_tx, "sig-debit": debit_tx},
            )
            evidence = evidence_db(tmp)
            scan_inflows(rpc, evidence, MINT, {GRANDFATHERED}, leg_of=lambda _d: "sol_burn",
                         target=GRANDFATHERED, pages=1)
            recorded = evidence.recorded_lamports(GRANDFATHERED)
            outflows = evidence.outflows_for(GRANDFATHERED)
            evidence.close()

            self.assertEqual(recorded, 1_000)  # the -600 row is NOT netted in
            self.assertEqual(len(outflows), 1)
            self.assertEqual(outflows[0]["signature"], "sig-debit")
            self.assertEqual(outflows[0]["lamports"], -600)


# -- backfill completeness gate ------------------------------------------
class TestBackfillGate(unittest.TestCase):
    def test_unchecked_while_backfill_incomplete_names_address_and_signature(self):
        with tempfile.TemporaryDirectory() as tmp:
            # A full page (limit default 1000) that never comes back short --
            # backfill_complete stays 0 after this bounded call.
            page = [
                {"signature": f"sig-{i}", "err": None, "slot": i, "blockTime": i}
                for i in range(1000)
            ]
            transactions = {row["signature"]: make_tx({GRANDFATHERED: (0, 1)}) for row in page}
            rpc = FakeScanRpc(pages=[page], transactions=transactions)
            evidence = evidence_db(tmp)
            _newest, oldest, complete = scan_inflows(
                rpc, evidence, MINT, {GRANDFATHERED}, leg_of=lambda _d: "sol_burn",
                target=GRANDFATHERED, pages=1,
            )
            self.assertFalse(complete)

            split = split_with([sol_burn_attr(GRANDFATHERED)])
            check = invariants.sol_burn_balance(split=split, evidence=evidence, balances={GRANDFATHERED: 1000})
            evidence.close()

            self.assertEqual(check.status, invariants.UNCHECKED)
            self.assertIn(GRANDFATHERED, check.detail)
            self.assertIn(oldest, check.detail)

    def test_incomplete_walk_names_signature_and_endpoint_for_the_production_path(self):
        """WR-01: `scan_inflows_all_endpoints()` -- the production path
        (D-13) -- writes cursors under real endpoint identifiers, never under
        `get_cursor(destination, "inflow")`'s single-endpoint sentinel. The
        incomplete-walk detail must read the way production actually writes,
        naming each contributing endpoint beside the signature it reached.
        """
        with tempfile.TemporaryDirectory() as tmp:
            page_a = [
                {"signature": f"sig-a-{i}", "err": None, "slot": i, "blockTime": i}
                for i in range(1000)
            ]
            transactions_a = {row["signature"]: make_tx({GRANDFATHERED: (0, 1)}) for row in page_a}
            endpoint_a = FakeScanRpc(pages=[page_a], transactions=transactions_a)

            page_b = [
                {"signature": f"sig-b-{i}", "err": None, "slot": i, "blockTime": i}
                for i in range(1000)
            ]
            transactions_b = {row["signature"]: make_tx({GRANDFATHERED: (0, 1)}) for row in page_b}
            endpoint_b = FakeScanRpc(pages=[page_b], transactions=transactions_b)

            evidence = evidence_db(tmp)
            scan_inflows_all_endpoints(
                {"endpoint-a": endpoint_a, "endpoint-b": endpoint_b},
                evidence, MINT, {GRANDFATHERED}, leg_of=lambda _d: "sol_burn",
                target=GRANDFATHERED, pages=1,
            )
            cursor_a = evidence.get_cursor(GRANDFATHERED, "inflow", endpoint="endpoint-a")
            cursor_b = evidence.get_cursor(GRANDFATHERED, "inflow", endpoint="endpoint-b")

            split = split_with([sol_burn_attr(GRANDFATHERED)])
            check = invariants.sol_burn_balance(split=split, evidence=evidence, balances={GRANDFATHERED: 1000})
            evidence.close()

            self.assertEqual(check.status, invariants.UNCHECKED)
            self.assertIn("endpoint-a", check.detail)
            self.assertIn(cursor_a["oldest_signature"], check.detail)
            self.assertIn("endpoint-b", check.detail)
            self.assertIn(cursor_b["oldest_signature"], check.detail)
            # The single-endpoint helper's "*" cursor row is simply one more
            # endpoint row to this same aggregate read -- keep it recognisable.
            self.assertNotIn('"*"', check.detail)

    def test_ops_routed_unchecked_while_backfill_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = [
                {"signature": f"sig-{i}", "err": None, "slot": i, "blockTime": i}
                for i in range(1000)
            ]
            transactions = {row["signature"]: make_tx({OPS_WALLET: (0, 1)}) for row in page}
            rpc = FakeScanRpc(pages=[page], transactions=transactions)
            evidence = evidence_db(tmp)
            scan_inflows(rpc, evidence, MINT, {OPS_WALLET}, leg_of=lambda _d: "paid",
                         target=OPS_WALLET, pages=1)

            split = split_with([ops_attr(OPS_WALLET)])
            check = invariants.ops_routed(split, evidence=evidence, balances={OPS_WALLET: 1000})
            evidence.close()

            self.assertEqual(check.status, invariants.UNCHECKED)
            self.assertNotEqual(check.status, invariants.FAIL)


# -- per-destination comparator (EVID-04) ---------------------------------
class TestPerDestinationComparator(unittest.TestCase):
    def _complete(self, evidence, destination, tmp, delta):
        tx = make_tx({destination: (0, delta)})
        rpc = FakeScanRpc(
            pages=[[{"signature": f"sig-{destination}", "err": None, "slot": 1, "blockTime": 1}]],
            transactions={f"sig-{destination}": tx},
        )
        scan_inflows(rpc, evidence, MINT, {destination}, leg_of=lambda _d: "sol_burn",
                     target=destination, pages=1)

    def test_derived_vault_gets_equals_and_grandfathered_gets_less_equal(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Registry(program_id=PROGRAM, grandfathered_sol_burn=GRANDFATHERED_SOL_BURN)
            derived_vault = registry.sol_burn_vault(MINT)
            evidence = evidence_db(tmp)

            self._complete(evidence, derived_vault, tmp, 500)
            self._complete(evidence, GRANDFATHERED, tmp, 100)

            split = split_with([sol_burn_attr(derived_vault), sol_burn_attr(GRANDFATHERED)])
            balances = {derived_vault: 500, GRANDFATHERED: 9_999_999}  # grandfathered holds strangers' funds too
            check = invariants.sol_burn_balance(split=split, evidence=evidence, balances=balances, registry=registry)
            evidence.close()

            self.assertEqual(check.status, invariants.PASS)
            self.assertIn(derived_vault, check.detail)
            self.assertIn(GRANDFATHERED, check.detail)

    def test_aggregate_fails_if_either_destination_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Registry(program_id=PROGRAM, grandfathered_sol_burn=GRANDFATHERED_SOL_BURN)
            derived_vault = registry.sol_burn_vault(MINT)
            evidence = evidence_db(tmp)

            self._complete(evidence, derived_vault, tmp, 500)
            self._complete(evidence, GRANDFATHERED, tmp, 100)

            split = split_with([sol_burn_attr(derived_vault), sol_burn_attr(GRANDFATHERED)])
            # derived vault balance no longer matches recorded -- must fail
            balances = {derived_vault: 999, GRANDFATHERED: 9_999_999}
            check = invariants.sol_burn_balance(split=split, evidence=evidence, balances=balances, registry=registry)
            evidence.close()

            self.assertEqual(check.status, invariants.FAIL)


# -- OPS_ROUTED ------------------------------------------------------------
class TestOpsRouted(unittest.TestCase):
    def test_pass_and_fail_for_a_coin_with_an_ops_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            self._complete(evidence, OPS_WALLET, tmp, 500)
            split = split_with([ops_attr(OPS_WALLET)])
            check_pass = invariants.ops_routed(split, evidence=evidence, balances={OPS_WALLET: 500})
            evidence.close()
            self.assertEqual(check_pass.status, invariants.PASS)

    def test_unchecked_when_a_complete_walk_finds_no_inflow_at_all(self):
        """This asserted FAIL until 2026-09-02, and it was wrong.

        A completed walk that finds nothing is the normal state of a coin
        whose creator fees have never been claimed -- most coins, most of the
        time. FAIL is the loudest state on the page and it means "this
        contradicts"; it must not mean "nothing happened". Measured live
        against a real third-party coin, the old behaviour branded it FAIL
        for the crime of not having traded yet, and intake had just been
        wired to publish exactly such coins.

        PASS is not available either -- nothing was reconciled, so claiming
        the routing is verified would be the overclaim the silence rule
        exists to stop. UNCHECKED is the honest state and withholds
        `ops_total` exactly as hard.

        Why the balance cannot rescue a FAIL here: an OPS destination is an
        ordinary spendable wallet, so its balance is not attributable to this
        protocol at all. A non-zero balance with no recorded inflow is a
        person's own SOL, not evidence that fees arrived unrecorded -- which
        is why `balances` is deliberately not consulted in this branch.

        The bug never surfaced on $CHARLIE because it has no OPS destination
        and returns UNCHECKED before reaching this code.
        """
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            rpc = FakeScanRpc(pages=[[]], transactions={})
            scan_inflows(rpc, evidence, MINT, {OPS_WALLET}, leg_of=lambda _d: "paid",
                         target=OPS_WALLET, pages=1)
            split = split_with([ops_attr(OPS_WALLET)])
            check = invariants.ops_routed(split, evidence=evidence, balances={OPS_WALLET: 0})
            evidence.close()
            self.assertEqual(check.status, invariants.UNCHECKED)
            self.assertIn("nothing to reconcile", check.detail)

    def test_a_wallet_holding_sol_with_no_recorded_inflow_is_still_unchecked(self):
        """An OPS wallet's balance is its owner's, not the protocol's."""
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            rpc = FakeScanRpc(pages=[[]], transactions={})
            scan_inflows(rpc, evidence, MINT, {OPS_WALLET}, leg_of=lambda _d: "paid",
                         target=OPS_WALLET, pages=1)
            split = split_with([ops_attr(OPS_WALLET)])
            check = invariants.ops_routed(split, evidence=evidence,
                                          balances={OPS_WALLET: 9_000_000_000})
            evidence.close()
            self.assertEqual(check.status, invariants.UNCHECKED)

    def _complete(self, evidence, destination, tmp, delta):
        tx = make_tx({destination: (0, delta)})
        rpc = FakeScanRpc(
            pages=[[{"signature": f"sig-{destination}", "err": None, "slot": 1, "blockTime": 1}]],
            transactions={f"sig-{destination}": tx},
        )
        scan_inflows(rpc, evidence, MINT, {destination}, leg_of=lambda _d: "paid",
                     target=destination, pages=1)

    def test_unchanged_unchecked_wording_for_a_coin_without_an_ops_destination(self):
        split = split_with([sol_burn_attr(GRANDFATHERED)])
        check = invariants.ops_routed(split)
        self.assertEqual(check.status, invariants.UNCHECKED)
        self.assertIn("no OPS destination in this split", check.detail)


class RaisingRpc:
    """An endpoint that fails outright -- e.g. `solana.drpc.org`'s HTTP 400."""

    def signatures_for_address(self, address, before=None, until=None, limit=1000):
        raise RuntimeError("endpoint refused the request")

    def transaction(self, signature):
        raise RuntimeError("endpoint refused the request")


# -- D-13: the recorded set must not depend on which endpoint answered -----
class TestUnionAcrossEndpoints(unittest.TestCase):
    def test_disjoint_signature_sets_are_unioned_not_whichever_answered_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            tx_a = make_tx({GRANDFATHERED: (0, 100)})
            tx_b = make_tx({GRANDFATHERED: (100, 500)})
            endpoint_a = FakeScanRpc(
                pages=[[{"signature": "sig-a", "err": None, "slot": 1, "blockTime": 1}]],
                transactions={"sig-a": tx_a},
            )
            endpoint_b = FakeScanRpc(
                pages=[[{"signature": "sig-b", "err": None, "slot": 2, "blockTime": 2}]],
                transactions={"sig-b": tx_b},
            )
            evidence = evidence_db(tmp)
            newest, oldest, complete, contributing = scan_inflows_all_endpoints(
                {"endpoint-a": endpoint_a, "endpoint-b": endpoint_b},
                evidence, MINT, {GRANDFATHERED}, leg_of=lambda _d: "sol_burn",
                target=GRANDFATHERED, pages=1,
            )
            rows = evidence.inflows_for(GRANDFATHERED)
            evidence.close()

            self.assertEqual({row["signature"] for row in rows}, {"sig-a", "sig-b"})
            self.assertEqual(contributing, 2)
            self.assertTrue(complete)

    def test_rerunning_the_union_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            tx_a = make_tx({GRANDFATHERED: (0, 100)})
            tx_b = make_tx({GRANDFATHERED: (100, 500)})
            evidence = evidence_db(tmp)

            def endpoints():
                return {
                    "endpoint-a": FakeScanRpc(
                        pages=[[{"signature": "sig-a", "err": None, "slot": 1, "blockTime": 1}]],
                        transactions={"sig-a": tx_a},
                    ),
                    "endpoint-b": FakeScanRpc(
                        pages=[[{"signature": "sig-b", "err": None, "slot": 2, "blockTime": 2}]],
                        transactions={"sig-b": tx_b},
                    ),
                }

            scan_inflows_all_endpoints(
                endpoints(), evidence, MINT, {GRANDFATHERED}, leg_of=lambda _d: "sol_burn",
                target=GRANDFATHERED, pages=1,
            )
            first_rows = evidence.inflows_for(GRANDFATHERED)

            scan_inflows_all_endpoints(
                endpoints(), evidence, MINT, {GRANDFATHERED}, leg_of=lambda _d: "sol_burn",
                target=GRANDFATHERED, pages=1,
            )
            second_rows = evidence.inflows_for(GRANDFATHERED)
            evidence.close()

            self.assertEqual(len(first_rows), 2)
            self.assertEqual(first_rows, second_rows)  # same rows, same lamports, unchanged

    def test_an_erroring_endpoint_is_recorded_but_does_not_abort_the_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            tx = make_tx({GRANDFATHERED: (0, 100)})
            good = FakeScanRpc(
                pages=[[{"signature": "sig-good", "err": None, "slot": 1, "blockTime": 1}]],
                transactions={"sig-good": tx},
            )
            evidence = evidence_db(tmp)
            newest, oldest, complete, contributing = scan_inflows_all_endpoints(
                {"endpoint-bad": RaisingRpc(), "endpoint-good": good},
                evidence, MINT, {GRANDFATHERED}, leg_of=lambda _d: "sol_burn",
                target=GRANDFATHERED, pages=1,
            )
            rows = evidence.inflows_for(GRANDFATHERED)
            bad_cursor = evidence.get_cursor(GRANDFATHERED, "inflow", endpoint="endpoint-bad")
            evidence.close()

            self.assertEqual(len(rows), 1)  # the good endpoint's data made it in
            self.assertIsNotNone(bad_cursor)
            self.assertIsNotNone(bad_cursor["last_error"])
            self.assertIsNone(bad_cursor["last_signature"])  # never counted as a contribution
            self.assertTrue(complete)  # the good endpoint alone completed the walk
            self.assertEqual(contributing, 1)

    def test_backfill_never_completes_while_the_only_endpoint_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            _newest, _oldest, complete, contributing = scan_inflows_all_endpoints(
                {"endpoint-bad": RaisingRpc()},
                evidence, MINT, {GRANDFATHERED}, leg_of=lambda _d: "sol_burn",
                target=GRANDFATHERED, pages=1,
            )
            evidence.close()
            self.assertFalse(complete)
            self.assertEqual(contributing, 0)

    def test_sol_burn_balance_unchecked_never_fail_while_no_endpoint_has_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            scan_inflows_all_endpoints(
                {"endpoint-bad": RaisingRpc()},
                evidence, MINT, {GRANDFATHERED}, leg_of=lambda _d: "sol_burn",
                target=GRANDFATHERED, pages=1,
            )
            split = split_with([sol_burn_attr(GRANDFATHERED)])
            check = invariants.sol_burn_balance(split=split, evidence=evidence, balances={GRANDFATHERED: 0})
            evidence.close()

            self.assertEqual(check.status, invariants.UNCHECKED)
            self.assertNotEqual(check.status, invariants.FAIL)

    def test_contributing_endpoint_count_reaches_the_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            tx = make_tx({GRANDFATHERED: (0, 100)})
            endpoint = FakeScanRpc(
                pages=[[{"signature": "sig-1", "err": None, "slot": 1, "blockTime": 1}]],
                transactions={"sig-1": tx},
            )
            evidence = evidence_db(tmp)
            scan_inflows_all_endpoints(
                {"endpoint-only": endpoint}, evidence, MINT, {GRANDFATHERED},
                leg_of=lambda _d: "sol_burn", target=GRANDFATHERED, pages=1,
            )
            coverage = evidence.cursor_endpoints(GRANDFATHERED, "inflow")
            evidence.close()
            self.assertEqual(len(coverage), 1)


# -- WR-03: "pre-balance is zero" vs "pre-balance could not be determined" --
class TestOpeningBalanceThreeWayDistinction(unittest.TestCase):
    def test_zero_pre_balance_records_no_opening_balance(self):
        """History was readable to its start and there is nothing to admit."""
        with tempfile.TemporaryDirectory() as tmp:
            tx = make_tx({SHAREHOLDER_A: (0, 500)})
            rpc = FakeScanRpc(
                pages=[[{"signature": "sig-1", "err": None, "slot": 1, "blockTime": 1}]],
                transactions={"sig-1": tx},
            )
            evidence = evidence_db(tmp)
            _newest, _oldest, complete = scan_inflows(
                rpc, evidence, MINT, {SHAREHOLDER_A}, leg_of=lambda _d: "sol_burn",
                target=SHAREHOLDER_A, pages=1,
            )
            opening = evidence.active_opening_balance(SHAREHOLDER_A)
            evidence.close()

            self.assertTrue(complete)
            self.assertIsNone(opening)

    def test_positive_pre_balance_still_records_the_admission(self):
        """Unchanged from before the three-way distinction: a real non-zero
        pre-balance still records D-05's admission.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tx = make_tx({SHAREHOLDER_A: (250, 500)})
            rpc = FakeScanRpc(
                pages=[[{"signature": "sig-1", "err": None, "slot": 1, "blockTime": 1}]],
                transactions={"sig-1": tx},
            )
            evidence = evidence_db(tmp)
            scan_inflows(
                rpc, evidence, MINT, {SHAREHOLDER_A}, leg_of=lambda _d: "sol_burn",
                target=SHAREHOLDER_A, pages=1,
            )
            opening = evidence.active_opening_balance(SHAREHOLDER_A)
            evidence.close()

            self.assertIsNotNone(opening)
            self.assertEqual(opening["lamports"], 250)

    def test_undeterminable_pre_balance_records_no_opening_balance_but_a_named_cursor_error(self):
        """`target` absent from the transaction's account list is an
        untrusted-response edge case where `_pre_balance` returns `None` --
        genuinely unknown, not zero. Must not be silently treated as
        "nothing to admit": no opening balance is fabricated, the failure is
        recorded against the destination's cursor for this endpoint naming
        the signature, and the backfill is left NOT complete so
        `SOL_BURN_BALANCE` reads `UNCHECKED` with a stated reason.
        """
        with tempfile.TemporaryDirectory() as tmp:
            # SHAREHOLDER_A is never a key in this transaction's account
            # list, so `_pre_balance(tx, SHAREHOLDER_A)` returns None.
            tx = make_tx({SHAREHOLDER_B: (0, 500)})
            rpc = FakeScanRpc(
                pages=[[{"signature": "sig-1", "err": None, "slot": 1, "blockTime": 1}]],
                transactions={"sig-1": tx},
            )
            evidence = evidence_db(tmp)
            _newest, _oldest, complete = scan_inflows(
                rpc, evidence, MINT, {SHAREHOLDER_A}, leg_of=lambda _d: "sol_burn",
                target=SHAREHOLDER_A, pages=1,
            )
            opening = evidence.active_opening_balance(SHAREHOLDER_A)
            cursor = evidence.get_cursor(SHAREHOLDER_A, "inflow")
            split = split_with([sol_burn_attr(SHAREHOLDER_A)])
            check = invariants.sol_burn_balance(split=split, evidence=evidence, balances={SHAREHOLDER_A: 500})
            evidence.close()

            self.assertFalse(complete)
            self.assertIsNone(opening)  # no fabricated zero-lamport row
            self.assertIsNotNone(cursor["last_error"])
            self.assertIn("sig-1", cursor["last_error"])
            self.assertEqual(check.status, invariants.UNCHECKED)
            self.assertNotEqual(check.status, invariants.FAIL)


class TestRecipientKind(unittest.TestCase):
    """D-40. What a fee recipient IS, from its owner program.

    Every category here was found in real data on 2026-09-02 by frequency
    analysis over 1,681 multi-shareholder configs -- none was invented to
    make the code tidy.
    """

    def test_system_owned_is_a_wallet(self):
        self.assertEqual(
            legs.recipient_kind({"owner": legs.SYSTEM_PROGRAM}), legs.RECIPIENT_WALLET)

    def test_fee_share_owned_is_another_sharing_config(self):
        """Chained fee splitting: a config naming another config. Eight of
        the twenty-four most-shared recipients were exactly this.
        """
        self.assertEqual(
            legs.recipient_kind({"owner": legs.FEE_SHARE_PROGRAM}),
            legs.RECIPIENT_SHARING_CONFIG)

    def test_either_token_program_is_a_token_account(self):
        for owner in (legs.TOKEN_PROGRAM, legs.TOKEN_2022_PROGRAM):
            with self.subTest(owner=owner):
                self.assertEqual(
                    legs.recipient_kind({"owner": owner}), legs.RECIPIENT_TOKEN_ACCOUNT)

    def test_any_other_owner_is_program_owned(self):
        self.assertEqual(
            legs.recipient_kind({"owner": "SomeOtherProgram1111111111111111111111111111"}),
            legs.RECIPIENT_PROGRAM_OWNED)

    def test_absent_account_has_never_received_a_lamport(self):
        """A finding, not an error. A Solana account is created on first
        receipt, so an address that does not exist has never been paid. Two
        addresses named by 96 configs each were in this state when measured.
        """
        self.assertEqual(legs.recipient_kind(None), legs.RECIPIENT_NEVER_FUNDED)

    def test_every_kind_is_declared(self):
        for owner in (legs.SYSTEM_PROGRAM, legs.FEE_SHARE_PROGRAM, legs.TOKEN_PROGRAM,
                      legs.TOKEN_2022_PROGRAM, "Whatever111111111111111111111111111111111111"):
            self.assertIn(legs.recipient_kind({"owner": owner}), legs.RECIPIENT_KINDS)
        self.assertIn(legs.recipient_kind(None), legs.RECIPIENT_KINDS)

    def test_is_not_a_gated_figure(self):
        """It states what an address is, never how much reached it, so it is
        an observed fact and must never appear in `invariants.FIGURES`.
        """
        for kind in legs.RECIPIENT_KINDS:
            self.assertNotIn(kind, invariants.FIGURES)


if __name__ == "__main__":
    unittest.main()

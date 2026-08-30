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

from indexer import invariants
from indexer.evidence import Evidence
from indexer.legs import Attribution, GRANDFATHERED_SEAL, Registry, Split
from indexer.scan import scan_inflows

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
    seal = sum(a.bps for a in attributions if a.leg == "seal")
    burn = sum(a.bps for a in attributions if a.leg == "burn")
    paid = sum(a.bps for a in attributions if a.leg == "paid")
    return Split(seal=seal, burn=burn, paid=paid, attributions=tuple(attributions))


def seal_attr(address, bps=10_000):
    return Attribution(address=address, bps=bps, leg="seal", reason="test fixture", keyless=True)


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
            scan_inflows(rpc, evidence, MINT, destinations, leg_of=lambda _d: "seal",
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
                rpc, evidence, MINT, {GRANDFATHERED}, leg_of=lambda _d: "seal",
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
                rpc, evidence, MINT, {GRANDFATHERED}, leg_of=lambda _d: "seal",
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
            scan_inflows(rpc, evidence, MINT, {GRANDFATHERED}, leg_of=lambda _d: "seal",
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
                rpc, evidence, MINT, {GRANDFATHERED}, leg_of=lambda _d: "seal",
                target=GRANDFATHERED, pages=1,
            )
            self.assertFalse(complete)

            split = split_with([seal_attr(GRANDFATHERED)])
            check = invariants.seal_balance(split=split, evidence=evidence, balances={GRANDFATHERED: 1000})
            evidence.close()

            self.assertEqual(check.status, invariants.UNCHECKED)
            self.assertIn(GRANDFATHERED, check.detail)
            self.assertIn(oldest, check.detail)

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
        scan_inflows(rpc, evidence, MINT, {destination}, leg_of=lambda _d: "seal",
                     target=destination, pages=1)

    def test_derived_vault_gets_equals_and_grandfathered_gets_less_equal(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Registry(program_id=PROGRAM, grandfathered_seal=GRANDFATHERED_SEAL)
            derived_vault = registry.seal_vault(MINT)
            evidence = evidence_db(tmp)

            self._complete(evidence, derived_vault, tmp, 500)
            self._complete(evidence, GRANDFATHERED, tmp, 100)

            split = split_with([seal_attr(derived_vault), seal_attr(GRANDFATHERED)])
            balances = {derived_vault: 500, GRANDFATHERED: 9_999_999}  # grandfathered holds strangers' funds too
            check = invariants.seal_balance(split=split, evidence=evidence, balances=balances, registry=registry)
            evidence.close()

            self.assertEqual(check.status, invariants.PASS)
            self.assertIn(derived_vault, check.detail)
            self.assertIn(GRANDFATHERED, check.detail)

    def test_aggregate_fails_if_either_destination_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Registry(program_id=PROGRAM, grandfathered_seal=GRANDFATHERED_SEAL)
            derived_vault = registry.seal_vault(MINT)
            evidence = evidence_db(tmp)

            self._complete(evidence, derived_vault, tmp, 500)
            self._complete(evidence, GRANDFATHERED, tmp, 100)

            split = split_with([seal_attr(derived_vault), seal_attr(GRANDFATHERED)])
            # derived vault balance no longer matches recorded -- must fail
            balances = {derived_vault: 999, GRANDFATHERED: 9_999_999}
            check = invariants.seal_balance(split=split, evidence=evidence, balances=balances, registry=registry)
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

    def test_fail_when_no_inflow_ever_recorded_despite_a_complete_walk(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            rpc = FakeScanRpc(pages=[[]], transactions={})
            scan_inflows(rpc, evidence, MINT, {OPS_WALLET}, leg_of=lambda _d: "paid",
                         target=OPS_WALLET, pages=1)
            split = split_with([ops_attr(OPS_WALLET)])
            check = invariants.ops_routed(split, evidence=evidence, balances={OPS_WALLET: 0})
            evidence.close()
            self.assertEqual(check.status, invariants.FAIL)

    def _complete(self, evidence, destination, tmp, delta):
        tx = make_tx({destination: (0, delta)})
        rpc = FakeScanRpc(
            pages=[[{"signature": f"sig-{destination}", "err": None, "slot": 1, "blockTime": 1}]],
            transactions={f"sig-{destination}": tx},
        )
        scan_inflows(rpc, evidence, MINT, {destination}, leg_of=lambda _d: "paid",
                     target=destination, pages=1)

    def test_unchanged_unchecked_wording_for_a_coin_without_an_ops_destination(self):
        split = split_with([seal_attr(GRANDFATHERED)])
        check = invariants.ops_routed(split)
        self.assertEqual(check.status, invariants.UNCHECKED)
        self.assertIn("no OPS destination in this split", check.detail)


if __name__ == "__main__":
    unittest.main()

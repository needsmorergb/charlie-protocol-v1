"""Offline tests for `BURN_ATOMIC` (EVID-09) and the publication boundary
(PUB-01/PUB-02, `indexer/publish.py`).

`python -m unittest discover -s tests -t tests -p "test_publication.py"`.

No network. Every fixture is built byte-by-byte or field-by-field in the
style of `tests/test_burns.py` -- a pump layout change must show up as a
failing decode/classification test, never as a wrong published number.
"""

from __future__ import annotations

import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import export, invariants, legs, publish, report
from indexer.evidence import Evidence
from indexer.legs import Registry, Split, split_of
from indexer.observe import Observation, observe
from indexer.pump import MintState, TOKEN_2022_PROGRAM, TOKEN_PROGRAM
from indexer.scan import classify_atomicity

CHARLIE = "8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump"
PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
GRANDFATHERED = "burn111111111111111111111111111111111111111"


# -- fixtures (mirrors tests/test_burns.py's style) --------------------------
def burn_instruction(mint, amount, program_id=TOKEN_2022_PROGRAM, account="acct-1", authority="auth-1"):
    return {
        "parsed": {
            "type": "burn",
            "info": {"account": account, "amount": str(amount), "authority": authority, "mint": mint},
        },
        "program": "spl-token",
        "programId": program_id,
        "stackHeight": 1,
    }


def transfer_checked_instruction(program_id=TOKEN_2022_PROGRAM):
    return {
        "parsed": {"type": "transferChecked", "info": {"destination": "quote-vault", "amount": "1", "decimals": 6}},
        "program": "spl-token",
        "programId": program_id,
        "stackHeight": 1,
    }


def unrelated_instruction():
    return {"parsed": {"type": "transfer", "info": {"destination": "x", "amount": "1"}}, "programId": "11111111111111111111111111111111"}


def tx_with(top_instructions=None, inner=None, err=None):
    return {
        "transaction": {"message": {"instructions": top_instructions or []}},
        "meta": {"err": err, "innerInstructions": inner or [], "logMessages": []},
        "blockTime": 1_000,
        "slot": 10,
    }


def evidence_db(tmp_dir: str) -> Evidence:
    return Evidence(Path(tmp_dir) / "evidence.db")


# -- classify_atomicity / find_swap_shaped (EVID-09) -------------------------
class TestClassifyAtomicity(unittest.TestCase):
    def test_boost_crank_shaped_tree_passes(self):
        """RESEARCH.md Q6's real instruction tree: transferChecked at inner
        index 0, burn at inner index 1, both CPIs of the same top-level
        pump-AMM instruction. This fixture is the phase's evidence that
        atomicity is checkable for burns that predate any program of ours.
        """
        tx = tx_with(
            top_instructions=[{"programId": PUMP_AMM_PROGRAM}],
            inner=[{"index": 0, "instructions": [
                transfer_checked_instruction(),
                burn_instruction(CHARLIE, 1_500_000_000),
            ]}],
        )
        self.assertEqual(classify_atomicity(tx, CHARLIE), "PASS")

    def test_lone_burn_with_no_swap_fails(self):
        tx = tx_with(top_instructions=[burn_instruction(CHARLIE, 50)])
        self.assertEqual(classify_atomicity(tx, CHARLIE), "FAIL")

    def test_swap_in_a_different_top_level_instruction_still_passes(self):
        """The requirement is one transaction, not one parent instruction."""
        tx = tx_with(top_instructions=[
            {"programId": PUMP_AMM_PROGRAM},
            burn_instruction(CHARLIE, 100),
        ])
        self.assertEqual(classify_atomicity(tx, CHARLIE), "PASS")

    def test_failed_transaction_fails(self):
        tx = tx_with(
            top_instructions=[{"programId": PUMP_AMM_PROGRAM}, burn_instruction(CHARLIE, 100)],
            err={"InstructionError": [0, "Custom"]},
        )
        self.assertEqual(classify_atomicity(tx, CHARLIE), "FAIL")

    def test_no_burn_for_the_mint_fails(self):
        tx = tx_with(top_instructions=[{"programId": PUMP_AMM_PROGRAM}, unrelated_instruction()])
        self.assertEqual(classify_atomicity(tx, CHARLIE), "FAIL")

    def test_token_transfer_quote_side_without_amm_program_still_passes(self):
        tx = tx_with(top_instructions=[
            transfer_checked_instruction(program_id=TOKEN_PROGRAM),
            burn_instruction(CHARLIE, 10, program_id=TOKEN_PROGRAM),
        ])
        self.assertEqual(classify_atomicity(tx, CHARLIE), "PASS")


# -- scan_burns populates atomic / Evidence.set_atomic / unclassified_burns --
class TestScanBurnsPopulatesAtomic(unittest.TestCase):
    def test_scan_burns_writes_the_atomic_column(self):
        from tests.test_burns import FakeBurnScanRpc
        from indexer.scan import scan_burns

        with tempfile.TemporaryDirectory() as tmp:
            boost_tx = tx_with(
                top_instructions=[{"programId": PUMP_AMM_PROGRAM}],
                inner=[{"index": 0, "instructions": [
                    transfer_checked_instruction(), burn_instruction(CHARLIE, 1_500_000_000),
                ]}],
            )
            bare_tx = tx_with(top_instructions=[burn_instruction(CHARLIE, 50)])
            rpc = FakeBurnScanRpc(
                pages=[[
                    {"signature": "sig-boost", "err": None, "slot": 1, "blockTime": 1},
                    {"signature": "sig-bare", "err": None, "slot": 2, "blockTime": 2},
                ]],
                transactions={"sig-boost": boost_tx, "sig-bare": bare_tx},
            )
            evidence = evidence_db(tmp)
            scan_burns(rpc, evidence, CHARLIE, pages=1)
            rows = {row["signature"]: row for row in evidence.burns_for(CHARLIE)}
            evidence.close()

            self.assertEqual(rows["sig-boost"]["atomic"], "PASS")
            self.assertEqual(rows["sig-bare"]["atomic"], "FAIL")

    def test_fail_classified_burn_still_contributes_to_supply_destroyed(self):
        """D-09: the mint account counted it, so the protocol does too --
        BURN_ATOMIC withholds the figure while the classification stands,
        but the tokens are never subtracted from supply_destroyed.
        """
        from tests.test_burns import FakeBurnScanRpc
        from indexer.scan import scan_burns

        with tempfile.TemporaryDirectory() as tmp:
            bare_tx = tx_with(top_instructions=[burn_instruction(CHARLIE, 777)])
            rpc = FakeBurnScanRpc(
                pages=[[{"signature": "sig-bare", "err": None, "slot": 1, "blockTime": 1}]],
                transactions={"sig-bare": bare_tx},
            )
            evidence = evidence_db(tmp)
            scan_burns(rpc, evidence, CHARLIE, pages=1)
            total = evidence.total_burned(CHARLIE)
            rows = evidence.burns_for(CHARLIE)
            evidence.close()

            self.assertEqual(total, 777)
            self.assertEqual(rows[0]["atomic"], "FAIL")

    def test_set_atomic_updates_only_the_classification_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            evidence.record_burn_event(
                signature="sig-1", mint=CHARLIE, instruction_index=0,
                tokens_burned=123, source="spl_burn", slot=1,
            )
            evidence.set_atomic("sig-1", CHARLIE, 0, "PASS")
            row = evidence.burns_for(CHARLIE)[0]
            evidence.close()

            self.assertEqual(row["atomic"], "PASS")
            self.assertEqual(row["tokens_burned"], 123)  # never overwritten

    def test_unclassified_burns_finds_only_null_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            evidence.record_burn_event(
                signature="sig-1", mint=CHARLIE, instruction_index=0,
                tokens_burned=1, source="spl_burn", slot=1,
            )
            evidence.record_burn_event(
                signature="sig-2", mint=CHARLIE, instruction_index=0,
                tokens_burned=2, source="spl_burn", slot=1, atomic="PASS",
            )
            unclassified = evidence.unclassified_burns(CHARLIE)
            evidence.close()

            self.assertEqual(len(unclassified), 1)
            self.assertEqual(unclassified[0]["signature"], "sig-1")


# -- invariants.burn_atomic ----------------------------------------------------
class TestBurnAtomicCheck(unittest.TestCase):
    def test_unchecked_when_no_burn_recorded(self):
        check = invariants.burn_atomic(CHARLIE, [], walk_complete=True)
        self.assertEqual(check.status, invariants.UNCHECKED)

    def test_unchecked_when_walk_incomplete_never_fail(self):
        rows = [{"signature": "sig-1", "atomic": "FAIL"}]
        check = invariants.burn_atomic(CHARLIE, rows, walk_complete=False)
        self.assertEqual(check.status, invariants.UNCHECKED)
        self.assertNotEqual(check.status, invariants.FAIL)

    def test_unchecked_when_a_row_is_unclassified(self):
        rows = [{"signature": "sig-1", "atomic": None}]
        check = invariants.burn_atomic(CHARLIE, rows, walk_complete=True)
        self.assertEqual(check.status, invariants.UNCHECKED)
        self.assertIn("sig-1", check.detail)

    def test_pass_when_every_row_passes(self):
        rows = [{"signature": "sig-1", "atomic": "PASS"}, {"signature": "sig-2", "atomic": "PASS"}]
        check = invariants.burn_atomic(CHARLIE, rows, walk_complete=True)
        self.assertEqual(check.status, invariants.PASS)

    def test_fail_names_the_offending_signature(self):
        rows = [{"signature": "sig-1", "atomic": "PASS"}, {"signature": "sig-2", "atomic": "FAIL"}]
        check = invariants.burn_atomic(CHARLIE, rows, walk_complete=True)
        self.assertEqual(check.status, invariants.FAIL)
        self.assertIn("sig-2", check.detail)

    def test_backs_supply_destroyed(self):
        check = invariants.burn_atomic(CHARLIE, [], walk_complete=False)
        self.assertIn(invariants.SUPPLY_DESTROYED, check.backs)

    def test_is_in_observe_checks_tuple(self):
        """`invariants.burn_atomic` must join `record.checks` in `observe.py`,
        or the new check exists without gating anything.
        """
        import inspect
        from indexer import observe as observe_module

        source = inspect.getsource(observe_module)
        self.assertIn("atomic_check", source)
        tree = ast.parse(source)
        names = [
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ]
        self.assertIn("burn_atomic", names)


# -- invariants.burn_spend ------------------------------------------------------
class TestBurnSpendCheck(unittest.TestCase):
    def test_unchecked_with_no_burn_destination(self):
        split = Split(seal=10_000, burn=0, paid=0, attributions=())
        check = invariants.burn_spend(split)
        self.assertEqual(check.status, invariants.UNCHECKED)
        self.assertIn(invariants.BURN_TOTAL, check.backs)

    def test_unchecked_even_with_a_burn_destination(self):
        attribution = legs.Attribution(address="burn-pda", bps=100, leg="burn", reason="x", keyless=True)
        split = Split(seal=9_900, burn=100, paid=0, attributions=(attribution,))
        check = invariants.burn_spend(split)
        self.assertEqual(check.status, invariants.UNCHECKED)


# -- publish.Publisher / publish.Withheld --------------------------------------
def mint_state(supply=900) -> MintState:
    return MintState(mint=CHARLIE, supply=supply, decimals=6, mint_authority=None, freeze_authority=None, program=TOKEN_2022_PROGRAM)


def make_registry() -> Registry:
    return Registry(program_id=None, grandfathered_seal=frozenset({GRANDFATHERED}))


def make_split(seal_address=GRANDFATHERED) -> Split:
    return split_of(
        type("Cfg", (), {"mint": CHARLIE, "shareholders": ((seal_address, 10_000),)})(),
        make_registry(),
    )


def build_observation(*, evidence=None, seal_balance=0, mint_supply=900, config_mismatch=False) -> Observation:
    class FakeRpc:
        def __init__(self, balance):
            self._balance = balance

        def balance(self, address):
            return self._balance

    split = make_split()
    record = Observation(mint=CHARLIE, observed_at=1.0)
    record.config = type("Cfg", (), {
        "mint": CHARLIE if not config_mismatch else "other-mint",
        "address": "config-address",
        "version": 2,
        "status": 1,
        "admin": "admin-address",
        "admin_revoked": True,
    })()
    record.graduated = True
    record.split = split
    record.mint_state = mint_state(mint_supply)
    for attribution in split.attributions:
        if attribution.leg == "seal":
            record.seal_balances[attribution.address] = seal_balance

    seal_check = invariants.seal_balance()
    ops_check = invariants.ops_routed(split)
    burn_check = invariants.burn_supply(record.mint_state)
    atomic_check = invariants.burn_atomic(CHARLIE, [], False)
    spend_check = invariants.burn_spend(split)

    if evidence is not None:
        seal_destinations = [a.address for a in split.attributions if a.leg == "seal"]
        record.evidence = {address: evidence.recorded_lamports(address) for address in seal_destinations}
        balances = {address: seal_balance for address in seal_destinations}
        seal_check = invariants.seal_balance(split=split, evidence=evidence, balances=balances, registry=make_registry())
        burn_rows = evidence.burns_for(CHARLIE)
        walk_complete = evidence.is_backfill_complete(CHARLIE, "burn")
        initial_supply_row = evidence.initial_supply_for(CHARLIE)
        burned = evidence.total_burned(CHARLIE)
        burn_check = invariants.burn_supply(record.mint_state, initial_supply_row, burned, walk_complete)
        atomic_check = invariants.burn_atomic(CHARLIE, burn_rows, walk_complete)
        record.evidence["burn_total"] = burned

    record.checks = (
        invariants.config_mint(CHARLIE, record.config),
        invariants.split_sum(split),
        invariants.seal_unspendable(split),
        seal_check,
        burn_check,
        invariants.burn_irreversible(record.mint_state),
        atomic_check,
        spend_check,
        ops_check,
    )
    record.verdict = invariants.apply_silence_rule(record.checks)
    return record


class TestPublisher(unittest.TestCase):
    def test_figure_raises_withheld_when_blocked(self):
        observation = build_observation(config_mismatch=True)  # CONFIG_MINT fails -> SPLIT blocked
        publisher = publish.Publisher(observation)
        with self.assertRaises(publish.Withheld):
            publisher.figure(invariants.SPLIT)

    def test_figure_returns_value_and_backing_checks_when_publishable(self):
        observation = build_observation()
        publisher = publish.Publisher(observation)
        value, backs = publisher.figure(invariants.SPLIT)
        self.assertEqual(value, {"seal": 10_000, "burn": 0, "paid": 0})
        self.assertIn("CONFIG_MINT", backs)
        self.assertIn("SPLIT_SUM", backs)

    def test_supply_destroyed_withheld_while_burn_atomic_is_unchecked(self):
        observation = build_observation()
        publisher = publish.Publisher(observation)
        with self.assertRaises(publish.Withheld):
            publisher.figure(invariants.SUPPLY_DESTROYED)

    def test_burn_total_always_withheld_this_phase(self):
        observation = build_observation()
        publisher = publish.Publisher(observation)
        with self.assertRaises(publish.Withheld):
            publisher.figure(invariants.BURN_TOTAL)


class TestSilenceRuleSweep(unittest.TestCase):
    """PUB-01/PUB-02: drive the sweep from `invariants.FIGURES` itself, not
    a hand-written list -- a figure added later without a backing check
    fails these tests rather than slipping through.
    """

    def test_every_blocked_figure_value_is_absent_from_report_and_json(self):
        observation = build_observation(config_mismatch=True)  # blocks SPLIT
        text = report.render(observation)
        record = publish.public_record(observation)
        json_text = json.dumps(record, sort_keys=True)

        publisher = publish.Publisher(observation)
        for figure in invariants.FIGURES:
            if publisher.verdict.may_publish(figure):
                continue
            source = publish.FIGURE_SOURCES.get(figure)
            if source is None:
                continue
            value = source(observation)
            if value is None:
                continue
            rendered = str(value)
            self.assertNotIn(rendered, text, f"{figure}'s value leaked into report text")
            self.assertNotIn(rendered, json_text, f"{figure}'s value leaked into JSON")

    def test_every_publishable_figure_shows_value_with_backing_check(self):
        observation = build_observation()
        text = report.render(observation)
        publisher = publish.Publisher(observation)
        for figure in invariants.FIGURES:
            if not publisher.verdict.may_publish(figure):
                continue
            value, backs = publisher.figure(figure)
            self.assertTrue(backs, f"{figure} publishable with no backing check names")
            for check_name in backs:
                self.assertIn(check_name, text)

    def test_no_figure_resolves_to_no_check_for_a_complete_observation(self):
        observation = build_observation()
        for figure in invariants.FIGURES:
            reasons = observation.verdict.blocked.get(figure)
            if reasons is None:
                continue  # publishable -- nothing withheld to inspect
            names = [name for name, _status, _detail in reasons]
            self.assertNotIn("NO_CHECK", names, f"{figure} rests on NO_CHECK")


# -- export: discrepancy table registered, determinism across all tables ----
class TestExportIncludesEveryPhaseTable(unittest.TestCase):
    def test_discrepancy_is_registered_in_export_tables(self):
        names = [table for table, _order in export.EXPORT_TABLES]
        self.assertIn("discrepancy", names)

    def test_two_exports_of_an_unchanged_database_are_byte_identical_for_every_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            evidence.record_burn_event(signature="sig-1", mint=CHARLIE, instruction_index=0, tokens_burned=1, source="spl_burn", slot=1, atomic="PASS")
            evidence.record_initial_supply(mint=CHARLIE, raw_supply=1000, decimals=6)
            evidence.record_discrepancy(
                mint=CHARLIE, observed_at=1, live_supply=900, attributed_burned=1, decimals=6,
                initial_supply=1000, implied_total_burned=100, residual=99,
            )
            out_dir = Path(tmp) / "out"
            export.export_all(evidence, out_dir)
            first = {p.name: p.read_bytes() for p in out_dir.glob("*.jsonl")}
            export.export_all(evidence, out_dir)
            second = {p.name: p.read_bytes() for p in out_dir.glob("*.jsonl")}
            evidence.close()

            self.assertEqual(first, second)
            self.assertGreaterEqual(len(first), len(export.EXPORT_TABLES))

    def test_exported_record_keys_equal_table_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            evidence.record_discrepancy(
                mint=CHARLIE, observed_at=1, live_supply=900, attributed_burned=1, decimals=6,
            )
            out_dir = Path(tmp) / "out"
            export.export_all(evidence, out_dir)
            for table, _order in export.EXPORT_TABLES:
                columns = {row[1] for row in evidence.connection.execute(f"PRAGMA table_info({table})")}
                path = out_dir / f"{table}.jsonl"
                for line in path.read_text(encoding="utf-8").splitlines():
                    record = json.loads(line)
                    self.assertEqual(set(record.keys()), columns, f"{table} exported keys mismatch columns")
            evidence.close()


# -- discipline-style check: every figure-formatting module imports publish --
class TestFigureFormattingModulesImportPublish(unittest.TestCase):
    FIGURE_ATTRS = {"SPLIT", "SEAL_TOTAL", "BURN_TOTAL", "OPS_TOTAL", "SUPPLY_DESTROYED"}

    def test_every_module_referencing_a_figure_imports_publish(self):
        indexer_dir = Path(__file__).resolve().parents[1] / "indexer"
        offenders = []
        for path in sorted(indexer_dir.glob("*.py")):
            if path.name in ("invariants.py", "publish.py"):
                continue
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            references_a_figure = any(
                isinstance(node, ast.Attribute)
                and node.attr in self.FIGURE_ATTRS
                and isinstance(node.value, ast.Name)
                and node.value.id == "invariants"
                for node in ast.walk(tree)
            )
            if not references_a_figure:
                continue
            imports_publish = any(
                isinstance(node, ast.ImportFrom)
                and node.level and node.level > 0
                and any(alias.name == "publish" for alias in node.names)
                for node in ast.walk(tree)
            )
            if not imports_publish:
                offenders.append(path.name)
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()

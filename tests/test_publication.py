"""Offline tests for `BURN_ATOMIC` (EVID-09) and the publication boundary
(PUB-01/PUB-02, `indexer/publish.py`).

`python -m unittest discover -s tests -t tests -p "test_publication.py"`.

No network. Every fixture is built byte-by-byte or field-by-field in the
style of `tests/test_burns.py` -- a pump layout change must show up as a
failing decode/classification test, never as a wrong published number.
"""

from __future__ import annotations

import ast
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import export, invariants, legs, publish, report, site
from indexer.evidence import Evidence
from indexer.legs import Registry, Split, split_of
from indexer.observe import Observation, observe
from indexer.pump import MintState, TOKEN_2022_PROGRAM, TOKEN_PROGRAM
from indexer.scan import classify_atomicity

CHARLIE = "8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump"
PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
GRANDFATHERED = "burn111111111111111111111111111111111111111"
# An ordinary on-curve address: someone holds its key, so SOL that reaches it
# can leave again. Fixtures use it where they need SOL_BURN_UNSPENDABLE to FAIL,
# which a recognised burn address no longer does.
SPENDABLE = "So11111111111111111111111111111111111111112"


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
        # D-14: protocol_attributed=1 so this exercises the classification
        # ladder itself, not the not-applicable reading in front of it.
        rows = [{"signature": "sig-1", "atomic": None, "protocol_attributed": 1}]
        check = invariants.burn_atomic(CHARLIE, rows, walk_complete=True)
        self.assertEqual(check.status, invariants.UNCHECKED)
        self.assertIn("sig-1", check.detail)

    def test_pass_when_every_row_passes(self):
        rows = [
            {"signature": "sig-1", "atomic": "PASS", "protocol_attributed": 1},
            {"signature": "sig-2", "atomic": "PASS", "protocol_attributed": 1},
        ]
        check = invariants.burn_atomic(CHARLIE, rows, walk_complete=True)
        self.assertEqual(check.status, invariants.PASS)

    def test_fail_names_the_offending_signature(self):
        rows = [
            {"signature": "sig-1", "atomic": "PASS", "protocol_attributed": 1},
            {"signature": "sig-2", "atomic": "FAIL", "protocol_attributed": 1},
        ]
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


# -- D-14: BURN_ATOMIC narrowed to the protocol's own BURN leg -------------
class TestBurnAtomicProtocolScope(unittest.TestCase):
    """The two proofs D-14 requires, asserted end-to-end through
    `apply_silence_rule`/`Publisher` rather than on the check object alone --
    `TestBurnAtomicCheck` above already pins the object-level ladder; this
    proves the narrowing actually changes what `SUPPLY_DESTROYED` does.
    """

    def _build(self, evidence, *, protocol_attributed: int, atomic: str):
        burned_amount = 100
        initial_supply = 1_000
        split = make_split()
        record = Observation(mint=CHARLIE, observed_at=1.0)
        record.config = type("Cfg", (), {
            "mint": CHARLIE, "address": "config-address", "version": 2, "status": 1,
            "admin": "admin-address", "admin_revoked": True,
            "shareholders": tuple((a.address, a.bps) for a in split.attributions),
        })()
        record.graduated = True
        record.split = split
        record.mint_state = mint_state(initial_supply - burned_amount)

        evidence.record_burn_event(
            signature="sig-scope", mint=CHARLIE, instruction_index=0,
            tokens_burned=burned_amount, source="spl_burn", slot=1,
            protocol_attributed=protocol_attributed, atomic=atomic,
        )
        evidence.set_cursor(CHARLIE, "burn", backfill_complete=1)
        evidence.record_initial_supply(mint=CHARLIE, raw_supply=initial_supply, decimals=6)

        burn_rows = evidence.burns_for(CHARLIE)
        walk_complete = evidence.is_backfill_complete(CHARLIE, "burn")
        initial_supply_row = evidence.initial_supply_for(CHARLIE)
        burned = evidence.total_burned(CHARLIE)
        burn_check = invariants.burn_supply(record.mint_state, initial_supply_row, burned, walk_complete)
        atomic_check = invariants.burn_atomic(CHARLIE, burn_rows, walk_complete)

        record.evidence = {"burn_total": burned}
        record.checks = (
            invariants.config_mint(CHARLIE, record.config),
            invariants.split_sum(split),
            invariants.sol_burn_unspendable(split),
            invariants.sol_burn_balance(),
            burn_check,
            invariants.burn_irreversible(record.mint_state),
            atomic_check,
            invariants.burn_spend(split),
            invariants.ops_routed(split),
        )
        record.verdict = invariants.apply_silence_rule(record.checks)
        return record, atomic_check

    def test_third_party_fail_no_longer_withholds_supply_destroyed(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            observation, atomic_check = self._build(evidence, protocol_attributed=0, atomic="FAIL")
            evidence.close()

        # The not-applicable reading: UNCHECKED, empty backs, never PASS --
        # a vacuous PASS would put BURN_ATOMIC on phase 2's page as a check
        # backing a figure it never evaluated.
        self.assertEqual(atomic_check.status, invariants.UNCHECKED)
        self.assertEqual(atomic_check.backs, ())
        self.assertIn("not-applicable", atomic_check.detail)

        publisher = publish.Publisher(observation)
        value, backs = publisher.figure(invariants.SUPPLY_DESTROYED)  # must not raise Withheld
        self.assertEqual(value, 100)
        self.assertNotIn("BURN_ATOMIC", backs)

    def test_protocol_attributed_non_atomic_still_withholds_supply_destroyed(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            observation, atomic_check = self._build(evidence, protocol_attributed=1, atomic="FAIL")
            evidence.close()

        self.assertEqual(atomic_check.status, invariants.FAIL)
        publisher = publish.Publisher(observation)
        with self.assertRaises(publish.Withheld) as ctx:
            publisher.figure(invariants.SUPPLY_DESTROYED)
        names = [name for name, _status, _detail in ctx.exception.reasons]
        self.assertIn("BURN_ATOMIC", names)


# -- invariants.burn_spend ------------------------------------------------------
class TestBurnSpendCheck(unittest.TestCase):
    def test_unchecked_with_no_burn_destination(self):
        split = Split(sol_burn=10_000, burn=0, paid=0, attributions=())
        check = invariants.burn_spend(split)
        self.assertEqual(check.status, invariants.UNCHECKED)
        self.assertIn(invariants.BURN_TOTAL, check.backs)

    def test_unchecked_even_with_a_burn_destination(self):
        attribution = legs.Attribution(address="burn-pda", bps=100, leg="burn", reason="x", keyless=True)
        split = Split(sol_burn=9_900, burn=100, paid=0, attributions=(attribution,))
        check = invariants.burn_spend(split)
        self.assertEqual(check.status, invariants.UNCHECKED)


# -- publish.Publisher / publish.Withheld --------------------------------------
def mint_state(supply=900) -> MintState:
    return MintState(mint=CHARLIE, supply=supply, decimals=6, mint_authority=None, freeze_authority=None, program=TOKEN_2022_PROGRAM)


def make_registry(sol_burn_address=GRANDFATHERED) -> Registry:
    """The attributing registry.

    `sol_burn_address` is grandfathered here so `split_of` puts it on the
    sol_burn leg. That is what makes a spendable destination reachable at all:
    `legs.py` will not attribute an ordinary wallet to sol_burn on its own.
    """
    return Registry(program_id=None, grandfathered_sol_burn=frozenset({sol_burn_address}))


def make_split(sol_burn_address=GRANDFATHERED) -> Split:
    return split_of(
        type("Cfg", (), {"mint": CHARLIE, "shareholders": ((sol_burn_address, 10_000),)})(),
        make_registry(sol_burn_address),
    )


def build_observation(*, evidence=None, sol_burn_balance=0, mint_supply=900,
                     config_mismatch=False, sol_burn_spendable=False) -> Observation:
    class FakeRpc:
        def __init__(self, balance):
            self._balance = balance

        def balance(self, address):
            return self._balance

    destination = SPENDABLE if sol_burn_spendable else GRANDFATHERED
    split = make_split(destination)
    record = Observation(mint=CHARLIE, observed_at=1.0)
    record.config = type("Cfg", (), {
        "mint": CHARLIE if not config_mismatch else "other-mint",
        "address": "config-address",
        "version": 2,
        "status": 1,
        "admin": "admin-address",
        "admin_revoked": True,
        "shareholders": tuple((a.address, a.bps) for a in split.attributions),
    })()
    record.graduated = True
    record.split = split
    record.mint_state = mint_state(mint_supply)
    for attribution in split.attributions:
        if attribution.leg == "sol_burn":
            record.sol_burn_balances[attribution.address] = sol_burn_balance

    sol_burn_check = invariants.sol_burn_balance()
    ops_check = invariants.ops_routed(split)
    burn_check = invariants.burn_supply(record.mint_state)
    atomic_check = invariants.burn_atomic(CHARLIE, [], False)
    spend_check = invariants.burn_spend(split)

    if evidence is not None:
        sol_burn_destinations = [a.address for a in split.attributions if a.leg == "sol_burn"]
        record.evidence = {address: evidence.recorded_lamports(address) for address in sol_burn_destinations}
        balances = {address: sol_burn_balance for address in sol_burn_destinations}
        sol_burn_check = invariants.sol_burn_balance(split=split, evidence=evidence, balances=balances, registry=make_registry(destination))
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
        invariants.sol_burn_unspendable(split),
        sol_burn_check,
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
        self.assertEqual(value, {"sol_burn": 10_000, "burn": 0, "paid": 0})
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


class TestNoInflowsIsUncheckedNotZero(unittest.TestCase):
    """A destination with no recorded inflow has no SOL burn total, not a
    total of zero.

    The grandfathered address is checked under `<=`, and `0 <= balance` is
    true for every balance there is. Before this was caught, an evidence
    store that had never walked the destination passed the check vacuously
    and the page published "0 lamports" for a coin that has burned SOL, which
    reads as "this coin burned nothing". Nothing measured is UNCHECKED.
    """

    def _observation(self, tmp, balance):
        evidence = evidence_db(tmp)
        try:
            # The walk finished and found nothing. An unfinished walk is its
            # own UNCHECKED, upstream of this one; the case here is the walk
            # that completes over a destination with no inflow to record.
            evidence.set_cursor(GRANDFATHERED, "inflow", backfill_complete=1)
            return build_observation(evidence=evidence, sol_burn_balance=balance)
        finally:
            evidence.close()

    def test_sol_burn_balance_is_unchecked_with_nothing_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            observation = self._observation(tmp, balance=178_734_302_038)
        check = {c.name: c for c in observation.checks}["SOL_BURN_BALANCE"]
        self.assertEqual(check.status, invariants.UNCHECKED)
        self.assertIn("no inflows are recorded", check.detail)
        self.assertIn(GRANDFATHERED, check.detail)

    def test_sol_burn_total_is_withheld_rather_than_published_as_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            observation = self._observation(tmp, balance=178_734_302_038)
        publisher = publish.Publisher(observation)
        with self.assertRaises(publish.Withheld) as ctx:
            publisher.figure(invariants.SOL_BURN_TOTAL)
        self.assertIn("SOL_BURN_BALANCE", [name for name, _s, _d in ctx.exception.reasons])

    def test_a_zero_balance_is_still_not_a_measurement(self):
        """The vacuous case exactly: 0 recorded, 0 live. It used to PASS."""
        with tempfile.TemporaryDirectory() as tmp:
            observation = self._observation(tmp, balance=0)
        check = {c.name: c for c in observation.checks}["SOL_BURN_BALANCE"]
        self.assertEqual(check.status, invariants.UNCHECKED)


SENTINEL_PROGRAM = "Charr1eProtoco11111111111111111111111111111"
SENTINEL_OPS_ADDRESS = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
SOL_BURN_RECORDED_SENTINEL = 24_680_135
OPS_RECORDED_SENTINEL = 13_579_246
BURN_SENTINEL_TOKENS = 98_765_432_100
SPLIT_SOL_BURN_BPS = 4_242
SPLIT_PAID_BPS = 5_758
INITIAL_SUPPLY_SENTINEL = 1_000_000_000_000
# Deliberately different from SOL_BURN_RECORDED_SENTINEL -- 01-04's plan calls
# this out by name: a naive sweep that never distinguishes the live vault
# balance (a non-figure fact report.py always prints) from the recorded
# inflow total (SOL_BURN_TOTAL's actual figure source) could pass even when the
# two happen to collide. Two DIFFERENT sentinels is what forces a real test.
LIVE_SOL_BURN_BALANCE_SENTINEL_BLOCKED = 777_000_111

FULL_DETAIL_SURFACES = ("report_text", "observe_json", "durable_record", "web_page")


def _sentinel_registry() -> Registry:
    return Registry(program_id=SENTINEL_PROGRAM, grandfathered_sol_burn=frozenset({GRANDFATHERED}))


def _build_sentinel_split():
    """Always classified against `CHARLIE` -- a CONFIG_MINT mismatch is
    simulated separately, on `record.config.mint` only, exactly like
    `build_observation(config_mismatch=True)` does above: `split_of()` must
    keep classifying the real split, or a mismatched mint would derive a
    different PDA and silently reclassify the SOL-burn address as OPS instead of
    exercising the CONFIG_MINT gate this fixture exists to test.
    """
    registry = _sentinel_registry()
    sol_burn_address = registry.sol_burn_vault(CHARLIE)
    config = type("Cfg", (), {
        "mint": CHARLIE,
        "shareholders": ((sol_burn_address, SPLIT_SOL_BURN_BPS), (SENTINEL_OPS_ADDRESS, SPLIT_PAID_BPS)),
    })()
    return split_of(config, registry), sol_burn_address, registry, config.shareholders


def build_all_publishable_sentinel_observation(evidence: Evidence) -> Observation:
    """Every figure PASSES, each backed by a sentinel value distinctive
    enough that only a real gate -- not a `str(value) in text` coincidence --
    could make this sweep pass.
    """
    split, sol_burn_address, registry, shareholders = _build_sentinel_split()

    evidence.record_inflow(signature="sig-sol-burn", destination=sol_burn_address, mint=CHARLIE, leg="sol_burn",
                            lamports=SOL_BURN_RECORDED_SENTINEL, block_time=1, slot=1)
    evidence.set_cursor(sol_burn_address, "inflow", backfill_complete=1)
    evidence.record_inflow(signature="sig-ops", destination=SENTINEL_OPS_ADDRESS, mint=CHARLIE, leg="paid",
                            lamports=OPS_RECORDED_SENTINEL, block_time=1, slot=1)
    evidence.set_cursor(SENTINEL_OPS_ADDRESS, "inflow", backfill_complete=1)
    evidence.record_burn_event(signature="sig-burn", mint=CHARLIE, instruction_index=0,
                                tokens_burned=BURN_SENTINEL_TOKENS, source="spl_burn", slot=1,
                                protocol_attributed=1, atomic="PASS")
    evidence.set_cursor(CHARLIE, "burn", backfill_complete=1)
    evidence.record_initial_supply(mint=CHARLIE, raw_supply=INITIAL_SUPPLY_SENTINEL, decimals=6)

    record = Observation(mint=CHARLIE, observed_at=1.0)
    record.config = type("Cfg", (), {
        "mint": CHARLIE, "address": "config-address", "version": 2, "status": 1,
        "admin": "admin-address", "admin_revoked": True, "shareholders": shareholders,
    })()
    record.graduated = True
    record.split = split
    record.mint_state = mint_state(INITIAL_SUPPLY_SENTINEL - BURN_SENTINEL_TOKENS)
    record.sol_burn_balances[sol_burn_address] = SOL_BURN_RECORDED_SENTINEL  # "==" comparator: must match exactly

    sol_burn_destinations = [a.address for a in split.attributions if a.leg == "sol_burn"]
    ops_destinations = [a.address for a in split.attributions if a.leg == "paid"]
    record.evidence = {addr: evidence.recorded_lamports(addr) for addr in sol_burn_destinations + ops_destinations}
    record.evidence_coverage = {
        addr: len(evidence.cursor_endpoints(addr, "inflow")) for addr in sol_burn_destinations + ops_destinations
    }

    balances = dict(record.sol_burn_balances)
    for addr in ops_destinations:
        balances[addr] = 0  # OPS_ROUTED only requires recorded > 0, not a balance match

    sol_burn_check = invariants.sol_burn_balance(split=split, evidence=evidence, balances=balances, registry=registry)
    ops_check = invariants.ops_routed(split, evidence=evidence, balances=balances)
    burn_rows = evidence.burns_for(CHARLIE)
    walk_complete = evidence.is_backfill_complete(CHARLIE, "burn")
    initial_supply_row = evidence.initial_supply_for(CHARLIE)
    burned = evidence.total_burned(CHARLIE)
    burn_check = invariants.burn_supply(record.mint_state, initial_supply_row, burned, walk_complete)
    atomic_check = invariants.burn_atomic(CHARLIE, burn_rows, walk_complete)
    spend_check = invariants.burn_spend(split)
    record.evidence["burn_total"] = burned
    record.evidence["initial_supply"] = initial_supply_row

    record.checks = (
        invariants.config_mint(CHARLIE, record.config),
        invariants.split_sum(split),
        invariants.sol_burn_unspendable(split),
        sol_burn_check,
        burn_check,
        invariants.burn_irreversible(record.mint_state),
        atomic_check,
        spend_check,
        ops_check,
    )
    record.verdict = invariants.apply_silence_rule(record.checks)
    return record


def build_all_blocked_sentinel_observation(evidence: Evidence) -> Observation:
    """Every figure BLOCKED, by a single `CONFIG_MINT` mismatch -- yet the
    same sentinel values are genuinely present in `observation.evidence`
    (recorded independently of any check's status), so a leak here is a real
    bypass, not an artifact of the fixture never having the data at all.

    The inflow/burn walks are deliberately left incomplete: `SOL_BURN_BALANCE`/
    `OPS_ROUTED`/`BURN_SUPPLY`/`BURN_ATOMIC` all resolve to their natural
    UNCHECKED "walk incomplete" branch, which -- unlike their PASS/FAIL
    branches -- never populates `expected`/`actual` with the recorded
    numbers. That keeps the sentinel check honest: the only way a sentinel
    could appear on a surface is through the actual bypass this plan closes,
    not through the check's own legitimate, always-shown diagnostic fields.
    """
    split, sol_burn_address, registry, shareholders = _build_sentinel_split()

    evidence.record_inflow(signature="sig-sol-burn", destination=sol_burn_address, mint=CHARLIE, leg="sol_burn",
                            lamports=SOL_BURN_RECORDED_SENTINEL, block_time=1, slot=1)
    evidence.record_inflow(signature="sig-ops", destination=SENTINEL_OPS_ADDRESS, mint=CHARLIE, leg="paid",
                            lamports=OPS_RECORDED_SENTINEL, block_time=1, slot=1)
    evidence.record_burn_event(signature="sig-burn", mint=CHARLIE, instruction_index=0,
                                tokens_burned=BURN_SENTINEL_TOKENS, source="spl_burn", slot=1,
                                protocol_attributed=1, atomic="PASS")
    evidence.record_initial_supply(mint=CHARLIE, raw_supply=INITIAL_SUPPLY_SENTINEL, decimals=6)

    record = Observation(mint=CHARLIE, observed_at=1.0)
    record.config = type("Cfg", (), {
        "mint": "some-other-mint", "address": "config-address", "version": 2, "status": 1,
        "admin": "admin-address", "admin_revoked": True, "shareholders": shareholders,
    })()
    record.graduated = True
    record.split = split
    record.mint_state = mint_state(INITIAL_SUPPLY_SENTINEL - BURN_SENTINEL_TOKENS)
    record.sol_burn_balances[sol_burn_address] = LIVE_SOL_BURN_BALANCE_SENTINEL_BLOCKED

    sol_burn_destinations = [a.address for a in split.attributions if a.leg == "sol_burn"]
    ops_destinations = [a.address for a in split.attributions if a.leg == "paid"]
    record.evidence = {addr: evidence.recorded_lamports(addr) for addr in sol_burn_destinations + ops_destinations}
    record.evidence_coverage = {
        addr: len(evidence.cursor_endpoints(addr, "inflow")) for addr in sol_burn_destinations + ops_destinations
    }

    balances = dict(record.sol_burn_balances)
    for addr in ops_destinations:
        balances[addr] = 0

    sol_burn_check = invariants.sol_burn_balance(split=split, evidence=evidence, balances=balances, registry=registry)
    ops_check = invariants.ops_routed(split, evidence=evidence, balances=balances)
    burn_rows = evidence.burns_for(CHARLIE)
    walk_complete = evidence.is_backfill_complete(CHARLIE, "burn")  # False -- never marked complete
    initial_supply_row = evidence.initial_supply_for(CHARLIE)
    burned = evidence.total_burned(CHARLIE)
    burn_check = invariants.burn_supply(record.mint_state, initial_supply_row, burned, walk_complete)
    atomic_check = invariants.burn_atomic(CHARLIE, burn_rows, walk_complete)
    spend_check = invariants.burn_spend(split)
    record.evidence["burn_total"] = burned
    record.evidence["initial_supply"] = initial_supply_row

    record.checks = (
        invariants.config_mint(CHARLIE, record.config),  # FAILS -- backs every FIGURE
        invariants.split_sum(split),
        invariants.sol_burn_unspendable(split),
        sol_burn_check,
        burn_check,
        invariants.burn_irreversible(record.mint_state),
        atomic_check,
        spend_check,
        ops_check,
    )
    record.verdict = invariants.apply_silence_rule(record.checks)
    return record


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

    # -- driven from invariants.FIGURES x publish.SURFACES, sentinel-valued --
    def test_no_blocked_figures_sentinel_leaks_into_any_registered_surface(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            observation = build_all_blocked_sentinel_observation(evidence)
            stored_records = [observation.as_dict()]
            evidence.close()

        self.assertEqual(observation.verdict.publishable, frozenset())
        # SPLIT's sentinel is the composed {"sol_burn":..,"burn":..,"paid":..}
        # shape, not a bare bps number: `config.shareholders` legitimately
        # carries the same raw bps unconditionally (it is chain-read input,
        # not the classified/checked SPLIT figure -- report.py never prints
        # it either), so a bare-number containment check would false-fail on
        # that always-visible, correctly-unguarded field.
        sentinels = {
            invariants.SPLIT: json.dumps({"sol_burn": SPLIT_SOL_BURN_BPS, "burn": 0, "paid": SPLIT_PAID_BPS}, sort_keys=True),
            invariants.SOL_BURN_TOTAL: str(SOL_BURN_RECORDED_SENTINEL),
            invariants.OPS_TOTAL: str(OPS_RECORDED_SENTINEL),
            invariants.SUPPLY_DESTROYED: str(BURN_SENTINEL_TOKENS),
        }

        for surface_name, entry in publish.SURFACES.items():
            subject = observation if entry["input"] == "observation" else stored_records
            rendered = publish.render_surface(surface_name, subject)
            for figure, sentinel in sentinels.items():
                self.assertNotIn(sentinel, rendered, f"{figure}'s sentinel leaked into {surface_name}")

    def test_every_publishable_figure_shows_its_sentinel_and_backing_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            observation = build_all_publishable_sentinel_observation(evidence)
            stored_records = [observation.as_dict()]
            evidence.close()

        publisher = publish.Publisher(observation)
        needles = {
            invariants.SPLIT: (str(SPLIT_SOL_BURN_BPS), str(SPLIT_PAID_BPS)),
            invariants.SOL_BURN_TOTAL: (str(SOL_BURN_RECORDED_SENTINEL),),
            invariants.OPS_TOTAL: (str(OPS_RECORDED_SENTINEL),),
            invariants.SUPPLY_DESTROYED: (str(BURN_SENTINEL_TOKENS),),
        }
        for surface_name in FULL_DETAIL_SURFACES:
            rendered = publish.render_surface(surface_name, observation)
            for figure, needle_values in needles.items():
                if not publisher.verdict.may_publish(figure):
                    continue
                _value, backs = publisher.figure(figure)
                self.assertTrue(backs, f"{figure} publishable with no backing check names")
                for needle in needle_values:
                    self.assertIn(needle, rendered, f"{figure}'s value {needle!r} missing from {surface_name}")
                for name in backs:
                    self.assertIn(name, rendered, f"{figure}'s backing check {name!r} missing from {surface_name}")

        # log_text keeps its pre-existing, deliberately compact design: only
        # SPLIT appears in the one-line summary.
        log_text = publish.render_surface("log_text", stored_records)
        self.assertIn(str(SPLIT_SOL_BURN_BPS), log_text)

        # log_json passes the already-gated stored record through verbatim --
        # every publishable figure's value survives replay.
        log_json = publish.render_surface("log_json", stored_records)
        self.assertIn(str(SOL_BURN_RECORDED_SENTINEL), log_json)
        self.assertIn(str(OPS_RECORDED_SENTINEL), log_json)
        self.assertIn(str(BURN_SENTINEL_TOKENS), log_json)


# -- QT-01/QT-03 (02-03 quick task): landing_page is swept but shows nothing -
class TestLandingPageExcludedFromFullDetailSurfaces(unittest.TestCase):
    """`landing_page` is registered in `publish.SURFACES` (so
    `TestSilenceRuleSweep`'s sweep covers it automatically the moment it is
    added) but is deliberately absent from `FULL_DETAIL_SURFACES`: those
    surfaces are required to show every publishable figure, and the landing
    page is required to show none at all -- a decision, not an oversight.
    """

    def test_landing_page_registered_in_surfaces(self):
        self.assertIn("landing_page", publish.SURFACES)

    def test_landing_page_not_in_full_detail_surfaces(self):
        self.assertNotIn("landing_page", FULL_DETAIL_SURFACES)


# -- the durable record: the blocking gap 01-VERIFICATION.md reproduced -----
class TestDurableRecordSilence(unittest.TestCase):
    """The inverse of the verifier's reproduction: what
    `Observation.as_dict()` used to serialise with no reference to
    `self.verdict` at all must now obey exactly the rule `report.render()`/
    `publish.public_record()` already enforced.
    """

    def test_config_mint_failed_serialises_no_split_value_anywhere(self):
        observation = build_observation(config_mismatch=True)
        record = observation.as_dict()

        self.assertNotIn("split", record)
        self.assertNotIn("attribution", record)
        self.assertIn(invariants.SPLIT, record["blocked"])
        self.assertEqual(record["blocked"][invariants.SPLIT][0]["check"], "CONFIG_MINT")

    def test_burn_atomic_unchecked_serialises_no_supply_destroyed_value_anywhere(self):
        observation = build_observation()  # no evidence handle -- BURN_ATOMIC/BURN_SUPPLY UNCHECKED
        record = observation.as_dict()

        self.assertNotIn("evidence", record)
        self.assertIn(invariants.SUPPLY_DESTROYED, record["blocked"])

    def test_burn_atomic_unchecked_with_real_evidence_withholds_the_real_burn_total(self):
        """The verifier's exact reproduction: a real, non-trivial burn total
        exists in `observation.evidence`, and must still not reach the
        record while BURN_ATOMIC/BURN_SUPPLY are UNCHECKED (walk incomplete).
        """
        with tempfile.TemporaryDirectory() as tmp:
            evidence = evidence_db(tmp)
            evidence.record_burn_event(
                signature="sig-1", mint=CHARLIE, instruction_index=0,
                tokens_burned=43_575_480_427_900, source="boost_buy_and_burn", slot=1,
            )
            observation = build_observation(evidence=evidence)
            record = observation.as_dict()
            evidence.close()

        self.assertIn(invariants.SUPPLY_DESTROYED, record["blocked"])
        text = json.dumps(record, sort_keys=True)
        self.assertNotIn("43575480427900", text)

    def test_publishable_split_carries_value_and_backing_check_names(self):
        observation = build_observation()
        record = observation.as_dict()

        self.assertEqual(record["split"], {"sol_burn": 10_000, "burn": 0, "paid": 0})
        self.assertIn("CONFIG_MINT", record["backed_by"][invariants.SPLIT])
        self.assertIn("SPLIT_SUM", record["backed_by"][invariants.SPLIT])

    def test_schema_is_bumped_to_three_and_committed_records_are_never_rewritten(self):
        self.assertEqual(Observation(mint=CHARLIE, observed_at=1.0).schema, 3)


class TestLogSurfaceRedaction(unittest.TestCase):
    """A stored schema-2 record whose split was not publishable must be
    redacted on replay -- the append-only file itself is never touched.
    """

    def _legacy_unpublishable_record(self) -> dict:
        return {
            "schema": 2,
            "mint": CHARLIE,
            "observed_at": 1.0,
            "error": None,
            "split": {"sol_burn": 10_000, "burn": 0, "paid": 0},
            "attribution": [
                {"address": GRANDFATHERED, "bps": 10_000, "leg": "sol_burn", "keyless": False, "reason": "x"}
            ],
            "checks": [],
            "publishable": [],
            "blocked": {
                invariants.SPLIT: [{"check": "CONFIG_MINT", "status": "FAIL", "detail": "mismatch"}]
            },
        }

    def test_gate_stored_record_strips_split_and_attribution(self):
        gated = publish.gate_stored_record(self._legacy_unpublishable_record())
        self.assertNotIn("split", gated)
        self.assertNotIn("attribution", gated)
        self.assertEqual(gated["_redacted"], [invariants.SPLIT])

    def test_log_text_names_the_withholding_instead_of_a_bare_dash(self):
        from indexer.cli import _log_lines

        lines = _log_lines([self._legacy_unpublishable_record()])
        joined = "\n".join(lines)
        self.assertNotIn("10000", joined)
        self.assertIn("CONFIG_MINT", joined)
        self.assertIn("redacted", joined.lower())

    def test_log_json_strips_the_figure_and_marks_the_redaction(self):
        from indexer.cli import _log_json_lines

        lines = _log_json_lines([self._legacy_unpublishable_record()])
        parsed = json.loads(lines[0])
        self.assertNotIn("split", parsed)
        self.assertNotIn("attribution", parsed)
        self.assertEqual(parsed["_redacted"], [invariants.SPLIT])

    def test_a_record_with_nothing_blocked_passes_through_unredacted(self):
        record = {
            "schema": 3, "mint": CHARLIE, "observed_at": 1.0, "error": None,
            "split": {"sol_burn": 10_000, "burn": 0, "paid": 0}, "checks": [],
            "publishable": [invariants.SPLIT], "blocked": {},
        }
        gated = publish.gate_stored_record(record)
        self.assertEqual(gated["split"], {"sol_burn": 10_000, "burn": 0, "paid": 0})
        self.assertNotIn("_redacted", gated)


# -- the surface registry: every emitter classified or the test fails -------
def _iter_module_functions(tree: ast.AST):
    """`(qualified_name, node)` for every function under a module: plain
    name for a module-level function, `Class.method` for a class method.
    One level deep is sufficient -- this codebase nests neither.
    """
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.name, node
        elif isinstance(node, ast.ClassDef):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield f"{node.name}.{child.name}", child


def _function_emits(node: ast.AST) -> bool:
    """True iff `node`'s body calls `print(...)` or `json.dumps(...)`
    anywhere -- the two ways a value in this codebase reaches a human or the
    committed record.
    """
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if isinstance(func, ast.Name) and func.id == "print":
            return True
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "dumps"
            and isinstance(func.value, ast.Name)
            and func.value.id == "json"
        ):
            return True
    return False


def _every_emitting_function() -> list[str]:
    indexer_dir = Path(__file__).resolve().parents[1] / "indexer"
    found = []
    for path in sorted(indexer_dir.glob("*.py")):
        module = f"indexer.{path.stem}"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for qualname, node in _iter_module_functions(tree):
            if _function_emits(node):
                found.append(f"{module}:{qualname}")
    return found


class TestSurfaceRegistryCoversEveryEmitter(unittest.TestCase):
    """01-VERIFICATION.md's second `missing:` item: the old sweep enumerated
    surfaces by hand, which is why a third one went unswept. This walks
    `indexer/` with `ast` and fails when a function that emits (`print` or
    `json.dumps`) is neither a `publish.SURFACES` target nor an explicitly
    reasoned `publish.NON_FIGURE_EMITTERS` entry -- a fourth unswept surface
    fails a test instead of shipping silently.
    """

    def test_every_emitting_function_is_classified(self):
        classified = {entry["target"] for entry in publish.SURFACES.values()}
        classified |= set(publish.NON_FIGURE_EMITTERS.keys())
        offenders = [name for name in _every_emitting_function() if name not in classified]
        self.assertEqual(offenders, [], f"unclassified emitting function(s): {offenders}")

    def test_every_non_figure_emitter_carries_a_reason(self):
        for target, reason in publish.NON_FIGURE_EMITTERS.items():
            self.assertIsInstance(reason, str)
            self.assertTrue(reason.strip(), f"{target} has an empty reason")

    def test_every_surface_target_resolves_to_a_real_callable(self):
        for name in publish.SURFACES:
            entry = publish.SURFACES[name]
            module_name, _, attr = entry["target"].partition(":")
            module = importlib.import_module(module_name)
            target = module
            for part in attr.split("."):
                target = getattr(target, part)
            self.assertTrue(callable(target), f"{name}'s target {entry['target']} is not callable")


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


# -- 03-02 Task 2: the coverage sentence and the index it sits on agree -----
class TestIndexObservedCountMatchesRenderedRows(unittest.TestCase):
    """D-35 feeds `coverage_statement` the number of coins whose records are
    actually committed under the output directory -- the same set
    `site.index_rows` renders. A test asserts the two agree so the sentence
    and the page can never drift apart.
    """

    def test_observed_count_equals_index_rows_count_for_the_same_record_set(self):
        records = [
            {"mint": "MINT-A", "split": {"sol_burn": 10_000, "burn": 0, "paid": 0}, "backed_by": {"split": ["CONFIG_MINT"]}},
            {"mint": "MINT-B", "split": {"sol_burn": 0, "burn": 0, "paid": 10_000}, "backed_by": {"split": ["CONFIG_MINT"]}},
        ]
        observed_count = len(records)
        rows = site.index_rows(records)
        self.assertEqual(observed_count, len(rows))

        sentence = site.coverage_statement({"observed": observed_count})
        self.assertIn(str(observed_count), sentence)


# -- discipline-style check: every figure-formatting module imports publish --
class TestFigureFormattingModulesImportPublish(unittest.TestCase):
    FIGURE_ATTRS = {"SPLIT", "SOL_BURN_TOTAL", "BURN_TOTAL", "OPS_TOTAL", "SUPPLY_DESTROYED"}

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

"""One tick: read a coin's on-chain state, run every check, produce a record.

ARCHITECTURE.md sec.3 -- per coin, per tick:

    read sharing config      -> { seal_bps, burn_bps, paid_bps }, admin_revoked
    read seal vault balance  -> SEAL invariant
    read mint supply         -> BURN invariant
    read ops inflows         -> OPS routed total
    recompute all invariants -> pass / fail

An observation that could not be produced is itself an observation. A read
failure returns a record with `error` set rather than raising past the caller,
because "the RPC was down at 14:02" has to be as durable and as linkable as a
green state, or the log is only a record of the times things worked.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import invariants, publish
from .legs import Registry, Split, split_of
from .pump import DecodeError, MintState, SharingConfig, read_bonding_curve, read_mint, read_sharing_config

# Bumped 2 -> 3 (01-04, PUB-01): schema 2's as_dict() emitted `split` and
# `evidence["burn_total"]` unconditionally, with no reference to
# `self.verdict` -- the exact gap 01-VERIFICATION.md reproduced against the
# shipped code. Schema 3's as_dict() delegates to publish.durable_record(),
# which obtains every figure through publish.Publisher the same way
# report.render()/publish.public_record() always have. A reader must be able
# to tell a gated record from an ungated one by schema number alone; the two
# committed schema-2 records in state/observations.jsonl are never rewritten
# to match -- they are read through publish.gate_stored_record() instead
# (indexer/cli.py's log/log --json).
SCHEMA = 3


@dataclass
class Observation:
    mint: str
    observed_at: float
    schema: int = SCHEMA
    error: str | None = None

    config: SharingConfig | None = None
    graduated: bool | None = None
    split: Split | None = None
    mint_state: MintState | None = None
    seal_balances: dict = field(default_factory=dict)   # address -> lamports
    checks: tuple = ()
    verdict: invariants.Verdict | None = None
    evidence: dict | None = None   # address -> recorded lamports, only when an evidence handle was consulted
    evidence_coverage: dict | None = None   # address -> count of distinct endpoints that contributed (D-13)
    burn_events: list = field(default_factory=list)   # raw evidence.burns_for(mint) rows -- observed fact, never a figure

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def failures(self):
        return [c for c in self.checks if c.status == invariants.FAIL]

    @property
    def unchecked(self):
        return [c for c in self.checks if c.status == invariants.UNCHECKED]

    def as_dict(self) -> dict:
        """PUB-01: delegates to `publish.durable_record()`, the single gate
        every figure-shaped field passes through before reaching this
        append-only surface. See `publish.durable_record()`'s docstring for
        exactly what is gated and why; nothing here reads `self.split` or
        `self.evidence` directly any more.
        """
        return publish.durable_record(self)


def observe(rpc, mint: str, registry: Registry | None = None, now=None, evidence=None) -> Observation:
    registry = registry or Registry()
    observed_at = now() if callable(now) else (now if now is not None else time.time())
    record = Observation(mint=mint, observed_at=observed_at)

    try:
        curve = read_bonding_curve(rpc, mint)
        record.graduated = curve.graduated
        config = read_sharing_config(rpc, curve)
        record.config = config
        mint_state = read_mint(rpc, mint)
        record.mint_state = mint_state
    except DecodeError as exc:
        record.error = str(exc)
        return record
    except Exception as exc:  # RPC failure, malformed response, anything
        record.error = f"{type(exc).__name__}: {exc}"
        return record

    split = split_of(config, registry)
    record.split = split

    # Balances of the SEAL destinations. Recorded as an observed fact; the
    # SEAL_BALANCE check deliberately does not consume it, because a balance is
    # not a reconciliation until there are recorded inflows to reconcile it to.
    for attribution in split.attributions:
        if attribution.leg != "seal":
            continue
        try:
            record.seal_balances[attribution.address] = rpc.balance(attribution.address)
        except Exception as exc:  # a missing balance must not lose the whole tick
            record.seal_balances[attribution.address] = None
            record.error = f"seal balance read failed: {type(exc).__name__}: {exc}"

    # Evidence plugs in here: the no-argument call sites below are exactly
    # what forced SEAL_BALANCE/OPS_ROUTED/BURN_SUPPLY to UNCHECKED before
    # this evidence store existed.
    seal_check = invariants.seal_balance()
    ops_check = invariants.ops_routed(split)
    burn_check = invariants.burn_supply(mint_state)
    atomic_check = invariants.burn_atomic(mint, [], False)
    spend_check = invariants.burn_spend(split)
    if evidence is not None:
        seal_destinations = [a.address for a in split.attributions if a.leg == "seal"]
        ops_destinations = [a.address for a in split.attributions if a.leg == "paid"]

        record.evidence = {
            address: evidence.recorded_lamports(address)
            for address in seal_destinations + ops_destinations
        }
        # D-13: an inflow set assembled from one endpoint when three were
        # configured is a materially weaker claim than one where three
        # agreed -- the reader is entitled to know which they are looking at.
        record.evidence_coverage = {
            address: len(evidence.cursor_endpoints(address, "inflow"))
            for address in seal_destinations + ops_destinations
        }

        # OPS balances are read the same way SEAL balances already are.
        balances = dict(record.seal_balances)
        for address in ops_destinations:
            try:
                balances[address] = rpc.balance(address)
            except Exception as exc:  # a missing balance must not lose the whole tick
                balances[address] = None
                record.error = f"ops balance read failed: {type(exc).__name__}: {exc}"

        seal_check = invariants.seal_balance(
            split=split, evidence=evidence, balances=balances, registry=registry
        )
        ops_check = invariants.ops_routed(split, evidence=evidence, balances=balances)

        # A burn observed on an earlier tick may only now have a `supply_after`
        # to fill -- this tick's own supply reading is the next observation
        # after any burn recorded before it.
        evidence.fill_missing_supply_after(mint, mint_state.supply)

        initial_supply_row = evidence.initial_supply_for(mint)
        burned = evidence.total_burned(mint)
        walk_complete = evidence.is_backfill_complete(mint, "burn")
        burn_check = invariants.burn_supply(mint_state, initial_supply_row, burned, walk_complete)

        # EVID-09: BURN_ATOMIC over every burn recorded for this mint so far.
        burn_rows = evidence.burns_for(mint)
        record.burn_events = burn_rows   # 02-02: the raw rows site.py's "The Burn" section needs
        burn_walk_complete = evidence.is_backfill_complete(mint, "burn")
        atomic_check = invariants.burn_atomic(mint, burn_rows, burn_walk_complete)

        spend_check = invariants.burn_spend(split, evidence=evidence)

        record.evidence["burn_total"] = burned
        record.evidence["initial_supply"] = initial_supply_row

    record.checks = (
        invariants.config_mint(mint, config),
        invariants.split_sum(split),
        invariants.seal_unspendable(split),
        seal_check,
        burn_check,
        invariants.burn_irreversible(mint_state),
        atomic_check,
        spend_check,
        ops_check,
    )
    record.verdict = invariants.apply_silence_rule(record.checks)
    return record

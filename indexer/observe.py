"""One tick: read a coin's on-chain state, run every check, produce a record.

ARCHITECTURE.md sec.3 -- per coin, per tick:

    read sharing config      -> { sol_burn_bps, burn_bps, paid_bps }, admin_revoked
    read SOL burn vault balance  -> SOL burn invariant
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
from .legs import (Registry, Split, charity_recipients, donate_gg_fee_bps,
                   recipient_kind, split_of)
from .pump import DecodeError, MintState, SharingConfig, read_bonding_curve, read_mint, read_sharing_config
from .rpc import RpcError, RpcUnavailable

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

# 03-02 Task 1 (COV-03): which chain read was in progress when `observe()`
# failed to complete, keyed to the reason `error_kind` records for it. Named
# here rather than inline so the exception handlers below and any future
# reader agree on the same three stage names.
_STAGE_BONDING_CURVE = "bonding_curve"
_STAGE_SHARING_CONFIG = "sharing_config"
_STAGE_MINT = "mint"

# A `DecodeError` during each stage means something different about the
# COIN, not about our infrastructure -- see `observe()`'s docstring. Keyed by
# stage name; `intake.REASONS` is the closed vocabulary these values belong
# to (intake.py depends on this dict's values, not the other way around --
# observe.py does not import intake, to avoid a cycle).
_DECODE_ERROR_KIND_BY_STAGE = {
    _STAGE_BONDING_CURVE: "not_pump_coin",
    _STAGE_SHARING_CONFIG: "no_sharing_config",
    _STAGE_MINT: "mint_decode_failed",
}


@dataclass
class Observation:
    mint: str
    observed_at: float
    schema: int = SCHEMA
    error: str | None = None
    # COV-03 (03-02 Task 1): set only when `observe()` could not produce a
    # checkable record at all -- never for the two late balance-read failure
    # paths below, which still produce a full checks tuple. A typed value
    # from a closed vocabulary (`intake.REASONS`), never derived by matching
    # text in `error`: `intake.reason_for()` reads this field directly.
    error_kind: str | None = None

    config: SharingConfig | None = None
    graduated: bool | None = None
    split: Split | None = None
    mint_state: MintState | None = None
    sol_burn_balances: dict = field(default_factory=dict)   # address -> lamports
    checks: tuple = ()
    verdict: invariants.Verdict | None = None
    evidence: dict | None = None   # address -> recorded lamports, only when an evidence handle was consulted
    evidence_coverage: dict | None = None   # address -> count of distinct endpoints that contributed (D-13)
    burn_events: list = field(default_factory=list)   # raw evidence.burns_for(mint) rows -- observed fact, never a figure
    burn_walk_complete: bool = False
    # D-40: address -> one of `legs.RECIPIENT_KINDS`. An OBSERVED FACT, not a
    # figure: it says what a fee recipient IS (a wallet, another sharing
    # config, a token account, some other program, or an account that has
    # never received a lamport), and claims nothing about how much reached
    # it. pump labels none of this, and "unproven is OPS" collapses all of
    # it into one bucket that tells a reader nothing.
    recipient_kinds: dict = field(default_factory=dict)
    # pump's launch modes, both observed facts about configuration rather than
    # gated figures: they say what a coin IS, never how much moved.
    # `cashback` is None when the curve predates the field -- unknown, not off.
    cashback: bool | None = None
    charity_recipients: tuple = ()
    donate_gg_fee_bps: int | None = None   # evidence.is_backfill_complete(mint, "burn") -- already computed for the checks; carried so a surface can state it instead of asserting it

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


def observe(
    rpc, mint: str, registry: Registry | None = None, now=None, evidence=None, config=None
) -> Observation:
    """COV-01 is one code path, not a coverage-mode fork: the optional
    `config` keyword lets a caller who already has a config in hand (from
    `coverage.sweep`'s enumeration) skip the `read_sharing_config` round
    trip, but every check downstream runs exactly as it does for an
    on-demand, hand-run observation.

    A supplied config is a claim about which coin it belongs to; the
    bonding curve is the independent statement that either corroborates it
    or does not. When they disagree, the record carries `error` naming both
    addresses and no split is computed -- handing in a wrong config must
    never silently publish a wrong coin's split.
    """
    registry = registry or Registry()
    observed_at = now() if callable(now) else (now if now is not None else time.time())
    record = Observation(mint=mint, observed_at=observed_at)

    stage = _STAGE_BONDING_CURVE
    try:
        curve = read_bonding_curve(rpc, mint)
        record.graduated = curve.graduated
        record.cashback = curve.cashback
        if config is not None:
            if config.address != curve.creator:
                record.error = (
                    f"{mint}: the supplied config {config.address} is not the config "
                    f"this coin's bonding curve names ({curve.creator}) -- a config "
                    "handed in from enumeration is a claim about which coin it belongs "
                    "to, and the bonding curve disagrees"
                )
                record.error_kind = "config_mismatch"
                return record
        else:
            stage = _STAGE_SHARING_CONFIG
            config = read_sharing_config(rpc, curve)
        record.config = config
        stage = _STAGE_MINT
        mint_state = read_mint(rpc, mint)
        record.mint_state = mint_state
    except DecodeError as exc:
        record.error = str(exc)
        record.error_kind = _DECODE_ERROR_KIND_BY_STAGE.get(stage, "mint_decode_failed")
        return record
    except RpcUnavailable as exc:
        record.error = f"{type(exc).__name__}: {exc}"
        record.error_kind = "rpc_unavailable"
        return record
    except RpcError as exc:
        record.error = f"{type(exc).__name__}: {exc}"
        record.error_kind = "rpc_error"
        return record
    except Exception as exc:  # RPC failure, malformed response, anything
        record.error = f"{type(exc).__name__}: {exc}"
        return record

    split = split_of(config, registry)
    record.split = split
    record.charity_recipients = charity_recipients(split)
    record.donate_gg_fee_bps = donate_gg_fee_bps(split)

    # D-40: what each fee recipient IS, from one batched account read. This
    # is the only per-coin measurement available before the program exists
    # that differs between two coins -- the split alone reads the same for
    # every coin that has not enrolled. A failure here must not lose the
    # tick: an unknown kind is simply absent from the mapping, which reads as
    # "not established" rather than as a claim.
    recipients = [a.address for a in split.attributions]
    if recipients:
        try:
            for address, account in zip(recipients, rpc.accounts(recipients)):
                record.recipient_kinds[address] = recipient_kind(account)
        except Exception:
            pass

    # Balances of the SOL burn destinations. Recorded as an observed fact; the
    # SOL_BURN_BALANCE check deliberately does not consume it, because a balance is
    # not a reconciliation until there are recorded inflows to reconcile it to.
    for attribution in split.attributions:
        if attribution.leg != "sol_burn":
            continue
        try:
            record.sol_burn_balances[attribution.address] = rpc.balance(attribution.address)
        except Exception as exc:  # a missing balance must not lose the whole tick
            record.sol_burn_balances[attribution.address] = None
            record.error = f"SOL burn balance read failed: {type(exc).__name__}: {exc}"

    # Evidence plugs in here: the no-argument call sites below are exactly
    # what forced SOL_BURN_BALANCE/OPS_ROUTED/BURN_SUPPLY to UNCHECKED before
    # this evidence store existed.
    sol_burn_check = invariants.sol_burn_balance()
    ops_check = invariants.ops_routed(split)
    burn_check = invariants.burn_supply(mint_state)
    atomic_check = invariants.burn_atomic(mint, [], False)
    spend_check = invariants.burn_spend(split)
    if evidence is not None:
        sol_burn_destinations = [a.address for a in split.attributions if a.leg == "sol_burn"]
        ops_destinations = [a.address for a in split.attributions if a.leg == "paid"]

        record.evidence = {
            address: evidence.recorded_lamports(address)
            for address in sol_burn_destinations + ops_destinations
        }
        # D-13: an inflow set assembled from one endpoint when three were
        # configured is a materially weaker claim than one where three
        # agreed -- the reader is entitled to know which they are looking at.
        record.evidence_coverage = {
            address: len(evidence.cursor_endpoints(address, "inflow"))
            for address in sol_burn_destinations + ops_destinations
        }

        # OPS balances are read the same way SOL burn balances already are.
        balances = dict(record.sol_burn_balances)
        for address in ops_destinations:
            try:
                balances[address] = rpc.balance(address)
            except Exception as exc:  # a missing balance must not lose the whole tick
                balances[address] = None
                record.error = f"ops balance read failed: {type(exc).__name__}: {exc}"

        sol_burn_check = invariants.sol_burn_balance(
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
        burn_walk_complete = walk_complete
        record.burn_walk_complete = burn_walk_complete   # carried so a surface states the walk's state rather than asserting it
        atomic_check = invariants.burn_atomic(mint, burn_rows, burn_walk_complete)

        spend_check = invariants.burn_spend(split, evidence=evidence)

        record.evidence["burn_total"] = burned
        record.evidence["initial_supply"] = initial_supply_row

    record.checks = (
        invariants.config_mint(mint, config),
        invariants.split_sum(split),
        invariants.sol_burn_unspendable(split),
        sol_burn_check,
        burn_check,
        invariants.burn_irreversible(mint_state),
        atomic_check,
        spend_check,
        ops_check,
    )
    record.verdict = invariants.apply_silence_rule(record.checks)
    return record

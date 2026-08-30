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

from . import invariants
from .legs import Registry, Split, split_of
from .pump import DecodeError, MintState, SharingConfig, read_bonding_curve, read_mint, read_sharing_config

SCHEMA = 2


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
        record = {
            "schema": self.schema,
            "mint": self.mint,
            "observed_at": self.observed_at,
            "error": self.error,
        }
        if self.config is not None:
            record["config"] = {
                "address": self.config.address,
                "mint": self.config.mint,
                "version": self.config.version,
                "status": self.config.status,
                "admin": self.config.admin,
                "admin_revoked": self.config.admin_revoked,
                "shareholders": [
                    {"address": who, "bps": bps} for who, bps in self.config.shareholders
                ],
            }
        if self.graduated is not None:
            record["graduated"] = self.graduated
        if self.split is not None:
            record["split"] = self.split.as_dict()
            record["attribution"] = [
                {
                    "address": a.address,
                    "bps": a.bps,
                    "leg": a.leg,
                    "keyless": a.keyless,
                    "reason": a.reason,
                }
                for a in self.split.attributions
            ]
        if self.mint_state is not None:
            record["mint_state"] = {
                "supply": self.mint_state.supply,
                "decimals": self.mint_state.decimals,
                "mint_authority": self.mint_state.mint_authority,
                "freeze_authority": self.mint_state.freeze_authority,
                "token_program": self.mint_state.program,
            }
        if self.seal_balances:
            record["seal_balances"] = self.seal_balances
        if self.evidence is not None:
            record["evidence"] = self.evidence
        record["checks"] = [c.as_dict() for c in self.checks]
        if self.verdict is not None:
            record["publishable"] = sorted(self.verdict.publishable)
            record["blocked"] = {
                figure: [
                    {"check": name, "status": status, "detail": detail}
                    for name, status, detail in reasons
                ]
                for figure, reasons in sorted(self.verdict.blocked.items())
            }
        return record


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
    # what forced SEAL_BALANCE/OPS_ROUTED to UNCHECKED before this evidence
    # store existed.
    seal_check = invariants.seal_balance()
    ops_check = invariants.ops_routed(split)
    if evidence is not None:
        seal_destinations = [a.address for a in split.attributions if a.leg == "seal"]
        ops_destinations = [a.address for a in split.attributions if a.leg == "paid"]

        record.evidence = {
            address: evidence.recorded_lamports(address)
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

    record.checks = (
        invariants.config_mint(mint, config),
        invariants.split_sum(split),
        invariants.seal_unspendable(split),
        seal_check,
        invariants.burn_supply(mint_state),
        invariants.burn_irreversible(mint_state),
        ops_check,
    )
    record.verdict = invariants.apply_silence_rule(record.checks)
    return record

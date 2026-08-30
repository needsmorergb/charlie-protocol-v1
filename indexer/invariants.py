"""The checks. This module is the product.

PROTOCOL.md sec.4: every published figure is backed by an equation that **can
fail**. Three rules govern this file:

1. A check that has not been computed is `UNCHECKED`, never `PASS`. An indexer
   that reports green for arithmetic it never did is worse than one that
   reports nothing, because it looks the same as one that did the work.
2. `UNCHECKED` gates publication exactly as hard as `FAIL`. The silence rule
   does not distinguish "the number is wrong" from "we do not know whether the
   number is right".
3. Every check names the figure it backs. A figure with no passing backing
   check is not publishable, and the indexer says which check stopped it.
"""

from __future__ import annotations

from dataclasses import dataclass

from .legs import Registry

PASS = "PASS"
FAIL = "FAIL"
UNCHECKED = "UNCHECKED"

# The figures a publisher might want to put in a post.
SPLIT = "split"
SEAL_TOTAL = "seal_total"
BURN_TOTAL = "burn_total"
OPS_TOTAL = "ops_total"
FIGURES = (SPLIT, SEAL_TOTAL, BURN_TOTAL, OPS_TOTAL)


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    backs: tuple
    equation: str
    detail: str
    expected: str | None = None
    actual: str | None = None

    @property
    def passed(self) -> bool:
        return self.status == PASS

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "backs": list(self.backs),
            "equation": self.equation,
            "detail": self.detail,
            "expected": self.expected,
            "actual": self.actual,
        }


def _check(name, status, backs, equation, detail, expected=None, actual=None) -> Check:
    return Check(name, status, tuple(backs), equation, detail, expected, actual)


# -- the checks -----------------------------------------------------------
def config_mint(requested: str, config) -> Check:
    """The config we read must belong to the coin we were asked about.

    `bonding_curve.creator` names the config, and a bonding curve address is a
    PDA that anyone may fund. This check is what stops a stray account from
    being reported as some other coin's split.
    """
    ok = config.mint == requested
    return _check(
        "CONFIG_MINT",
        PASS if ok else FAIL,
        FIGURES,
        "sharing_config.mint == mint",
        "the sharing config names this coin"
        if ok
        else "the sharing config names a DIFFERENT coin -- nothing read here describes "
        "the mint that was asked about",
        expected=requested,
        actual=config.mint,
    )


def split_sum(split) -> Check:
    """PROTOCOL.md sec.2: splits are expressed in bps and must sum to 10000."""
    ok = split.total == 10_000
    return _check(
        "SPLIT_SUM",
        PASS if ok else FAIL,
        [SPLIT],
        "seal_bps + burn_bps + paid_bps == 10000",
        "the split accounts for the whole fee stream"
        if ok
        else "the shareholder list does not sum to 10000 bps -- either pump changed the "
        "account layout or this is not the account we decoded it as",
        expected="10000",
        actual=str(split.total),
    )


def seal_unspendable(split) -> Check:
    """PROTOCOL.md sec.3: a seal destination must be provably keyless.

    Off the ed25519 curve means no private key *can* exist: a cryptographic
    guarantee. On the curve means a key can exist, and the address is safe only
    on the assumption that nobody ground one out. The protocol exists to refuse
    that assumption, so an on-curve seal destination fails -- grandfathered or
    not, ours included.
    """
    sealed = [a for a in split.attributions if a.leg == "seal"]
    if not sealed:
        return _check(
            "SEAL_UNSPENDABLE",
            UNCHECKED,
            [SEAL_TOTAL],
            "is_on_curve(seal_vault) == False",
            "no SEAL destination in this split -- nothing to check",
        )
    keyed = [a.address for a in sealed if not a.keyless]
    return _check(
        "SEAL_UNSPENDABLE",
        PASS if not keyed else FAIL,
        [SEAL_TOTAL],
        "is_on_curve(seal_vault) == False",
        "every SEAL destination is off the ed25519 curve: no private key can exist"
        if not keyed
        else "a SEAL destination is ON the ed25519 curve, so a private key can exist for "
        "it. Its unspendability rests on nobody having ground that key out, which is an "
        "assumption and not a proof",
        expected="off-curve",
        actual=("on-curve: " + ", ".join(keyed)) if keyed else "off-curve",
    )


def seal_balance(
    destination=None,
    recorded=None,
    vault_balance=None,
    comparator="==",
    reason=None,
    split=None,
    evidence=None,
    balances=None,
    registry=None,
) -> Check:
    """PROTOCOL.md sec.4: sum(recorded_inflows) == getBalance(vault).

    Two call shapes:

    * **Single-destination** (`destination`/`recorded`/`vault_balance`/
      `comparator`) -- task 1 of `01-01`'s tracer path, and still the
      building block every destination in the aggregate path below is
      evaluated with. A caller with no evidence handle still gets today's
      `UNCHECKED` and today's wording; old callers are not broken by this
      becoming computable.
    * **Aggregate** (`split`/`evidence`/`balances`/`registry`) -- task 2: every
      SEAL destination of a split, each evaluated on its own comparator
      chosen from the registry (EVID-04) -- `==` for a destination equal to
      `registry.seal_vault(mint)`, `<=` for the grandfathered shared address
      (PROTOCOL.md sec.3, D-06) -- folded into one Check. A destination whose
      walk is incomplete contributes `UNCHECKED` and the whole check is
      `UNCHECKED`, never `FAIL`, for an unfinished scan.
    """
    if split is not None and evidence is not None:
        return _seal_balance_aggregate(split, evidence, balances or {}, registry)

    if recorded is None or vault_balance is None:
        return _check(
            "SEAL_BALANCE",
            UNCHECKED,
            [SEAL_TOTAL],
            "sum(recorded_inflows) == getBalance(vault)",
            reason
            or "inflow recording is not built. Until it is, a vault balance is a number "
            "read off the chain rather than a reconciled total, and the protocol will "
            "not publish it",
        )
    if comparator == "<=":
        ok = recorded <= vault_balance
        equation = "opening + sum(recorded_inflows) <= getBalance(vault)"
    else:
        ok = recorded == vault_balance
        equation = "sum(recorded_inflows) == getBalance(vault)"
    detail_ok = "recorded inflows reconcile against the vault balance"
    detail_fail = "recorded inflows do not match the vault balance"
    if destination:
        detail_ok += f" ({destination})"
        detail_fail += f" ({destination})"
    return _check(
        "SEAL_BALANCE",
        PASS if ok else FAIL,
        [SEAL_TOTAL],
        equation,
        detail_ok if ok else detail_fail,
        expected=str(recorded),
        actual=str(vault_balance),
    )


def _seal_balance_aggregate(split, evidence, balances: dict, registry) -> Check:
    registry = registry or Registry()
    seal_destinations = [a.address for a in split.attributions if a.leg == "seal"]
    if not seal_destinations:
        return _check(
            "SEAL_BALANCE",
            UNCHECKED,
            [SEAL_TOTAL],
            "sum(recorded_inflows) == getBalance(vault)",
            "no SEAL destination in this split -- nothing to check",
        )

    per_destination = []
    for destination in seal_destinations:
        comparator = "<=" if destination in registry.grandfathered_seal else "=="
        if not evidence.is_backfill_complete(destination, "inflow"):
            cursor = evidence.get_cursor(destination, "inflow") or {}
            reached = cursor.get("oldest_signature") or "no signature yet"
            per_destination.append(
                {
                    "destination": destination,
                    "status": UNCHECKED,
                    "detail": f"the walk of {destination} is incomplete -- reached {reached}",
                }
            )
            continue
        opening = evidence.active_opening_balance(destination)
        if opening is not None:
            # D-05: an opening balance is an admission that history was
            # unreadable. This destination's balance is reported as an
            # observation only -- it contributes no reconciled total, and
            # the whole figure cannot be published while any piece of it
            # is unreconciled.
            per_destination.append(
                {
                    "destination": destination,
                    "status": "OBSERVATION",
                    "detail": f"{destination} carries an active opening balance "
                    f"({opening['lamports']} lamports as of {opening['opening_signature']}) -- "
                    "observed, not reconciled; no total is published for it",
                }
            )
            continue
        recorded = evidence.recorded_lamports(destination)
        vault_balance = balances.get(destination)
        check = seal_balance(
            destination=destination,
            recorded=recorded,
            vault_balance=vault_balance,
            comparator=comparator,
        )
        detail = check.detail
        if check.status == FAIL:
            outflows = evidence.outflows_for(destination)
            if outflows:
                signatures = ", ".join(row["signature"] for row in outflows)
                detail += f" -- outflow signatures accounting for the gap: {signatures}"
        per_destination.append(
            {
                "destination": destination,
                "status": check.status,
                "detail": detail,
                "expected": check.expected,
                "actual": check.actual,
            }
        )

    statuses = {p["status"] for p in per_destination}
    if FAIL in statuses:
        overall = FAIL
    elif UNCHECKED in statuses or "OBSERVATION" in statuses:
        overall = UNCHECKED
    else:
        overall = PASS

    return _check(
        "SEAL_BALANCE",
        overall,
        [SEAL_TOTAL],
        "per-destination: sum(recorded_inflows) == getBalance(vault) for a derived vault, "
        "<= for the grandfathered address",
        "; ".join(p["detail"] for p in per_destination),
        expected="; ".join(
            f"{p['destination']}: {p['expected']}" for p in per_destination if p.get("expected") is not None
        )
        or None,
        actual="; ".join(
            f"{p['destination']}: {p['actual']}" for p in per_destination if p.get("actual") is not None
        )
        or None,
    )


def burn_supply(mint_state, initial_supply=None, burned=None) -> Check:
    """PROTOCOL.md sec.4: initial_supply - sum(burn_amounts) == getMint(mint).supply."""
    if initial_supply is None or burned is None:
        return _check(
            "BURN_SUPPLY",
            UNCHECKED,
            [BURN_TOTAL],
            "initial_supply - sum(burn_amounts) == getMint(mint).supply",
            "burn events are not recorded yet. Live supply is observed and stored, but a "
            "supply reading on its own proves a total, not that we know which burns "
            "produced it",
            actual=str(mint_state.supply),
        )
    ok = initial_supply - burned == mint_state.supply
    return _check(
        "BURN_SUPPLY",
        PASS if ok else FAIL,
        [BURN_TOTAL],
        "initial_supply - sum(burn_amounts) == getMint(mint).supply",
        "every claimed burn is visible in the mint's supply"
        if ok
        else "claimed burns do not reconcile against the mint supply",
        expected=str(initial_supply - burned),
        actual=str(mint_state.supply),
    )


def burn_irreversible(mint_state) -> Check:
    """A burn is only "permanently destroyed" if the supply cannot be restored.

    Not in PROTOCOL.md sec.4 -- found while building. A live mint authority can
    reissue every token a crank ever burned, which makes the permitted claim
    "permanently destroyed" false however honest the burn arithmetic is.
    """
    ok = mint_state.mint_authority is None
    return _check(
        "BURN_IRREVERSIBLE",
        PASS if ok else FAIL,
        [BURN_TOTAL],
        "getMint(mint).mint_authority == None",
        "the mint authority is revoked: burned supply cannot be reissued"
        if ok
        else "a mint authority still exists, so burned supply can be reissued. "
        "'permanently destroyed' is not a permitted claim for this coin",
        expected="None",
        actual=str(mint_state.mint_authority),
    )


def ops_routed(split, evidence=None, balances=None) -> Check:
    """PROTOCOL.md sec.4: sum(routed_to_OPS) == sum(protocol_inflows(ops_wallet)).

    This proves how much was routed to OPS and nothing about what happened
    afterwards -- once SOL reaches a spendable wallet the chain stops being
    evidence (PROJECT.md's "Auditing OPS spending" is explicitly out of
    scope). PASS/FAIL is therefore about whether the split's claimed OPS
    destination(s) actually received the lamports the config says they
    should, not about anything downstream of that.
    """
    ops_destinations = [a.address for a in split.attributions if a.leg == "paid"]
    if not ops_destinations:
        return _check(
            "OPS_ROUTED",
            UNCHECKED,
            [OPS_TOTAL],
            "sum(routed_to_OPS) == sum(protocol_inflows(ops_wallet))",
            "no OPS destination in this split -- nothing to check",
        )
    if evidence is None:
        return _check(
            "OPS_ROUTED",
            UNCHECKED,
            [OPS_TOTAL],
            "sum(routed_to_OPS) == sum(protocol_inflows(ops_wallet))",
            "inflow recording is not built. Note that once it is, this proves how much was "
            "routed and nothing about what happened afterwards (PROTOCOL.md sec.4)",
        )

    balances = balances or {}
    per_destination = []
    for destination in ops_destinations:
        if not evidence.is_backfill_complete(destination, "inflow"):
            cursor = evidence.get_cursor(destination, "inflow") or {}
            reached = cursor.get("oldest_signature") or "no signature yet"
            per_destination.append(
                {
                    "destination": destination,
                    "status": UNCHECKED,
                    "detail": f"the walk of {destination} is incomplete -- reached {reached}",
                }
            )
            continue
        recorded = evidence.recorded_lamports(destination)
        ok = recorded > 0
        per_destination.append(
            {
                "destination": destination,
                "status": PASS if ok else FAIL,
                "detail": f"{destination}: recorded inflows {'observed' if ok else 'NOT observed'} "
                f"({recorded} lamports)",
                "expected": "> 0",
                "actual": str(recorded),
            }
        )

    statuses = {p["status"] for p in per_destination}
    if FAIL in statuses:
        overall = FAIL
    elif UNCHECKED in statuses:
        overall = UNCHECKED
    else:
        overall = PASS

    return _check(
        "OPS_ROUTED",
        overall,
        [OPS_TOTAL],
        "sum(routed_to_OPS) == sum(protocol_inflows(ops_wallet))",
        "; ".join(p["detail"] for p in per_destination),
        expected="; ".join(
            f"{p['destination']}: {p['expected']}" for p in per_destination if p.get("expected") is not None
        )
        or None,
        actual="; ".join(
            f"{p['destination']}: {p['actual']}" for p in per_destination if p.get("actual") is not None
        )
        or None,
    )


# -- the silence rule -----------------------------------------------------
@dataclass(frozen=True)
class Verdict:
    """Which figures a publisher may post, and what stopped the rest."""

    publishable: frozenset
    blocked: dict          # figure -> [(check name, status, detail), ...]

    def may_publish(self, figure: str) -> bool:
        return figure in self.publishable


def apply_silence_rule(checks) -> Verdict:
    """A figure is publishable only if every check backing it passed.

    UNCHECKED blocks exactly as hard as FAIL. A figure that nothing checks at
    all is not publishable either -- an unbacked number is the thing this
    protocol was written against.
    """
    backed = {figure: [] for figure in FIGURES}
    for check in checks:
        for figure in check.backs:
            backed.setdefault(figure, []).append(check)

    publishable, blocked = set(), {}
    for figure in FIGURES:
        relevant = backed.get(figure) or []
        failures = [c for c in relevant if not c.passed]
        if relevant and not failures:
            publishable.add(figure)
            continue
        if failures:
            blocked[figure] = [(c.name, c.status, c.detail) for c in failures]
        else:
            blocked[figure] = [
                ("NO_CHECK", UNCHECKED, "no check backs this figure, so it is not a "
                 "figure this protocol will publish")
            ]
    return Verdict(publishable=frozenset(publishable), blocked=blocked)

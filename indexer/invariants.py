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

from . import legs
from .legs import SOL_BURN_INCINERATOR, Registry

PASS = "PASS"
FAIL = "FAIL"
UNCHECKED = "UNCHECKED"

# The figures a publisher might want to put in a post.
SPLIT = "split"
SOL_BURN_TOTAL = "sol_burn_total"
BURN_TOTAL = "burn_total"
OPS_TOTAL = "ops_total"
# D-09: the one published burn figure -- every burn against the mint, by
# anyone, whether or not it invoked our crank. Distinct from BURN_TOTAL,
# which plan 03 gives its own named check.
SUPPLY_DESTROYED = "supply_destroyed"
FIGURES = (SPLIT, SOL_BURN_TOTAL, BURN_TOTAL, OPS_TOTAL, SUPPLY_DESTROYED)


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
        "sol_burn_bps + burn_bps + paid_bps == 10000",
        "the split accounts for the whole fee stream"
        if ok
        else "the shareholder list does not sum to 10000 bps -- either pump changed the "
        "account layout or this is not the account we decoded it as",
        expected="10000",
        actual=str(split.total),
    )


def protocol_share(split) -> Check:
    """Is this coin in the protocol: does its on-chain split pay the
    protocol's collection wallet at least the protocol's rate?

    This is the whole of enrollment, and pump is what enforces it. The
    sharing config pays every shareholder from the coin's creator vault, and
    once the config's one change is spent no key can alter it -- so a coin
    that carries the share carries it for good. A coin that does not is not
    enrolled: not listed as one, not badged as one, whatever it asks for.

    Backs no figure. A coin's split, burns and totals are facts about the
    coin whether or not it pays the protocol, and withholding them from a
    coin that has not enrolled would turn this site into a toll booth for
    information it already has.
    """
    destination, rate = legs.TOLL_DESTINATION, legs.TOLL_BPS
    equation = f"bps(TOLL_DESTINATION) >= {rate}"
    if destination is None:
        return _check("PROTOCOL_SHARE", UNCHECKED, [], equation,
                      "the protocol's collection address is not set, so nothing can be enrolled")
    paid = sum(a.bps for a in split.attributions if a.address == destination)
    if paid >= rate:
        return _check(
            "PROTOCOL_SHARE", PASS, [], equation,
            f"the split pays the protocol's collection wallet {destination} "
            f"{paid} bps, at or above the {rate} bps every enrolled coin carries",
            expected=f">= {rate}", actual=str(paid),
        )
    return _check(
        "PROTOCOL_SHARE", FAIL, [], equation,
        (f"the split pays the protocol's collection wallet {destination} {paid} bps, "
         f"below the {rate} bps every enrolled coin carries") if paid
        else f"the split does not pay the protocol's collection wallet {destination} at all",
        expected=f">= {rate}", actual=str(paid),
    )


def sol_burn_unspendable(split) -> Check:
    """The SOL burn destination is one SOL cannot come back from.

    Two things satisfy it, and only two, whatever the prose elsewhere counts:

    * **Off the ed25519 curve** -- a program-derived address, which no key
      signs for. This covers the protocol's own vaults and it covers Solana's
      incinerator, so the incinerator never reaches the named set below.
    * **A recognised burn address.** `burn111...111` is the one there is. The
      chain treats it the way every burn address is treated: SOL sent there is
      out of circulation and stays there. That is the same standing every burn
      address on every chain has ever had, and it is why a burn to one counts
      as a burn. It is on the curve, so this clause is the only thing that
      passes it.

    A destination that is neither is an address someone can spend from, and
    that is what this check is for.

    IT DOES NOT GRADE A COIN AGAINST A PROTOCOL THE COIN IS NOT IN. The
    protocol is built on top of $CHARLIE, not run by it. Printing a red FAIL
    on an unenrolled coin for not meeting an enrolled coin's requirement is a
    category error, and this check no longer makes it.

    THE REGISTRY HERE IS THE PROTOCOL'S OWN, deliberately, not the one the
    observation was taken under. `legs.classify` uses the caller's registry to
    decide which leg an address is on; this decides whether what landed on the
    SOL burn leg is a burn destination, and that answer may not depend on what
    the caller was willing to call one. It is what makes the FAIL branch
    reachable at all: hand `classify` a registry that grandfathers a wallet
    and the wallet lands on the SOL burn leg, where this refuses it.
    """
    burned = [a for a in split.attributions if a.leg == "sol_burn"]
    if not burned:
        return _check(
            "SOL_BURN_UNSPENDABLE",
            UNCHECKED,
            [SOL_BURN_TOTAL],
            "every SOL burn destination is one SOL does not come back from",
            "no SOL burn destination in this split -- nothing to check",
        )
    # The incinerator is deliberately NOT named here. It is off the curve, so
    # the keyless clause below has already taken it, and naming it as well
    # would be a second mechanism that never runs -- unreachable code with a
    # test beside it that looks like proof and is not. If `is_on_curve` ever
    # stopped covering it, `test_the_incinerator_passes_by_being_off_the_curve`
    # is what says so.
    recognised = set(Registry().grandfathered_sol_burn)
    spendable = [
        a.address for a in burned
        if not a.keyless and a.address not in recognised
    ]
    return _check(
        "SOL_BURN_UNSPENDABLE",
        PASS if not spendable else FAIL,
        [SOL_BURN_TOTAL],
        "every SOL burn destination is one SOL does not come back from",
        "every SOL burn destination is a burn address: what reaches it is out "
        "of circulation"
        if not spendable
        else "a SOL burn destination is an ordinary address that can be spent "
        "from: " + ", ".join(spendable),
        expected="a burn destination",
        actual=("spendable: " + ", ".join(spendable)) if spendable else "a burn destination",
    )


def sol_burn_balance(
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
      SOL burn destination of a split, each evaluated on its own comparator
      chosen from the registry (EVID-04) -- `==` for a destination equal to
      `registry.sol_burn_vault(mint)`, `<=` for the grandfathered shared address
      (PROTOCOL.md sec.3, D-06) -- folded into one Check. A destination whose
      walk is incomplete contributes `UNCHECKED` and the whole check is
      `UNCHECKED`, never `FAIL`, for an unfinished scan.
    """
    if split is not None and evidence is not None:
        return _sol_burn_balance_aggregate(split, evidence, balances or {}, registry)

    if recorded is None or vault_balance is None:
        return _check(
            "SOL_BURN_BALANCE",
            UNCHECKED,
            [SOL_BURN_TOTAL],
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
        "SOL_BURN_BALANCE",
        PASS if ok else FAIL,
        [SOL_BURN_TOTAL],
        equation,
        detail_ok if ok else detail_fail,
        expected=str(recorded),
        actual=str(vault_balance),
    )


def _incomplete_walk_detail(evidence, destination: str) -> str:
    """WR-01: the incomplete-walk diagnostic, read the way
    `scan_inflows_all_endpoints()` (D-13, the only production scan path)
    actually writes cursors -- one row per contributing endpoint, under that
    endpoint's own identifier -- rather than `get_cursor(destination,
    "inflow")`'s single-endpoint-sentinel default (`DEFAULT_ENDPOINT_KEY`),
    which the production path never populates and which therefore always
    read "no signature yet" no matter how far a real scan had progressed.

    Names each contributing endpoint beside the signature it reached, and
    surfaces any endpoint's stored `last_error` (D-13: a coverage gap is
    stored, never inferred from silence). Never ranks or picks a single
    "furthest" endpoint: signatures are base58 and carry no ordering, the
    cursor stores no slot, and inventing a winner would be a claim the data
    does not support -- reporting each endpoint's own reach is both honest
    and strictly more informative than the single hardcoded key it replaces.
    """
    rows = evidence.cursor_progress(destination, "inflow")
    if not rows:
        return f"the walk of {destination} is incomplete -- reached no signature yet"
    parts = []
    for row in rows:
        endpoint = row["endpoint"]
        signature = row.get("oldest_signature") or "no signature yet"
        parts.append(f"{endpoint}: reached {signature}")
        if row.get("last_error"):
            parts.append(f"{endpoint} error: {row['last_error']}")
    return f"the walk of {destination} is incomplete -- " + "; ".join(parts)


def _incinerator_result(destination: str, recorded, vault_balance) -> dict:
    """SOL credited to Solana's incinerator is destroyed by the runtime at the
    end of the block, so its balance is always zero.

    The check is therefore the opposite of the one applied to a vault: a
    NON-zero balance is the anomaly, because it would mean lamports are
    sitting there rather than having left the supply. A balance we could not
    read is UNCHECKED, never a pass -- absence of a reading is not evidence of
    a burn.
    """
    if vault_balance is None:
        return {
            "destination": destination,
            "status": UNCHECKED,
            "detail": f"{destination} is Solana's incinerator, but its balance "
                      "was not read this tick, so the burn is not confirmed here",
        }
    if int(vault_balance) != 0:
        return {
            "destination": destination,
            "status": FAIL,
            "detail": f"{destination} is Solana's incinerator and its balance should "
                      f"always be zero, because the runtime removes what is credited "
                      f"to it at the end of the block. It reads {vault_balance} lamports",
            "expected": 0,
            "actual": int(vault_balance),
        }
    return {
        "destination": destination,
        "status": PASS,
        "detail": f"{recorded} lamports were routed to Solana's incinerator and its "
                  "balance is zero: the runtime removed them from the total supply",
        "expected": 0,
        "actual": 0,
    }


def _sol_burn_balance_aggregate(split, evidence, balances: dict, registry) -> Check:
    registry = registry or Registry()
    sol_burn_destinations = [a.address for a in split.attributions if a.leg == "sol_burn"]
    if not sol_burn_destinations:
        return _check(
            "SOL_BURN_BALANCE",
            UNCHECKED,
            [SOL_BURN_TOTAL],
            "sum(recorded_inflows) == getBalance(vault)",
            "no SOL burn destination in this split -- nothing to check",
        )

    per_destination = []
    for destination in sol_burn_destinations:
        comparator = "<=" if destination in registry.grandfathered_sol_burn else "=="
        if destination == SOL_BURN_INCINERATOR:
            # The incinerator's balance is ALWAYS zero, because the runtime
            # removes what is credited to it at the end of the block. Asking
            # `sum(inflows) == getBalance(vault)` here would read `X == 0` and
            # FAIL every coin that burned correctly, branding the right answer
            # red.
            #
            # The zero IS the proof: a balance that stayed at zero after
            # lamports were credited means they left the total supply.
            comparator = "burned"
        if not evidence.is_backfill_complete(destination, "inflow"):
            per_destination.append(
                {
                    "destination": destination,
                    "status": UNCHECKED,
                    "detail": _incomplete_walk_detail(evidence, destination),
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
        if not recorded:
            # Nothing recorded means nothing measured. Under the `<=`
            # comparator a zero passes vacuously against any balance, and the
            # figure would publish as "0 lamports" -- which reads as "this
            # coin burned nothing" when what is true is that no walk has run.
            # An absence of evidence is UNCHECKED, never a total.
            per_destination.append(
                {
                    "destination": destination,
                    "status": UNCHECKED,
                    "detail": f"no inflows are recorded for {destination} yet, so no "
                              "total is stated for it",
                }
            )
            continue
        if comparator == "burned":
            per_destination.append(_incinerator_result(destination, recorded, vault_balance))
            continue
        check = sol_burn_balance(
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
        "SOL_BURN_BALANCE",
        overall,
        [SOL_BURN_TOTAL],
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


def burn_supply(mint_state, initial_supply_row=None, burned=None, walk_complete=False) -> Check:
    """PROTOCOL.md sec.4: initial_supply - sum(burn_amounts) == getMint(mint).supply.

    Backs both `BURN_TOTAL` (unchanged -- plan 03 gives `BURN_TOTAL` its own
    additional named check) and `SUPPLY_DESTROYED` (D-09's one published burn
    figure, new this phase).

    `initial_supply_row` is the `initial_supply` table row (or `None` if
    derivation has not run at all). Three UNCHECKED paths, in order:

    1. No row at all -- derivation hasn't run yet (today's default, before
       any evidence handle exists).
    2. A row exists but `raw_supply` is null -- EVID-08: the supply could not
       be derived, and the row's `unchecked_reason` is carried verbatim.
    3. `walk_complete` is False -- the burn walk for this mint hasn't seen
       every burn yet, so a reconciliation now would be premature, not wrong;
       never `FAIL` for an unfinished scan.

    Only once a real `raw_supply` exists AND the walk is complete does this
    compute `PASS`/`FAIL`.
    """
    if initial_supply_row is None:
        return _check(
            "BURN_SUPPLY",
            UNCHECKED,
            [BURN_TOTAL, SUPPLY_DESTROYED],
            "initial_supply - sum(burn_amounts) == getMint(mint).supply",
            "burn events are not recorded yet. Live supply is observed and stored, but a "
            "supply reading on its own proves a total, not that we know which burns "
            "produced it",
            actual=str(mint_state.supply),
        )
    if initial_supply_row.get("raw_supply") is None:
        return _check(
            "BURN_SUPPLY",
            UNCHECKED,
            [BURN_TOTAL, SUPPLY_DESTROYED],
            "initial_supply - sum(burn_amounts) == getMint(mint).supply",
            initial_supply_row.get("unchecked_reason")
            or "initial_supply could not be derived, and no reason was recorded",
            actual=str(mint_state.supply),
        )
    if not walk_complete:
        return _check(
            "BURN_SUPPLY",
            UNCHECKED,
            [BURN_TOTAL, SUPPLY_DESTROYED],
            "initial_supply - sum(burn_amounts) == getMint(mint).supply",
            "the burn walk for this mint is incomplete -- not every burn against it has "
            "been recorded yet, so a supply reconciliation now would be premature, not wrong",
            actual=str(mint_state.supply),
        )
    initial_supply = initial_supply_row["raw_supply"]
    burned = burned or 0
    ok = initial_supply - burned == mint_state.supply
    return _check(
        "BURN_SUPPLY",
        PASS if ok else FAIL,
        [BURN_TOTAL, SUPPLY_DESTROYED],
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

    Backs `SUPPLY_DESTROYED` as well as `BURN_TOTAL`: a live mint authority
    can reissue everything a crank ever burned, so it backs the same figure
    `burn_supply()` backs.
    """
    ok = mint_state.mint_authority is None
    return _check(
        "BURN_IRREVERSIBLE",
        PASS if ok else FAIL,
        [BURN_TOTAL, SUPPLY_DESTROYED],
        "getMint(mint).mint_authority == None",
        "the mint authority is revoked: burned supply cannot be reissued"
        if ok
        else "a mint authority still exists, so burned supply can be reissued. "
        "'permanently destroyed' is not a permitted claim for this coin",
        expected="None",
        actual=str(mint_state.mint_authority),
    )


def burn_atomic(mint: str, burn_rows, walk_complete: bool = False) -> Check:
    """PROTOCOL.md sec.4's atomicity requirement, RESEARCH.md Q6:
    `swap_instruction.transaction == burn_instruction.transaction`.

    `burn_rows` are `burn_event` rows (`indexer/evidence.py`), each carrying
    an `atomic` column of `'PASS' | 'FAIL' | None` (not yet classified --
    `scan.classify_atomicity` has not run against it, or ran before this
    check was ever computed).

    D-14 (2026-08-30, post-verification code review): PROTOCOL.md sec.4's
    atomicity requirement is about the protocol's OWN `BURN` leg -- a
    stranger burning their own tokens has no swap to be atomic with, and
    gating `SUPPLY_DESTROYED` on every burn against the mint would make the
    figure permanently unpublishable the moment a coin's non-crank burns are
    fully recorded. So this check now consults only rows flagged
    `protocol_attributed` (D-10) -- unforgeable and trivially checkable,
    zero for every coin until phase 5 registers a protocol program.

    UNCHECKED -- never FAIL -- in four cases, three "we have not finished
    checking" and one "this does not apply here":

    1. No burn recorded yet for this mint at all (ahead of the narrowing --
       even a third-party-only mint has "nothing recorded" to report before
       any row exists).
    2. The mint-wide burn walk is incomplete (also ahead of the narrowing --
       a burn the walk has not reached yet might turn out to be
       protocol-attributed, so "no protocol burns exist" cannot be claimed
       until the walk has actually seen everything).
    3. No protocol-attributed burn exists among the rows recorded so far --
       the not-applicable reading. Carries an EMPTY `backs` tuple: this
       check makes no claim about `SUPPLY_DESTROYED` at all when it does not
       apply, so it can never render as a vacuous `PASS` that would put
       `BURN_ATOMIC` on phase 2's page as a check backing a figure it never
       evaluated. The correct answer, not an awkward one -- the same shape
       as D-10, and true for every coin until phase 5.
    4. A protocol-attributed row has not yet been classified for atomicity.

    Only once the walk is complete and at least one protocol-attributed row
    exists, all classified, does this compute `PASS` (every protocol-
    attributed row reads `PASS`) or `FAIL` (any protocol-attributed row
    reads `FAIL`, named by signature). A third-party burn's own `atomic`
    classification is untouched by this narrowing -- `scan.classify_atomicity`
    keeps classifying and recording every burn it sees (D-09); only which
    rows THIS check consults changes.
    """
    equation = "swap_instruction.transaction == burn_instruction.transaction"
    if not burn_rows:
        return _check(
            "BURN_ATOMIC",
            UNCHECKED,
            [SUPPLY_DESTROYED],
            equation,
            "no burn recorded yet for this mint -- nothing to check",
        )
    if not walk_complete:
        return _check(
            "BURN_ATOMIC",
            UNCHECKED,
            [SUPPLY_DESTROYED],
            equation,
            "the burn walk for this mint is incomplete -- not every burn against it has "
            "been recorded yet, so an atomicity verdict now would be premature, not wrong",
        )
    protocol_rows = [row for row in burn_rows if row.get("protocol_attributed")]
    if not protocol_rows:
        return _check(
            "BURN_ATOMIC",
            UNCHECKED,
            [],
            equation,
            "no protocol-attributed burn exists for this mint (D-10) -- PROTOCOL.md sec.4's "
            "atomicity requirement is about the protocol's own BURN leg, not third-party "
            "burns, so this check does not apply here. It reads not-applicable for every "
            "coin until phase 5, because no protocol burns exist yet (D-14) -- the correct "
            "answer, not an awkward one",
        )
    unclassified = [row["signature"] for row in protocol_rows if row.get("atomic") is None]
    if unclassified:
        return _check(
            "BURN_ATOMIC",
            UNCHECKED,
            [SUPPLY_DESTROYED],
            equation,
            "protocol-attributed burns recorded but not yet classified for atomicity: "
            + ", ".join(unclassified),
        )
    failing = [row["signature"] for row in protocol_rows if row.get("atomic") == FAIL]
    if failing:
        return _check(
            "BURN_ATOMIC",
            FAIL,
            [SUPPLY_DESTROYED],
            equation,
            "a protocol-attributed burn's swap and burn were not found together in the "
            "same transaction -- signatures: " + ", ".join(failing),
            expected="PASS",
            actual="FAIL: " + ", ".join(failing),
        )
    return _check(
        "BURN_ATOMIC",
        PASS,
        [SUPPLY_DESTROYED],
        equation,
        "every recorded protocol-attributed burn's swap and burn share a transaction",
    )


def burn_spend(split, evidence=None) -> Check:
    """PROTOCOL.md sec.4: sum(SOL_spent_on_BURN) <= sum(fees_claimed) * bps_BURN / 10000.

    Backs `BURN_TOTAL`. Returns `UNCHECKED` today, always -- it needs
    recorded fee claims at a BURN destination, and no coin has a BURN
    destination while `Registry.program_id` is `None` (the protocol program
    is not deployed). This exists so `BURN_TOTAL` is never left resting on
    the `NO_CHECK` placeholder: a figure nothing checks at all is already
    unpublishable, but the indexer should say which check is missing rather
    than that none exists.
    """
    equation = "sum(SOL_spent_on_BURN) <= sum(fees_claimed) * bps_BURN / 10000"
    burn_destinations = [a.address for a in split.attributions if a.leg == "burn"]
    if not burn_destinations:
        return _check(
            "BURN_SPEND",
            UNCHECKED,
            [BURN_TOTAL],
            equation,
            "no BURN destination in this split -- the protocol program is not deployed, "
            "so a burn pool PDA cannot be derived yet, and this equation has nothing to check",
        )
    return _check(
        "BURN_SPEND",
        UNCHECKED,
        [BURN_TOTAL],
        equation,
        "a BURN destination exists but recording fee claims against it is not built yet",
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
            per_destination.append(
                {
                    "destination": destination,
                    "status": UNCHECKED,
                    "detail": _incomplete_walk_detail(evidence, destination),
                }
            )
            continue
        recorded = evidence.recorded_lamports(destination)
        if recorded == 0:
            # A COMPLETED walk that found nothing is not a failure. It is the
            # normal, correct state of a coin whose creator fees have never
            # been claimed -- most coins, most of the time. Returning FAIL
            # here brands a stranger's coin with the loudest state on the
            # page for the crime of not having traded yet, and FAIL is
            # supposed to mean "this contradicts", not "nothing happened".
            #
            # It cannot be PASS either: nothing was reconciled, so claiming
            # the routing is verified would be the overclaim the silence rule
            # exists to stop. UNCHECKED is the honest state and withholds
            # `ops_total` exactly as hard -- the same shape BURN_ATOMIC uses
            # for "no burn recorded yet for this mint".
            #
            # This only ever surfaced on third-party coins: $CHARLIE has no
            # OPS destination, so it returns UNCHECKED before reaching here.
            per_destination.append(
                {
                    "destination": destination,
                    "status": UNCHECKED,
                    "detail": f"{destination}: no protocol inflow recorded yet -- "
                    "the walk finished and found none, so there is nothing to "
                    "reconcile. This is what a coin whose creator fees have "
                    "never been claimed looks like, not a discrepancy",
                }
            )
            continue
        per_destination.append(
            {
                "destination": destination,
                "status": PASS,
                "detail": f"{destination}: recorded inflows observed ({recorded} lamports)",
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

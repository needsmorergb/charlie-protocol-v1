"""Burn campaigns: a stated goal, and the evidence that answers it.

A campaign is the one thing on this site that is **not** a measurement. Every
other surface reports what the chain did; a campaign reports what somebody
said they would do, beside what the chain shows so far. That difference is
the whole design, and two rules fall out of it.

**A campaign never invents a figure.** Progress is not stored. It is
recomputed, every time it is asked for, from the same `inflow` and
`burn_event` rows `SOL_BURN_BALANCE` and `SUPPLY_DESTROYED` are computed
from -- and it is obtained through `publish.Publisher`, so a coin whose
totals are withheld produces a campaign with **no progress figure**, not a
campaign with an ungated one. A progress bar that kept filling while the
check behind it read UNCHECKED would be exactly the decoration PROTOCOL.md
sec.4 was written against.

**A campaign only ships triggers the evidence store can answer.** The
obvious wishlist -- holder count, market cap, price, volume, social
milestones -- is absent, and the absence is deliberate rather than
unfinished. This store records lamport movements, token burns, mint supply
and sharing configs. It has no holder count and no price, so a
`holder_count` trigger would be a campaign that can never progress past
UNCHECKED while wearing a word that implies it can. `REFUSED_TRIGGERS`
names each one and why, so asking for one gets a sentence rather than a
silent failure.

What a campaign may claim is narrow: the goal, what the chain has recorded
toward it, and the check that backs that figure. Nothing more. PROTOCOL.md
sec.2 already forbids implying a price floor, and a campaign page is bound
by the same rule.

**The window is a subset, which is what makes the sum trustworthy.** A
campaign counts a slice of the same rows the coin-level check verified --
`SOL_BURN_TOTAL` and `SUPPLY_DESTROYED` are all-time aggregates, and a
campaign's window is always contained within that. A windowed subset of
checked evidence is still checked evidence. If those windows are ever
decoupled, this containment is the property that has to be re-established
before `progress_of` can keep calling itself gated.

`REACHED` is a statement about recorded evidence, not about execution. A
campaign whose target is met has had that much burned; it has not thereby
caused anything to happen. Execution is `distribute`'s and the cranks' job,
and this module does not sign, send, or build a transaction.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from . import invariants, legs, publish

LAMPORTS_PER_SOL = 1_000_000_000

# Below this, a SOL target is almost certainly SOL typed as lamports.
#
# `--target 5` meaning five SOL stores five LAMPORTS -- a billionth of the
# goal -- and the next speck of dust that lands satisfies it, so a campaign
# named "Burn 5 SOL" reports its target reached having burned effectively
# nothing. The figure behind that is properly gated and correctly computed;
# the GOAL is garbage, and a gate cannot catch a target that was wrong on
# the way in. This is the one input a typo can turn into a false public
# claim, so it is refused rather than accepted and displayed small.
#
# 0.001 SOL is deliberately low: it refuses the typo without refusing a
# genuinely tiny campaign, and `allow_dust_target` exists for anyone who
# really means it.
MINIMUM_SANE_SOL_TARGET = 1_000_000

# -- the closed vocabularies (owned here, enforced by `evidence.py`) --------

# What a campaign counts toward its target.
ASSET_SOL = "sol"
ASSET_TOKEN = "token"
ASSETS = (ASSET_SOL, ASSET_TOKEN)

# What starts a campaign counting, and what the engine can evaluate from
# stored evidence alone. Every one of these is answerable from `inflow`,
# `burn_event` or the clock -- nothing here needs a price feed or a holder
# census.
TRIGGER_MANUAL = "manual"          # counts from the moment it was declared
TRIGGER_SCHEDULE = "schedule"      # counts within a declared window
TRIGGER_SOL_AMOUNT = "sol_amount"  # reached when N lamports are burned
TRIGGER_TOKEN_AMOUNT = "token_amount"  # reached when N tokens are burned
TRIGGERS = (TRIGGER_MANUAL, TRIGGER_SCHEDULE, TRIGGER_SOL_AMOUNT, TRIGGER_TOKEN_AMOUNT)

# Triggers whose meaning depends on a threshold the declarer supplies.
TRIGGERS_REQUIRING_VALUE = (TRIGGER_SOL_AMOUNT, TRIGGER_TOKEN_AMOUNT)

# The wishlist, refused with a reason each. Not "coming soon": each of these
# needs a data source this protocol does not have and would have to trust
# somebody else for, which is the opposite of what every other figure here
# does. Naming them is what stops the set being quietly widened later by
# somebody who assumes the omission was an oversight.
REFUSED_TRIGGERS = {
    "holder_count": (
        "a holder count is a scan of every token account for the mint, which "
        "this store does not keep and which changes faster than it could be "
        "walked. A campaign gated on it would read UNCHECKED forever."
    ),
    "volume": (
        "trade volume is not recorded here. It would have to come from an "
        "indexer this protocol does not run, and a figure this protocol "
        "cannot recompute is a figure it does not publish."
    ),
    "market_cap": (
        "market cap needs a price, and a price needs an oracle or a pool "
        "read at a moment. PROTOCOL.md sec.2 forbids implying a price floor; "
        "a campaign that triggers on price is the same claim wearing a "
        "schedule."
    ),
    "price": (
        "the same as market_cap, and more directly: nothing in this "
        "repository reads a price, on purpose."
    ),
    "social": (
        "a follower or post count is somebody else's API answering about "
        "itself. It is not on a chain and it is not recomputable by a "
        "stranger, which is the standard every other figure here meets."
    ),
}

# Lifecycle. `status` is the one mutable field on a campaign row, and every
# transition is written to the append-only `campaign_event` table.
STATUS_ACTIVE = "active"
STATUS_REACHED = "reached"
STATUS_CLOSED = "closed"
STATUS_CANCELLED = "cancelled"
STATUSES = (STATUS_ACTIVE, STATUS_REACHED, STATUS_CLOSED, STATUS_CANCELLED)

# A campaign that is no longer counting. `REACHED` is terminal too: a target
# that was met and then kept counting would make the figure on the page mean
# something different from the figure that met it.
TERMINAL_STATUSES = (STATUS_REACHED, STATUS_CLOSED, STATUS_CANCELLED)


class CampaignError(Exception):
    """A campaign that cannot be declared, with the reason a person can act on."""


def campaign_id(mint: str, name: str) -> str:
    """A deterministic id, so re-declaring the same campaign is a no-op rather
    than a duplicate. Derived rather than random for the same reason
    `Evidence._config_hash` is: the identity of a thing should be a function
    of the thing.

    **`created_at` is deliberately NOT in the hash**, and the first version of
    this function had it. That version made identity a function of *when the
    command ran*, so declaring the same campaign twice produced two campaigns
    with the same name and the same goal and different ids -- the duplicate
    this function exists to prevent, created by the function meant to prevent
    it. A coin therefore holds at most one campaign of a given name, which is
    also the rule a reader would assume from seeing the name on a page.
    """
    digest = hashlib.sha256(f"{mint}\x00{name}".encode("utf-8")).hexdigest()
    return digest[:16]


def refuse_unsupported(trigger_type: str) -> None:
    """Raise with the written reason when `trigger_type` is one of the
    knowingly-refused set. Called before the vocabulary check so the refusal
    a person gets names *why*, not just that the word is unknown.
    """
    if trigger_type in REFUSED_TRIGGERS:
        raise CampaignError(
            f"trigger {trigger_type!r} is not supported: {REFUSED_TRIGGERS[trigger_type]}"
        )


def validate(
    *,
    trigger_type: str,
    target_value: int,
    asset: str,
    trigger_value: int | None = None,
    starts_at: int | None = None,
    ends_at: int | None = None,
    allow_dust_target: bool = False,
) -> None:
    """Everything that must hold before a campaign is worth storing.

    Raises `CampaignError` with a sentence, never a bare `False`: a dev who
    typed something impossible should be told which part.
    """
    refuse_unsupported(trigger_type)
    if trigger_type != TRIGGER_SCHEDULE and ends_at is not None:
        # A deadline on a campaign that does not have a window is the one
        # combination that silently creates a permanent public failure: a
        # manual campaign counts from its declaration and never closes
        # itself, but `advance()` gates closure on `ends_at` alone, so a
        # deadline handed to a manual trigger closes it exactly as a
        # scheduled one -- `closed`, unmet, forever, on a page the coin does
        # not control. Either the caller meant a scheduled campaign or they
        # meant no deadline; both are one word away, and neither is what
        # this was.
        raise CampaignError(
            f"trigger {trigger_type!r} takes no deadline: --ends-at closes a campaign "
            "whatever its trigger, so this one would close unmet at that time even "
            "though nothing about it is scheduled. Use --trigger schedule with "
            "--starts-at and --ends-at for a dated campaign, or drop --ends-at."
        )
    if trigger_type not in TRIGGERS:
        raise CampaignError(
            f"trigger {trigger_type!r} is not one of {', '.join(TRIGGERS)}"
        )
    if asset not in ASSETS:
        raise CampaignError(f"asset {asset!r} is not one of {', '.join(ASSETS)}")
    if int(target_value) <= 0:
        raise CampaignError(
            "a campaign's target must be positive -- a goal of zero is met by doing nothing"
        )
    if (
        asset == ASSET_SOL
        and not allow_dust_target
        and 0 < int(target_value) < MINIMUM_SANE_SOL_TARGET
    ):
        raise CampaignError(
            f"a SOL target is in LAMPORTS, and {int(target_value)} lamports is "
            f"{int(target_value) / LAMPORTS_PER_SOL:.9f} SOL. For "
            f"{int(target_value)} SOL pass {int(target_value) * LAMPORTS_PER_SOL} "
            "(1 SOL = 1,000,000,000 lamports). A target this small is met by "
            "the next speck of dust that lands, so the campaign would report "
            "its goal reached having burned almost nothing -- pass "
            "allow_dust_target (--allow-dust-target) if that is genuinely what "
            "you meant."
        )
    if trigger_type in TRIGGERS_REQUIRING_VALUE and trigger_value is None:
        raise CampaignError(f"trigger {trigger_type!r} needs a threshold (--trigger-value)")
    if trigger_type == TRIGGER_SCHEDULE and (starts_at is None or ends_at is None):
        raise CampaignError("a scheduled campaign needs both --starts-at and --ends-at")
    if starts_at is not None and ends_at is not None and int(ends_at) <= int(starts_at):
        raise CampaignError("a campaign's window must end after it starts")


def declare(
    evidence,
    *,
    mint: str,
    name: str,
    trigger_type: str,
    target_value: int,
    asset: str = ASSET_SOL,
    description: str | None = None,
    trigger_value: int | None = None,
    starts_at: int | None = None,
    ends_at: int | None = None,
    created_at: int | None = None,
    allow_dust_target: bool = False,
    config=None,
    declared_by: str | None = None,
    allow_unverified_declarer: bool = False,
) -> dict:
    """Validate and store one campaign. Returns the stored row.

    Declaring the same `(mint, name)` twice is a no-op, by construction of
    `campaign_id` and `record_campaign`'s `INSERT OR IGNORE` -- the second
    call returns the row already stored, with its original target and
    `created_at` intact. Re-declaring is therefore never a way to retarget a
    campaign that is not going to meet its goal.

    **Who may speak for a coin.** A campaign puts words in a coin's mouth on
    a public page: a goal it will be measured against, and a `closed` status
    if it misses. Without a check, anyone could declare "burn 500 SOL by
    Friday" for a token they have nothing to do with, and the coin's page
    would carry a promise it never made.

    So `declared_by` is checked against the coin's live
    `sharing_config.admin`, which is the same authority pump already
    recognises and the same rule the on-chain program applies to
    `init_route`/`set_route` -- ownership is not a claim this protocol
    invents, it is one it reads. `config` is the coin's `SharingConfig`
    (the caller reads it; this module holds no RPC handle, which is what
    keeps every test of it offline).

    A coin with no sharing config has no admin to check against, and that is
    a refusal rather than a pass: there is nobody the chain says may speak
    for it. `allow_unverified_declarer` is the deliberate opt-out, for an
    operator declaring on a coin's behalf with its knowledge -- it records
    that the declaration was not verified rather than pretending it was.
    """
    if not allow_unverified_declarer:
        if config is None:
            raise CampaignError(
                "declaring needs the coin's sharing config, so the declarer can be "
                "checked against the admin pump recognises. A coin with no sharing "
                "config has nobody the chain says may speak for it -- pass "
                "allow_unverified_declarer (--allow-unverified-declarer) to declare "
                "anyway, and the declaration is recorded as unverified."
            )
        admin = getattr(config, "admin", None)
        if declared_by is None:
            raise CampaignError(
                "declaring needs --declared-by: the wallet claiming to speak for this "
                f"coin. Its sharing config names {admin} as admin."
            )
        if declared_by != admin:
            raise CampaignError(
                f"{declared_by} is not this coin's admin. Its sharing config names "
                f"{admin}, and that is the authority pump recognises -- the same one "
                "the protocol program checks before it will write a coin's route. A "
                "campaign declared by anyone else would put a goal on the coin's page "
                "that the coin never set."
            )

    validate(
        trigger_type=trigger_type,
        target_value=target_value,
        asset=asset,
        trigger_value=trigger_value,
        starts_at=starts_at,
        ends_at=ends_at,
        allow_dust_target=allow_dust_target,
    )
    created_at = created_at if created_at is not None else int(time.time())
    identifier = campaign_id(mint, name)
    inserted = evidence.record_campaign(
        campaign_id=identifier,
        mint=mint,
        name=name,
        description=description,
        trigger_type=trigger_type,
        trigger_value=trigger_value,
        target_value=int(target_value),
        asset=asset,
        status=STATUS_ACTIVE,
        starts_at=starts_at,
        ends_at=ends_at,
        created_at=created_at,
    )
    if inserted:
        # The declarer travels with the declaration. An unverified one says so
        # in the record rather than being indistinguishable from a checked one.
        who = declared_by if declared_by else "unverified declarer"
        evidence.record_campaign_event(
            campaign_id=identifier,
            event="declared",
            detail=f"{name}: {target_value} {asset} (by {who})",
            occurred_at=created_at,
        )
    return evidence.campaign(identifier)


def counting_window(row: dict) -> int | None:
    """The block time a campaign starts counting from -- `None` means "all
    recorded history for this coin".

    A manual campaign counts from when it was declared, not from the coin's
    creation: a campaign that opened already complete because of burns that
    happened last year is not a campaign, it is a retroactive claim.
    """
    if row["trigger_type"] == TRIGGER_SCHEDULE:
        return row["starts_at"]
    return row["created_at"]


def _sol_burn_destinations(split) -> tuple:
    """The addresses on this coin's SOL-burn leg, from its own split.

    Read from the split rather than assumed, for the reason
    `legs.classify_split` is never cached: which addresses count as a SOL
    burn changes the moment a program id is registered.
    """
    if split is None:
        return ()
    return tuple(a.address for a in split.attributions if a.leg == legs.SOL_BURN)


@dataclass(frozen=True)
class Progress:
    """What the chain has recorded toward a campaign's goal, or why it cannot say.

    `value` is `None` exactly when `withheld_by` is set -- never both, never
    neither, mirroring `record_initial_supply`'s exactly-one-of contract.
    A `None` value is not zero, and a surface must render it as "not
    published" rather than as an empty bar.
    """

    campaign_id: str
    target: int
    asset: str
    value: int | None
    backed_by: tuple = ()
    withheld_by: tuple = ()

    @property
    def reached(self) -> bool:
        """True only on evidence. A withheld figure never reaches a target --
        "we cannot say" is not "it happened"."""
        return self.value is not None and self.value >= self.target

    @property
    def percent(self) -> float | None:
        if self.value is None:
            return None
        return min(100.0, self.value / self.target * 100.0) if self.target else None

    def as_dict(self) -> dict:
        return {
            "campaign_id": self.campaign_id,
            "target": self.target,
            "asset": self.asset,
            "value": self.value,
            "percent": self.percent,
            "reached": self.reached,
            "backed_by": list(self.backed_by),
            "withheld_by": [
                {"check": name, "status": status, "detail": detail}
                for name, status, detail in self.withheld_by
            ],
        }


def progress_of(row: dict, observation, evidence) -> Progress:
    """Recompute one campaign's progress, through the publication gate.

    The gate is the point. A campaign counting SOL asks for
    `SOL_BURN_TOTAL`; one counting tokens asks for `SUPPLY_DESTROYED`. If
    that figure is withheld for this coin, the campaign's progress is
    withheld with it, carrying the same blocking checks -- so a campaign can
    never be the surface through which an unchecked number reaches a reader.

    The gated figure decides *whether* a number may be shown; the windowed
    sum decides *which* number. Both come from the same rows.
    """
    figure = (
        invariants.SOL_BURN_TOTAL if row["asset"] == ASSET_SOL else invariants.SUPPLY_DESTROYED
    )
    publisher = publish.Publisher(observation)
    try:
        _value, backing = publisher.figure(figure)
    except publish.Withheld as exc:
        return Progress(
            campaign_id=row["campaign_id"],
            target=int(row["target_value"]),
            asset=row["asset"],
            value=None,
            withheld_by=tuple(exc.reasons),
        )

    since = counting_window(row)
    if row["asset"] == ASSET_SOL:
        destinations = _sol_burn_destinations(observation.split)
        value = evidence.burned_lamports_since(row["mint"], destinations, since)
    else:
        value = evidence.tokens_burned_since(row["mint"], since)

    return Progress(
        campaign_id=row["campaign_id"],
        target=int(row["target_value"]),
        asset=row["asset"],
        value=int(value),
        backed_by=tuple(backing),
    )


def advance(row: dict, progress: Progress, evidence, *, now: int | None = None) -> str:
    """Move a campaign to its next status if the evidence says so, and return
    the status it now holds.

    Only two transitions are automatic, and both are one-way:

    * an active campaign whose recorded progress meets its target becomes
      `reached`;
    * an active scheduled campaign whose window has closed becomes `closed`.

    A withheld progress moves nothing. Neither does a terminal campaign --
    `reached` never reverts, because a target met on the evidence of the day
    was met, and a later re-scan that widened a window would otherwise
    silently un-meet it.
    """
    status = row["status"]
    if status in TERMINAL_STATUSES:
        return status
    now = now if now is not None else int(time.time())

    if progress.reached:
        evidence.set_campaign_status(
            row["campaign_id"],
            STATUS_REACHED,
            detail=f"{progress.value} of {progress.target} {progress.asset}",
            at=now,
        )
        return STATUS_REACHED

    ends_at = row["ends_at"]
    if ends_at is not None and now >= int(ends_at):
        evidence.set_campaign_status(
            row["campaign_id"],
            STATUS_CLOSED,
            detail=(
                f"window ended with {progress.value} of {progress.target} {progress.asset}"
                if progress.value is not None
                else "window ended; progress was withheld"
            ),
            at=now,
        )
        return STATUS_CLOSED

    return status


def evaluate(mint: str, observation, evidence, *, now: int | None = None) -> list:
    """Every campaign for one coin, with progress and any status change.

    Returns `[(row, Progress, status), ...]` in declaration order. This is
    the whole engine: read the declared campaigns, recompute each one's
    progress through the gate, advance what the evidence advances.
    """
    results = []
    for row in evidence.campaigns(mint=mint):
        progress = progress_of(row, observation, evidence)
        status = advance(row, progress, evidence, now=now)
        current = dict(row)
        current["status"] = status
        results.append((current, progress, status))
    return results


def format_amount(value: int | None, asset: str, decimals: int = 9) -> str:
    """A campaign's amount in the unit a reader expects -- SOL for the SOL
    leg, raw token units otherwise. `None` renders as the withheld marker,
    never as `0`.
    """
    if value is None:
        return "not published"
    if asset == ASSET_SOL:
        return f"{value / LAMPORTS_PER_SOL:.6f} SOL"
    scale = 10 ** decimals
    return f"{value / scale:,.6f}" if decimals else f"{value:,}"

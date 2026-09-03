"""Attributing each shareholder of a sharing config to a protocol leg.

pump's config is a flat list of `(address, bps)`. The protocol's three legs are
a *reading* of that list, and the reading has to be conservative in one
specific direction:

    an address is OPS unless it can be proven to be SOL burn or BURN.

Nothing about a bare address on chain says "this is a SOL burn vault". Program
derivation can be established, and even that is not sufficient: a program-derived
address belongs to *some* program, and that program may well have an instruction
that moves its lamports. Only two things license a SOL burn classification:

* the address is `PDA(["sol_burn", mint])` of the protocol program, whose code
  contains no instruction that moves lamports out of a SOL-burn PDA, and whose
  upgrade authority is revoked so it never will (ARCHITECTURE.md sec.2); or
* the address is in the grandfathered registry -- a legacy shared vanity
  address named in PROTOCOL.md sec.3, which carries the weaker `<=` invariant
  because attribution across coins is impossible.

Everything else, including program-derived addresses we cannot attribute, is
reported as OPS. That will occasionally understate a coin. It will never
overstate one, and overstating is the failure mode that matters.
"""

from __future__ import annotations

from dataclasses import dataclass

from .base58 import pubkey_bytes
from .curve import find_program_address, is_on_curve

SOL_BURN = "sol_burn"
BURN = "burn"
OPS = "paid"

# -- what a fee recipient actually IS (D-40) ---------------------------
# pump's sharing config is N addresses with bps and NO labels. The leg names
# above describe what this protocol will enforce once its program exists;
# they say nothing about an unenrolled coin, where every address falls to
# "unproven is OPS" and the classification carries no information.
#
# An account's owner program does carry information, costs nothing extra --
# `rpc.accounts()` already returns it -- and the categories below were not
# invented: they are what a frequency analysis over 1,681 multi-shareholder
# configs actually found on 2026-09-02.
SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
FEE_SHARE_PROGRAM = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"

RECIPIENT_WALLET = "wallet"
RECIPIENT_SHARING_CONFIG = "sharing_config"
RECIPIENT_TOKEN_ACCOUNT = "token_account"
RECIPIENT_PROGRAM_OWNED = "program_owned"
RECIPIENT_NEVER_FUNDED = "never_funded"

RECIPIENT_KINDS = (
    RECIPIENT_WALLET,
    RECIPIENT_SHARING_CONFIG,
    RECIPIENT_TOKEN_ACCOUNT,
    RECIPIENT_PROGRAM_OWNED,
    RECIPIENT_NEVER_FUNDED,
)


# -- pump's charity coins (D-41) --------------------------------------
# donate.gg's fee wallet. Established structurally, not from a press release:
# across all 17,646 multi-shareholder sharing configs, 568 name this address
# and 565 have the exact shape (1000, 9000) -- 10% always to it, 90% to one of
# 122 distinct counterparts. That is donate.gg's published 10% Charity-Coins
# fee, taken before funds reach the charity's wallet, and the counterparts are
# the charity-designated wallets (donate.gg-controlled, not the charities').
DONATE_GG_FEE_WALLET = "98fYvtYvLt56PujTNugQcYf4JTAg85kZXvoWTuhhu7eu"
DONATE_GG_FEE_BPS = 1000


def charity_recipients(split) -> tuple:
    """The charity-designated destinations of a charity coin, or ().

    A coin is a charity coin when its config pays donate.gg's fee wallet.
    Everything OTHER than that fee share is the charity side.

    This says where the config points. It does NOT say a charity received
    anything: pump's own disclaimer puts conversion and forwarding entirely
    inside donate.gg, so the chain stops being evidence at that wallet.
    """
    attributions = getattr(split, "attributions", ()) or ()
    if not any(a.address == DONATE_GG_FEE_WALLET for a in attributions):
        return ()
    return tuple(a.address for a in attributions if a.address != DONATE_GG_FEE_WALLET)


def donate_gg_fee_bps(split) -> int | None:
    """The bps donate.gg takes, read off the config rather than assumed.

    565 of 568 charity coins use 1000 (10%) and three use 500. Reading it
    means a coin on different terms is described correctly instead of being
    told what it should have been.
    """
    for a in getattr(split, "attributions", ()) or ():
        if a.address == DONATE_GG_FEE_WALLET:
            return a.bps
    return None


def recipient_kind(account) -> str:
    """What a fee recipient is, from the account's owner program.

    `account` is one entry of `rpc.accounts()` -- `None` when the account
    does not exist.

    **`never_funded` is a real finding, not an error.** A Solana account is
    created on first receipt, so an address that does not exist has never
    received a lamport. Two addresses named by 96 configs each were in
    exactly this state when measured; either those fees were never claimed,
    or they are routed somewhere nobody is watching.

    **`sharing_config` means fee splitting is CHAINED** -- a config naming
    another config as a shareholder. Attribution walks one level and stops,
    so a coin whose fees flow onward through a second config is described by
    neither. Reporting the kind at least makes that visible instead of
    silently flattening it into "a wallet got paid".

    This is an OBSERVED FACT, not a figure: it is not a member of
    `invariants.FIGURES` and is not gated. It states what an address is, and
    claims nothing about how much reached it.
    """
    if account is None:
        return RECIPIENT_NEVER_FUNDED
    owner = account.get("owner") if isinstance(account, dict) else None
    if owner == SYSTEM_PROGRAM:
        return RECIPIENT_WALLET
    if owner == FEE_SHARE_PROGRAM:
        return RECIPIENT_SHARING_CONFIG
    if owner in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        return RECIPIENT_TOKEN_ACCOUNT
    return RECIPIENT_PROGRAM_OWNED

# D-26: the five buckets a coin's split can fall into today, computed from
# the bps -- never cached (see classify_split's docstring).
CLASSIFICATIONS = ("none", "sol_burn-only", "burn-only", "ops-only", "mixed")

# The protocol's own three names, in display form -- `paid` is this module's
# internal leg key, but D-26's index shows "ops", the name a reader actually
# recognises.
_DISPLAY_NAME = {SOL_BURN: "sol_burn", BURN: "burn", OPS: "ops"}

# Solana's incinerator. From the runtime's own source
# (sdk/program/src/incinerator.rs):
#
#     "Lamports credited to this address will be removed from the total
#      supply (burned) at the end of the current block."
#
# SOL routed here leaves the total supply, which is what the protocol's
# deflation claim rests on. Checked against mainnet: the account does not
# exist and its balance is 0, which is what an address whose credits are
# removed every block looks like.
#
# NOTE, because it is the trap next to this one: sending an SPL TOKEN here
# does not burn it. Only lamports are destroyed by the runtime. Token burns
# go through the token program's own burn instruction, which is the BURN
# leg, not this one.
SOL_BURN_INCINERATOR = "1nc1nerator11111111111111111111111111111111"

# PROTOCOL.md sec.3: $CHARLIE's vault, shared with other creators, predating the
# spec. Grandfathered, weaker invariant, no new entries by default -- adding one
# is a spec decision, not a code change.
GRANDFATHERED_SOL_BURN = frozenset({"burn111111111111111111111111111111111111111"})

# The protocol program is not deployed. Until it is, no address can be derived
# as a SOL-burn or token-burn PDA, and every enrolled-looking split reads as OPS. That is
# the correct answer today, and the indexer says it in those words rather than
# guessing.
PROGRAM_ID: str | None = None


@dataclass(frozen=True)
class Attribution:
    address: str
    bps: int
    leg: str
    reason: str
    keyless: bool


@dataclass(frozen=True)
class Split:
    """The *fact* of PROTOCOL.md sec.6, in bps, plus how it was arrived at."""

    sol_burn: int
    burn: int
    paid: int
    attributions: tuple[Attribution, ...]

    @property
    def total(self) -> int:
        return self.sol_burn + self.burn + self.paid

    def as_dict(self) -> dict:
        return {"sol_burn": self.sol_burn, "burn": self.burn, "paid": self.paid}


# Addresses the chain treats as burns: SOL sent to one is out of circulation
# and stays there. This is a different question from which addresses are
# ATTRIBUTED to the sol_burn leg (`GRANDFATHERED_SOL_BURN`, above). The two
# sets coincide today and are kept apart on purpose: a registry may put an
# address on the sol_burn leg because a coin routes there calling it a burn,
# without vouching that it is one. `SOL_BURN_UNSPENDABLE` is the check that
# tells those apart, and it can only do that if the sets are separate.
RECOGNISED_BURN = frozenset({"burn111111111111111111111111111111111111111"})


@dataclass(frozen=True)
class Registry:
    program_id: str | None = PROGRAM_ID
    grandfathered_sol_burn: frozenset = GRANDFATHERED_SOL_BURN
    recognised_burn: frozenset = RECOGNISED_BURN

    def is_burn_destination(self, address: str) -> bool:
        """One SOL does not come back from: the incinerator, where the runtime
        removes credited lamports from the total supply, or a recognised burn
        address."""
        return address == SOL_BURN_INCINERATOR or address in self.recognised_burn

    def sol_burn_vault(self, mint: str) -> str | None:
        if not self.program_id:
            return None
        return find_program_address([b"sol_burn", pubkey_bytes(mint)], self.program_id)[0]

    def burn_pool(self, mint: str) -> str | None:
        if not self.program_id:
            return None
        return find_program_address([b"burn", pubkey_bytes(mint)], self.program_id)[0]


def classify(address: str, mint: str, registry: Registry) -> tuple[str, str]:
    if address == SOL_BURN_INCINERATOR:
        return SOL_BURN, (
            "Solana's incinerator: the runtime removes lamports credited here "
            "from the total supply at the end of the block. Not merely "
            "unspendable -- destroyed."
        )
    if address == registry.sol_burn_vault(mint):
        return SOL_BURN, "PDA(['sol_burn', mint]) of the protocol program"
    if address == registry.burn_pool(mint):
        return BURN, "PDA(['burn', mint]) of the protocol program"
    if address in registry.grandfathered_sol_burn:
        return SOL_BURN, "grandfathered legacy SOL-burn address (PROTOCOL.md sec.3)"
    if not is_on_curve(address):
        return OPS, (
            "program-derived -- but it is some other program's PDA, and that "
            "program may be able to move it. Not provably burned."
        )
    return OPS, "an ordinary account, not program-derived"


def split_of(config, registry: Registry | None = None) -> Split:
    registry = registry or Registry()
    totals = {SOL_BURN: 0, BURN: 0, OPS: 0}
    attributions = []
    for address, bps in config.shareholders:
        leg, reason = classify(address, config.mint, registry)
        totals[leg] += bps
        attributions.append(
            Attribution(
                address=address,
                bps=bps,
                leg=leg,
                reason=reason,
                keyless=not is_on_curve(address),
            )
        )
    return Split(
        sol_burn=totals[SOL_BURN],
        burn=totals[BURN],
        paid=totals[OPS],
        attributions=tuple(attributions),
    )


def classify_split(split) -> str:
    """D-26: the label is what the split does today, computed from the bps --
    count the legs with a non-zero share; zero legs is `none`, one leg is
    that leg's name with an `-only` suffix, more than one is `mixed`.

    Accepts either a `Split` (attribute access) or a plain `{sol_burn, burn,
    paid}` mapping (the SPLIT figure's already-gated value, which is exactly
    the shape `publish.classification` hands this function) -- both carry
    the same three numbers and nothing else this function needs.

    Never cached: `Registry.program_id` is `None` today, so nothing
    classifies as SOL burn or BURN, but the moment phase 5 registers a program
    id the same addresses reclassify. A stored label would go quietly
    stale -- this is computed fresh every time, from the bps in hand.
    """
    if isinstance(split, dict):
        sol_burn, burn, paid = split.get("sol_burn", 0), split.get("burn", 0), split.get("paid", 0)
    else:
        sol_burn, burn, paid = split.sol_burn, split.burn, split.paid

    present = [leg for leg, bps in ((SOL_BURN, sol_burn), (BURN, burn), (OPS, paid)) if bps]
    if not present:
        return "none"
    if len(present) > 1:
        return "mixed"
    return f"{_DISPLAY_NAME[present[0]]}-only"

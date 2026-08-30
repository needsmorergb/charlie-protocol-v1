"""Attributing each shareholder of a sharing config to a protocol leg.

pump's config is a flat list of `(address, bps)`. The protocol's three legs are
a *reading* of that list, and the reading has to be conservative in one
specific direction:

    an address is OPS unless it can be proven to be SEAL or BURN.

Nothing about a bare address on chain says "this is a seal vault". What can be
proven is the negative -- that no private key exists for it -- and even that is
not sufficient: an off-curve address is a PDA of *some* program, and that
program may well have an instruction that moves its lamports. Only two things
license a SEAL classification:

* the address is `PDA(["seal", mint])` of the protocol program, whose code
  contains no instruction that moves lamports out of a seal PDA, and whose
  upgrade authority is revoked so it never will (ARCHITECTURE.md sec.2); or
* the address is in the grandfathered registry -- a legacy shared vanity
  address named in PROTOCOL.md sec.3, which carries the weaker `<=` invariant
  because attribution across coins is impossible.

Everything else, including off-curve addresses we cannot attribute, is
reported as OPS. That will occasionally understate a coin. It will never
overstate one, and overstating is the failure mode that matters.
"""

from __future__ import annotations

from dataclasses import dataclass

from .base58 import pubkey_bytes
from .curve import find_program_address, is_on_curve

SEAL = "seal"
BURN = "burn"
OPS = "paid"

# PROTOCOL.md sec.3: $CHARLIE's vault, shared with other creators, predating the
# spec. Grandfathered, weaker invariant, no new entries by default -- adding one
# is a spec decision, not a code change.
GRANDFATHERED_SEAL = frozenset({"burn111111111111111111111111111111111111111"})

# The protocol program is not deployed. Until it is, no address can be derived
# as a seal or burn PDA, and every enrolled-looking split reads as OPS. That is
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

    seal: int
    burn: int
    paid: int
    attributions: tuple[Attribution, ...]

    @property
    def total(self) -> int:
        return self.seal + self.burn + self.paid

    def as_dict(self) -> dict:
        return {"seal": self.seal, "burn": self.burn, "paid": self.paid}


@dataclass(frozen=True)
class Registry:
    program_id: str | None = PROGRAM_ID
    grandfathered_seal: frozenset = GRANDFATHERED_SEAL

    def seal_vault(self, mint: str) -> str | None:
        if not self.program_id:
            return None
        return find_program_address([b"seal", pubkey_bytes(mint)], self.program_id)[0]

    def burn_pool(self, mint: str) -> str | None:
        if not self.program_id:
            return None
        return find_program_address([b"burn", pubkey_bytes(mint)], self.program_id)[0]


def classify(address: str, mint: str, registry: Registry) -> tuple[str, str]:
    if address == registry.seal_vault(mint):
        return SEAL, "PDA(['seal', mint]) of the protocol program"
    if address == registry.burn_pool(mint):
        return BURN, "PDA(['burn', mint]) of the protocol program"
    if address in registry.grandfathered_seal:
        return SEAL, "grandfathered legacy seal address (PROTOCOL.md sec.3)"
    if not is_on_curve(address):
        return OPS, (
            "off the ed25519 curve, so no private key exists -- but it is some "
            "program's PDA and that program may be able to move it. Not provably sealed."
        )
    return OPS, "an ordinary keyed address: spendable by whoever holds the key"


def split_of(config, registry: Registry | None = None) -> Split:
    registry = registry or Registry()
    totals = {SEAL: 0, BURN: 0, OPS: 0}
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
        seal=totals[SEAL],
        burn=totals[BURN],
        paid=totals[OPS],
        attributions=tuple(attributions),
    )

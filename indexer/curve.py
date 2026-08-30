"""ed25519 point decompression, and the PDA derivation that depends on it.

This module is the cryptographic core of the SEAL leg. PROTOCOL.md sec.3 says a
seal vault must be a PDA rather than a vanity address because a PDA is
*provably* off the ed25519 curve -- no private key can exist for it, as opposed
to the assumption that nobody ground one out. `is_on_curve` is where that proof
is actually computed, so it is written out longhand rather than imported.

Mirrors curve25519-dalek's `CompressedEdwardsY::decompress`, which is what the
Solana runtime uses when it rejects a PDA candidate.
"""

from __future__ import annotations

import hashlib

from .base58 import encode, pubkey_bytes

_Q = 2**255 - 19
_D = -121665 * pow(121666, _Q - 2, _Q) % _Q
_I = pow(2, (_Q - 1) // 4, _Q)

PDA_MARKER = b"ProgramDerivedAddress"


def _xrecover(y: int) -> int | None:
    """The even x for this y, or None if no curve point has that y."""
    denom = pow(_D * y * y + 1, _Q - 2, _Q)
    xx = (y * y - 1) * denom % _Q
    x = pow(xx, (_Q + 3) // 8, _Q)
    if (x * x - xx) % _Q != 0:
        x = x * _I % _Q
    if (x * x - xx) % _Q != 0:
        return None
    return _Q - x if x % 2 else x


def is_on_curve(candidate) -> bool:
    """Does this 32-byte value decompress to an ed25519 point?

    True  -> a private key could exist for it. It is spendable by whoever holds
             that key, and the protocol may not call it a seal.
    False -> no private key exists. Only a program that can sign for the
             address may move its lamports -- and if no such program exists, or
             the program that owns it has no instruction that moves them, the
             lamports cannot move at all.
    """
    raw = candidate if isinstance(candidate, (bytes, bytearray)) else pubkey_bytes(candidate)
    if len(raw) != 32:
        return False
    encoded = int.from_bytes(raw, "little")
    sign_bit = encoded >> 255
    y = (encoded & ((1 << 255) - 1)) % _Q
    x = _xrecover(y)
    if x is None:
        return False
    return not (x == 0 and sign_bit)


def create_program_address(seeds: list[bytes], program_id) -> bytes | None:
    digest = hashlib.sha256(
        b"".join(seeds) + pubkey_bytes(program_id) + PDA_MARKER
    ).digest()
    return None if is_on_curve(digest) else digest


def find_program_address(seeds: list[bytes], program_id) -> tuple[str, int]:
    for bump in range(255, -1, -1):
        address = create_program_address(seeds + [bytes([bump])], program_id)
        if address is not None:
            return encode(address), bump
    raise ValueError("no viable program address for these seeds")  # pragma: no cover

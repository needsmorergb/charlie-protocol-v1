"""ed25519 signing, RFC 8032, standard library only.

The indexer never needed to sign anything: it reads. The buyback crank does,
because the wallet that buys and burns has to sign the transaction that does
it, and the standard-library rule (a verifier you have to build an
environment for is a verifier fewer people run) applies to the keeper too.

Written out longhand, the same way `curve.py` writes point decompression
longhand, and checked three ways in `tests/test_ed25519.py`: RFC 8032's own
test vectors, a round trip through `verify`, and -- when the `cryptography`
package happens to be installed -- agreement with an independent
implementation on random keys. `sign` also verifies its own output before
returning it, so a wrong signature is an exception here rather than a fee
paid for a rejected transaction.

The key never leaves the process that loads it. Nothing here prints, logs
or transmits a secret; `Keypair.__repr__` shows the public address only.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .base58 import decode, encode
from .curve import _D, _Q, _xrecover

# The group order.
_L = 2**252 + 27742317777372353535851937790883648493
# The base point: y = 4/5, x the even root.
_BY = 4 * pow(5, _Q - 2, _Q) % _Q
_BX = _xrecover(_BY)
_B = (_BX, _BY, 1, _BX * _BY % _Q)
_IDENTITY = (0, 1, 1, 0)
_2D = 2 * _D % _Q


def _add(p, q):
    """Unified addition on the extended coordinates (X, Y, Z, T) of the
    a = -1 twisted Edwards curve -- valid for doubling as well, which keeps
    the scalar multiplication to one formula.
    """
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = (y1 - x1) * (y2 - x2) % _Q
    b = (y1 + x1) * (y2 + x2) % _Q
    c = t1 * _2D * t2 % _Q
    d = z1 * 2 * z2 % _Q
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _Q, g * h % _Q, f * g % _Q, e * h % _Q)


def _mul(scalar: int, point):
    result = _IDENTITY
    addend = point
    while scalar:
        if scalar & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        scalar >>= 1
    return result


def _encode_point(p) -> bytes:
    x, y, z, _t = p
    inv = pow(z, _Q - 2, _Q)
    x, y = x * inv % _Q, y * inv % _Q
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decode_point(raw: bytes):
    if len(raw) != 32:
        return None
    encoded = int.from_bytes(raw, "little")
    sign = encoded >> 255
    y = encoded & ((1 << 255) - 1)
    if y >= _Q:
        return None
    x = _xrecover(y)
    if x is None:
        return None
    if x == 0 and sign:
        return None
    if x & 1 != sign:
        x = _Q - x
    return (x, y, 1, x * y % _Q)


def _sha512(*parts: bytes) -> bytes:
    h = hashlib.sha512()
    for part in parts:
        h.update(part)
    return h.digest()


def _expand(seed: bytes) -> tuple[int, bytes]:
    if len(seed) != 32:
        raise ValueError("an ed25519 seed is 32 bytes")
    digest = _sha512(seed)
    a = int.from_bytes(digest[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, digest[32:]


def public_key(seed: bytes) -> bytes:
    a, _prefix = _expand(seed)
    return _encode_point(_mul(a, _B))


def sign(seed: bytes, message: bytes) -> bytes:
    a, prefix = _expand(seed)
    public = _encode_point(_mul(a, _B))
    r = int.from_bytes(_sha512(prefix, message), "little") % _L
    big_r = _encode_point(_mul(r, _B))
    k = int.from_bytes(_sha512(big_r, public, message), "little") % _L
    s = (r + k * a) % _L
    signature = big_r + s.to_bytes(32, "little")
    if not verify(public, message, signature):
        raise RuntimeError("ed25519: the signature just produced does not verify -- refusing to return it")
    return signature


def verify(public: bytes, message: bytes, signature: bytes) -> bool:
    if len(signature) != 64 or len(public) != 32:
        return False
    point_a = _decode_point(public)
    point_r = _decode_point(signature[:32])
    if point_a is None or point_r is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        return False
    k = int.from_bytes(_sha512(signature[:32], public, message), "little") % _L
    left = _encode_point(_mul(s, _B))
    right = _encode_point(_add(point_r, _mul(k, point_a)))
    return left == right


@dataclass(frozen=True)
class Keypair:
    seed: bytes
    public: bytes

    def __repr__(self) -> str:  # never the seed
        return f"Keypair({self.address})"

    @property
    def address(self) -> str:
        return encode(self.public)

    def sign(self, message: bytes) -> bytes:
        return sign(self.seed, message)

    @classmethod
    def from_seed(cls, seed: bytes) -> "Keypair":
        return cls(bytes(seed), public_key(bytes(seed)))

    @classmethod
    def from_secret_bytes(cls, raw: bytes) -> "Keypair":
        """32 bytes (a seed) or 64 bytes (seed followed by public key, the
        layout Solana's CLI and most wallet exports use). The 64-byte form
        is checked: a public half that does not match the seed means the
        file is not what it claims to be, and signing with it would produce
        transactions from a wallet the caller did not intend.
        """
        raw = bytes(raw)
        if len(raw) == 32:
            return cls.from_seed(raw)
        if len(raw) == 64:
            pair = cls.from_seed(raw[:32])
            if pair.public != raw[32:]:
                raise ValueError("secret key's public half does not match its seed -- not a valid keypair")
            return pair
        raise ValueError(f"a secret key is 32 or 64 bytes, got {len(raw)}")

    @classmethod
    def from_base58(cls, text: str) -> "Keypair":
        return cls.from_secret_bytes(decode(text.strip()))

    @classmethod
    def from_file(cls, path: str | Path) -> "Keypair":
        """A Solana CLI keypair file (a JSON array of 64 byte values), or a
        file holding one base58 secret key.
        """
        text = Path(path).read_text(encoding="utf-8").strip()
        if text.startswith("["):
            values = json.loads(text)
            if not isinstance(values, list) or not all(isinstance(v, int) and 0 <= v < 256 for v in values):
                raise ValueError(f"{path}: not a Solana keypair file (expected a JSON array of bytes)")
            return cls.from_secret_bytes(bytes(values))
        return cls.from_base58(text)

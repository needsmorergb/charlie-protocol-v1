"""Base58 — the encoding Solana addresses are written in.

Standard library only. The whole indexer is, deliberately: a verifier that
cannot be installed is a verifier nobody checks your work with.
"""

from __future__ import annotations

ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {char: value for value, char in enumerate(ALPHABET)}


def encode(raw: bytes) -> str:
    leading = len(raw) - len(raw.lstrip(b"\0"))
    number = int.from_bytes(raw, "big")
    out = ""
    while number:
        number, rem = divmod(number, 58)
        out = ALPHABET[rem] + out
    return "1" * leading + out


def decode(text: str) -> bytes:
    number = 0
    for char in text:
        if char not in _INDEX:
            raise ValueError("invalid base58 character " + repr(char))
        number = number * 58 + _INDEX[char]
    leading = len(text) - len(text.lstrip("1"))
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\0" * leading + body


def pubkey_bytes(value) -> bytes:
    """Accept a base58 address or raw 32 bytes; always return 32 bytes."""
    raw = value if isinstance(value, (bytes, bytearray)) else decode(value)
    if len(raw) != 32:
        raise ValueError(f"pubkey must be 32 bytes, got {len(raw)}")
    return bytes(raw)

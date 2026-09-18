"""Send a transaction the dev has already signed, and send it again.

Measured on mainnet 2026-09-17: handed a single-signer transaction through
`signAndSendTransaction`, the wallet rewrote it (two ComputeBudget
instructions in front), sent it through its own node once, and returned a
signature the chain never saw. The coin sat with no split. Nothing this side
could resend it, because the signed bytes never came back.

So the page asks the wallet to sign only, and sends through here. This never
signs: it checks that every signature on the transaction is a real ed25519
signature by the key in that slot, that the transaction is one of this
door's (pump's program or its fee-sharing program is among the accounts),
and hands it to the node. Sending the same bytes twice is harmless -- the
signature is the identity -- so the page calls this every few seconds until
the chain has seen it.
"""

from __future__ import annotations

import base64

from . import ed25519, enroll
from .base58 import decode, encode
from .message import MAX_TRANSACTION_BYTES, SIGNATURE_BYTES

# A transaction is relayed only if it touches one of these. This is a door
# for the two transactions the door builds, not a public send endpoint.
OURS = (enroll.PUMP_PROGRAM, enroll.FEE_SHARE_PROGRAM)


class RelayError(ValueError):
    """The transaction is not one this door will send."""


def _compact_u16(raw: bytes, offset: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if offset >= len(raw) or shift > 14:
            raise RelayError("the transaction is cut short")
        byte = raw[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7


def with_signature(transaction: bytes, signature: bytes) -> bytes:
    """The fee payer's signature written into slot zero. For a wallet that
    answers `signTransaction` with the signature alone."""
    if len(signature) != SIGNATURE_BYTES:
        raise RelayError("a signature must be 64 bytes")
    count, start = _compact_u16(transaction, 0)
    if count < 1:
        raise RelayError("the transaction has no signature slot")
    return transaction[:start] + signature + transaction[start + SIGNATURE_BYTES:]


def assemble(built: bytes, signed) -> bytes:
    """The signed transaction, from whatever the wallet handed back.

    Wallets disagree about what `signTransaction` returns: the whole signed
    transaction or the signature alone, as base58, base64, or a list of
    bytes. None of that is trusted -- `check` decides -- so every reading is
    tried and the first that is truly signed wins. A 64-byte value is the
    fee payer's signature for the transaction this door built."""
    readings: list[bytes] = []
    if isinstance(signed, (list, tuple)):
        try:
            readings.append(bytes(signed))
        except (TypeError, ValueError):
            pass
    elif isinstance(signed, str) and signed:
        for read in (decode, lambda text: base64.b64decode(text, validate=True)):
            try:
                readings.append(read(signed))
            except Exception:  # noqa: BLE001 -- a wrong guess at the encoding
                pass
    last: RelayError = RelayError("the wallet returned nothing that reads as a signature or a transaction")
    for raw in readings:
        try:
            candidate = with_signature(built, raw) if len(raw) == SIGNATURE_BYTES else raw
            check(candidate)
            return candidate
        except RelayError as exc:
            last = exc
    raise last


def check(transaction: bytes) -> str:
    """The transaction's signature (its id), after proving it is fully and
    truly signed and is one of ours. Raises RelayError otherwise."""
    if not transaction or len(transaction) > MAX_TRANSACTION_BYTES:
        raise RelayError(f"a transaction is at most {MAX_TRANSACTION_BYTES} bytes")
    count, offset = _compact_u16(transaction, 0)
    if count < 1 or offset + SIGNATURE_BYTES * count >= len(transaction):
        raise RelayError("the transaction is cut short")
    signatures = [transaction[offset + SIGNATURE_BYTES * i: offset + SIGNATURE_BYTES * (i + 1)] for i in range(count)]
    message = transaction[offset + SIGNATURE_BYTES * count:]
    if message[0] & 0x80:
        raise RelayError("only legacy transactions are sent from here")
    if message[0] != count:
        raise RelayError("the signature count does not match the message")
    keys_count, at = _compact_u16(message, 3)
    if keys_count < count or at + 32 * keys_count > len(message):
        raise RelayError("the transaction is cut short")
    keys = [message[at + 32 * i: at + 32 * (i + 1)] for i in range(keys_count)]
    if not any(encode(key) in OURS for key in keys):
        raise RelayError("this is not a transaction this door built")
    for slot, (key, signature) in enumerate(zip(keys, signatures)):
        if signature == b"\x00" * SIGNATURE_BYTES:
            raise RelayError(f"signature {slot + 1} of {count} is missing")
        if not ed25519.verify(key, message, signature):
            # The wallet changed the message before signing, or signed
            # something else. Either way these bytes cannot land.
            raise RelayError(f"signature {slot + 1} of {count} does not match this transaction")
    return encode(signatures[0])


def send(rpc, transaction: bytes) -> str:
    """Check, then hand to the node. Preflight is skipped on purpose: the
    builder already simulated it, and a resend of a transaction that has
    landed must not come back as a preflight error."""
    signature = check(transaction)
    rpc.call("sendTransaction", [base64.b64encode(transaction).decode(), {
        "encoding": "base64", "skipPreflight": True, "maxRetries": 3,
    }])
    return signature

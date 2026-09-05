"""Compiling instructions into a legacy Solana transaction message.

`enroll.message` builds one instruction by hand. The buyback transaction
carries eight, so the compilation step -- merging account metas, ordering
them the way the runtime requires, writing the header counts -- lives here
once, and `enroll` stays as it was.

Legacy rather than v0 for the same reason `enroll` gives: no address lookup
table is needed, every wallet accepts it, and the format is small enough to
be written and checked here rather than trusted from a dependency.

Account ordering is not free choice. The runtime requires writable signers,
then readonly signers, then writable non-signers, then readonly non-signers,
and the header counts must agree with that grouping. The fee payer must be
the first account. Within a group, accounts sort by address -- the same
tiebreak `enroll.message` uses, so the two produce byte-identical output for
the same instruction (pinned by `tests/test_message.py`).
"""

from __future__ import annotations

from .base58 import pubkey_bytes

# An account meta: (address, is_signer, is_writable).
Meta = tuple[str, bool, bool]
# An instruction: (program_id, metas in the order the program reads them, data).
Instruction = tuple[str, list[Meta], bytes]

# Solana's packet limit. A message that serialises past this (plus its
# signatures) is refused by every RPC, so it is refused here first.
MAX_TRANSACTION_BYTES = 1232
SIGNATURE_BYTES = 64


class MessageError(ValueError):
    """A message that cannot be sent as built."""


def compact_u16(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def compile_legacy(payer: str, instructions: list[Instruction], recent_blockhash: str) -> bytes:
    """A serialised legacy message: header, account keys, blockhash,
    compiled instructions. Unsigned -- signing is the caller's business.
    """
    if not instructions:
        raise MessageError("a message needs at least one instruction")

    merged: dict[str, list[bool]] = {}

    def note(address: str, signer: bool, writable: bool) -> None:
        row = merged.setdefault(address, [False, False])
        row[0] = row[0] or signer
        row[1] = row[1] or writable

    note(payer, True, True)
    for program, metas, _data in instructions:
        note(program, False, False)
        for address, signer, writable in metas:
            note(address, signer, writable)

    for program, _metas, _data in instructions:
        if merged[program][0] or merged[program][1]:
            raise MessageError(f"program {program} is also used as a signer or writable account")

    def rank(item):
        address, (signer, writable) = item
        if address == payer:
            return (0, address)
        return (1 if signer and writable else 2 if signer else 3 if writable else 4, address)

    ordered = [address for address, _flags in sorted(merged.items(), key=rank)]
    index = {address: i for i, address in enumerate(ordered)}
    signers = [a for a in ordered if merged[a][0]]
    readonly_signed = sum(1 for a in signers if not merged[a][1])
    readonly_unsigned = sum(1 for a in ordered if not merged[a][0] and not merged[a][1])

    out = bytearray()
    out.append(len(signers))
    out.append(readonly_signed)
    out.append(readonly_unsigned)
    out += compact_u16(len(ordered))
    for address in ordered:
        out += pubkey_bytes(address)
    out += pubkey_bytes(recent_blockhash)
    out += compact_u16(len(instructions))
    for program, metas, data in instructions:
        out.append(index[program])
        out += compact_u16(len(metas))
        for address, _signer, _writable in metas:
            out.append(index[address])
        out += compact_u16(len(data))
        out += data

    total = len(compact_u16(len(signers))) + SIGNATURE_BYTES * len(signers) + len(out)
    if total > MAX_TRANSACTION_BYTES:
        raise MessageError(
            f"the transaction would be {total} bytes, above Solana's {MAX_TRANSACTION_BYTES} "
            f"byte limit ({len(ordered)} accounts, {len(instructions)} instructions)"
        )
    return bytes(out)


def signer_count(message: bytes) -> int:
    return message[0]


def unsigned_transaction(message: bytes) -> bytes:
    """The wire form with every signature zeroed -- what `simulateTransaction`
    with `sigVerify: false` accepts.
    """
    n = signer_count(message)
    return compact_u16(n) + b"\x00" * (SIGNATURE_BYTES * n) + message


def signed_transaction(message: bytes, signatures: list[bytes]) -> bytes:
    n = signer_count(message)
    if len(signatures) != n:
        raise MessageError(f"the message needs {n} signature(s), {len(signatures)} given")
    for signature in signatures:
        if len(signature) != SIGNATURE_BYTES:
            raise MessageError("a signature must be 64 bytes")
    return compact_u16(n) + b"".join(signatures) + message

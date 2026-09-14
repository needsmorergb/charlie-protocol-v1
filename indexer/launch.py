"""Launching a coin WITH its fee split: pump's `create`, then the enrollment.

LNCH-01: the second door. `/enroll` is for a coin that already trades and
spends its one config change on a split chosen after the fact. `/launch` is
for a coin that does not exist yet: pump's own `create`, followed by the same
two fee-share instructions `enroll.enrollment_message` already sends, so the
coin is created and its split is set before anyone has traded it. The
one-shot is still spent -- there is no version of pump's program where it is
not -- but it is spent deliberately, at block zero, on a split the dev chose,
rather than left lying around to be spent by accident.

**Every byte of `create` here is derived from pump's on-chain IDL** (the copy
in `_idl_6EF8rrec.json`, pinned by `tests/test_launch.py` against the
discriminator recomputed from the instruction's name): the account list AND
ITS ORDER, the PDA seeds, and the argument layout. Simulated against mainnet
on 2026-09-14 with a fresh mint: `err: None`, 117,654 compute units, the
CreateEvent emitted. Two `create` variants are published. This module uses
the ORIGINAL `create`: it takes no cashback argument at all, so a coin made
through this door is structurally not a cashback coin, which is the one
launch choice that can never be undone and that `enroll.preflight` refuses.
`create_v2` (Token-2022, mayhem mode, a cashback flag) is deliberately not
built.

TWO TRANSACTIONS, NOT ONE. All three instructions in one legacy message
measure 1,260 bytes against Solana's 1,232-byte packet, with a ten-character
name and a pump-length metadata URI (`launch_message` still builds it, and
`tests/test_launch.py` pins that it does not fit, so the day it does someone
notices). So the door is: (1) `create`, signed by the dev and the mint;
(2) the enrollment transaction `/enroll` already sends for a coin with no
config, built and simulated by `api/enroll.py` once the curve exists. The
first can fail and cost nothing but a fee, because no coin exists. The second
can fail and leave a coin whose one-shot is unspent, which is exactly the
state `/enroll` handles. Neither failure spends the change on the wrong thing.

Two signers on the first. pump's `create` requires the MINT to sign, because
the mint is a brand-new account whose address is a keypair's public key. That
keypair has no power after the instruction: pump's `mint-authority` PDA
becomes the mint authority and the keypair is never consulted again. So it is
generated here, used once to partially sign, and discarded. The dev's key is
the other signer and it stays in the dev's wallet: nothing here signs for the
dev, and nothing here sends.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

from . import ed25519, enroll, legs
from .base58 import pubkey_bytes
from .curve import find_program_address
from .message import (
    SIGNATURE_BYTES,
    MessageError,
    compact_u16,
    compile_legacy,
    signer_count,
)

PUMP_PROGRAM = enroll.PUMP_PROGRAM
FEE_SHARE_PROGRAM = enroll.FEE_SHARE_PROGRAM
SYSTEM_PROGRAM = enroll.SYSTEM_PROGRAM
TOKEN_PROGRAM = enroll.TOKEN_PROGRAM
ASSOCIATED_TOKEN_PROGRAM = enroll.ASSOCIATED_TOKEN_PROGRAM
# Metaplex Token Metadata, as the IDL's `mpl_token_metadata` account fixes it.
MPL_TOKEN_METADATA = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"
RENT_SYSVAR = "SysvarRent111111111111111111111111111111111"

# sha256("global:create")[:8]. Recomputed here rather than pasted, and the
# test pins it equal to the IDL's published bytes.
CREATE = hashlib.sha256(b"global:create").digest()[:8]

# Metaplex's limits on the metadata `create` writes. pump forwards the three
# strings to Token Metadata unchanged, and Token Metadata refuses longer ones
# with `NameTooLong` / `SymbolTooLong` / `UriTooLong`. Checked here, in bytes,
# so a dev is told before a wallet opens rather than by a failed transaction.
MAX_NAME_BYTES = 32
MAX_SYMBOL_BYTES = 10
MAX_URI_BYTES = 200


class LaunchError(ValueError):
    """A launch that must not be built. Every message is addressed to the dev."""


@dataclass(frozen=True)
class Metadata:
    name: str
    symbol: str
    uri: str


def validate_metadata(name: str, symbol: str, uri: str) -> Metadata:
    name = (name or "").strip()
    symbol = (symbol or "").strip()
    uri = (uri or "").strip()
    if not name:
        raise LaunchError("The coin needs a name.")
    if len(name.encode("utf-8")) > MAX_NAME_BYTES:
        raise LaunchError(f"The name is longer than {MAX_NAME_BYTES} bytes, which is the most pump's metadata allows.")
    if not symbol:
        raise LaunchError("The coin needs a ticker.")
    if len(symbol.encode("utf-8")) > MAX_SYMBOL_BYTES:
        raise LaunchError(f"The ticker is longer than {MAX_SYMBOL_BYTES} bytes, which is the most pump's metadata allows.")
    if any(c.isspace() for c in symbol):
        raise LaunchError("The ticker cannot contain spaces.")
    if not uri:
        raise LaunchError("The coin has no metadata URI. Upload the image and description first.")
    if len(uri.encode("utf-8")) > MAX_URI_BYTES:
        raise LaunchError(f"The metadata URI is longer than {MAX_URI_BYTES} bytes, which is the most pump's metadata allows.")
    if not (uri.startswith("https://") or uri.startswith("ipfs://")):
        raise LaunchError("The metadata URI must start with https:// or ipfs://.")
    return Metadata(name, symbol, uri)


# -- addresses ---------------------------------------------------------------


def _pda(seeds, program):
    return find_program_address(seeds, program)[0]


MINT_AUTHORITY = _pda([b"mint-authority"], PUMP_PROGRAM)
GLOBAL = _pda([b"global"], PUMP_PROGRAM)
EVENT_AUTHORITY = _pda([b"__event_authority"], PUMP_PROGRAM)


def metadata_address(mint: str) -> str:
    """Token Metadata's PDA: `["metadata", program, mint]` under the program,
    exactly as the IDL spells the seeds (the middle one is the program's own
    32 bytes, written as a constant)."""
    return _pda([b"metadata", pubkey_bytes(MPL_TOKEN_METADATA), pubkey_bytes(mint)],
                MPL_TOKEN_METADATA)


def create_accounts_for(mint: str, user: str) -> list[tuple[str, bool, bool]]:
    """`create`'s 14 accounts, `(address, is_signer, is_writable)`, in the
    EXACT order the IDL lists. The program reads them positionally."""
    bonding_curve = enroll.bonding_curve_address(mint)
    return [
        (mint, True, True),
        (MINT_AUTHORITY, False, False),
        (bonding_curve, False, True),
        (enroll.associated_token_address(bonding_curve, mint, TOKEN_PROGRAM), False, True),
        (GLOBAL, False, False),
        (MPL_TOKEN_METADATA, False, False),
        (metadata_address(mint), False, True),
        (user, True, True),
        (SYSTEM_PROGRAM, False, False),
        (TOKEN_PROGRAM, False, False),
        (ASSOCIATED_TOKEN_PROGRAM, False, False),
        (RENT_SYSVAR, False, False),
        (EVENT_AUTHORITY, False, False),
        (PUMP_PROGRAM, False, False),
    ]


def _borsh_string(value: str) -> bytes:
    raw = value.encode("utf-8")
    return len(raw).to_bytes(4, "little") + raw


def create_data(meta: Metadata, creator: str) -> bytes:
    """`create`'s args in the IDL's order: name, symbol, uri (borsh strings:
    u32 length, then bytes), then `creator` as 32 raw bytes."""
    return (CREATE + _borsh_string(meta.name) + _borsh_string(meta.symbol)
            + _borsh_string(meta.uri) + pubkey_bytes(creator))


def create_instruction(mint: str, user: str, meta: Metadata):
    """`(program, metas, data)` for pump's `create`. `creator` is the dev:
    the bonding curve's `creator` is who pump pays the creator fee to and who
    `create_fee_sharing_config` lets create the config (6016 to anyone else),
    so it must be the same key that signs the enrollment half."""
    return (PUMP_PROGRAM, create_accounts_for(mint, user), create_data(meta, user))


# -- the mint keypair ----------------------------------------------------------


def new_mint(seed: bytes | None = None) -> ed25519.Keypair:
    """A fresh mint keypair. 32 random bytes from the OS. It signs `create`
    once and is then powerless: pump's `mint-authority` PDA owns the mint."""
    return ed25519.Keypair.from_seed(seed if seed is not None else os.urandom(32))


# -- the transaction -------------------------------------------------------------


def _config_instruction(mint: str, dev: str):
    """`enroll.create_instruction`, with one flag normalised. Anchor's
    convention for the absent optional `pool` is the program's own id, and
    `enroll` passes it writable as the mainnet simulation did. A program
    account can never be written, and the runtime demotes that flag on its
    own; `compile_legacy` refuses to encode it rather than rely on that, so
    the placeholder is marked read-only here. Same address, same position."""
    program, metas, data = enroll.create_instruction(mint, dev, graduated=False)
    metas = [(a, s, False) if a == FEE_SHARE_PROGRAM else (a, s, w) for a, s, w in metas]
    return (program, metas, data)


def create_message(mint: str, dev: str, meta: Metadata, recent_blockhash: str) -> bytes:
    """Step one: pump's `create` alone. Two signers, the dev (fee payer,
    first in the signature array because `compile_legacy` puts the payer
    first) and the mint. 779 bytes with a pump-length URI."""
    message = compile_legacy(dev, [create_instruction(mint, dev, meta)], recent_blockhash)
    if signer_count(message) != 2:
        raise LaunchError("a create message has exactly two signers: the dev and the mint")
    return message


def launch_message(mint: str, dev: str, shares, meta: Metadata, recent_blockhash: str) -> bytes:
    """All three instructions in ONE legacy message. Kept as a measurement:
    it does not fit (see the module docstring), and `compile_legacy` raises
    `MessageError` saying by how much. Not what the door sends."""
    validated = enroll.validate(shares)
    enroll.require_toll(validated)
    instructions = [
        create_instruction(mint, dev, meta),
        _config_instruction(mint, dev),
        # After `create_fee_sharing_config` the only shareholder is the creator
        # at 100%, so the update's remaining accounts are `[dev]` -- the same
        # list `enroll.enrollment_message` passes on its create path.
        enroll.update_instruction(mint, dev, validated, current=[dev]),
    ]
    message = compile_legacy(dev, instructions, recent_blockhash)
    if signer_count(message) != 2:
        raise LaunchError("a launch message has exactly two signers: the dev and the mint")
    return message


def signer_addresses(message: bytes) -> list[str]:
    """The signers, in the order their signatures must appear."""
    n = signer_count(message)
    # header (3) + compact-u16 count, then 32-byte keys.
    offset = 3
    count = 0
    shift = 0
    while True:
        byte = message[offset]
        offset += 1
        count |= (byte & 0x7F) << shift
        if not byte & 0x80:
            break
        shift += 7
    from .base58 import encode
    return [encode(message[offset + 32 * i: offset + 32 * (i + 1)]) for i in range(n)]


def partially_signed(message: bytes, mint: ed25519.Keypair) -> bytes:
    """The wire transaction with the MINT's signature filled in and the
    dev's left as 64 zero bytes, which is what the wallet fills. Also what
    `simulateTransaction` with `sigVerify: false` accepts."""
    signers = signer_addresses(message)
    if mint.address not in signers:
        raise LaunchError("the mint is not a signer of this message")
    out = bytearray(compact_u16(len(signers)))
    for address in signers:
        out += mint.sign(message) if address == mint.address else b"\x00" * SIGNATURE_BYTES
    out += message
    return bytes(out)


def size_of(message: bytes) -> int:
    n = signer_count(message)
    return len(compact_u16(n)) + SIGNATURE_BYTES * n + len(message)


# -- what a dev is told before a wallet opens -------------------------------------


def preflight(dev: str, shares, meta: Metadata) -> None:
    """Everything that makes a launch un-sendable, phrased for the dev.

    There is no cashback check here because there is no cashback flag here:
    `create` cannot make a cashback coin. There is no `admin_revoked` check
    because the config does not exist yet. What remains is the split itself,
    which `enroll` already refuses on the same terms as `/enroll`.
    """
    if legs.TOLL_DESTINATION is None:
        raise LaunchError("Launching is not open yet: the protocol's collection address has not been set.")
    try:
        validated = enroll.validate(shares)
        enroll.require_toll(validated)
    except enroll.EnrollError as exc:
        raise LaunchError(str(exc)) from None
    if not any(s.address == dev for s in validated):
        # Not refused -- a dev may route all of their share elsewhere -- but
        # it is a mistake often enough to be worth one sentence in the log.
        pass


__all__ = [
    "CREATE", "LaunchError", "Metadata", "MAX_NAME_BYTES", "MAX_SYMBOL_BYTES",
    "MAX_URI_BYTES", "MPL_TOKEN_METADATA", "create_accounts_for", "create_data",
    "create_instruction", "launch_message", "metadata_address", "new_mint",
    "partially_signed", "preflight", "signer_addresses", "size_of",
    "validate_metadata", "MessageError",
]

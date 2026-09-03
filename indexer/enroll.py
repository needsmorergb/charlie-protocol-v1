"""Building the transaction that sets a coin's fee split.

ENRL-01/ENRL-02: a dev proves they own a coin by holding the key its sharing
config names as `admin`, and sets where the creator fee goes. No program of
ours is deployed, and none is needed for this: pump's fee-share program
already owns the split, and its `update_fee_shares_v2` instruction is the one
the pump UI itself calls.

**Every byte here is derived from the program's own on-chain IDL**, read from
the Anchor IDL account at `createWithSeed(base, "anchor:idl", program)` rather
than from documentation or a guess: the instruction discriminator, the account
list AND ITS ORDER, the PDA seeds, and the `Shareholder` layout. The IDL also
confirms two things this project had already established empirically, which is
why it is trusted here -- the `SharingConfig` discriminator matches
`pump.DISC_SHARING_CONFIG`, and `Shareholder` is a 32-byte pubkey plus a u16,
matching `pump.SHAREHOLDER_RECORD_BYTES`.

Nothing in this module signs, and nothing sends. It returns an unsigned
message for a wallet to sign, because the authority is the dev's key and must
never be anywhere but the dev's wallet.
"""

from __future__ import annotations

from dataclasses import dataclass

from .base58 import decode, encode, pubkey_bytes
from .curve import find_program_address

FEE_SHARE_PROGRAM = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ASSOCIATED_TOKEN_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
WSOL_MINT = "So11111111111111111111111111111111111111112"

# sha256("global:update_fee_shares_v2")[:8], as published in the program's IDL.
UPDATE_FEE_SHARES_V2 = bytes.fromhex("6ffb31064e4e6a12")

# pump caps a config at this many shareholders; `pump.MAX_SHAREHOLDERS` is the
# same bound applied when READING one. Rejecting here means a dev is told the
# split is too long before a wallet ever opens, rather than by a failed
# transaction they paid for.
MAX_SHAREHOLDERS = 8
TOTAL_BPS = 10_000


class EnrollError(ValueError):
    """A split that must not be sent. Every message is addressed to the dev."""


@dataclass(frozen=True)
class Share:
    address: str
    bps: int


def _pda(seeds, program):
    return find_program_address(seeds, program)[0]


def sharing_config_address(mint: str) -> str:
    return _pda([b"sharing-config", pubkey_bytes(mint)], FEE_SHARE_PROGRAM)


def bonding_curve_address(mint: str) -> str:
    return _pda([b"bonding-curve", pubkey_bytes(mint)], PUMP_PROGRAM)


def associated_token_address(owner: str, mint: str, token_program: str = TOKEN_PROGRAM) -> str:
    """The standard ATA derivation. The IDL spells the seeds out as
    `[owner, token_program, quote_mint]` under the associated-token program,
    which is exactly this.
    """
    return _pda(
        [pubkey_bytes(owner), pubkey_bytes(token_program), pubkey_bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM,
    )


def accounts_for(mint: str, authority: str, *, quote_mint: str = WSOL_MINT,
                 token_program: str = TOKEN_PROGRAM) -> list[tuple[str, bool, bool]]:
    """`(address, is_signer, is_writable)` in the EXACT order the IDL lists.

    Order is not cosmetic: the program reads its accounts positionally, so a
    list that is right in content and wrong in order is a transaction that
    fails at best and touches the wrong account at worst. Nothing here is
    reordered for readability.
    """
    config = sharing_config_address(mint)
    creator_vault = _pda([b"creator-vault", pubkey_bytes(config)], PUMP_PROGRAM)
    creator_vault_authority = _pda([b"creator_vault", pubkey_bytes(config)], PUMP_AMM_PROGRAM)
    return [
        (_pda([b"__event_authority"], FEE_SHARE_PROGRAM), False, False),
        (FEE_SHARE_PROGRAM, False, False),
        (authority, True, True),
        # Derived under PUMP's program, not the fee-share program. The IDL
        # states it explicitly (`pda.program`), and assuming the enclosing
        # program instead produced an address that does not exist -- the
        # simulation failed with AccountNotInitialized naming this account.
        (_pda([b"global"], PUMP_PROGRAM), False, False),
        (mint, False, False),
        (config, False, True),
        (bonding_curve_address(mint), False, False),
        (creator_vault, False, True),
        (associated_token_address(creator_vault, quote_mint, token_program), False, True),
        (SYSTEM_PROGRAM, False, False),
        (PUMP_PROGRAM, False, False),
        (_pda([b"__event_authority"], PUMP_PROGRAM), False, False),
        (PUMP_AMM_PROGRAM, False, False),
        (_pda([b"__event_authority"], PUMP_AMM_PROGRAM), False, False),
        (quote_mint, False, False),
        (token_program, False, False),
        (ASSOCIATED_TOKEN_PROGRAM, False, False),
        (creator_vault_authority, False, True),
        (associated_token_address(creator_vault_authority, quote_mint, token_program), False, True),
    ]


def validate(shares) -> tuple[Share, ...]:
    """Every rule the program enforces, checked before a wallet opens.

    A rejected split costs the dev nothing. A split that fails on chain costs
    them a fee and leaves them guessing which rule they broke, so each message
    below names the rule and the number that broke it.
    """
    rows = tuple(shares)
    if not rows:
        raise EnrollError("A split needs at least one destination.")
    if len(rows) > MAX_SHAREHOLDERS:
        raise EnrollError(
            f"{len(rows)} destinations, but a pump fee split holds at most "
            f"{MAX_SHAREHOLDERS}."
        )
    seen = set()
    for row in rows:
        if row.bps <= 0:
            raise EnrollError(
                f"{row.address} is set to {row.bps} bps. Every destination must "
                "receive more than zero; remove it instead."
            )
        if row.bps > TOTAL_BPS:
            raise EnrollError(f"{row.address} is set to {row.bps} bps, above 10000.")
        try:
            raw = decode(row.address)
        except Exception:
            raise EnrollError(f"{row.address} is not a valid Solana address.") from None
        if len(raw) != 32 or encode(raw) != row.address:
            raise EnrollError(f"{row.address} is not a valid Solana address.")
        if row.address in seen:
            raise EnrollError(
                f"{row.address} appears twice. Combine them into one share."
            )
        seen.add(row.address)
    total = sum(r.bps for r in rows)
    if total != TOTAL_BPS:
        short = TOTAL_BPS - total
        direction = f"{short} bps short" if short > 0 else f"{-short} bps over"
        raise EnrollError(f"The split totals {total} bps. It must be exactly 10000 ({direction}).")
    return rows


def instruction_data(shares) -> bytes:
    """`discriminator || u32 length || (pubkey, u16) * n`, Anchor's encoding
    for `Vec<Shareholder>`.
    """
    rows = validate(shares)
    out = bytearray(UPDATE_FEE_SHARES_V2)
    out += len(rows).to_bytes(4, "little")
    for row in rows:
        out += pubkey_bytes(row.address)
        out += row.bps.to_bytes(2, "little")
    return bytes(out)


def _compact_u16(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def message(mint: str, authority: str, shares, recent_blockhash: str, *,
            quote_mint: str = WSOL_MINT, token_program: str = TOKEN_PROGRAM,
            current=()) -> bytes:
    """A serialised legacy transaction message, unsigned.

    `current` is the addresses the config pays TODAY. They are appended as
    Anchor remaining accounts; see the note at the append for how that was
    established, since no IDL describes remaining accounts.

    Legacy rather than v0: it needs no address lookup table, every wallet
    accepts it, and the format is small enough to be written and checked here
    rather than trusted from a dependency.

    Account ordering in a message is not free choice -- the runtime requires
    writable signers, then readonly signers, then writable non-signers, then
    readonly non-signers, and the header counts must agree with that grouping.
    The fee payer is the authority and must sort first, which it does: it is
    the only signer.
    """
    data = instruction_data(shares)
    metas = accounts_for(mint, authority, quote_mint=quote_mint, token_program=token_program)
    # Anchor `remaining_accounts`, which no IDL records: the CURRENT
    # shareholders, writable. Determined by simulation, not by guessing --
    # with none the program answers NotEnoughRemainingAccounts, with the NEW
    # shareholders it answers 6020, and with the current ones it succeeds.
    # The program settles what each existing shareholder is already owed
    # before the split changes underneath them, so it needs to touch them.
    metas = metas + [(address, False, True) for address in current]

    # Collapse duplicates, keeping the strongest privilege each appearance
    # asks for. An account named twice with different flags must appear once
    # with the union, or the runtime sees a weaker permission than the
    # instruction needs.
    merged: dict[str, list[bool]] = {}
    for address, signer, writable in metas:
        row = merged.setdefault(address, [False, False])
        row[0] = row[0] or signer
        row[1] = row[1] or writable

    def rank(item):
        address, (signer, writable) = item
        return (0 if signer and writable else 1 if signer else 2 if writable else 3, address)

    ordered = [addr for addr, _flags in sorted(merged.items(), key=rank)]
    if ordered[0] != authority:
        raise EnrollError("the fee payer must sort first among accounts")

    index = {addr: i for i, addr in enumerate(ordered)}
    signers = [a for a in ordered if merged[a][0]]
    readonly_signed = sum(1 for a in signers if not merged[a][1])
    readonly_unsigned = sum(1 for a in ordered if not merged[a][0] and not merged[a][1])

    out = bytearray()
    out.append(len(signers))
    out.append(readonly_signed)
    out.append(readonly_unsigned)
    out += _compact_u16(len(ordered))
    for address in ordered:
        out += pubkey_bytes(address)
    out += pubkey_bytes(recent_blockhash)
    out += _compact_u16(1)                      # one instruction
    out.append(index[FEE_SHARE_PROGRAM])
    out += _compact_u16(len(metas))
    for address, _signer, _writable in metas:
        out.append(index[address])
    out += _compact_u16(len(data))
    out += data
    return bytes(out)


def owns(config, authority: str) -> bool:
    """Ownership is holding the key the config names as `admin`. Not the
    creator wallet, not whoever deployed the coin: `update_fee_shares_v2`
    checks the admin signature and nothing else, so anything else this page
    called "ownership" would be a claim the chain does not make.
    """
    return bool(config) and getattr(config, "admin", None) == authority


def preflight(config, authority: str, shares) -> None:
    """Everything that makes a split un-sendable, checked before a wallet
    opens and phrased for the dev rather than for us.

    `FeeSharesAlreadyUpdated` is the one that matters most: **pump allows the
    split to be changed exactly once.** A dev who spends it by accident cannot
    undo it, so this is stated before they sign rather than discovered from a
    failed transaction.
    """
    if config is None:
        raise EnrollError(
            "This coin has no pump fee-sharing config, so there is no split to "
            "set. Its creator fee goes to one ordinary wallet."
        )
    if getattr(config, "admin_revoked", False):
        raise EnrollError(
            "This coin's config is admin_revoked: its split is permanent and "
            "only pump could reset it. Nothing can change it, including this."
        )
    if not owns(config, authority):
        raise EnrollError(
            f"The connected wallet is not this coin's admin. Its config names "
            f"{config.admin} and only that key can set the split."
        )
    validate(shares)

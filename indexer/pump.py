"""Reading pump's on-chain state: bonding curve, sharing config, SPL mint.

Every decode here checks the account's **owner** and its Anchor
**discriminator** before trusting a single byte. An address existing is not
evidence of anything -- a PDA derived for one program is an ordinary address
that anyone may fund, and mainnet has such strays. Decoding one of those as a
bonding curve reads a "creator" pubkey out of unrelated bytes, and the indexer
would then report a split for a config that does not exist.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

from .base58 import encode, pubkey_bytes
from .curve import find_program_address

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
# Fee sharing is its own program. A SharingConfig is one of ITS PDAs, not
# pump's, and it is named by bonding_curve.creator rather than derived.
PUMP_FEE_SHARE_PROGRAM = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
SYSTEM_PROGRAM = "11111111111111111111111111111111"

DISC_BONDING_CURVE = bytes.fromhex("17b7f83760d8ac60")
DISC_SHARING_CONFIG = bytes.fromhex("d84a0900388c5d4b")
# Computed, not pasted: a literal here would be a second copy of the
# discriminator that could silently drift from DISC_SHARING_CONFIG. This is
# the exact `bytes` value getProgramAccounts' memcmp filter needs.
SHARING_CONFIG_DISCRIMINATOR_B58 = encode(DISC_SHARING_CONFIG)

MAX_SHAREHOLDERS = 64

# Measured live against mainnet (03-RESEARCH.md): every SharingConfig
# account has `space: 1024`, fixed, regardless of shareholder count -- single-
# and multi-shareholder alike. That is why a full-data enumeration sweep is
# not affordable (it would be a ~965 MB response at today's scale) and why
# `coverage.sweep` requests a `dataSlice` instead.
SHARING_CONFIG_ACCOUNT_BYTES = 1024
# disc 8 | bump 1 | version 1 | status 1 | mint 32 | admin 32 |
# admin_revoked 1 | shareholder vec_len u32 = 80 bytes before the first
# shareholder record.
SHARING_CONFIG_HEADER_BYTES = 80
# pubkey 32 | bps u16 = 34 bytes per shareholder.
SHAREHOLDER_RECORD_BYTES = 34
# The complete account for the 97.0% of configs that have exactly one
# shareholder -- header plus one shareholder record. A deliberate truncation
# for every config with more than one.
SINGLE_SHAREHOLDER_SLICE = SHARING_CONFIG_HEADER_BYTES + SHAREHOLDER_RECORD_BYTES


class DecodeError(ValueError):
    """The chain does not hold what we expected to find at this address."""


class TruncatedConfig(DecodeError):
    """A partial `dataSlice` read truncated a real multi-shareholder config --
    not a layout violation.

    Raised by `decode_sharing_config` when, and only when, the declared
    shareholder count does not fit the bytes in hand AND the account's own
    `space` field says the real account is larger than the bytes in hand.
    That second condition is what tells "we asked for 114 bytes of a 1024
    byte account" apart from "pump changed the layout" -- the two must not
    raise the same exception, or a truncated read could be silently recorded
    as a coin with fewer shareholders than it actually has.

    Carries the address, the mint and the declared count so a caller (the
    enumeration sweep) can queue the account for a full fetch without
    re-decoding it.
    """

    def __init__(self, address: str, mint: str, declared_count: int):
        super().__init__(
            f"{address}: sharing config declares {declared_count} shareholders -- "
            "truncated by a partial dataSlice read, not a layout violation"
        )
        self.address = address
        self.mint = mint
        self.declared_count = declared_count


def bonding_curve(mint: str) -> str:
    return find_program_address([b"bonding-curve", pubkey_bytes(mint)], PUMP_PROGRAM)[0]


def _raw(account: dict | None, discriminator: bytes, label: str, owners: tuple[str, ...]) -> bytes:
    if not account:
        raise DecodeError(f"no {label} account exists")
    owner = account.get("owner")
    if owner not in owners:
        raise DecodeError(
            f"the {label} address exists but is owned by {owner}, "
            f"not {' or '.join(owners)} -- it is not a {label}"
        )
    data = base64.b64decode(account["data"][0])
    if discriminator and data[:8] != discriminator:
        raise DecodeError(f"the {label} account holds a different account type")
    return data


# -- bonding curve --------------------------------------------------------
@dataclass(frozen=True)
class BondingCurve:
    """Only the two fields the protocol needs.

    disc 8 | virtual/real reserves + total supply 5x u64 = 40 | complete 1 |
    creator 32
    """

    mint: str
    graduated: bool
    creator: str          # a wallet, OR the SharingConfig address for a fee-shared coin


def read_bonding_curve(rpc, mint: str) -> BondingCurve:
    account = rpc.accounts([bonding_curve(mint)])[0]
    try:
        data = _raw(account, DISC_BONDING_CURVE, "bonding curve", (PUMP_PROGRAM,))
    except DecodeError as exc:
        raise DecodeError(f"{mint}: {exc}") from None
    return BondingCurve(mint=mint, graduated=bool(data[48]), creator=encode(data[49:81]))


# -- sharing config -------------------------------------------------------
@dataclass(frozen=True)
class SharingConfig:
    """pump's fee split, exactly as the chain states it.

    disc 8 | bump 1 | version 1 | status 1 | mint 32 | admin 32 |
    admin_revoked 1 | vec<(pubkey 32, bps u16)> len u32 then 34 bytes each
    """

    address: str
    mint: str                     # as recorded IN the config -- not the mint we asked about
    version: int
    status: int
    admin: str
    admin_revoked: bool
    shareholders: tuple[tuple[str, int], ...] = ()

    @property
    def total_bps(self) -> int:
        return sum(bps for _who, bps in self.shareholders)

    def share_of(self, address: str) -> int:
        return sum(bps for who, bps in self.shareholders if who == address)


def decode_sharing_config(address: str, account: dict | None) -> SharingConfig:
    """The one sharing-config byte decoder (RESEARCH.md Pattern 2).

    Both `read_sharing_config`'s RPC-fetching wrapper and `coverage.sweep`'s
    enumeration path route every account through this function, over the
    same field offsets, so a pump layout change fails a decode instead of
    publishing a wrong number.

    `account` is the `{owner, data, ...}` shape both `getMultipleAccounts`
    and `getProgramAccounts` nest a result under -- the same dict `_raw()`
    already expects. Routes through `_raw()`'s owner-and-discriminator guard
    first: a memcmp filter match is a byte comparison at an offset, never
    proof the account is a valid `SharingConfig`.

    Raises `TruncatedConfig`, not the ordinary `DecodeError`, when the
    declared shareholder count does not fit the bytes in hand AND the
    account's own `space` field says the real account is larger than the
    bytes in hand -- see `TruncatedConfig`'s docstring. When `space` is
    absent or equal to the data length, raises the existing `DecodeError`
    exactly as before this function existed.
    """
    data = _raw(account, DISC_SHARING_CONFIG, "sharing config", (PUMP_FEE_SHARE_PROGRAM,))

    mint = encode(data[11:43])
    count = int.from_bytes(data[76:80], "little")
    cursor = SHARING_CONFIG_HEADER_BYTES
    space = account.get("space") if account else None
    truncated_by_slice = space is not None and space > len(data)
    if count > MAX_SHAREHOLDERS or cursor + count * SHAREHOLDER_RECORD_BYTES > len(data):
        if truncated_by_slice:
            raise TruncatedConfig(address, mint, count)
        raise DecodeError(
            f"{address}: sharing config declares {count} shareholders, "
            "which its data cannot hold"
        )
    holders = []
    for _ in range(count):
        holders.append(
            (
                encode(data[cursor : cursor + 32]),
                int.from_bytes(data[cursor + 32 : cursor + 34], "little"),
            )
        )
        cursor += SHAREHOLDER_RECORD_BYTES

    return SharingConfig(
        address=address,
        mint=mint,
        version=data[9],
        status=data[10],
        admin=encode(data[43:75]),
        admin_revoked=bool(data[75]),
        shareholders=tuple(holders),
    )


def read_sharing_config(rpc, curve: BondingCurve) -> SharingConfig:
    """Resolve the config named by the bonding curve.

    Not derivable: `bonding_curve.creator` names it. For a fee-shared coin that
    address is a SharingConfig owned by the fee-share program; for an ordinary
    coin it is just the creator's wallet, and there is no split to read.

    Keeps the bonding-curve lookup and the "ordinary creator address" error
    verbatim, then delegates the byte decode entirely -- there is exactly
    one decoder.
    """
    account = rpc.accounts([curve.creator])[0]
    if not account or account.get("owner") != PUMP_FEE_SHARE_PROGRAM:
        raise DecodeError(
            f"{curve.mint}: its creator {curve.creator} is not a fee-sharing config "
            "(it is an ordinary creator address). There is no split to report."
        )
    return decode_sharing_config(curve.creator, account)


# -- SPL mint -------------------------------------------------------------
@dataclass(frozen=True)
class MintState:
    """The BURN leg's evidence lives here.

    mint_authority COption 4+32 | supply u64 | decimals 1 | initialized 1 |
    freeze_authority COption 4+32
    """

    mint: str
    supply: int
    decimals: int
    mint_authority: str | None
    freeze_authority: str | None
    program: str

    @property
    def ui_supply(self) -> float:
        return self.supply / (10**self.decimals)


def read_mint(rpc, mint: str) -> MintState:
    account = rpc.accounts([mint])[0]
    data = _raw(account, b"", "mint", (TOKEN_PROGRAM, TOKEN_2022_PROGRAM))
    if len(data) < 82:
        raise DecodeError(f"{mint}: mint account is {len(data)} bytes, expected at least 82")
    has_mint_authority = int.from_bytes(data[0:4], "little") == 1
    has_freeze_authority = int.from_bytes(data[46:50], "little") == 1
    return MintState(
        mint=mint,
        supply=int.from_bytes(data[36:44], "little"),
        decimals=data[44],
        mint_authority=encode(data[4:36]) if has_mint_authority else None,
        freeze_authority=encode(data[50:82]) if has_freeze_authority else None,
        program=account.get("owner"),
    )

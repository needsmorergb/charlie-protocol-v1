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

MAX_SHAREHOLDERS = 64


class DecodeError(ValueError):
    """The chain does not hold what we expected to find at this address."""


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


def read_sharing_config(rpc, curve: BondingCurve) -> SharingConfig:
    """Resolve the config named by the bonding curve.

    Not derivable: `bonding_curve.creator` names it. For a fee-shared coin that
    address is a SharingConfig owned by the fee-share program; for an ordinary
    coin it is just the creator's wallet, and there is no split to read.
    """
    account = rpc.accounts([curve.creator])[0]
    if not account or account.get("owner") != PUMP_FEE_SHARE_PROGRAM:
        raise DecodeError(
            f"{curve.mint}: its creator {curve.creator} is not a fee-sharing config "
            "(it is an ordinary creator address). There is no split to report."
        )
    data = _raw(account, DISC_SHARING_CONFIG, "sharing config", (PUMP_FEE_SHARE_PROGRAM,))

    count = int.from_bytes(data[76:80], "little")
    cursor = 80
    if count > MAX_SHAREHOLDERS or cursor + count * 34 > len(data):
        raise DecodeError(
            f"{curve.mint}: sharing config declares {count} shareholders, "
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
        cursor += 34

    return SharingConfig(
        address=curve.creator,
        mint=encode(data[11:43]),
        version=data[9],
        status=data[10],
        admin=encode(data[43:75]),
        admin_revoked=bool(data[75]),
        shareholders=tuple(holders),
    )


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

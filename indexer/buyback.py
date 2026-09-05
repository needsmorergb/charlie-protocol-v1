"""The BURN leg, run by hand: buy the coin and burn it, in one transaction.

PROTOCOL.md sec.1 defines the BURN leg as `SOL -> buy token -> SPL burn`, and
sec.4 requires the swap and the burn to be instructions in the same
transaction, so the tokens never rest anywhere they could be diverted. The
protocol program that would crank this from a coin's fee stream is not
deployed (`legs.PROGRAM_ID is None`), and $CHARLIE's own sharing config is
`admin_revoked`, so nobody -- its deployer included -- can point its fees at
a burn pool. That leaves the one route that needs no program and no admin
key: **a holder runs the leg from their own wallet.** SOL they choose to
spend buys the coin on its PumpSwap pool and the tokens are burned in the
same transaction. Anyone may do it, for any graduated pump coin.

What the indexer will say about such a burn, stated here so nobody has to
discover it on the coin's page:

* The mint-wide burn walk (`scan.scan_burns`, D-09) records every burn
  against the mint by anyone, so it records these. Each is a `burn_event`
  row with `source = spl_burn`, and `scan.classify_atomicity` marks it
  `atomic = PASS` because a PumpSwap invocation shares the transaction.
  It counts toward `SUPPLY_DESTROYED` and toward the coin page's
  "burned by hand, not by boost" figure.
* It is NOT `protocol_attributed` (D-10). That flag is reserved for burns
  cranked by the protocol's own program from a coin's fee stream; a wallet
  spending its own SOL is a third party, however protocol-shaped the
  transaction is. `BURN_ATOMIC`, narrowed to protocol burns (D-14), stays
  not-applicable. This module does not pretend otherwise.

The crank properties ARCHITECTURE.md sec.2 specifies for `crank_burn` are
kept where they can be kept off-chain: fixed lots, an in-transaction
slippage bound (`max_quote_amount_in` is the lot, and the buy fails whole
rather than overpaying), a minimum interval between cranks, and the burn in
the same transaction as the swap. What cannot be kept is the part that
needs a program: the SOL comes from a wallet, not a PDA, so this is
PROTOCOL.md sec.5's option 3 -- an operator keeper -- and is described as
such.

**Every byte here comes from pump's published PumpSwap IDL and its own SDK
(`@pump-fun/pump-swap-sdk` 1.19.0, July 2026)**: the `buy` discriminator,
the account list and its order, the trailing remaining accounts the IDL
does not describe (the `pool-v2` PDA and a buyback fee recipient with its
WSOL account), the fee-tier arithmetic and the WSOL wrapping sequence. The
transaction is simulated against mainnet before any key signs it, and a
simulation that reports an error is a transaction this module refuses to
sign -- the same gate `api/enroll.py` applies.

Standard library only, like the rest of the indexer.
"""

from __future__ import annotations

import base64
import json
import random
import time
from dataclasses import asdict, dataclass, field

from . import decode as decode_mod
from .base58 import encode, pubkey_bytes
from .curve import find_program_address
from .enroll import associated_token_address
from .message import Instruction, compile_legacy, signed_transaction, unsigned_transaction
from .pump import (
    DecodeError,
    PUMP_AMM_PROGRAM,
    PUMP_PROGRAM,
    SYSTEM_PROGRAM,
    TOKEN_2022_PROGRAM,
    TOKEN_PROGRAM,
    _raw,
    read_mint,
)

FEE_PROGRAM = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
ASSOCIATED_TOKEN_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"
WSOL_MINT = "So11111111111111111111111111111111111111112"
DEFAULT_PUBKEY = "11111111111111111111111111111111"

# sha256("global:buy")[:8] and sha256("global:extend_account")[:8], as
# published in pump_amm.json.
DISC_BUY = bytes.fromhex("66063d1201daebea")
DISC_EXTEND_ACCOUNT = bytes.fromhex("ea66c2cb96483ee5")
# Account discriminators, from the same IDL.
DISC_POOL = bytes.fromhex("f19a6d0411b16dbc")
DISC_GLOBAL_CONFIG = bytes.fromhex("95089ccaa0fcb0d9")
DISC_FEE_CONFIG = bytes.fromhex("8f3492bbdb7b4c9b")

# pump-swap-sdk `POOL_ACCOUNT_NEW_SIZE`: a pool shorter than this must be
# extended before it can be traded.
POOL_ACCOUNT_NEW_SIZE = 300
CANONICAL_POOL_INDEX = 0

LAMPORTS_PER_SOL = 1_000_000_000
BPS = 10_000
# ARCHITECTURE.md sec.2: "Spend in fixed increments (0.05 SOL, Snowball's
# figure) so a single crank can never move the market far enough to be
# worth attacking."
DEFAULT_LOT_LAMPORTS = 50_000_000
DEFAULT_SLIPPAGE_BPS = 100
DEFAULT_COMPUTE_UNITS = 250_000
# Below this a lot is fee noise; the runtime would accept it but the
# figures it produced would be meaningless.
MIN_LOT_LAMPORTS = 1_000_000
# What the wallet must hold beyond the lot: transaction fee, the two
# associated token accounts' rent while they exist, the volume accumulator
# pump creates on a first buy, priority fee headroom.
RESERVE_LAMPORTS = 10_000_000

# SPL Token instruction indexes (identical for Token and Token-2022).
_TOKEN_IX_BURN = 8
_TOKEN_IX_CLOSE_ACCOUNT = 9
_TOKEN_IX_SYNC_NATIVE = 17
_ATA_IX_CREATE_IDEMPOTENT = 1
_SYSTEM_IX_TRANSFER = 2
_COMPUTE_IX_SET_UNIT_LIMIT = 2
_COMPUTE_IX_SET_UNIT_PRICE = 3


class BuybackError(ValueError):
    """A crank that must not be sent. Every message is addressed to the
    person holding the wallet."""


# -- derivations -------------------------------------------------------------
def _pda(seeds, program: str) -> str:
    return find_program_address(seeds, program)[0]


def pool_authority(mint: str) -> str:
    """pump's `pool-authority` PDA: the creator of every canonical pool, and
    what makes a pool canonical (pump-swap-sdk `isPumpPool`)."""
    return _pda([b"pool-authority", pubkey_bytes(mint)], PUMP_PROGRAM)


def pool_address(index: int, creator: str, base_mint: str, quote_mint: str) -> str:
    return _pda(
        [b"pool", index.to_bytes(2, "little"), pubkey_bytes(creator), pubkey_bytes(base_mint), pubkey_bytes(quote_mint)],
        PUMP_AMM_PROGRAM,
    )


def canonical_pool(mint: str, quote_mint: str = WSOL_MINT) -> str:
    """The pool pump's `migrate` creates for a graduated coin."""
    return pool_address(CANONICAL_POOL_INDEX, pool_authority(mint), mint, quote_mint)


def lp_mint(pool: str) -> str:
    return _pda([b"pool_lp_mint", pubkey_bytes(pool)], PUMP_AMM_PROGRAM)


def global_config() -> str:
    return _pda([b"global_config"], PUMP_AMM_PROGRAM)


def event_authority() -> str:
    return _pda([b"__event_authority"], PUMP_AMM_PROGRAM)


def global_volume_accumulator() -> str:
    return _pda([b"global_volume_accumulator"], PUMP_AMM_PROGRAM)


def user_volume_accumulator(user: str) -> str:
    return _pda([b"user_volume_accumulator", pubkey_bytes(user)], PUMP_AMM_PROGRAM)


def coin_creator_vault_authority(coin_creator: str) -> str:
    return _pda([b"creator_vault", pubkey_bytes(coin_creator)], PUMP_AMM_PROGRAM)


def pool_v2(mint: str) -> str:
    return _pda([b"pool-v2", pubkey_bytes(mint)], PUMP_AMM_PROGRAM)


def fee_config() -> str:
    """The fee program's config for the AMM: seeds `["fee_config",
    pump_amm_program_id]` under the fee program -- the IDL spells the second
    seed as 32 constant bytes, which decode to the AMM's own address."""
    return _pda([b"fee_config", pubkey_bytes(PUMP_AMM_PROGRAM)], FEE_PROGRAM)


# -- account decoders --------------------------------------------------------
@dataclass(frozen=True)
class Pool:
    """pump_amm `Pool`, exactly as the chain states it.

    disc 8 | bump u8 | index u16 | creator | base_mint | quote_mint | lp_mint
    | pool_base_token_account | pool_quote_token_account | lp_supply u64 |
    coin_creator | is_mayhem_mode u8 | is_cashback_coin u8 |
    virtual_quote_reserves i128

    Older pools are shorter (the last four fields were appended over time);
    a missing field reads as its default, which is what the program does.
    """

    address: str
    index: int
    creator: str
    base_mint: str
    quote_mint: str
    lp_mint: str
    base_vault: str
    quote_vault: str
    lp_supply: int
    coin_creator: str
    is_mayhem_mode: bool
    is_cashback_coin: bool
    virtual_quote_reserves: int
    data_len: int

    @property
    def canonical(self) -> bool:
        return self.creator == pool_authority(self.base_mint)

    @property
    def has_coin_creator(self) -> bool:
        return self.coin_creator != DEFAULT_PUBKEY


def decode_pool(address: str, account: dict | None) -> Pool:
    data = _raw(account, DISC_POOL, "PumpSwap pool", (PUMP_AMM_PROGRAM,))
    if len(data) < 211:
        raise DecodeError(f"{address}: pool account is {len(data)} bytes, expected at least 211")
    coin_creator = encode(data[211:243]) if len(data) >= 243 else DEFAULT_PUBKEY
    mayhem = bool(data[243]) if len(data) > 243 else False
    cashback = bool(data[244]) if len(data) > 244 else False
    vqr = int.from_bytes(data[245:261], "little", signed=True) if len(data) >= 261 else 0
    return Pool(
        address=address,
        index=int.from_bytes(data[9:11], "little"),
        creator=encode(data[11:43]),
        base_mint=encode(data[43:75]),
        quote_mint=encode(data[75:107]),
        lp_mint=encode(data[107:139]),
        base_vault=encode(data[139:171]),
        quote_vault=encode(data[171:203]),
        lp_supply=int.from_bytes(data[203:211], "little"),
        coin_creator=coin_creator,
        is_mayhem_mode=mayhem,
        is_cashback_coin=cashback,
        virtual_quote_reserves=vqr,
        data_len=len(data),
    )


@dataclass(frozen=True)
class GlobalConfig:
    lp_fee_bps: int
    protocol_fee_bps: int
    protocol_fee_recipients: tuple[str, ...]
    coin_creator_fee_bps: int
    reserved_fee_recipients: tuple[str, ...]
    buyback_fee_recipients: tuple[str, ...]


def _pubkeys(data: bytes, start: int, count: int) -> tuple[str, ...]:
    return tuple(encode(data[start + 32 * i : start + 32 * (i + 1)]) for i in range(count))


def decode_global_config(account: dict | None) -> GlobalConfig:
    """disc 8 | admin | lp_fee u64 | protocol_fee u64 | disable_flags u8 |
    protocol_fee_recipients [8] | coin_creator_fee u64 |
    admin_set_coin_creator_authority | whitelist_pda | reserved_fee_recipient
    | mayhem_mode_enabled u8 | reserved_fee_recipients [7] |
    is_cashback_enabled u8 | buyback_fee_recipients [8] | ...
    """
    data = _raw(account, DISC_GLOBAL_CONFIG, "PumpSwap global config", (PUMP_AMM_PROGRAM,))
    if len(data) < 899:
        raise DecodeError(
            f"PumpSwap global config is {len(data)} bytes, shorter than the layout this "
            "was written against (899+) -- pump changed the account, refusing to guess"
        )
    reserved = (encode(data[385:417]),) + _pubkeys(data, 418, 7)
    return GlobalConfig(
        lp_fee_bps=int.from_bytes(data[40:48], "little"),
        protocol_fee_bps=int.from_bytes(data[48:56], "little"),
        protocol_fee_recipients=_pubkeys(data, 57, 8),
        coin_creator_fee_bps=int.from_bytes(data[313:321], "little"),
        reserved_fee_recipients=reserved,
        buyback_fee_recipients=_pubkeys(data, 643, 8),
    )


@dataclass(frozen=True)
class Fees:
    lp_bps: int
    protocol_bps: int
    creator_bps: int

    @property
    def total_bps(self) -> int:
        return self.lp_bps + self.protocol_bps + self.creator_bps


@dataclass(frozen=True)
class FeeTier:
    market_cap_lamports_threshold: int
    fees: Fees


@dataclass(frozen=True)
class FeeConfig:
    flat_fees: Fees
    fee_tiers: tuple[FeeTier, ...]


def _fees_at(data: bytes, at: int) -> Fees:
    return Fees(
        int.from_bytes(data[at : at + 8], "little"),
        int.from_bytes(data[at + 8 : at + 16], "little"),
        int.from_bytes(data[at + 16 : at + 24], "little"),
    )


def decode_fee_config(account: dict | None) -> FeeConfig:
    """disc 8 | bump u8 | admin | flat_fees (3 x u64) | fee_tiers vec<(u128
    threshold, 3 x u64)> | stable_fee_tiers vec<...> (not read)."""
    data = _raw(account, DISC_FEE_CONFIG, "pump fee config", (FEE_PROGRAM,))
    if len(data) < 69:
        raise DecodeError(f"pump fee config is {len(data)} bytes, too short for its header")
    flat = _fees_at(data, 41)
    count = int.from_bytes(data[65:69], "little")
    cursor = 69
    if count > 256 or cursor + count * 40 > len(data):
        raise DecodeError(f"pump fee config declares {count} fee tiers, which its data cannot hold")
    tiers = []
    for _ in range(count):
        threshold = int.from_bytes(data[cursor : cursor + 16], "little")
        tiers.append(FeeTier(threshold, _fees_at(data, cursor + 16)))
        cursor += 40
    return FeeConfig(flat_fees=flat, fee_tiers=tuple(tiers))


def fee_tier(tiers, market_cap_lamports: int) -> Fees:
    """pump-fees-math `calculate_fee_tier`, via pump-swap-sdk: below the
    first threshold the first tier applies; otherwise the highest tier
    whose threshold the market cap meets."""
    if not tiers:
        raise BuybackError("the fee config carries no fee tiers")
    first = tiers[0]
    if market_cap_lamports < first.market_cap_lamports_threshold:
        return first.fees
    for tier in reversed(tiers):
        if market_cap_lamports >= tier.market_cap_lamports_threshold:
            return tier.fees
    return first.fees


def decode_token_amount(account: dict | None, *, expect_mint: str | None = None) -> int | None:
    """A token account's balance, or `None` when the account does not exist.
    Token and Token-2022 share the base layout: mint | owner | amount u64."""
    if not account:
        return None
    owner = account.get("owner")
    if owner not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        raise DecodeError(f"expected a token account, found one owned by {owner}")
    data = base64.b64decode(account["data"][0])
    if len(data) < 72:
        raise DecodeError(f"token account is {len(data)} bytes, expected at least 72")
    if expect_mint is not None and encode(data[0:32]) != expect_mint:
        raise DecodeError(f"token account holds {encode(data[0:32])}, not {expect_mint}")
    return int.from_bytes(data[64:72], "little")


# -- arithmetic --------------------------------------------------------------
def _ceil_div(a: int, b: int) -> int:
    return -(-a // b)


def _fee(amount: int, bps: int) -> int:
    return _ceil_div(amount * bps, BPS)


def pool_market_cap(supply: int, base_reserve: int, quote_reserve: int) -> int:
    """pump-swap-sdk `poolMarketCap`, in lamports: what the fee tier is
    chosen against."""
    if base_reserve == 0:
        raise BuybackError("the pool has no base reserve")
    return quote_reserve * supply // base_reserve


def cost_of(base_out: int, base_reserve: int, quote_reserve: int, fees: Fees, charge_creator: bool) -> dict:
    """pump-swap-sdk `buyBaseInput`: what `buy(base_out, ...)` charges.

    quote_in = ceil(quote_reserve * base_out / (base_reserve - base_out)),
    then each fee is ceil(quote_in * bps / 10000) on top of it.
    """
    if base_out <= 0 or base_out >= base_reserve:
        raise BuybackError("base_out must be positive and below the pool's base reserve")
    quote_in = _ceil_div(quote_reserve * base_out, base_reserve - base_out)
    lp = _fee(quote_in, fees.lp_bps)
    protocol = _fee(quote_in, fees.protocol_bps)
    creator = _fee(quote_in, fees.creator_bps) if charge_creator else 0
    return {
        "quote_in": quote_in,
        "lp_fee": lp,
        "protocol_fee": protocol,
        "creator_fee": creator,
        "total": quote_in + lp + protocol + creator,
    }


def base_out_for_lot(lot: int, base_reserve: int, quote_reserve: int, fees: Fees, charge_creator: bool, slippage_bps: int) -> int:
    """The largest `base_out` whose cost fits inside `lot`, then reduced by
    `slippage_bps` so the buy still fits if the pool moves against it
    between quoting and landing. `max_quote_amount_in` stays equal to the
    lot: the bound is in the transaction, and the unspent remainder comes
    back when the WSOL account is closed.
    """
    if not 0 <= slippage_bps < BPS:
        raise BuybackError("slippage must be between 0 and 9999 bps")
    total_bps = fees.lp_bps + fees.protocol_bps + (fees.creator_bps if charge_creator else 0)
    effective = lot * BPS // (BPS + total_bps)
    if effective <= 1:
        raise BuybackError("the lot is too small to buy anything after fees")
    # pump-swap-sdk `buyQuoteInput`, including its `effectiveQuote - 1`.
    inputs = effective - 1
    base_out = base_reserve * inputs // (quote_reserve + inputs)
    while base_out > 0 and cost_of(base_out, base_reserve, quote_reserve, fees, charge_creator)["total"] > lot:
        base_out -= max(1, base_out // 10_000)
    base_out = base_out * (BPS - slippage_bps) // BPS
    if base_out <= 0:
        raise BuybackError("the lot is too small to buy anything after fees and slippage")
    return base_out


# -- instructions ------------------------------------------------------------
def ix_compute_unit_limit(units: int) -> Instruction:
    return (COMPUTE_BUDGET_PROGRAM, [], bytes([_COMPUTE_IX_SET_UNIT_LIMIT]) + units.to_bytes(4, "little"))


def ix_compute_unit_price(micro_lamports: int) -> Instruction:
    return (COMPUTE_BUDGET_PROGRAM, [], bytes([_COMPUTE_IX_SET_UNIT_PRICE]) + micro_lamports.to_bytes(8, "little"))


def ix_create_ata_idempotent(payer: str, ata: str, owner: str, mint: str, token_program: str) -> Instruction:
    return (
        ASSOCIATED_TOKEN_PROGRAM,
        [(payer, True, True), (ata, False, True), (owner, False, False), (mint, False, False),
         (SYSTEM_PROGRAM, False, False), (token_program, False, False)],
        bytes([_ATA_IX_CREATE_IDEMPOTENT]),
    )


def ix_system_transfer(source: str, destination: str, lamports: int) -> Instruction:
    return (
        SYSTEM_PROGRAM,
        [(source, True, True), (destination, False, True)],
        _SYSTEM_IX_TRANSFER.to_bytes(4, "little") + lamports.to_bytes(8, "little"),
    )


def ix_sync_native(ata: str) -> Instruction:
    return (TOKEN_PROGRAM, [(ata, False, True)], bytes([_TOKEN_IX_SYNC_NATIVE]))


def ix_close_account(ata: str, destination: str, owner: str) -> Instruction:
    return (TOKEN_PROGRAM, [(ata, False, True), (destination, False, True), (owner, True, False)], bytes([_TOKEN_IX_CLOSE_ACCOUNT]))


def ix_burn(token_program: str, ata: str, mint: str, owner: str, amount: int) -> Instruction:
    """SPL `Burn` (not `BurnChecked`): the RPC parses it as `type: burn` with
    `mint` and a string `amount` in its info, which is the exact shape
    `decode.find_burns` was proven against on $CHARLIE's 29 boost burns --
    pump's boost crank issues this same instruction on Token-2022. Using it
    means the walk records this burn through a path that has already read
    real ones, not a sibling instruction it has never seen."""
    return (
        token_program,
        [(ata, False, True), (mint, False, True), (owner, True, False)],
        bytes([_TOKEN_IX_BURN]) + amount.to_bytes(8, "little"),
    )


def ix_extend_account(pool: str, user: str) -> Instruction:
    return (
        PUMP_AMM_PROGRAM,
        [(pool, False, True), (user, True, False), (SYSTEM_PROGRAM, False, False),
         (event_authority(), False, False), (PUMP_AMM_PROGRAM, False, False)],
        DISC_EXTEND_ACCOUNT,
    )


def buy_accounts(pool: Pool, user: str, *, base_token_program: str, protocol_fee_recipient: str,
                 buyback_fee_recipient: str, quote_token_program: str = TOKEN_PROGRAM) -> list:
    """`(address, is_signer, is_writable)` in the EXACT order the IDL lists,
    followed by the remaining accounts pump-swap-sdk appends. Order is not
    cosmetic: the program reads its accounts positionally.
    """
    vault_authority = coin_creator_vault_authority(pool.coin_creator)
    metas = [
        (pool.address, False, True),
        (user, True, True),
        (global_config(), False, False),
        (pool.base_mint, False, False),
        (pool.quote_mint, False, False),
        (associated_token_address(user, pool.base_mint, base_token_program), False, True),
        (associated_token_address(user, pool.quote_mint, quote_token_program), False, True),
        (pool.base_vault, False, True),
        (pool.quote_vault, False, True),
        (protocol_fee_recipient, False, False),
        (associated_token_address(protocol_fee_recipient, pool.quote_mint, quote_token_program), False, True),
        (base_token_program, False, False),
        (quote_token_program, False, False),
        (SYSTEM_PROGRAM, False, False),
        (ASSOCIATED_TOKEN_PROGRAM, False, False),
        (event_authority(), False, False),
        (PUMP_AMM_PROGRAM, False, False),
        (associated_token_address(vault_authority, pool.quote_mint, quote_token_program), False, True),
        (vault_authority, False, False),
        (global_volume_accumulator(), False, False),
        (user_volume_accumulator(user), False, True),
        (fee_config(), False, False),
        (FEE_PROGRAM, False, False),
    ]
    # Anchor remaining accounts, which no IDL records. From pump-swap-sdk
    # `buyInstructionsNoPool`, in this order.
    if pool.is_cashback_coin:
        metas.append((associated_token_address(user_volume_accumulator(user), pool.quote_mint, quote_token_program), False, True))
    if pool.has_coin_creator:
        metas.append((pool_v2(pool.base_mint), False, False))
    metas.append((buyback_fee_recipient, False, False))
    metas.append((associated_token_address(buyback_fee_recipient, pool.quote_mint, quote_token_program), False, True))
    return metas


def buy_data(base_amount_out: int, max_quote_amount_in: int, track_volume: bool = True) -> bytes:
    """`discriminator || u64 || u64 || OptionBool`. `OptionBool` is a
    one-field struct around a bool in the IDL, so it is a single byte; the
    SDK passes `{0: true}`."""
    return DISC_BUY + base_amount_out.to_bytes(8, "little") + max_quote_amount_in.to_bytes(8, "little") + bytes([1 if track_volume else 0])


def ix_buy(pool: Pool, user: str, base_amount_out: int, max_quote_amount_in: int, **kw) -> Instruction:
    return (PUMP_AMM_PROGRAM, buy_accounts(pool, user, **kw), buy_data(base_amount_out, max_quote_amount_in))


# -- reading the chain -------------------------------------------------------
@dataclass(frozen=True)
class State:
    mint: str
    user: str
    decimals: int
    supply: int
    token_program: str
    mint_authority: str | None
    pool: Pool
    base_reserve: int
    quote_reserve: int          # effective: vault balance + virtual_quote_reserves
    config: GlobalConfig
    fee_config: FeeConfig | None
    user_lamports: int
    user_base_balance: int | None   # None: no token account yet
    user_quote_exists: bool

    @property
    def market_cap_lamports(self) -> int:
        return pool_market_cap(self.supply, self.base_reserve, self.quote_reserve)

    @property
    def fees(self) -> Fees:
        if self.fee_config is None:
            return Fees(self.config.lp_fee_bps, self.config.protocol_fee_bps, self.config.coin_creator_fee_bps)
        if self.pool.canonical:
            return fee_tier(self.fee_config.fee_tiers, self.market_cap_lamports)
        return self.fee_config.flat_fees


def observe(rpc, mint: str, user: str) -> State:
    """Everything a plan needs, read in three round trips and checked the
    way `pump.py` checks: owner and discriminator before any byte is
    trusted."""
    mint_state = read_mint(rpc, mint)
    pool_key = canonical_pool(mint)
    first = rpc.accounts([pool_key, global_config(), fee_config(), user])
    if first[0] is None:
        raise BuybackError(
            f"{mint} has no canonical PumpSwap pool at {pool_key}. Either the coin has not "
            "graduated from its bonding curve yet, or it is not a pump.fun coin."
        )
    pool = decode_pool(pool_key, first[0])
    if pool.base_mint != mint or pool.quote_mint != WSOL_MINT:
        raise BuybackError(f"the pool at {pool_key} is for {pool.base_mint}/{pool.quote_mint}, not {mint}/SOL")
    config = decode_global_config(first[1])
    fees_cfg = decode_fee_config(first[2]) if first[2] is not None else None
    user_lamports = int((first[3] or {}).get("lamports") or 0)

    user_base_ata = associated_token_address(user, mint, mint_state.program)
    user_quote_ata = associated_token_address(user, WSOL_MINT, TOKEN_PROGRAM)
    second = rpc.accounts([pool.base_vault, pool.quote_vault, user_base_ata, user_quote_ata])
    base_reserve = decode_token_amount(second[0], expect_mint=mint)
    quote_vault = decode_token_amount(second[1], expect_mint=WSOL_MINT)
    if base_reserve is None or quote_vault is None:
        raise BuybackError("the pool's token vaults could not be read")
    return State(
        mint=mint,
        user=user,
        decimals=mint_state.decimals,
        supply=mint_state.supply,
        token_program=mint_state.program,
        mint_authority=mint_state.mint_authority,
        pool=pool,
        base_reserve=base_reserve,
        quote_reserve=quote_vault + pool.virtual_quote_reserves,
        config=config,
        fee_config=fees_cfg,
        user_lamports=user_lamports,
        user_base_balance=decode_token_amount(second[2], expect_mint=mint),
        user_quote_exists=second[3] is not None,
    )


# -- the plan ----------------------------------------------------------------
@dataclass
class Plan:
    kind: str                       # "buy_and_burn" | "burn"
    mint: str
    user: str
    decimals: int
    lot_lamports: int
    base_out: int                   # tokens the buy delivers (raw units), 0 for a plain burn
    also_burn: int                  # held tokens burned on top (raw units)
    burn_total: int
    expected_cost: dict             # cost_of(base_out) at quote time
    fees: dict
    market_cap_lamports: int
    base_reserve: int
    quote_reserve: int
    price_before: float             # SOL per whole token
    price_after: float
    impact_bps: int
    supply_before: int
    supply_after: int
    supply_reduction_bps: float
    notes: list = field(default_factory=list)
    instructions: list = field(default_factory=list)
    accounts: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        out = asdict(self)
        out.pop("instructions")
        out["instruction_count"] = len(self.instructions)
        return out


def _ui(raw: int, decimals: int) -> float:
    return raw / (10 ** decimals)


def plan_buy_and_burn(state: State, *, lot_lamports: int = DEFAULT_LOT_LAMPORTS, slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
                      also_burn: int = 0, priority_micro_lamports: int = 0, compute_units: int = DEFAULT_COMPUTE_UNITS,
                      choose=random.choice) -> Plan:
    """Quote the lot against the pool as it reads right now and lay out the
    instructions: wrap the lot, buy exactly `base_out`, burn `base_out +
    also_burn`, unwrap what is left. One transaction, all or nothing.
    """
    if lot_lamports < MIN_LOT_LAMPORTS:
        raise BuybackError(f"a lot below {MIN_LOT_LAMPORTS / LAMPORTS_PER_SOL} SOL is fee noise; refusing")
    if also_burn < 0:
        raise BuybackError("also_burn cannot be negative")
    needed = lot_lamports + RESERVE_LAMPORTS + priority_micro_lamports * compute_units // 1_000_000
    if state.user_lamports < needed:
        raise BuybackError(
            f"{state.user} holds {state.user_lamports / LAMPORTS_PER_SOL:.4f} SOL; this crank needs at least "
            f"{needed / LAMPORTS_PER_SOL:.4f} (the lot plus fee and rent headroom)"
        )
    if also_burn and (state.user_base_balance or 0) < also_burn:
        raise BuybackError(
            f"asked to burn {_ui(also_burn, state.decimals):,.{state.decimals}f} held tokens on top, but the wallet "
            f"holds {_ui(state.user_base_balance or 0, state.decimals):,.{state.decimals}f}"
        )
    if state.pool.data_len < POOL_ACCOUNT_NEW_SIZE:
        extend = True
    else:
        extend = False

    fees = state.fees
    charge_creator = state.pool.has_coin_creator
    base_out = base_out_for_lot(lot_lamports, state.base_reserve, state.quote_reserve, fees, charge_creator, slippage_bps)
    cost = cost_of(base_out, state.base_reserve, state.quote_reserve, fees, charge_creator)

    price_before = state.quote_reserve / state.base_reserve
    price_after = (state.quote_reserve + cost["quote_in"]) / (state.base_reserve - base_out)
    scale = 10 ** state.decimals / LAMPORTS_PER_SOL
    burn_total = base_out + also_burn

    recipients = state.config.reserved_fee_recipients if state.pool.is_mayhem_mode else state.config.protocol_fee_recipients
    protocol_fee_recipient = choose(list(recipients))
    buyback_fee_recipient = choose(list(state.config.buyback_fee_recipients))

    user_base_ata = associated_token_address(state.user, state.mint, state.token_program)
    user_quote_ata = associated_token_address(state.user, WSOL_MINT, TOKEN_PROGRAM)

    instructions: list[Instruction] = [ix_compute_unit_limit(compute_units)]
    if priority_micro_lamports:
        instructions.append(ix_compute_unit_price(priority_micro_lamports))
    if extend:
        instructions.append(ix_extend_account(state.pool.address, state.user))
    instructions += [
        ix_create_ata_idempotent(state.user, user_base_ata, state.user, state.mint, state.token_program),
        ix_create_ata_idempotent(state.user, user_quote_ata, state.user, WSOL_MINT, TOKEN_PROGRAM),
        ix_system_transfer(state.user, user_quote_ata, lot_lamports),
        ix_sync_native(user_quote_ata),
        ix_buy(state.pool, state.user, base_out, lot_lamports,
               base_token_program=state.token_program,
               protocol_fee_recipient=protocol_fee_recipient,
               buyback_fee_recipient=buyback_fee_recipient),
        ix_burn(state.token_program, user_base_ata, state.mint, state.user, burn_total),
        ix_close_account(user_quote_ata, state.user, state.user),
    ]

    notes = [
        "The buy and the burn are instructions in one transaction: both land or neither does (PROTOCOL.md sec.4).",
        f"max_quote_amount_in is the lot itself ({lot_lamports / LAMPORTS_PER_SOL} SOL). If the pool moves so the buy "
        "would cost more, the whole transaction fails rather than overpaying; whatever is unspent returns when the "
        "WSOL account is closed.",
        "The indexer records this as a third-party burn (source spl_burn, atomic PASS). It counts toward supply "
        "destroyed; it is not protocol-attributed, because no protocol program cranked it (D-10).",
    ]
    if charge_creator:
        notes.append(
            f"{cost['creator_fee'] / LAMPORTS_PER_SOL:.9f} SOL of this buy is the coin's creator fee, paid into "
            f"the vault of {state.pool.coin_creator}. Where it goes from there is that coin's fee split."
        )
    if state.mint_authority is not None:
        notes.append(f"WARNING: mint authority {state.mint_authority} is live -- burned supply can be reissued (BURN_IRREVERSIBLE fails).")
    if also_burn:
        notes.append(
            f"{_ui(also_burn, state.decimals):,.{state.decimals}f} tokens already held are burned in the same transaction. "
            "That reduces supply; it is not a buy and moves no price."
        )
    if extend:
        notes.append("The pool account predates pump's coin-creator fields and is extended first, as the SDK does.")

    return Plan(
        kind="buy_and_burn",
        mint=state.mint,
        user=state.user,
        decimals=state.decimals,
        lot_lamports=lot_lamports,
        base_out=base_out,
        also_burn=also_burn,
        burn_total=burn_total,
        expected_cost=cost,
        fees={"lp_bps": fees.lp_bps, "protocol_bps": fees.protocol_bps,
              "creator_bps": fees.creator_bps if charge_creator else 0, "total_bps": fees.total_bps if charge_creator else fees.lp_bps + fees.protocol_bps},
        market_cap_lamports=state.market_cap_lamports,
        base_reserve=state.base_reserve,
        quote_reserve=state.quote_reserve,
        price_before=price_before * scale,
        price_after=price_after * scale,
        impact_bps=round((price_after / price_before - 1) * BPS),
        supply_before=state.supply,
        supply_after=state.supply - burn_total,
        supply_reduction_bps=burn_total / state.supply * BPS,
        notes=notes,
        instructions=instructions,
        accounts={
            "pool": state.pool.address,
            "pool_base_vault": state.pool.base_vault,
            "pool_quote_vault": state.pool.quote_vault,
            "coin_creator": state.pool.coin_creator,
            "protocol_fee_recipient": protocol_fee_recipient,
            "buyback_fee_recipient": buyback_fee_recipient,
            "user_base_token_account": user_base_ata,
            "user_wsol_account": user_quote_ata,
            "token_program": state.token_program,
        },
    )


def plan_burn(state: State, amount: int, *, compute_units: int = 50_000) -> Plan:
    """A plain burn of held tokens. No swap, so the indexer classifies it
    `atomic FAIL` -- correctly: PROTOCOL.md sec.4's atomicity is about a
    swap-and-burn, and this has no swap. It still counts toward supply
    destroyed, as every burn against the mint does (D-09)."""
    if amount <= 0:
        raise BuybackError("the amount to burn must be positive")
    held = state.user_base_balance or 0
    if held < amount:
        raise BuybackError(
            f"the wallet holds {_ui(held, state.decimals):,.{state.decimals}f} tokens, fewer than the "
            f"{_ui(amount, state.decimals):,.{state.decimals}f} asked for"
        )
    user_base_ata = associated_token_address(state.user, state.mint, state.token_program)
    price = state.quote_reserve / state.base_reserve * (10 ** state.decimals) / LAMPORTS_PER_SOL
    return Plan(
        kind="burn",
        mint=state.mint,
        user=state.user,
        decimals=state.decimals,
        lot_lamports=0,
        base_out=0,
        also_burn=amount,
        burn_total=amount,
        expected_cost={"quote_in": 0, "lp_fee": 0, "protocol_fee": 0, "creator_fee": 0, "total": 0},
        fees={"lp_bps": 0, "protocol_bps": 0, "creator_bps": 0, "total_bps": 0},
        market_cap_lamports=state.market_cap_lamports,
        base_reserve=state.base_reserve,
        quote_reserve=state.quote_reserve,
        price_before=price,
        price_after=price,
        impact_bps=0,
        supply_before=state.supply,
        supply_after=state.supply - amount,
        supply_reduction_bps=amount / state.supply * BPS,
        notes=[
            "A burn of held tokens. Supply falls; no SOL enters the pool and the price does not move.",
            "The indexer records it as a third-party burn with no swap alongside (atomic FAIL, as it should read).",
        ],
        instructions=[
            ix_compute_unit_limit(compute_units),
            ix_burn(state.token_program, user_base_ata, state.mint, state.user, amount),
        ],
        accounts={"user_base_token_account": user_base_ata, "token_program": state.token_program},
    )


# -- building, simulating, sending -------------------------------------------
def build(plan: Plan, recent_blockhash: str) -> bytes:
    return compile_legacy(plan.user, plan.instructions, recent_blockhash)


def latest_blockhash(rpc, commitment: str = "confirmed") -> str:
    return rpc.call("getLatestBlockhash", [{"commitment": commitment}])["value"]["blockhash"]


def simulate(rpc, message: bytes) -> dict:
    """The gate. Returns the RPC's `value`: `err`, `logs`, `unitsConsumed`.
    A non-null `err` is a transaction nothing here will sign."""
    encoded = base64.b64encode(unsigned_transaction(message)).decode()
    result = rpc.call("simulateTransaction", [encoded, {
        "encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True, "commitment": "processed",
    }])
    return (result or {}).get("value") or {}


def explain(value: dict) -> str:
    """The program's error in words a person can act on. Patterns come from
    pump's IDL error names and the token program's messages."""
    logs = " ".join(value.get("logs") or [])
    err = json.dumps(value.get("err"))
    if "ExceededSlippage" in logs:
        return "The pool moved between quoting and simulating: the buy would cost more than the lot. Re-run to quote again, or widen --slippage-bps."
    if "insufficient funds" in logs.lower() or "insufficient lamports" in logs.lower():
        return "The wallet does not hold enough SOL for the lot plus fees and rent."
    if "InvalidBaseAmountOut" in logs or "base_amount_out" in logs.lower():
        return "pump refused the base amount; the pool state may have changed. Re-run to quote again."
    if "AccountNotInitialized" in logs:
        return "An account pump expects does not exist yet. The logs name it."
    if "ConstraintSeeds" in logs or "ConstraintAssociated" in logs:
        return "An account was derived differently from what the program expects -- pump may have changed the instruction. Do not retry blindly; the logs name the account."
    if "custom program error: 0x1" in logs and "Token" in logs:
        return "The token program reported insufficient funds -- the burn amount exceeds what the account would hold."
    if "TransferHook" in logs or "transfer hook" in logs.lower():
        return "This mint has a Token-2022 transfer hook and the buy needs extra accounts this builder does not add."
    return f"The simulation failed ({err}). Nothing was sent."


def send_signed(rpc, keypair, message: bytes) -> str:
    """Sign, then hand the transaction to the RPC with preflight on: the
    node simulates once more against its latest state before forwarding."""
    if keypair.address != _payer_of(message):
        raise BuybackError(f"the keypair is {keypair.address} but the message's payer is {_payer_of(message)}")
    signature = keypair.sign(message)
    encoded = base64.b64encode(signed_transaction(message, [signature])).decode()
    return rpc.call("sendTransaction", [encoded, {
        "encoding": "base64", "skipPreflight": False, "preflightCommitment": "confirmed", "maxRetries": 5,
    }])


def _payer_of(message: bytes) -> str:
    # header 3 bytes, compact-u16 count (1 byte below 128 accounts), then keys.
    return encode(message[4:36])


def confirm(rpc, signature: str, *, timeout: float = 90.0, sleep=time.sleep, clock=time.monotonic) -> dict:
    """Poll until the signature is confirmed or finalized. A transaction
    whose blockhash expires unconfirmed raises; it did not land."""
    deadline = clock() + timeout
    while True:
        result = rpc.call("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
        status = ((result or {}).get("value") or [None])[0]
        if status:
            if status.get("err") is not None:
                raise BuybackError(f"{signature} landed but FAILED on chain: {json.dumps(status['err'])}")
            if status.get("confirmationStatus") in ("confirmed", "finalized"):
                return status
        if clock() >= deadline:
            raise BuybackError(f"{signature} was not confirmed within {timeout:.0f}s -- check an explorer before retrying")
        sleep(2.0)


def verify_recorded(rpc, signature: str, mint: str) -> dict:
    """Read the landed transaction back exactly as `scan._walk_burns` will,
    and apply the same two decoders. This is the protocol's own view of what
    just happened, not this module's."""
    from .scan import classify_atomicity
    tx = rpc.transaction(signature)
    if not tx:
        raise BuybackError(f"{signature} could not be fetched back")
    burns = decode_mod.find_burns(tx, mint)
    return {
        "signature": signature,
        "slot": tx.get("slot"),
        "block_time": tx.get("blockTime"),
        "burn_instructions": len(burns),
        "tokens_burned": sum(b["amount"] for b in burns),
        "swap_present": decode_mod.find_swap_shaped(tx),
        "atomic": classify_atomicity(tx, mint),
        "source": "spl_burn",
        "protocol_attributed": 0,
    }


# -- rendering ----------------------------------------------------------------
def render(plan: Plan) -> str:
    d = plan.decimals
    sol = LAMPORTS_PER_SOL
    lines = [f"{plan.kind.replace('_', ' ')} -- {plan.mint}", f"wallet {plan.user}", ""]
    rows = []
    if plan.kind in ("buy_and_burn", "curve_buy_and_burn"):
        c = plan.expected_cost
        on_curve = plan.kind == "curve_buy_and_burn"
        rows += [
            ("venue", "pump bonding curve" if on_curve else "PumpSwap pool", "where the coin trades right now"),
            ("lot (max spend)", f"{plan.lot_lamports / sol:.9f} SOL", "max_sol_cost" if on_curve else "max_quote_amount_in"),
            ("expected spend", f"{c['total'] / sol:.9f} SOL", "quote_in + lp + protocol + creator fee, at quote time"),
            ("  of which creator fee", f"{c['creator_fee'] / sol:.9f} SOL", f"{plan.fees['creator_bps']} bps -> the coin's fee split"),
            ("tokens bought", f"{_ui(plan.base_out, d):,.{d}f}", "amount, exact" if on_curve else "base_amount_out, exact"),
            ("price before", f"{plan.price_before:.12f} SOL", "quote_reserve / base_reserve"),
            ("price after", f"{plan.price_after:.12f} SOL", f"+{plan.impact_bps / 100:.2f}% from this buy alone"),
            ("reserves", f"{plan.quote_reserve / sol:,.4f} SOL / {_ui(plan.base_reserve, d):,.0f} tokens",
             "virtual reserves" if on_curve else "effective quote reserve"),
            ("market cap", f"{plan.market_cap_lamports / sol:,.2f} SOL", "quote_reserve * supply / base_reserve"),
            ("fee tier", f"{plan.fees['total_bps']} bps", f"lp {plan.fees['lp_bps']} + protocol {plan.fees['protocol_bps']} + creator {plan.fees['creator_bps']}"),
        ]
    if plan.also_burn:
        rows.append(("held tokens burned too", f"{_ui(plan.also_burn, d):,.{d}f}", "from the wallet's own balance"))
    rows += [
        ("tokens burned, total", f"{_ui(plan.burn_total, d):,.{d}f}", "the Burn instruction's amount"),
        ("supply", f"{_ui(plan.supply_before, d):,.{d}f} -> {_ui(plan.supply_after, d):,.{d}f}", f"-{plan.supply_reduction_bps / 100:.4f}%"),
        ("instructions", str(len(plan.instructions)), "one transaction"),
    ]
    width = max(len(r[0]) for r in rows)
    for label, value, source in rows:
        lines.append(f"  {label:<{width}}  {value}    [{source}]")
    lines.append("")
    for note in plan.notes:
        lines.append(f"- {note}")
    return "\n".join(lines)


# -- one crank, and the keeper loop ------------------------------------------
def _execute(rpc, plan: Plan, keypair, *, send: bool, sleep=time.sleep, confirm_timeout: float = 90.0) -> dict:
    """build -> simulate -> (sign -> send -> confirm -> read back). The
    simulation gate sits between the two halves; nothing crosses it with
    an error."""
    msg = build(plan, latest_blockhash(rpc))
    sim = simulate(rpc, msg)
    result = {
        "kind": plan.kind,
        "mint": plan.mint,
        "wallet": plan.user,
        "plan": plan.as_dict(),
        "simulation": {
            "err": sim.get("err"),
            "units_consumed": sim.get("unitsConsumed"),
            "logs_tail": (sim.get("logs") or [])[-8:],
        },
        # base58 of the MESSAGE, base58 of the zero-signed transaction (what a
        # wallet's signAndSendTransaction takes: Phantom parses its `message`
        # parameter as a whole transaction) and base64 of the same, as
        # api/enroll.py returns.
        "message_base58": encode(msg),
        "transaction_base58": encode(unsigned_transaction(msg)),
        "transaction_base64": base64.b64encode(unsigned_transaction(msg)).decode(),
        "sent": False,
    }
    if sim.get("err") is not None:
        result["error"] = explain(sim)
        return result
    if not send:
        return result
    if keypair is None:
        result["error"] = "no keypair given: built and simulated, not sent"
        return result
    signature = send_signed(rpc, keypair, msg)
    result["signature"] = signature
    result["sent"] = True
    status = confirm(rpc, signature, timeout=confirm_timeout, sleep=sleep)
    result["confirmation"] = status.get("confirmationStatus")
    result["recorded"] = verify_recorded(rpc, signature, plan.mint)
    return result


def crank_once(rpc, mint: str, wallet: str, keypair=None, *, lot_lamports: int = DEFAULT_LOT_LAMPORTS,
               slippage_bps: int = DEFAULT_SLIPPAGE_BPS, also_burn_ui: float = 0.0, priority_micro_lamports: int = 0,
               send: bool = False, choose=random.choice, sleep=time.sleep, confirm_timeout: float = 90.0) -> dict:
    """One buy-and-burn, quoted against the venue the coin is on right
    now: its bonding curve before graduation, the PumpSwap pool after."""
    if keypair is not None and keypair.address != wallet:
        raise BuybackError(f"the keypair is {keypair.address}, not the wallet {wallet}")
    plan = plan_for(rpc, mint, wallet, lot_lamports=lot_lamports, slippage_bps=slippage_bps,
                    also_burn_ui=also_burn_ui, priority_micro_lamports=priority_micro_lamports, choose=choose)
    return _execute(rpc, plan, keypair, send=send, sleep=sleep, confirm_timeout=confirm_timeout)


def plan_for(rpc, mint: str, wallet: str, *, lot_lamports: int = DEFAULT_LOT_LAMPORTS,
             slippage_bps: int = DEFAULT_SLIPPAGE_BPS, also_burn_ui: float = 0.0,
             priority_micro_lamports: int = 0, choose=random.choice) -> Plan:
    """Read which venue the coin is on and quote the lot there. A coin
    whose bonding curve is complete is bought on its PumpSwap pool; one
    still on the curve is bought with pump's own `buy` (`curvebuy`)."""
    from . import curvebuy  # imports this module; resolved lazily
    # The pool exists exactly when the coin has graduated: pump's `migrate`
    # creates it from the completed curve and nothing else does.
    if rpc.accounts([canonical_pool(mint)])[0] is not None:
        state = observe(rpc, mint, wallet)
        also_burn = int(round(also_burn_ui * 10 ** state.decimals))
        return plan_buy_and_burn(state, lot_lamports=lot_lamports, slippage_bps=slippage_bps, also_burn=also_burn,
                                 priority_micro_lamports=priority_micro_lamports, choose=choose)
    state = curvebuy.observe(rpc, mint, wallet)
    also_burn = int(round(also_burn_ui * 10 ** state.decimals))
    return curvebuy.plan_buy_and_burn(state, lot_lamports=lot_lamports, slippage_bps=slippage_bps,
                                      also_burn=also_burn, priority_micro_lamports=priority_micro_lamports,
                                      choose=choose)


def burn_once(rpc, mint: str, wallet: str, keypair=None, *, amount_ui: float, send: bool = False,
              sleep=time.sleep, confirm_timeout: float = 90.0) -> dict:
    """One plain burn of held tokens."""
    if keypair is not None and keypair.address != wallet:
        raise BuybackError(f"the keypair is {keypair.address}, not the wallet {wallet}")
    state = observe(rpc, mint, wallet)
    plan = plan_burn(state, int(round(amount_ui * 10 ** state.decimals)))
    return _execute(rpc, plan, keypair, send=send, sleep=sleep, confirm_timeout=confirm_timeout)


MAX_CONSECUTIVE_FAILURES = 5


def run_keeper(rpc, mint: str, keypair, *, lot_lamports: int, slippage_bps: int, every_seconds: float,
               max_total_lamports: int | None, log, sleep=time.sleep, choose=random.choice,
               max_cranks: int | None = None, also_burn_ui: float = 0.0, priority_micro_lamports: int = 0,
               confirm_timeout: float = 90.0) -> dict:
    """PROTOCOL.md sec.5 option 3, an operator keeper: a fixed lot every
    `every_seconds`, until a SOL budget or a crank count is reached. Each
    crank writes one JSON line through `log`; a run that stops says why.

    Failures are counted, not retried in a tight loop: a crank that fails
    waits out the interval like a crank that landed, and
    `MAX_CONSECUTIVE_FAILURES` in a row stops the keeper -- a pool or an RPC
    that keeps refusing is something a person should look at, not something
    a loop should keep paying to poke.
    """
    spent = 0
    burned = 0
    cranks = 0
    failures = 0
    reason = None
    while True:
        if max_total_lamports is not None and spent >= max_total_lamports:
            reason = "budget reached"
            break
        if max_cranks is not None and cranks >= max_cranks:
            reason = "crank count reached"
            break
        if failures >= MAX_CONSECUTIVE_FAILURES:
            reason = f"{MAX_CONSECUTIVE_FAILURES} consecutive failures"
            break
        if cranks or failures:
            sleep(every_seconds)
        try:
            result = crank_once(rpc, mint, keypair.address, keypair, lot_lamports=lot_lamports, slippage_bps=slippage_bps,
                                also_burn_ui=also_burn_ui, priority_micro_lamports=priority_micro_lamports, send=True,
                                choose=choose, sleep=sleep, confirm_timeout=confirm_timeout)
        except Exception as exc:  # noqa: BLE001 -- every failure is logged, none is silent
            failures += 1
            log(json.dumps({"at": int(time.time()), "ok": False, "error": f"{type(exc).__name__}: {exc}"}, sort_keys=True))
            continue
        if not result["sent"]:
            failures += 1
            log(json.dumps({"at": int(time.time()), "ok": False, "error": result.get("error"),
                            "simulation": result["simulation"]}, sort_keys=True))
            continue
        failures = 0
        cranks += 1
        spent += result["plan"]["expected_cost"]["total"]
        burned += result["recorded"]["tokens_burned"]
        log(json.dumps({
            "at": int(time.time()), "ok": True, "signature": result["signature"],
            "spent_lamports_max": result["plan"]["expected_cost"]["total"],
            "tokens_burned": result["recorded"]["tokens_burned"], "atomic": result["recorded"]["atomic"],
            "impact_bps": result["plan"]["impact_bps"], "cranks": cranks, "spent_total_max": spent,
        }, sort_keys=True))
    return {"cranks": cranks, "spent_lamports": spent, "tokens_burned": burned, "stopped_because": reason}

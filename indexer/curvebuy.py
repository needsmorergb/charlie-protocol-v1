"""The BURN leg on a coin that is still on its bonding curve.

`buyback.py` runs SOL -> buy -> SPL burn on the PumpSwap pool a coin gets
when it graduates. Most of a coin's life -- and all of it for the 97% that
never graduate -- is spent before that, on pump's bonding curve, where the
same leg is pump's `buy_v2`: exact tokens out for at most `max_sol_cost`,
paid in wrapped SOL from the buyer's own token account. This module builds
that buy from the deployed program's own declaration and burns what it
bought in the same transaction, so both land or neither does.

Why `buy_v2` and not `buy`: the legacy `buy` answered 6062
BuybackFeeRecipientMissing on mainnet (2026-09-05). pump now routes part of
its protocol fee to a buyback recipient, and only `buy_v2` names one. The
deploy repository's `probe_curve_buy` resolved `buy_v2`'s 27 accounts from
the on-chain IDL's own seeds and mainnet accepted the transaction; the
account list below is that one, in that order, and the tests pin the
addresses it printed for a real coin.

The arithmetic is pump's: for `amount` tokens the curve charges
`amount * virtual_sol / (virtual_token - amount) + 1` lamports, then the
protocol and creator fees on top, each rounded up. The fee rate comes from
the fee program's config for pump (the same market-cap tiers the AMM uses,
read off the chain), falling back to `Global`'s flat rates if that config
cannot be read. `max_sol_cost` is the lot itself, so a curve that moves
between quoting and landing fails the transaction rather than overpaying,
and whatever the buy does not spend comes back when the wSOL account is
closed at the end.

Nothing here signs. `buyback._execute` simulates and, with a keypair and
`--send`, sends; `buyback.plan_for` picks this venue whenever the coin has
not graduated.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from . import pump
from .base58 import encode, pubkey_bytes
from .buyback import (
    ASSOCIATED_TOKEN_PROGRAM,
    BPS,
    DEFAULT_COMPUTE_UNITS,
    DEFAULT_LOT_LAMPORTS,
    DEFAULT_PUBKEY,
    DEFAULT_SLIPPAGE_BPS,
    DISC_BUY,
    FEE_PROGRAM,
    LAMPORTS_PER_SOL,
    MIN_LOT_LAMPORTS,
    RESERVE_LAMPORTS,
    WSOL_MINT,
    BuybackError,
    FeeConfig,
    Fees,
    Plan,
    _ceil_div,
    _fee,
    _pda,
    _ui,
    decode_fee_config,
    decode_token_amount,
    fee_tier,
    ix_burn,
    ix_compute_unit_limit,
    ix_compute_unit_price,
    ix_close_account,
    ix_create_ata_idempotent,
    ix_sync_native,
    ix_system_transfer,
)
from .enroll import associated_token_address, sharing_config_address
from .message import Instruction
from .pump import DISC_BONDING_CURVE, PUMP_PROGRAM, SYSTEM_PROGRAM, TOKEN_PROGRAM, _raw, read_mint

# `Global` is `[b"global"]` under pump; the volume accumulators and the event
# authority are the PDAs the IDL spells out for `buy`. The fee program's
# config for pump is seeded with pump's own program id (the IDL's 32
# constant bytes decode to it), as the AMM's is with the AMM's.
GLOBAL = _pda([b"global"], PUMP_PROGRAM)
EVENT_AUTHORITY = _pda([b"__event_authority"], PUMP_PROGRAM)
GLOBAL_VOLUME_ACCUMULATOR = _pda([b"global_volume_accumulator"], PUMP_PROGRAM)
PUMP_FEE_CONFIG = _pda([b"fee_config", pubkey_bytes(PUMP_PROGRAM)], FEE_PROGRAM)

# `Global` field offsets, disc included: initialized u8 | authority |
# fee_recipient | 5x u64 (initial reserves, supply, fee_basis_points) |
# withdraw_authority | enable_migrate u8 | pool_migration_fee u64 |
# creator_fee_basis_points u64 | ...
_GLOBAL_FEE_RECIPIENT = 41
_GLOBAL_FEE_BPS = 105
_GLOBAL_CREATOR_FEE_BPS = 154
_GLOBAL_MIN_BYTES = 162
# ... fee_recipients [7] to 386 | set_creator_authority | admin_set_creator_
# authority | create_v2_enabled u8 | whitelist_pda | reserved_fee_recipient |
# mayhem_mode_enabled u8 | reserved_fee_recipients [7] | is_cashback_enabled
# u8 | buyback_fee_recipients [8] | buyback_basis_points u64
_GLOBAL_BUYBACK_RECIPIENTS = 741
_GLOBAL_BUYBACK_BPS = 997


# sha256("global:buy_v2")[:8], from pump's on-chain IDL. Args: amount u64,
# max_sol_cost u64. No OptionBool, unlike the legacy `buy`.
BUY_V2 = bytes.fromhex("b817ee6167c5d33d")

# sha256("global:init_user_volume_accumulator")[:8], from the same IDL. Its
# accounts: payer (signer, writable), user, the accumulator PDA (writable),
# the system program, the event authority, the program. No args.
INIT_USER_VOLUME_ACCUMULATOR = bytes.fromhex("5e06ca73ff60e8b7")


def user_volume_accumulator(user: str) -> str:
    return _pda([b"user_volume_accumulator", pubkey_bytes(user)], PUMP_PROGRAM)


def ix_init_user_volume_accumulator(user: str) -> Instruction:
    """A wallet that has never traded on pump has no volume accumulator, and
    `buy` names one as writable. Created once, by the wallet, for rent."""
    return (PUMP_PROGRAM, [
        (user, True, True),
        (user, False, False),
        (user_volume_accumulator(user), False, True),
        (SYSTEM_PROGRAM, False, False),
        (EVENT_AUTHORITY, False, False),
        (PUMP_PROGRAM, False, False),
    ], INIT_USER_VOLUME_ACCUMULATOR)


def creator_vault(creator: str) -> str:
    return _pda([b"creator-vault", pubkey_bytes(creator)], PUMP_PROGRAM)


@dataclass(frozen=True)
class Curve:
    """`BondingCurve` in full: disc 8 | virtual_token u64 | virtual_sol u64 |
    real_token u64 | real_sol u64 | total_supply u64 | complete u8 |
    creator 32 | is_mayhem_mode u8 | is_cashback_coin u8 | quote_mint 32.
    The last three fields were appended over time and read as their
    defaults when absent."""

    address: str
    virtual_token: int
    virtual_sol: int
    real_token: int
    real_sol: int
    total_supply: int
    complete: bool
    creator: str
    mayhem: bool
    quote_mint: str | None

    @property
    def sol_quoted(self) -> bool:
        return self.quote_mint in (None, DEFAULT_PUBKEY, WSOL_MINT)

    @property
    def market_cap_lamports(self) -> int:
        return self.virtual_sol * self.total_supply // self.virtual_token if self.virtual_token else 0


def decode_curve(address: str, account: dict | None) -> Curve:
    data = _raw(account, DISC_BONDING_CURVE, "bonding curve", (PUMP_PROGRAM,))
    if len(data) < 81:
        raise pump.DecodeError(f"{address}: bonding curve is {len(data)} bytes, expected at least 81")
    u64 = lambda at: int.from_bytes(data[at:at + 8], "little")  # noqa: E731
    return Curve(
        address=address,
        virtual_token=u64(8),
        virtual_sol=u64(16),
        real_token=u64(24),
        real_sol=u64(32),
        total_supply=u64(40),
        complete=bool(data[48]),
        creator=encode(data[49:81]),
        mayhem=bool(data[81]) if len(data) > 81 else False,
        quote_mint=encode(data[83:115]) if len(data) >= 115 else None,
    )


@dataclass(frozen=True)
class Global:
    fee_recipient: str
    fee_bps: int
    creator_fee_bps: int
    # The wallets pump's buyback share of the protocol fee may be paid to;
    # `buy_v2` names one. 5000 bps of the protocol fee on 2026-09-05.
    buyback_fee_recipients: tuple = ()
    buyback_bps: int = 0


def decode_global(account: dict | None) -> Global:
    data = _raw(account, b"", "pump global", (PUMP_PROGRAM,))
    if len(data) < _GLOBAL_MIN_BYTES:
        raise pump.DecodeError(f"pump global is {len(data)} bytes, expected at least {_GLOBAL_MIN_BYTES}")
    recipients = ()
    buyback_bps = 0
    if len(data) >= _GLOBAL_BUYBACK_BPS + 8:
        at = _GLOBAL_BUYBACK_RECIPIENTS
        recipients = tuple(
            r for r in (encode(data[at + 32 * i:at + 32 * (i + 1)]) for i in range(8)) if r != DEFAULT_PUBKEY
        )
        buyback_bps = int.from_bytes(data[_GLOBAL_BUYBACK_BPS:_GLOBAL_BUYBACK_BPS + 8], "little")
    return Global(
        fee_recipient=encode(data[_GLOBAL_FEE_RECIPIENT:_GLOBAL_FEE_RECIPIENT + 32]),
        fee_bps=int.from_bytes(data[_GLOBAL_FEE_BPS:_GLOBAL_FEE_BPS + 8], "little"),
        creator_fee_bps=int.from_bytes(data[_GLOBAL_CREATOR_FEE_BPS:_GLOBAL_CREATOR_FEE_BPS + 8], "little"),
        buyback_fee_recipients=recipients,
        buyback_bps=buyback_bps,
    )


@dataclass(frozen=True)
class CurveState:
    mint: str
    user: str
    decimals: int
    supply: int
    token_program: str
    mint_authority: str | None
    curve: Curve
    global_: Global
    fee_config: FeeConfig | None
    user_lamports: int
    user_base_balance: int | None
    user_volume_exists: bool = True

    @property
    def fees(self) -> Fees:
        if self.fee_config is None:
            return Fees(0, self.global_.fee_bps, self.global_.creator_fee_bps)
        return fee_tier(self.fee_config.fee_tiers, self.curve.market_cap_lamports)

    @property
    def charge_creator(self) -> bool:
        return self.curve.creator != DEFAULT_PUBKEY


def observe(rpc, mint: str, user: str) -> CurveState:
    """Everything a curve plan needs, in two round trips, checked the way
    `pump.py` checks before a byte is trusted."""
    mint_state = read_mint(rpc, mint)
    curve_address = pump.bonding_curve(mint)
    first = rpc.accounts([curve_address, GLOBAL, PUMP_FEE_CONFIG, user])
    if first[0] is None:
        raise BuybackError(f"{mint} has no bonding curve at {curve_address}: not a pump.fun coin")
    curve = decode_curve(curve_address, first[0])
    if curve.complete:
        raise BuybackError(f"{mint} has graduated: its bonding curve is complete, and the buy is on the pump AMM")
    if not curve.sol_quoted:
        raise BuybackError(f"{mint} is quoted in {curve.quote_mint}, not SOL: not bought by this keeper")
    if curve.mayhem:
        raise BuybackError(f"{mint} is in pump's mayhem mode, which routes fees differently: not bought by this keeper")
    global_ = decode_global(first[1])
    if not global_.buyback_fee_recipients:
        raise BuybackError("pump's global names no buyback fee recipient, and buy_v2 requires one")
    fee_config = decode_fee_config(first[2]) if first[2] is not None else None
    user_lamports = int((first[3] or {}).get("lamports") or 0)
    user_ata = associated_token_address(user, mint, mint_state.program)
    held, accumulator = rpc.accounts([user_ata, user_volume_accumulator(user)])
    return CurveState(
        mint=mint,
        user=user,
        decimals=mint_state.decimals,
        supply=mint_state.supply,
        token_program=mint_state.program,
        mint_authority=mint_state.mint_authority,
        curve=curve,
        global_=global_,
        fee_config=fee_config,
        user_lamports=user_lamports,
        user_base_balance=decode_token_amount(held, expect_mint=mint),
        user_volume_exists=accumulator is not None,
    )


# -- pump's arithmetic ---------------------------------------------------------
def cost_of(amount: int, curve: Curve, fees: Fees, charge_creator: bool) -> dict:
    """What `buy(amount, ...)` charges on the curve: the constant-product
    quote plus one lamport, then each fee rounded up on top. `lp_fee` is
    kept in the dict so `render` reads both venues alike; the curve has no
    LP, and the tier's lp rate is counted only as headroom."""
    if amount <= 0 or amount >= curve.virtual_token:
        raise BuybackError("amount must be positive and below the curve's virtual token reserve")
    sol_in = amount * curve.virtual_sol // (curve.virtual_token - amount) + 1
    lp = _fee(sol_in, fees.lp_bps)
    protocol = _fee(sol_in, fees.protocol_bps)
    creator = _fee(sol_in, fees.creator_bps) if charge_creator else 0
    return {"quote_in": sol_in, "lp_fee": lp, "protocol_fee": protocol, "creator_fee": creator,
            "total": sol_in + lp + protocol + creator}


def amount_for_lot(lot: int, curve: Curve, fees: Fees, charge_creator: bool, slippage_bps: int) -> int:
    """The most tokens whose cost fits the lot, capped at what the curve
    still holds, then reduced by `slippage_bps` so the buy still fits if the
    curve moves against it before landing."""
    if not 0 <= slippage_bps < BPS:
        raise BuybackError("slippage must be between 0 and 9999 bps")
    total_bps = fees.lp_bps + fees.protocol_bps + (fees.creator_bps if charge_creator else 0)
    effective = lot * BPS // (BPS + total_bps) - 1
    if effective <= 1:
        raise BuybackError("the lot is too small to buy anything after fees")
    amount = curve.virtual_token * effective // (curve.virtual_sol + effective)
    amount = min(amount, curve.real_token)
    while amount > 0 and cost_of(amount, curve, fees, charge_creator)["total"] > lot:
        amount -= max(1, amount // 10_000)
    amount = amount * (BPS - slippage_bps) // BPS
    if amount <= 0:
        raise BuybackError("the lot is too small to buy anything after fees and slippage")
    return amount


# -- the instruction -------------------------------------------------------------
def buy_accounts(mint: str, user: str, creator: str, base_token_program: str, fee_recipient: str,
                 buyback_fee_recipient: str, quote_mint: str = WSOL_MINT) -> list:
    """`buy_v2`'s 27 accounts in the IDL's order, as the deploy repository's
    probe resolved them from the on-chain seeds and mainnet accepted. The
    only signer is the user. Every wSOL account is the owner's associated
    token account for the quote mint under the classic token program; the
    coin's own token accounts follow the mint's program, which for a
    create_v2 coin is Token-2022."""
    curve = pump.bonding_curve(mint)
    vault = creator_vault(creator)
    accumulator = user_volume_accumulator(user)
    wsol = lambda owner: associated_token_address(owner, quote_mint, TOKEN_PROGRAM)  # noqa: E731
    return [
        (GLOBAL, False, False),
        (mint, False, False),
        (quote_mint, False, False),
        (base_token_program, False, False),
        (TOKEN_PROGRAM, False, False),
        (ASSOCIATED_TOKEN_PROGRAM, False, False),
        (fee_recipient, False, True),
        (wsol(fee_recipient), False, True),
        (buyback_fee_recipient, False, True),
        (wsol(buyback_fee_recipient), False, True),
        (curve, False, True),
        (associated_token_address(curve, mint, base_token_program), False, True),
        (wsol(curve), False, True),
        (user, True, True),
        (associated_token_address(user, mint, base_token_program), False, True),
        (wsol(user), False, True),
        (vault, False, True),
        (wsol(vault), False, True),
        (sharing_config_address(mint), False, False),
        (GLOBAL_VOLUME_ACCUMULATOR, False, False),
        (accumulator, False, True),
        (wsol(accumulator), False, True),
        (PUMP_FEE_CONFIG, False, False),
        (FEE_PROGRAM, False, False),
        (SYSTEM_PROGRAM, False, False),
        (EVENT_AUTHORITY, False, False),
        (PUMP_PROGRAM, False, False),
    ]


def buy_data(amount: int, max_sol_cost: int) -> bytes:
    """`discriminator || amount u64 || max_sol_cost u64`."""
    return BUY_V2 + amount.to_bytes(8, "little") + max_sol_cost.to_bytes(8, "little")


def ix_buy(state: CurveState, amount: int, max_sol_cost: int, buyback_fee_recipient: str) -> Instruction:
    return (PUMP_PROGRAM,
            buy_accounts(state.mint, state.user, state.curve.creator, state.token_program,
                         state.global_.fee_recipient, buyback_fee_recipient),
            buy_data(amount, max_sol_cost))


# -- the plan ----------------------------------------------------------------------
def plan_buy_and_burn(state: CurveState, *, lot_lamports: int = DEFAULT_LOT_LAMPORTS,
                      slippage_bps: int = DEFAULT_SLIPPAGE_BPS, also_burn: int = 0,
                      priority_micro_lamports: int = 0, compute_units: int = DEFAULT_COMPUTE_UNITS,
                      choose=random.choice) -> Plan:
    """Quote the lot against the curve as it reads right now and lay out the
    instructions: the token accounts, the lot wrapped into wSOL, buy exactly
    `amount` for at most the lot, burn `amount + also_burn`, unwrap what is
    left. One transaction, all or nothing."""
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
    curve, fees = state.curve, state.fees
    if curve.real_token <= 0:
        raise BuybackError(f"{state.mint}'s curve holds no tokens to sell")
    amount = amount_for_lot(lot_lamports, curve, fees, state.charge_creator, slippage_bps)
    cost = cost_of(amount, curve, fees, state.charge_creator)
    price_before = curve.virtual_sol / curve.virtual_token
    price_after = (curve.virtual_sol + cost["quote_in"]) / (curve.virtual_token - amount)
    scale = 10 ** state.decimals / LAMPORTS_PER_SOL
    burn_total = amount + also_burn
    user_ata = associated_token_address(state.user, state.mint, state.token_program)
    user_wsol = associated_token_address(state.user, WSOL_MINT, TOKEN_PROGRAM)
    buyback_fee_recipient = choose(list(state.global_.buyback_fee_recipients))

    instructions: list[Instruction] = [ix_compute_unit_limit(compute_units)]
    if priority_micro_lamports:
        instructions.append(ix_compute_unit_price(priority_micro_lamports))
    if not state.user_volume_exists:
        instructions.append(ix_init_user_volume_accumulator(state.user))
    instructions += [
        ix_create_ata_idempotent(state.user, user_ata, state.user, state.mint, state.token_program),
        ix_create_ata_idempotent(state.user, user_wsol, state.user, WSOL_MINT, TOKEN_PROGRAM),
        ix_system_transfer(state.user, user_wsol, lot_lamports),
        ix_sync_native(user_wsol),
        ix_buy(state, amount, lot_lamports, buyback_fee_recipient),
        ix_burn(state.token_program, user_ata, state.mint, state.user, burn_total),
        ix_close_account(user_wsol, state.user, state.user),
    ]
    notes = [
        "The buy and the burn are instructions in one transaction: both land or neither does (PROTOCOL.md sec.4).",
        f"max_sol_cost is the lot itself ({lot_lamports / LAMPORTS_PER_SOL} SOL). If the curve moves so the buy "
        "would cost more, the whole transaction fails rather than overpaying; whatever is unspent returns when the "
        "wSOL account is closed.",
        "This coin is on its bonding curve, not yet on the pump AMM: the buy is pump's own `buy_v2`, and the same "
        "keeper switches to the AMM the moment the coin graduates.",
        "The indexer records this as a third-party burn (source spl_burn, atomic PASS). It counts toward supply "
        "destroyed; it is not protocol-attributed, because no protocol program cranked it (D-10).",
    ]
    if state.charge_creator:
        notes.append(
            f"{cost['creator_fee'] / LAMPORTS_PER_SOL:.9f} SOL of this buy is the coin's creator fee, paid into "
            f"the vault of {curve.creator}. Where it goes from there is that coin's fee split."
        )
    if state.mint_authority is not None:
        notes.append(f"WARNING: mint authority {state.mint_authority} is live -- burned supply can be reissued (BURN_IRREVERSIBLE fails).")
    if also_burn:
        notes.append(
            f"{_ui(also_burn, state.decimals):,.{state.decimals}f} tokens already held are burned in the same transaction. "
            "That reduces supply; it is not a buy and moves no price."
        )
    if not state.user_volume_exists:
        notes.append("This wallet has never traded on pump: its volume accumulator is created first, once, for rent.")
    return Plan(
        kind="curve_buy_and_burn",
        mint=state.mint,
        user=state.user,
        decimals=state.decimals,
        lot_lamports=lot_lamports,
        base_out=amount,
        also_burn=also_burn,
        burn_total=burn_total,
        expected_cost=cost,
        fees={"lp_bps": fees.lp_bps, "protocol_bps": fees.protocol_bps,
              "creator_bps": fees.creator_bps if state.charge_creator else 0,
              "total_bps": fees.lp_bps + fees.protocol_bps + (fees.creator_bps if state.charge_creator else 0)},
        market_cap_lamports=curve.market_cap_lamports,
        base_reserve=curve.virtual_token,
        quote_reserve=curve.virtual_sol,
        price_before=price_before * scale,
        price_after=price_after * scale,
        impact_bps=round((price_after / price_before - 1) * BPS),
        supply_before=state.supply,
        supply_after=state.supply - burn_total,
        supply_reduction_bps=burn_total / state.supply * BPS,
        notes=notes,
        instructions=instructions,
        accounts={
            "bonding_curve": curve.address,
            "creator": curve.creator,
            "creator_vault": creator_vault(curve.creator),
            "fee_recipient": state.global_.fee_recipient,
            "buyback_fee_recipient": buyback_fee_recipient,
            "user_token_account": user_ata,
            "user_wsol_account": user_wsol,
            "token_program": state.token_program,
        },
    )


# -- which venue -------------------------------------------------------------------
def graduated(rpc, mint: str) -> bool:
    """Whether the coin's bonding curve is complete, which decides the
    venue: the AMM after graduation, the curve before it."""
    account = rpc.accounts([pump.bonding_curve(mint)])[0]
    if account is None:
        raise BuybackError(f"{mint} has no bonding curve: not a pump.fun coin")
    return decode_curve(pump.bonding_curve(mint), account).complete

"""The dev's buy at launch: pump's `buy` in the same transaction as `create`.

pump's own page bundles the creator's first purchase into the create
transaction, and for the same reason this does: the coin exists and the dev
holds tokens in one atomic step, so nobody can trade between the two. The
coin's curve is brand new at that moment, so its reserves are exactly the
`Global` account's initial values and the price is known before anything
exists; `quote` prices from those.

This is the LEGACY `buy` (sixteen accounts, SOL paid straight from the
wallet), not the wrapped-SOL `buy_v2` the buyback crank uses on older coins:
it needs no wSOL account, and nine of its accounts are already in `create`,
which is what keeps transaction 1 inside one packet. Proven against mainnet
by simulation; the measured size is pinned in tests.

It happens BEFORE the split is set (that is transaction 2), so the creator
fee on this buy goes where pump sends it by default, not to the split. The
page says so.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from . import curvebuy
from .buyback import DEFAULT_PUBKEY, FEE_PROGRAM, BuybackError, FeeConfig, Fees, fee_tier, ix_create_ata_idempotent
from .base58 import pubkey_bytes
from .enroll import associated_token_address, bonding_curve_address
from .message import Instruction
from .pump import PUMP_PROGRAM, SYSTEM_PROGRAM, TOKEN_PROGRAM

# sha256("global:buy")[:8]; the IDL spells it [102, 6, 61, 18, 1, 218, 235, 234].
BUY = hashlib.sha256(b"global:buy").digest()[:8]

LAMPORTS_PER_SOL = 1_000_000_000
# What the curve holds before graduation is about 85 SOL; a dev buy beyond
# that cannot fill. The cap is on the input so the refusal is plain.
MAX_BUY_LAMPORTS = 85 * LAMPORTS_PER_SOL
MIN_BUY_LAMPORTS = 1_000_000  # 0.001 SOL: below this fees eat the lot
# The curve cannot move before this buy lands (it is in the create
# transaction), so slippage only covers a fee-tier edit between the dry run
# and the signature. 1%.
SLIPPAGE_BPS = 100


class BuyError(ValueError):
    """A dev buy that cannot be built, phrased for the dev."""


def parse_sol(text: str) -> int:
    """'0.5' -> 500_000_000 lamports. Up to nine decimals, within the bounds."""
    raw = (text or "").strip()
    if not raw:
        raise BuyError("Enter the SOL to buy with, or leave it empty.")
    whole, dot, frac = raw.partition(".")
    if not (whole.isdecimal() or (whole == "" and frac)) or (frac and not frac.isdecimal()) or len(frac) > 9:
        raise BuyError("The buy amount must be a number of SOL with at most nine decimals.")
    lamports = int(whole or "0") * LAMPORTS_PER_SOL + int((frac + "000000000")[:9]) if dot else int(whole) * LAMPORTS_PER_SOL
    if lamports < MIN_BUY_LAMPORTS:
        raise BuyError("The buy must be at least 0.001 SOL.")
    if lamports > MAX_BUY_LAMPORTS:
        raise BuyError("The buy must be at most 85 SOL: that is about what a new curve holds.")
    return lamports


@dataclass(frozen=True)
class Quote:
    lamports: int         # what the dev put in
    max_sol_cost: int     # the bound the instruction carries: the lot
    tokens: int           # base units the buy asks for
    decimals: int
    total_supply: int
    cost: dict            # curvebuy.cost_of's breakdown at `tokens`
    fees: Fees

    @property
    def percent_of_supply(self) -> float:
        return self.tokens * 100 / self.total_supply if self.total_supply else 0.0


def fresh_curve(global_: curvebuy.Global, mint: str, creator: str) -> curvebuy.Curve:
    """The bonding curve as `create` leaves it: the global's initial
    reserves, nothing real yet, the dev as creator."""
    return curvebuy.Curve(
        address=bonding_curve_address(mint),
        virtual_token=global_.initial_virtual_token,
        virtual_sol=global_.initial_virtual_sol,
        real_token=global_.initial_real_token,
        real_sol=0,
        total_supply=global_.total_supply,
        complete=False,
        creator=creator,
        mayhem=False,
        quote_mint=None,
    )


def quote(lamports: int, global_: curvebuy.Global, fee_config: FeeConfig | None, mint: str, creator: str,
          decimals: int = 6) -> Quote:
    """The most tokens `lamports` buys on a fresh curve, fees included, less
    1% of tokens for safety. The bound sent on chain is the lot itself: the
    dev never pays more than they typed."""
    curve = fresh_curve(global_, mint, creator)
    if fee_config is None:
        fees = Fees(0, global_.fee_bps, global_.creator_fee_bps)
    else:
        fees = fee_tier(fee_config.fee_tiers, curve.market_cap_lamports)
    charge_creator = creator != DEFAULT_PUBKEY
    try:
        tokens = curvebuy.amount_for_lot(lamports, curve, fees, charge_creator, SLIPPAGE_BPS)
        cost = curvebuy.cost_of(tokens, curve, fees, charge_creator)
    except BuybackError as exc:
        raise BuyError(str(exc)) from None
    return Quote(lamports=lamports, max_sol_cost=lamports, tokens=tokens, decimals=decimals,
                 total_supply=curve.total_supply, cost=cost, fees=fees)


# -- the instruction ---------------------------------------------------------------


def bonding_curve_v2_address(mint: str) -> str:
    """`["bonding-curve-v2", mint]` under pump. Not in the IDL's account list
    for `buy`; the program reads it from the remaining accounts, and refuses
    with 6074 when it is absent. Read from mainnet transactions 2026-09-16."""
    return curvebuy._pda([b"bonding-curve-v2", pubkey_bytes(mint)], PUMP_PROGRAM)


def buy_accounts(mint: str, user: str, creator: str, fee_recipient: str, buyback_fee_recipient: str) -> list:
    """`buy`'s sixteen accounts in the IDL's order, then the two the program
    takes as remaining accounts and the IDL does not list: the coin's
    bonding-curve-v2 PDA (read) and one of the global's buyback fee
    recipients (written). Both learned from what pump's own create-and-buy
    transactions carry, and proven by simulation: without the first the
    program fails 6074, without the second 6062. The user is the only
    signer. `creator_vault` is seeded with the curve's creator, which
    `create` sets to the dev in the same transaction."""
    curve = bonding_curve_address(mint)
    return [
        (curvebuy.GLOBAL, False, False),
        (fee_recipient, False, True),
        (mint, False, False),
        (curve, False, True),
        (associated_token_address(curve, mint, TOKEN_PROGRAM), False, True),
        (associated_token_address(user, mint, TOKEN_PROGRAM), False, True),
        (user, True, True),
        (SYSTEM_PROGRAM, False, False),
        (TOKEN_PROGRAM, False, False),
        (curvebuy.creator_vault(creator), False, True),
        (curvebuy.EVENT_AUTHORITY, False, False),
        (PUMP_PROGRAM, False, False),
        (curvebuy.GLOBAL_VOLUME_ACCUMULATOR, False, False),
        (curvebuy.user_volume_accumulator(user), False, True),
        (curvebuy.PUMP_FEE_CONFIG, False, False),
        (FEE_PROGRAM, False, False),
        (bonding_curve_v2_address(mint), False, False),
        (buyback_fee_recipient, False, True),
    ]


def buy_data(amount: int, max_sol_cost: int, track_volume: bool = True) -> bytes:
    """`disc || amount u64 || max_sol_cost u64 || OptionBool`. The IDL's
    OptionBool is a struct of one bool: one byte."""
    return BUY + amount.to_bytes(8, "little") + max_sol_cost.to_bytes(8, "little") + bytes([1 if track_volume else 0])


def instructions(mint: str, dev: str, global_: curvebuy.Global, q: Quote, *, accumulator_exists: bool) -> list[Instruction]:
    """What follows `create` in transaction 1: the dev's token account
    (idempotent), the volume accumulator if this wallet has never traded
    on pump, then the buy."""
    out: list[Instruction] = [
        ix_create_ata_idempotent(dev, associated_token_address(dev, mint, TOKEN_PROGRAM), dev, mint, TOKEN_PROGRAM),
    ]
    if not accumulator_exists:
        out.append(curvebuy.ix_init_user_volume_accumulator(dev))
    if not global_.buyback_fee_recipients:
        raise BuyError("pump's global names no buyback fee recipient, which its buy requires.")
    out.append((PUMP_PROGRAM,
                buy_accounts(mint, dev, dev, global_.fee_recipient, global_.buyback_fee_recipients[0]),
                buy_data(q.tokens, q.max_sol_cost)))
    return out


def observe(rpc, dev: str) -> tuple[curvebuy.Global, FeeConfig | None, bool]:
    """One round trip: pump's global, its fee config, and whether the dev's
    volume accumulator exists."""
    global_account, fee_account, accumulator = rpc.accounts(
        [curvebuy.GLOBAL, curvebuy.PUMP_FEE_CONFIG, curvebuy.user_volume_accumulator(dev)])
    global_ = curvebuy.decode_global(global_account)
    fee_config = curvebuy.decode_fee_config(fee_account) if fee_account is not None else None
    return global_, fee_config, accumulator is not None


def describe(q: Quote) -> dict:
    """What the page shows: the bound, the tokens, the share, the fees."""
    return {
        "sol": q.lamports / LAMPORTS_PER_SOL,
        "max_sol_cost_lamports": q.max_sol_cost,
        "tokens": q.tokens / 10 ** q.decimals,
        "percent_of_supply": round(q.percent_of_supply, 2),
        "fee_lamports": q.cost["protocol_fee"] + q.cost["creator_fee"] + q.cost["lp_fee"],
        "before_split": True,
    }


__all__ = [
    "BUY", "BuyError", "MAX_BUY_LAMPORTS", "MIN_BUY_LAMPORTS", "Quote", "SLIPPAGE_BPS",
    "bonding_curve_v2_address", "buy_accounts", "buy_data", "describe", "fresh_curve", "instructions", "observe",
    "parse_sol", "quote",
]

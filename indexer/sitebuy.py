"""A holder's buy of any pump coin, built for the coin page to sign.

The coin page (`/coin/<mint>`) offers a Buy button. This builds the
transaction behind it: the buyer's own wallet pays, signs and receives the
tokens. Nothing here holds a key or sends; `api/buy.py` simulates what this
returns before the page ever sees it, and the page sends the signed bytes
through the door's relay (`relay.py`).

Two venues, chosen by the chain, not by the caller:

* **curve** -- the coin has not graduated. pump's LEGACY `buy`, the same
  instruction the launch door's dev buy uses (`launchbuy.buy_accounts`), SOL
  paid straight from the wallet. Priced from the LIVE curve's reserves, and
  its creator vault is seeded with the creator the curve names (for an
  enrolled coin that is its sharing config, so the creator fee of the buy
  lands in the split).
* **pumpswap** -- the curve is complete. PumpSwap's `buy` as the buyback
  crank builds it (`buyback.buy_accounts`), SOL wrapped into the buyer's
  wSOL account first and unwrapped after. Nothing is burned: the tokens stay
  in the buyer's own token account.

Both instructions are exact-out: `amount` tokens for at most `max_sol_cost`
lamports. So the slippage is on the SOL side, the way pump's own page does
it. The page's SOL figure buys `expected_tokens` at the price read now; the
transaction lets the buy cost up to `slippage_bps` more than that before it
fails whole. It never delivers fewer tokens than `min_tokens`, which for an
exact-out buy is the same number.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from . import buyback, curvebuy, launchbuy, pump
from .base58 import decode, encode
from .buyback import BPS, WSOL_MINT, BuybackError, Fees, fee_tier
from .enroll import associated_token_address
from .message import Instruction, compile_legacy
from .pump import TOKEN_2022_PROGRAM, TOKEN_PROGRAM

LAMPORTS_PER_SOL = 1_000_000_000
MIN_LAMPORTS = 1_000_000                  # 0.001 SOL
MAX_LAMPORTS = 100 * LAMPORTS_PER_SOL     # 100 SOL
DEFAULT_SLIPPAGE_BPS = 500
MIN_SLIPPAGE_BPS = 50
MAX_SLIPPAGE_BPS = 5000

# Measured by simulation on mainnet 2026-09-24, for a wallet with no token
# account yet: the curve buy used 94,115 units on an SPL coin (a new volume
# accumulator included) and 109,323 on a Token-2022 coin; the PumpSwap buy
# of $CHARLIE (Token-2022), wrap and unwrap included, used 108,620. The
# limits leave room for a pool that must be extended first. No priority fee.
CURVE_COMPUTE_UNITS = 150_000
AMM_COMPUTE_UNITS = 200_000

VENUE_CURVE = "curve"
VENUE_AMM = "pumpswap"


class BuyError(ValueError):
    """A buy this page cannot build, phrased for the buyer. `status` is the
    HTTP status `api/buy.py` answers with."""

    status = 400


class NotFound(BuyError):
    status = 404


class NotReady(BuyError):
    """The chain is between states (a coin mid-graduation). Ask again."""

    status = 503


# -- the inputs ----------------------------------------------------------------------


def parse_address(text: str, what: str) -> str:
    raw = (text or "").strip()
    try:
        decoded = decode(raw) if 32 <= len(raw) <= 44 else b""
    except Exception:  # noqa: BLE001 -- a character outside base58
        decoded = b""
    if len(decoded) != 32 or encode(decoded) != raw:
        raise BuyError(f"That is not a valid {what} address.")
    return raw


def parse_sol(text: str) -> int:
    """'0.5' -> 500_000_000 lamports. Up to nine decimals, 0.001 to 100 SOL."""
    raw = (text or "").strip()
    whole, dot, frac = raw.partition(".")
    if not raw or not (whole.isdecimal() or (whole == "" and frac)) or (dot and not frac.isdecimal()) \
            or len(frac) > 9 or not raw.isascii():
        raise BuyError("Enter the SOL to spend as a number, like 0.5.")
    lamports = int(whole or "0") * LAMPORTS_PER_SOL + (int((frac + "000000000")[:9]) if frac else 0)
    if lamports < MIN_LAMPORTS:
        raise BuyError("A buy is at least 0.001 SOL.")
    if lamports > MAX_LAMPORTS:
        raise BuyError("A buy from this page is at most 100 SOL.")
    return lamports


def parse_slippage(text: str) -> int:
    raw = (text or "").strip()
    if not raw:
        return DEFAULT_SLIPPAGE_BPS
    if not (raw.isascii() and raw.isdecimal()):
        raise BuyError("Slippage is a whole number of basis points, like 500 for 5%.")
    bps = int(raw)
    if not MIN_SLIPPAGE_BPS <= bps <= MAX_SLIPPAGE_BPS:
        raise BuyError("Slippage must be between 0.5% and 50% (50 to 5000 bps).")
    return bps


def max_cost(lamports: int, slippage_bps: int) -> int:
    """The most the buy may cost: the SOL typed, plus the slippage on it."""
    return lamports + lamports * slippage_bps // BPS


# -- the quote -------------------------------------------------------------------------


@dataclass
class Buy:
    venue: str
    mint: str
    buyer: str
    decimals: int
    token_program: str
    sol_lamports: int
    max_sol_lamports: int
    slippage_bps: int
    tokens: int                     # exact tokens out, raw units
    cost: dict                      # the venue's cost breakdown at `tokens`
    fees: Fees
    charge_creator: bool
    impact_bps: int
    instructions: list = field(default_factory=list)
    accounts: dict = field(default_factory=dict)

    def message(self, recent_blockhash: str) -> bytes:
        return compile_legacy(self.buyer, self.instructions, recent_blockhash)

    def describe(self) -> dict:
        scale = 10 ** self.decimals
        fee_bps = self.fees.lp_bps + self.fees.protocol_bps + (self.fees.creator_bps if self.charge_creator else 0)
        return {
            "venue": self.venue,
            "mint": self.mint,
            "wallet": self.buyer,
            "sol_lamports": self.sol_lamports,
            "max_sol_lamports": self.max_sol_lamports,
            "slippage_bps": self.slippage_bps,
            "expected_tokens": self.tokens / scale,
            "min_tokens": self.tokens / scale,
            "expected_tokens_raw": str(self.tokens),
            "min_tokens_raw": str(self.tokens),
            "decimals": self.decimals,
            "expected_cost_lamports": self.cost["total"],
            "fee_bps": fee_bps,
            "price_impact_bps": self.impact_bps,
            "token_program": self.token_program,
        }


def _impact(quote_before: int, base_before: int, quote_in: int, tokens: int) -> int:
    before = quote_before / base_before
    after = (quote_before + quote_in) / (base_before - tokens)
    return round((after / before - 1) * BPS)


def curve_buy(mint: str, buyer: str, mint_state, curve: curvebuy.Curve, global_: curvebuy.Global,
              fee_config, *, accumulator_exists: bool, lamports: int, slippage_bps: int,
              choose=random.choice) -> Buy:
    """pump's legacy `buy` on a live bonding curve."""
    if not curve.sol_quoted:
        raise BuyError("This coin's curve is quoted in another token, not SOL. Buying it here is not supported yet.")
    if curve.mayhem:
        raise BuyError("This coin is in pump's mayhem mode, which routes fees differently. Buying it here is not supported yet.")
    if curve.real_token <= 0:
        raise NotReady("This coin's curve has sold out and is graduating. Try again in a minute.")
    if not global_.buyback_fee_recipients:
        raise BuyError("pump's global names no buyback fee recipient, which its buy requires. Nothing was built.")
    fees = Fees(0, global_.fee_bps, global_.creator_fee_bps) if fee_config is None \
        else fee_tier(fee_config.fee_tiers, curve.market_cap_lamports)
    charge_creator = curve.creator != buyback.DEFAULT_PUBKEY
    try:
        tokens = curvebuy.amount_for_lot(lamports, curve, fees, charge_creator, 0)
        cost = curvebuy.cost_of(tokens, curve, fees, charge_creator)
    except BuybackError as exc:
        raise BuyError(f"This amount cannot buy anything on the curve: {exc}.") from None
    bound = max_cost(lamports, slippage_bps)
    program = mint_state.program
    user_ata = associated_token_address(buyer, mint, program)
    buyback_recipient = choose(list(global_.buyback_fee_recipients))
    instructions: list[Instruction] = [
        buyback.ix_compute_unit_limit(CURVE_COMPUTE_UNITS),
        buyback.ix_create_ata_idempotent(buyer, user_ata, buyer, mint, program),
    ]
    if not accumulator_exists:
        instructions.append(curvebuy.ix_init_user_volume_accumulator(buyer))
    instructions.append((pump.PUMP_PROGRAM,
                         launchbuy.buy_accounts(mint, buyer, curve.creator, global_.fee_recipient, buyback_recipient,
                                                token_program=program),
                         launchbuy.buy_data(tokens, bound)))
    return Buy(
        venue=VENUE_CURVE, mint=mint, buyer=buyer, decimals=mint_state.decimals, token_program=program,
        sol_lamports=lamports, max_sol_lamports=bound, slippage_bps=slippage_bps, tokens=tokens, cost=cost,
        fees=fees, charge_creator=charge_creator,
        impact_bps=_impact(curve.virtual_sol, curve.virtual_token, cost["quote_in"], tokens),
        instructions=instructions,
        accounts={"bonding_curve": curve.address, "creator": curve.creator,
                  "creator_vault": curvebuy.creator_vault(curve.creator), "user_token_account": user_ata,
                  "fee_recipient": global_.fee_recipient, "buyback_fee_recipient": buyback_recipient},
    )


def amm_buy(state: buyback.State, *, lamports: int, slippage_bps: int, choose=random.choice) -> Buy:
    """PumpSwap's `buy` for the buyer: wrap the bound into wSOL, buy exactly
    `tokens`, unwrap what the buy did not spend. No burn."""
    fees = state.fees
    charge_creator = state.pool.has_coin_creator
    try:
        tokens = buyback.base_out_for_lot(lamports, state.base_reserve, state.quote_reserve, fees, charge_creator, 0)
        cost = buyback.cost_of(tokens, state.base_reserve, state.quote_reserve, fees, charge_creator)
    except BuybackError as exc:
        raise BuyError(f"This amount cannot buy anything from the pool: {exc}.") from None
    bound = max_cost(lamports, slippage_bps)
    recipients = state.config.reserved_fee_recipients if state.pool.is_mayhem_mode \
        else state.config.protocol_fee_recipients
    recipients = [r for r in recipients if r != buyback.DEFAULT_PUBKEY]
    buybacks = [r for r in state.config.buyback_fee_recipients if r != buyback.DEFAULT_PUBKEY]
    if not recipients or not buybacks:
        raise BuyError("PumpSwap's config names no fee recipient for this pool. Nothing was built.")
    protocol_recipient = choose(recipients)
    buyback_recipient = choose(buybacks)
    user_ata = associated_token_address(state.user, state.mint, state.token_program)
    user_wsol = associated_token_address(state.user, WSOL_MINT, TOKEN_PROGRAM)
    instructions: list[Instruction] = [buyback.ix_compute_unit_limit(AMM_COMPUTE_UNITS)]
    if state.pool.data_len < buyback.POOL_ACCOUNT_NEW_SIZE:
        instructions.append(buyback.ix_extend_account(state.pool.address, state.user))
    instructions += [
        buyback.ix_create_ata_idempotent(state.user, user_ata, state.user, state.mint, state.token_program),
        buyback.ix_create_ata_idempotent(state.user, user_wsol, state.user, WSOL_MINT, TOKEN_PROGRAM),
        buyback.ix_system_transfer(state.user, user_wsol, bound),
        buyback.ix_sync_native(user_wsol),
        buyback.ix_buy(state.pool, state.user, tokens, bound, base_token_program=state.token_program,
                       protocol_fee_recipient=protocol_recipient, buyback_fee_recipient=buyback_recipient),
        buyback.ix_close_account(user_wsol, state.user, state.user),
    ]
    return Buy(
        venue=VENUE_AMM, mint=state.mint, buyer=state.user, decimals=state.decimals, token_program=state.token_program,
        sol_lamports=lamports, max_sol_lamports=bound, slippage_bps=slippage_bps, tokens=tokens, cost=cost,
        fees=fees, charge_creator=charge_creator,
        impact_bps=_impact(state.quote_reserve, state.base_reserve, cost["quote_in"], tokens),
        instructions=instructions,
        accounts={"pool": state.pool.address, "coin_creator": state.pool.coin_creator, "user_token_account": user_ata,
                  "user_wsol_account": user_wsol, "protocol_fee_recipient": protocol_recipient,
                  "buyback_fee_recipient": buyback_recipient},
    )


# -- reading the chain -------------------------------------------------------------------


def plan(rpc, mint: str, buyer: str, lamports: int, slippage_bps: int, *, choose=random.choice) -> Buy:
    """Read the coin, pick the venue, quote, and lay out the instructions."""
    curve_address = pump.bonding_curve(mint)
    mint_account, curve_account, global_account, fee_account, accumulator = rpc.accounts(
        [mint, curve_address, curvebuy.GLOBAL, curvebuy.PUMP_FEE_CONFIG, curvebuy.user_volume_accumulator(buyer)])
    if mint_account is None:
        raise NotFound("No token exists at that address.")
    try:
        mint_state = pump.decode_mint(mint, mint_account)
    except pump.DecodeError:
        raise BuyError("That address is not a token mint.") from None
    if curve_account is None:
        raise NotFound("This is not a pump.fun coin: it has no bonding curve. Buying it here is not supported.")
    curve = curvebuy.decode_curve(curve_address, curve_account)
    if curve.complete:
        try:
            state = buyback.observe(rpc, mint, buyer)
        except (BuybackError, pump.DecodeError):
            raise NotReady("This coin has graduated, and its PumpSwap pool is not open yet. Try again in a minute.") from None
        return amm_buy(state, lamports=lamports, slippage_bps=slippage_bps, choose=choose)
    global_ = curvebuy.decode_global(global_account)
    fee_config = buyback.decode_fee_config(fee_account) if fee_account is not None else None
    return curve_buy(mint, buyer, mint_state, curve, global_, fee_config, accumulator_exists=accumulator is not None,
                     lamports=lamports, slippage_bps=slippage_bps, choose=choose)


def explain(value: dict) -> str:
    """A failed simulation in words a buyer can act on. Nothing was sent."""
    logs = " ".join(value.get("logs") or [])
    err = value.get("err")
    lowered = logs.lower()
    if err == "AccountNotFound" or "insufficient lamports" in lowered or "insufficient funds" in lowered \
            or "BuyNotEnoughSol" in logs \
            or "Program 11111111111111111111111111111111 failed: custom program error: 0x1" in logs:
        return ("This wallet does not hold enough SOL for the buy, its slippage allowance, the token "
                "account rent (about 0.002 SOL) and the network fee. Nothing was sent.")
    if "TooMuchSolRequired" in logs or "ExceededSlippage" in logs or "SlippageExceeded" in logs:
        return "The price moved past your slippage while this was checked. Quote again, or allow more slippage."
    if "BondingCurveComplete" in logs:
        return "This coin graduated just now. Quote again to buy from its PumpSwap pool."
    return f"The buy failed in simulation ({err}). Nothing was sent."


__all__ = [
    "AMM_COMPUTE_UNITS", "Buy", "BuyError", "CURVE_COMPUTE_UNITS", "DEFAULT_SLIPPAGE_BPS", "MAX_LAMPORTS",
    "MAX_SLIPPAGE_BPS", "MIN_LAMPORTS", "MIN_SLIPPAGE_BPS", "NotFound", "NotReady", "VENUE_AMM", "VENUE_CURVE",
    "amm_buy", "curve_buy", "explain", "max_cost", "parse_address", "parse_slippage", "parse_sol", "plan",
]

"""The Charlie leg on the LaunchLab rail: claim the platform fee, end in SOL.

LAUNCHLAB-RAIL.md ships the rail as a Raydium platform with no program of
ours (decided 20 September 2026), so the platform fee accrues in
`platform_fee_vault(platform_config, quote)` and something has to spend it.
This is that something, and it stops at SOL: `indexer charlie-buyback` takes
it from there and buys and burns $CHARLIE in one transaction, which is the
atomicity PROTOCOL.md sec.4 actually requires.

WHY THIS IS THREE TRANSACTIONS AND NOT ONE. The four-leg design needed claim,
swap and burn in a single transaction because cranks 5 to 7 were meant to be
permissionless, and atomicity is what stops a caller profiting from picking a
bad route (sec.5). It never applied to this leg: `crank_platform` was already
keeper-only, "because its last hop buys $CHARLIE, which has no oracle". An
operator keeper is the trust position PROTOCOL.md sec.5 option 3 already
concedes, so the transactions can be separate -- and separating them retires
the whole of section 4. A multi-hop route is ~37 account locks against a limit
of 64; sharing that transaction with ~40 of ours was the problem, and not
sharing it is the answer. No `maxAccounts` juggling, no per-coin lookup table,
no Jito bundle.

WHAT IS CLAIMED HERE AND WHAT IS NOT. `claim_platform_fee_from_vault` takes no
arguments: it moves whatever the vault holds to the fee wallet's quote token
account. The fee wallet signs, and on this rail it is an ordinary wallet, which
is the same reason no program was needed to create the platform.

TIERS, in the order they are safe to run:

* **SOL quote** -- no swap at all. The fee arrives as wSOL; closing the token
  account unwraps it. Nothing here can get a price wrong because no price is
  consulted.
* **USDC or USDT quote** -- one Raydium CPMM hop to wSOL, with `min_out`
  computed from the pool's own reserves rather than an oracle, then unwrapped.
* **Stock quotes are NOT handled here.** They need the Token-2022 transfer-hook
  and pause guards, and a staleness rule that stops swapping when the market
  is closed (LAUNCHLAB-RAIL.md sec.5). Passing one raises rather than
  improvising.
"""
from __future__ import annotations

import base64
import struct

from . import buyback
from .base58 import encode as b58encode, pubkey_bytes
from .curve import find_program_address

LAUNCHLAB = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
CPMM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
SYSTEM = "11111111111111111111111111111111"
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

# Quotes this module will act on, and nothing else. A stock quote reaches the
# `unsupported` branch rather than an untested swap.
SWAPPABLE = {USDC, USDT}

# Anchor sighashes, the same constants launchlab/src/raydium.rs asserts.
DISC_CLAIM_PLATFORM_FEE_FROM_VAULT = bytes([117, 241, 198, 168, 248, 218, 80, 29])
DISC_SWAP_BASE_INPUT = bytes([143, 190, 90, 218, 196, 30, 51, 222])

DEFAULT_SLIPPAGE_BPS = 100
# Below this a claim costs more in fees than it moves. It is the floor for
# every tier: a USDC or USDT claim is measured by the SOL it would end in.
MIN_CLAIM_LAMPORTS = 1_000_000
# CPMM fee rates are parts per million (Raydium's FEE_RATE_DENOMINATOR_VALUE).
FEE_RATE_DENOMINATOR = 1_000_000


class PlatformFeeError(ValueError):
    """A cycle that must not be sent. Addressed to whoever holds the wallet."""


def _pda(seeds, program):
    return find_program_address(seeds, program)[0]


def platform_config(admin: str) -> str:
    return _pda([b"platform_config", pubkey_bytes(admin)], LAUNCHLAB)


def platform_fee_vault(config: str, quote_mint: str) -> str:
    return _pda([pubkey_bytes(config), pubkey_bytes(quote_mint)], LAUNCHLAB)


def platform_fee_vault_authority() -> str:
    return _pda([b"platform_fee_vault_auth_seed"], LAUNCHLAB)


def cpmm_authority() -> str:
    return _pda([b"vault_and_lp_mint_auth_seed"], CPMM)


def ix_claim_platform_fee(fee_wallet: str, config: str, recipient: str, quote_mint: str,
                          quote_token_program: str) -> buyback.Instruction:
    """`claim_platform_fee_from_vault`. No arguments: it moves what is there."""
    return (LAUNCHLAB, [
        (fee_wallet, True, True),
        (platform_fee_vault_authority(), False, False),
        (config, False, False),
        (platform_fee_vault(config, quote_mint), False, True),
        (recipient, False, True),
        (quote_mint, False, False),
        (quote_token_program, False, False),
        (SYSTEM, False, False),
        (buyback.ASSOCIATED_TOKEN_PROGRAM, False, False),
    ], DISC_CLAIM_PLATFORM_FEE_FROM_VAULT)


def ix_assert_token_balance(account: str, owner: str, amount: int) -> buyback.Instruction:
    """spl-token `Transfer` of `amount` from `account` to itself: moves
    nothing, and fails with InsufficientFunds if the account holds less."""
    return (buyback.TOKEN_PROGRAM, [(account, False, True), (account, False, True), (owner, True, False)],
            bytes([3]) + struct.pack("<Q", amount))


def read_pool(data: bytes) -> dict:
    """Raydium CPMM pool state. Offsets from launchlab/src/raydium.rs."""
    if len(data) < 328:
        raise PlatformFeeError("not a CPMM pool account")
    k = lambda o: b58encode(data[o:o + 32])  # noqa: E731
    q = lambda o: struct.unpack_from("<Q", data, o)[0]  # noqa: E731
    # The protocol, fund and creator fee counters are tokens that sit in the
    # vaults but are not reserves: the swap prices against vault minus these.
    # Offsets checked against a live pool and Raydium's API, 21 Sept 2026.
    creator = len(data) >= 413
    return {"amm_config": k(8), "vault0": k(72), "vault1": k(104), "lp_mint": k(136),
            "mint0": k(168), "mint1": k(200), "prog0": k(232), "prog1": k(264), "observation": k(296),
            "fees0": q(341) + q(357) + (q(397) if creator else 0),
            "fees1": q(349) + q(365) + (q(405) if creator else 0),
            "enable_creator_fee": bool(data[390]) if creator else False,
            # 0 = both tokens, 1 = token0 only, 2 = token1 only.
            "creator_fee_on": data[389] if creator else 0}


def creator_fee_on_input(pool: dict, in_is_0: bool) -> bool:
    """Whether the program takes the creator fee from the input or the output."""
    on = pool["creator_fee_on"]
    return on == 0 or (on == 1) == in_is_0


def read_amm_config(data: bytes) -> dict:
    """The CPMM AmmConfig a pool names. `trade_fee_rate` is what the swap takes."""
    if len(data) < 44:
        raise PlatformFeeError("not a CPMM amm config account")
    q = lambda o: struct.unpack_from("<Q", data, o)[0]  # noqa: E731
    return {"trade_fee_rate": q(12), "creator_fee_rate": q(108) if len(data) >= 116 else 0}


def ix_swap_base_input(payer: str, pool_key: str, pool: dict, in_is_0: bool, user_in: str,
                       user_out: str, amount_in: int, min_out: int) -> buyback.Instruction:
    if in_is_0:
        in_vault, out_vault, in_prog, out_prog, in_mint, out_mint = (
            pool["vault0"], pool["vault1"], pool["prog0"], pool["prog1"], pool["mint0"], pool["mint1"])
    else:
        in_vault, out_vault, in_prog, out_prog, in_mint, out_mint = (
            pool["vault1"], pool["vault0"], pool["prog1"], pool["prog0"], pool["mint1"], pool["mint0"])
    data = DISC_SWAP_BASE_INPUT + struct.pack("<QQ", amount_in, min_out)
    return (CPMM, [
        (payer, True, False),
        (cpmm_authority(), False, False),
        (pool["amm_config"], False, False),
        (pool_key, False, True),
        (user_in, False, True),
        (user_out, False, True),
        (in_vault, False, True),
        (out_vault, False, True),
        (in_prog, False, False),
        (out_prog, False, False),
        (in_mint, False, False),
        (out_mint, False, False),
        (pool["observation"], False, True),
    ], data)


def quote_out(amount_in: int, reserve_in: int, reserve_out: int,
              slippage_bps: int = DEFAULT_SLIPPAGE_BPS, *, fee_rate: int,
              creator_fee_rate: int = 0, creator_fee_on_input: bool = True) -> int:
    """The pool's fees, constant product, then reduced by slippage.

    The trade fee comes off the input. A creator fee comes off whichever side
    the pool names. Each fee is rounded up on its own, so the bound is never
    above what the program pays out.

    No oracle. The four-leg design needed one because a permissionless caller
    picked the route and had to be stopped from picking a bad price; here the
    keeper picks both, so the pool's own reserves are the reference and the
    bound is in the transaction as `min_out`.

    `fee_rate` is required: leaving the trade fee out overstates `min_out` by
    the fee, which eats a third of the default slippage on a 0.3% pool.
    """
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        raise PlatformFeeError("a swap needs a positive amount and a pool with both sides funded")
    for rate in (fee_rate, creator_fee_rate):
        if not 0 <= rate < FEE_RATE_DENOMINATOR:
            raise PlatformFeeError(f"fee rate {rate} is not a CPMM fee rate")
    ceil_fee = lambda amount, rate: -(-amount * rate // FEE_RATE_DENOMINATOR)  # noqa: E731
    net = amount_in - ceil_fee(amount_in, fee_rate)
    if creator_fee_on_input:
        net -= ceil_fee(amount_in, creator_fee_rate)
    if net <= 0:
        raise PlatformFeeError(f"{amount_in} does not cover the pool's fees")
    out = reserve_out * net // (reserve_in + net)
    if not creator_fee_on_input:
        out -= ceil_fee(out, creator_fee_rate)
    return out * (10_000 - slippage_bps) // 10_000


def plan(rpc, admin: str, quote_mint: str, *, pool_key: str | None = None,
         slippage_bps: int = DEFAULT_SLIPPAGE_BPS, forward_to: str | None = None) -> dict:
    """What one cycle would do for one quote. Builds nothing it cannot price.

    `forward_to` appends a transfer of `sol_out` to that wallet, in the same
    transaction as the claim. The scheduled keeper forwards to the collection
    wallet, whose existing $CHARLIE burn spends it, so there is still exactly
    one schedule buying $CHARLIE. On a swap only `min_out` is forwarded: what
    the pool pays above the bound stays with the fee wallet, which never
    forwards lamports it cannot prove it received."""
    if quote_mint != WSOL and quote_mint not in SWAPPABLE:
        raise PlatformFeeError(
            f"{quote_mint} is not a quote this module handles. A stock quote needs the transfer-hook, "
            "pause and staleness guards in LAUNCHLAB-RAIL.md sec.5, which are not built")
    config = platform_config(admin)
    vault = platform_fee_vault(config, quote_mint)
    mint_info = rpc.accounts([quote_mint])[0]
    if mint_info is None:
        raise PlatformFeeError(f"quote mint {quote_mint} does not exist")
    quote_program = mint_info["owner"]
    held = buyback.decode_token_amount(rpc.accounts([vault])[0], expect_mint=quote_mint) or 0
    recipient = buyback.associated_token_address(admin, quote_mint, quote_program)

    out = {"admin": admin, "quote_mint": quote_mint, "platform_config": config, "vault": vault,
           "vault_amount": held, "recipient": recipient, "instructions": [], "notes": []}
    if held < MIN_CLAIM_LAMPORTS and quote_mint == WSOL:
        out["notes"].append(f"nothing to claim: vault holds {held}, minimum is {MIN_CLAIM_LAMPORTS}")
        return out
    if held == 0:
        out["notes"].append("nothing to claim: the vault is empty")
        return out

    ixs = [buyback.ix_create_ata_idempotent(admin, recipient, admin, quote_mint, quote_program),
           ix_claim_platform_fee(admin, config, recipient, quote_mint, quote_program)]

    if quote_mint == WSOL:
        # Tier 1. The fee IS wSOL; closing the account unwraps it to the
        # wallet, and no price is consulted anywhere in this path.
        if forward_to and forward_to != admin:
            # The forward below is paid in lamports, which the fee wallet has
            # of its own. If the vault was drained between this read and the
            # landing, the claim moves nothing and the forward would spend
            # the operator's SOL. A self-transfer of `held` is an on-chain
            # assertion: spl-token checks the balance before it returns early
            # for source == destination, so the transaction fails whole.
            ixs.append(ix_assert_token_balance(recipient, admin, held))
        ixs.append(buyback.ix_close_account(recipient, admin, admin))
        out["sol_out"] = held
        out["notes"].append("SOL quote: claimed as wSOL and unwrapped, no swap")
    else:
        if pool_key is None:
            raise PlatformFeeError(f"a {quote_mint} quote needs --pool: the CPMM pool that trades it against wSOL")
        pool_info = rpc.accounts([pool_key])[0]
        if pool_info is None:
            raise PlatformFeeError(f"pool {pool_key} does not exist")
        pool = read_pool(base64.b64decode(pool_info["data"][0]))
        if WSOL not in (pool["mint0"], pool["mint1"]) or quote_mint not in (pool["mint0"], pool["mint1"]):
            raise PlatformFeeError(f"pool {pool_key} does not trade {quote_mint} against wSOL")
        in_is_0 = pool["mint0"] == quote_mint
        in_vault, out_vault = (pool["vault0"], pool["vault1"]) if in_is_0 else (pool["vault1"], pool["vault0"])
        infos = rpc.accounts([in_vault, out_vault, pool["amm_config"]])
        fees_in, fees_out = (pool["fees0"], pool["fees1"]) if in_is_0 else (pool["fees1"], pool["fees0"])
        reserve_in = (buyback.decode_token_amount(infos[0], expect_mint=quote_mint) or 0) - fees_in
        reserve_out = (buyback.decode_token_amount(infos[1], expect_mint=WSOL) or 0) - fees_out
        if infos[2] is None:
            raise PlatformFeeError(f"amm config {pool['amm_config']} does not exist")
        amm = read_amm_config(base64.b64decode(infos[2]["data"][0]))
        fee_rate = amm["trade_fee_rate"]
        creator_rate = amm["creator_fee_rate"] if pool["enable_creator_fee"] else 0
        min_out = quote_out(held, reserve_in, reserve_out, slippage_bps, fee_rate=fee_rate,
                            creator_fee_rate=creator_rate,
                            creator_fee_on_input=creator_fee_on_input(pool, in_is_0))
        if min_out < MIN_CLAIM_LAMPORTS:
            out["notes"].append(
                f"nothing to claim: {held} {quote_mint} would end as {min_out} lamports, "
                f"minimum is {MIN_CLAIM_LAMPORTS}")
            return out
        wsol_ata = buyback.associated_token_address(admin, WSOL, buyback.TOKEN_PROGRAM)
        ixs += [
            buyback.ix_create_ata_idempotent(admin, wsol_ata, admin, WSOL, buyback.TOKEN_PROGRAM),
            ix_swap_base_input(admin, pool_key, pool, in_is_0, recipient, wsol_ata, held, min_out),
            buyback.ix_close_account(wsol_ata, admin, admin),
        ]
        out["sol_out"] = min_out
        out["pool"] = pool_key
        out["notes"].append(
            f"{quote_mint} quote: one CPMM hop, {held} in, at least {min_out} wSOL out "
            f"(after the pool's {fee_rate}/1e6 fee, {slippage_bps} bps under its own reserves), then unwrapped")

    if forward_to and forward_to != admin:
        ixs.append(buyback.ix_system_transfer(admin, forward_to, out["sol_out"]))
        out["forward_to"] = forward_to
        out["notes"].append(f"forwards {out['sol_out']} lamports to {forward_to} in the same transaction")
    out["instructions"] = ixs
    out["notes"].append("ends in SOL. `indexer charlie-buyback --sweep` buys and burns $CHARLIE from "
                        + ("there" if out.get("forward_to") else "here"))
    return out


def execute(rpc, out: dict, keypair, *, send: bool, sleep=None) -> dict:
    """build -> simulate -> (sign -> send -> confirm), as buyback._execute.
    Nothing crosses the simulation with an error."""
    from .message import compile_legacy, unsigned_transaction
    msg = compile_legacy(out["admin"], out["instructions"], buyback.latest_blockhash(rpc))
    sim = buyback.simulate(rpc, msg)
    result = {"simulation": {"err": sim.get("err"), "units_consumed": sim.get("unitsConsumed"),
                             "logs_tail": (sim.get("logs") or [])[-8:]},
              "transaction_base64": base64.b64encode(unsigned_transaction(msg)).decode(),
              "sent": False}
    if sim.get("err") is not None:
        result["error"] = f"the simulation failed ({sim.get('err')}); nothing was sent"
        return result
    if not send:
        return result
    if keypair is None:
        result["error"] = "no keypair given: built and simulated, not sent"
        return result
    signature = buyback.send_signed(rpc, keypair, msg)
    result.update(signature=signature, sent=True)
    kwargs = {"sleep": sleep} if sleep else {}
    result["confirmation"] = buyback.confirm(rpc, signature, **kwargs).get("confirmationStatus")
    return result

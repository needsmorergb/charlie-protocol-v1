"""The hand-run BURN leg.

Offline, like every other test here. The derivations are pinned against
addresses pump publishes in its own documentation (a real mainnet pool, its
LP mint and vaults, the global config), the account decoders against bytes
built field by field from the IDL layout, the arithmetic against the SDK's
formulas, and the whole plan against a fake RPC that serves those bytes --
through the same `observe -> plan -> build -> simulate -> send -> confirm ->
verify_recorded` path the CLI runs, ending with the indexer's own decoders
reading the transaction back.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import buyback, ed25519, message  # noqa: E402
from indexer.base58 import encode  # noqa: E402
from indexer.enroll import associated_token_address as ata  # noqa: E402
from indexer.pump import DecodeError, PUMP_AMM_PROGRAM, TOKEN_2022_PROGRAM, TOKEN_PROGRAM  # noqa: E402

BLOCKHASH = "11111111111111111111111111111111"
SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
KEYPAIR = ed25519.Keypair.from_seed(SEED)
USER = KEYPAIR.address
MINT = "8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump"
DECIMALS = 6
SUPPLY = 956_382_187_958_910
BASE_RESERVE = 200_000_000 * 10**DECIMALS
QUOTE_RESERVE = 100 * buyback.LAMPORTS_PER_SOL
SOL = buyback.LAMPORTS_PER_SOL


def _key(label: str) -> str:
    return encode(hashlib.sha256(label.encode()).digest())


RECIPIENTS = [_key(f"protocol-{i}") for i in range(8)]
RESERVED = [_key(f"reserved-{i}") for i in range(8)]
BUYBACK = [_key(f"buyback-{i}") for i in range(8)]
COIN_CREATOR = _key("sharing-config-of-charlie")

# fees.png, the first three rows, thresholds in lamports.
TIERS = [
    (0, buyback.Fees(2, 93, 30)),
    (420 * SOL, buyback.Fees(20, 5, 95)),
    (1470 * SOL, buyback.Fees(20, 5, 90)),
    (2460 * SOL, buyback.Fees(20, 5, 85)),
]


def _account(owner: str, data: bytes, lamports: int = 1_000_000) -> dict:
    return {"owner": owner, "data": [base64.b64encode(data).decode(), "base64"], "lamports": lamports, "executable": False, "space": len(data)}


def _pk(address: str) -> bytes:
    from indexer.base58 import pubkey_bytes
    return pubkey_bytes(address)


def pool_bytes(*, creator, base_mint=MINT, coin_creator=COIN_CREATOR, mayhem=False, cashback=False, vqr=0, length=300) -> bytes:
    pool = buyback.pool_address(0, creator, base_mint, buyback.WSOL_MINT)
    out = bytearray(buyback.DISC_POOL)
    out += bytes([254]) + (0).to_bytes(2, "little")
    out += _pk(creator) + _pk(base_mint) + _pk(buyback.WSOL_MINT) + _pk(buyback.lp_mint(pool))
    out += _pk(ata(pool, base_mint, TOKEN_2022_PROGRAM)) + _pk(ata(pool, buyback.WSOL_MINT, TOKEN_PROGRAM))
    out += (12345).to_bytes(8, "little")
    out += _pk(coin_creator) + bytes([mayhem, cashback]) + vqr.to_bytes(16, "little", signed=True)
    return bytes(out[:length]) + b"\x00" * max(0, length - len(out))


def global_config_bytes() -> bytes:
    out = bytearray(buyback.DISC_GLOBAL_CONFIG)
    out += _pk(_key("admin")) + (20).to_bytes(8, "little") + (5).to_bytes(8, "little") + b"\x00"
    for r in RECIPIENTS:
        out += _pk(r)
    out += (30).to_bytes(8, "little") + _pk(_key("setter")) + _pk(_key("whitelist"))
    out += _pk(RESERVED[0]) + b"\x00"
    for r in RESERVED[1:]:
        out += _pk(r)
    out += b"\x00"
    for r in BUYBACK:
        out += _pk(r)
    out += (0).to_bytes(8, "little") + _pk(_key("boost")) + b"\x00"
    return bytes(out)


def fee_config_bytes(tiers=TIERS) -> bytes:
    out = bytearray(buyback.DISC_FEE_CONFIG) + b"\xff" + _pk(_key("fee-admin"))
    out += (20).to_bytes(8, "little") + (5).to_bytes(8, "little") + (0).to_bytes(8, "little")  # flat
    out += len(tiers).to_bytes(4, "little")
    for threshold, fees in tiers:
        out += threshold.to_bytes(16, "little")
        out += fees.lp_bps.to_bytes(8, "little") + fees.protocol_bps.to_bytes(8, "little") + fees.creator_bps.to_bytes(8, "little")
    out += (0).to_bytes(4, "little")  # stable tiers: none
    return bytes(out)


def token_account_bytes(mint: str, owner: str, amount: int) -> bytes:
    return _pk(mint) + _pk(owner) + amount.to_bytes(8, "little") + b"\x00" * 93


def mint_bytes(supply=SUPPLY, decimals=DECIMALS, mint_authority=None) -> bytes:
    out = bytearray()
    out += (1 if mint_authority else 0).to_bytes(4, "little") + (_pk(mint_authority) if mint_authority else bytes(32))
    out += supply.to_bytes(8, "little") + bytes([decimals, 1]) + (0).to_bytes(4, "little") + bytes(32)
    return bytes(out)


class FakeRpc:
    def __init__(self, accounts: dict, *, tx=None, statuses=None, sim=None):
        self._accounts = accounts
        self.tx = tx
        self.statuses = statuses or [{"confirmationStatus": "confirmed", "err": None}]
        self.sim = sim if sim is not None else {"err": None, "logs": ["Program pAMM ok"], "unitsConsumed": 90_000}
        self.sent = []
        self.calls = []

    def accounts(self, addresses):
        return [self._accounts.get(a) for a in addresses]

    def call(self, method, params=None):
        self.calls.append(method)
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": BLOCKHASH}}
        if method == "simulateTransaction":
            return {"value": self.sim}
        if method == "sendTransaction":
            self.sent.append(params[0])
            return "5ignature"
        if method == "getSignatureStatuses":
            return {"value": [self.statuses.pop(0) if self.statuses else None]}
        raise AssertionError(method)

    def transaction(self, signature):
        return self.tx


def chain(*, user_tokens=16_000_000 * 10**DECIMALS, user_lamports=2 * SOL, cashback=False, mint_authority=None, pool_length=300):
    creator = buyback.pool_authority(MINT)
    pool = buyback.canonical_pool(MINT)
    accounts = {
        MINT: _account(TOKEN_2022_PROGRAM, mint_bytes(mint_authority=mint_authority)),
        pool: _account(PUMP_AMM_PROGRAM, pool_bytes(creator=creator, cashback=cashback, length=pool_length)),
        buyback.global_config(): _account(PUMP_AMM_PROGRAM, global_config_bytes()),
        buyback.fee_config(): _account(buyback.FEE_PROGRAM, fee_config_bytes()),
        USER: {"owner": "11111111111111111111111111111111", "data": ["", "base64"], "lamports": user_lamports},
        ata(pool, MINT, TOKEN_2022_PROGRAM): _account(TOKEN_2022_PROGRAM, token_account_bytes(MINT, pool, BASE_RESERVE)),
        ata(pool, buyback.WSOL_MINT, TOKEN_PROGRAM): _account(TOKEN_PROGRAM, token_account_bytes(buyback.WSOL_MINT, pool, QUOTE_RESERVE)),
    }
    if user_tokens is not None:
        accounts[ata(USER, MINT, TOKEN_2022_PROGRAM)] = _account(TOKEN_2022_PROGRAM, token_account_bytes(MINT, USER, user_tokens))
    return accounts


def landed_tx(burn_amount: int, with_swap: bool = True, err=None) -> dict:
    instructions = []
    if with_swap:
        instructions.append({"programId": PUMP_AMM_PROGRAM, "accounts": [], "data": "3Bxs"})
    instructions.append({
        "programId": TOKEN_2022_PROGRAM, "program": "spl-token",
        "parsed": {"type": "burn", "info": {"mint": MINT, "account": ata(USER, MINT, TOKEN_2022_PROGRAM),
                                             "authority": USER, "amount": str(burn_amount)}},
    })
    return {"slot": 400, "blockTime": 1_790_000_000,
            "meta": {"err": err, "innerInstructions": [], "logMessages": []},
            "transaction": {"message": {"instructions": instructions}}}


def first(choices):
    return choices[0]


# -- derivations, pinned to pump's published examples ------------------------
class TestDerivations(unittest.TestCase):
    """PUMP_SWAP_README.md prints one real pool's account. Every derived
    address in it is recomputed here from seeds alone."""

    BASE = "7LSsEoJGhLeZzGvDofTdNg7M3JttxQqGWNLo6vWMpump"
    CREATOR = "9XDYTfQKwW8sHPqnFdUreMmtmffmkHVPGTNV2e3LKxNW"
    POOL = "GseMAnNDvntR5uFePZ51yZBXzNSn7GdFPkfHwfr6d77J"

    def test_global_config(self):
        self.assertEqual(buyback.global_config(), "ADyA8hdefvWN2dbGGWFotbzWxrAvLW83WG6QCVXvJKqw")

    def test_pool_seeds(self):
        self.assertEqual(buyback.pool_address(0, self.CREATOR, self.BASE, buyback.WSOL_MINT), self.POOL)

    def test_lp_mint_and_vaults(self):
        self.assertEqual(buyback.lp_mint(self.POOL), "6dpnPD6UWDw5hbJEuPQwnCCMba1JYwHANKuL6GQ6otAH")
        self.assertEqual(ata(self.POOL, self.BASE, TOKEN_PROGRAM), "5jMpkf4JF4noHftLgNKyPNh6roVfPSGSjuEk3U4eLKRa")
        self.assertEqual(ata(self.POOL, buyback.WSOL_MINT, TOKEN_PROGRAM), "43DVcZR4kQFjh4Xm2i3DcneRxNjZp7HMud8yDrJWrDr8")

    def test_fee_config_second_seed_is_the_amm_program_id(self):
        # The IDL writes the seed as 32 constant bytes; they decode to the AMM's address.
        self.assertEqual(
            encode(bytes.fromhex("0c14defc825ec67694250818bb654065f4298d3156d571b4d4f8090c18e9a863")),
            PUMP_AMM_PROGRAM,
        )

    def test_canonical_pool_is_index_zero_under_pumps_pool_authority(self):
        self.assertEqual(
            buyback.canonical_pool(MINT),
            buyback.pool_address(0, buyback.pool_authority(MINT), MINT, buyback.WSOL_MINT),
        )


# -- decoders -----------------------------------------------------------------
class TestPoolDecode(unittest.TestCase):
    def test_full_layout(self):
        creator = buyback.pool_authority(MINT)
        pool = buyback.decode_pool(buyback.canonical_pool(MINT), _account(PUMP_AMM_PROGRAM, pool_bytes(creator=creator, cashback=True, vqr=-5)))
        self.assertEqual(pool.creator, creator)
        self.assertTrue(pool.canonical)
        self.assertEqual(pool.base_mint, MINT)
        self.assertEqual(pool.quote_mint, buyback.WSOL_MINT)
        self.assertEqual(pool.coin_creator, COIN_CREATOR)
        self.assertTrue(pool.has_coin_creator)
        self.assertTrue(pool.is_cashback_coin)
        self.assertFalse(pool.is_mayhem_mode)
        self.assertEqual(pool.virtual_quote_reserves, -5)
        self.assertEqual(pool.lp_supply, 12345)
        self.assertEqual(pool.data_len, 300)

    def test_an_old_short_pool_reads_defaults(self):
        creator = buyback.pool_authority(MINT)
        pool = buyback.decode_pool("x", _account(PUMP_AMM_PROGRAM, pool_bytes(creator=creator, length=211)))
        self.assertEqual(pool.coin_creator, buyback.DEFAULT_PUBKEY)
        self.assertFalse(pool.has_coin_creator)
        self.assertEqual(pool.virtual_quote_reserves, 0)

    def test_wrong_owner_or_discriminator_is_refused(self):
        creator = buyback.pool_authority(MINT)
        with self.assertRaises(DecodeError):
            buyback.decode_pool("x", _account(TOKEN_PROGRAM, pool_bytes(creator=creator)))
        with self.assertRaises(DecodeError):
            buyback.decode_pool("x", _account(PUMP_AMM_PROGRAM, b"\x00" * 8 + pool_bytes(creator=creator)[8:]))
        with self.assertRaises(DecodeError):
            buyback.decode_pool("x", None)


class TestConfigDecode(unittest.TestCase):
    def test_global_config(self):
        config = buyback.decode_global_config(_account(PUMP_AMM_PROGRAM, global_config_bytes()))
        self.assertEqual((config.lp_fee_bps, config.protocol_fee_bps, config.coin_creator_fee_bps), (20, 5, 30))
        self.assertEqual(list(config.protocol_fee_recipients), RECIPIENTS)
        self.assertEqual(list(config.reserved_fee_recipients), RESERVED)
        self.assertEqual(list(config.buyback_fee_recipients), BUYBACK)

    def test_fee_config_and_tiers(self):
        config = buyback.decode_fee_config(_account(buyback.FEE_PROGRAM, fee_config_bytes()))
        self.assertEqual(config.flat_fees, buyback.Fees(20, 5, 0))
        self.assertEqual([t.market_cap_lamports_threshold for t in config.fee_tiers], [t for t, _ in TIERS])
        self.assertEqual(buyback.fee_tier(config.fee_tiers, 0), buyback.Fees(2, 93, 30))
        self.assertEqual(buyback.fee_tier(config.fee_tiers, 419 * SOL), buyback.Fees(2, 93, 30))
        self.assertEqual(buyback.fee_tier(config.fee_tiers, 420 * SOL), buyback.Fees(20, 5, 95))
        self.assertEqual(buyback.fee_tier(config.fee_tiers, 1469 * SOL), buyback.Fees(20, 5, 95))
        self.assertEqual(buyback.fee_tier(config.fee_tiers, 10_000 * SOL), buyback.Fees(20, 5, 85))

    def test_token_amount(self):
        account = _account(TOKEN_2022_PROGRAM, token_account_bytes(MINT, USER, 77))
        self.assertEqual(buyback.decode_token_amount(account, expect_mint=MINT), 77)
        self.assertIsNone(buyback.decode_token_amount(None))
        with self.assertRaises(DecodeError):
            buyback.decode_token_amount(account, expect_mint=buyback.WSOL_MINT)


# -- arithmetic ----------------------------------------------------------------
class TestArithmetic(unittest.TestCase):
    FEES = buyback.Fees(20, 5, 95)

    def test_cost_matches_the_sdk_formula(self):
        cost = buyback.cost_of(1_000_000, BASE_RESERVE, QUOTE_RESERVE, self.FEES, True)
        quote_in = -(-QUOTE_RESERVE * 1_000_000 // (BASE_RESERVE - 1_000_000))
        self.assertEqual(cost["quote_in"], quote_in)
        self.assertEqual(cost["lp_fee"], -(-quote_in * 20 // 10_000))
        self.assertEqual(cost["creator_fee"], -(-quote_in * 95 // 10_000))
        self.assertEqual(cost["total"], sum(v for k, v in cost.items() if k != "total"))
        self.assertEqual(buyback.cost_of(1_000_000, BASE_RESERVE, QUOTE_RESERVE, self.FEES, False)["creator_fee"], 0)

    def test_the_lot_is_an_upper_bound_that_is_nearly_reached(self):
        lot = buyback.DEFAULT_LOT_LAMPORTS
        out = buyback.base_out_for_lot(lot, BASE_RESERVE, QUOTE_RESERVE, self.FEES, True, slippage_bps=0)
        total = buyback.cost_of(out, BASE_RESERVE, QUOTE_RESERVE, self.FEES, True)["total"]
        self.assertLessEqual(total, lot)
        self.assertLess(lot - total, lot // 1000)

    def test_slippage_shrinks_the_buy_not_the_bound(self):
        lot = buyback.DEFAULT_LOT_LAMPORTS
        tight = buyback.base_out_for_lot(lot, BASE_RESERVE, QUOTE_RESERVE, self.FEES, True, slippage_bps=0)
        loose = buyback.base_out_for_lot(lot, BASE_RESERVE, QUOTE_RESERVE, self.FEES, True, slippage_bps=100)
        self.assertEqual(loose, tight * 9900 // 10_000)

    def test_a_dust_lot_is_refused(self):
        with self.assertRaises(buyback.BuybackError):
            buyback.base_out_for_lot(1, BASE_RESERVE, QUOTE_RESERVE, self.FEES, True, 0)

    def test_market_cap(self):
        self.assertEqual(buyback.pool_market_cap(SUPPLY, BASE_RESERVE, QUOTE_RESERVE), QUOTE_RESERVE * SUPPLY // BASE_RESERVE)


# -- instruction encoding -------------------------------------------------------
class TestBuyInstruction(unittest.TestCase):
    def pool(self, **kw):
        return buyback.decode_pool(buyback.canonical_pool(MINT), _account(PUMP_AMM_PROGRAM, pool_bytes(creator=buyback.pool_authority(MINT), **kw)))

    def test_data_layout(self):
        data = buyback.buy_data(1_000, 2_000)
        self.assertEqual(len(data), 25)
        self.assertEqual(data[:8], bytes.fromhex("66063d1201daebea"))
        self.assertEqual(int.from_bytes(data[8:16], "little"), 1_000)
        self.assertEqual(int.from_bytes(data[16:24], "little"), 2_000)
        self.assertEqual(data[24], 1)

    def test_account_order_matches_the_idl_then_the_sdk_remaining_accounts(self):
        pool = self.pool()
        metas = buyback.buy_accounts(pool, USER, base_token_program=TOKEN_2022_PROGRAM,
                                     protocol_fee_recipient=RECIPIENTS[3], buyback_fee_recipient=BUYBACK[5])
        vault_authority = buyback.coin_creator_vault_authority(COIN_CREATOR)
        expected = [
            pool.address, USER, buyback.global_config(), MINT, buyback.WSOL_MINT,
            ata(USER, MINT, TOKEN_2022_PROGRAM), ata(USER, buyback.WSOL_MINT, TOKEN_PROGRAM),
            pool.base_vault, pool.quote_vault,
            RECIPIENTS[3], ata(RECIPIENTS[3], buyback.WSOL_MINT, TOKEN_PROGRAM),
            TOKEN_2022_PROGRAM, TOKEN_PROGRAM, "11111111111111111111111111111111", buyback.ASSOCIATED_TOKEN_PROGRAM,
            buyback.event_authority(), PUMP_AMM_PROGRAM,
            ata(vault_authority, buyback.WSOL_MINT, TOKEN_PROGRAM), vault_authority,
            buyback.global_volume_accumulator(), buyback.user_volume_accumulator(USER),
            buyback.fee_config(), buyback.FEE_PROGRAM,
            # remaining accounts
            buyback.pool_v2(MINT), BUYBACK[5], ata(BUYBACK[5], buyback.WSOL_MINT, TOKEN_PROGRAM),
        ]
        self.assertEqual([m[0] for m in metas], expected)
        self.assertEqual(len(metas), 26)
        flags = {m[0]: (m[1], m[2]) for m in metas}
        self.assertEqual(flags[USER], (True, True))
        self.assertEqual(flags[pool.address], (False, True))
        self.assertEqual(flags[buyback.user_volume_accumulator(USER)], (False, True))
        self.assertEqual(flags[buyback.global_volume_accumulator()], (False, False))
        self.assertEqual(flags[PUMP_AMM_PROGRAM], (False, False))
        self.assertEqual(flags[buyback.pool_v2(MINT)], (False, False))
        self.assertEqual(flags[ata(BUYBACK[5], buyback.WSOL_MINT, TOKEN_PROGRAM)], (False, True))

    def test_cashback_coin_prepends_the_accumulators_wsol_account(self):
        metas = buyback.buy_accounts(self.pool(cashback=True), USER, base_token_program=TOKEN_2022_PROGRAM,
                                     protocol_fee_recipient=RECIPIENTS[0], buyback_fee_recipient=BUYBACK[0])
        self.assertEqual(len(metas), 27)
        self.assertEqual(metas[23][0], ata(buyback.user_volume_accumulator(USER), buyback.WSOL_MINT, TOKEN_PROGRAM))
        self.assertEqual(metas[24][0], buyback.pool_v2(MINT))

    def test_a_pool_without_a_coin_creator_omits_pool_v2(self):
        metas = buyback.buy_accounts(self.pool(coin_creator=buyback.DEFAULT_PUBKEY), USER, base_token_program=TOKEN_2022_PROGRAM,
                                     protocol_fee_recipient=RECIPIENTS[0], buyback_fee_recipient=BUYBACK[0])
        self.assertEqual(len(metas), 25)
        self.assertNotIn(buyback.pool_v2(MINT), [m[0] for m in metas])

    def test_burn_is_the_plain_token_burn(self):
        program, metas, data = buyback.ix_burn(TOKEN_2022_PROGRAM, "ata", MINT, USER, 5)
        self.assertEqual(program, TOKEN_2022_PROGRAM)
        self.assertEqual(data, b"\x08" + (5).to_bytes(8, "little"))
        self.assertEqual([m[1:] for m in metas], [(False, True), (False, True), (True, False)])


# -- the plan, end to end against a fake chain ----------------------------------
class TestPlan(unittest.TestCase):
    def state(self, **kw):
        return buyback.observe(FakeRpc(chain(**kw)), MINT, USER)

    def test_observe_reads_the_chain(self):
        state = self.state()
        self.assertEqual(state.token_program, TOKEN_2022_PROGRAM)
        self.assertEqual((state.base_reserve, state.quote_reserve), (BASE_RESERVE, QUOTE_RESERVE))
        self.assertEqual(state.user_base_balance, 16_000_000 * 10**DECIMALS)
        self.assertFalse(state.user_quote_exists)
        # 100 SOL * 956.38M / 200M = 478 SOL market cap -> the 420..1470 SOL tier
        self.assertEqual(state.fees, buyback.Fees(20, 5, 95))

    def test_no_pool_is_a_plain_sentence(self):
        accounts = chain()
        del accounts[buyback.canonical_pool(MINT)]
        with self.assertRaises(buyback.BuybackError) as ctx:
            buyback.observe(FakeRpc(accounts), MINT, USER)
        self.assertIn("graduated", str(ctx.exception))

    def test_instruction_sequence(self):
        plan = buyback.plan_buy_and_burn(self.state(), choose=first)
        programs = [ix[0] for ix in plan.instructions]
        self.assertEqual(programs, [
            buyback.COMPUTE_BUDGET_PROGRAM,
            buyback.ASSOCIATED_TOKEN_PROGRAM, buyback.ASSOCIATED_TOKEN_PROGRAM,
            "11111111111111111111111111111111", TOKEN_PROGRAM,
            PUMP_AMM_PROGRAM, TOKEN_2022_PROGRAM, TOKEN_PROGRAM,
        ])
        buy = plan.instructions[5]
        self.assertEqual(int.from_bytes(buy[2][8:16], "little"), plan.base_out)
        self.assertEqual(int.from_bytes(buy[2][16:24], "little"), plan.lot_lamports)
        burn = plan.instructions[6]
        self.assertEqual(burn[2], b"\x08" + plan.base_out.to_bytes(8, "little"))
        self.assertEqual(plan.burn_total, plan.base_out)
        self.assertGreater(plan.impact_bps, 0)
        self.assertLess(plan.expected_cost["total"], plan.lot_lamports)
        self.assertEqual(plan.supply_after, SUPPLY - plan.base_out)
        self.assertIn(plan.accounts["protocol_fee_recipient"], RECIPIENTS)

    def test_also_burn_adds_held_tokens_to_the_same_burn(self):
        extra = 1_000_000 * 10**DECIMALS
        plan = buyback.plan_buy_and_burn(self.state(), also_burn=extra, choose=first)
        self.assertEqual(plan.burn_total, plan.base_out + extra)
        self.assertEqual(plan.instructions[6][2], b"\x08" + (plan.base_out + extra).to_bytes(8, "little"))
        with self.assertRaises(buyback.BuybackError):
            buyback.plan_buy_and_burn(self.state(), also_burn=17_000_000 * 10**DECIMALS, choose=first)

    def test_priority_fee_and_pool_extension(self):
        plan = buyback.plan_buy_and_burn(self.state(pool_length=250), priority_micro_lamports=10_000, choose=first)
        self.assertEqual(plan.instructions[1][0], buyback.COMPUTE_BUDGET_PROGRAM)
        self.assertEqual(plan.instructions[2][2], buyback.DISC_EXTEND_ACCOUNT)

    def test_an_underfunded_wallet_is_refused_before_anything_is_built(self):
        with self.assertRaises(buyback.BuybackError):
            buyback.plan_buy_and_burn(self.state(user_lamports=55_000_000), choose=first)

    def test_a_live_mint_authority_is_named(self):
        plan = buyback.plan_buy_and_burn(self.state(mint_authority=_key("minter")), choose=first)
        self.assertTrue(any("mint authority" in n for n in plan.notes))

    def test_the_message_fits_one_packet_and_names_the_payer_first(self):
        plan = buyback.plan_buy_and_burn(self.state(), also_burn=1, priority_micro_lamports=1, choose=first)
        msg = buyback.build(plan, BLOCKHASH)
        self.assertLessEqual(len(msg) + 65, message.MAX_TRANSACTION_BYTES)
        self.assertEqual(buyback._payer_of(msg), USER)
        self.assertEqual(msg[0], 1)

    def test_plain_burn(self):
        plan = buyback.plan_burn(self.state(), 16_000_000 * 10**DECIMALS)
        self.assertEqual([ix[0] for ix in plan.instructions], [buyback.COMPUTE_BUDGET_PROGRAM, TOKEN_2022_PROGRAM])
        self.assertEqual(plan.impact_bps, 0)
        self.assertAlmostEqual(plan.supply_reduction_bps, 16_000_000 * 10**DECIMALS / SUPPLY * 10_000)
        with self.assertRaises(buyback.BuybackError):
            buyback.plan_burn(self.state(), 16_000_001 * 10**DECIMALS)
        with self.assertRaises(buyback.BuybackError):
            buyback.plan_burn(self.state(user_tokens=None), 1)

    def test_render_names_every_figure_and_its_source(self):
        text = buyback.render(buyback.plan_buy_and_burn(self.state(), choose=first))
        for needle in ("max_quote_amount_in", "base_amount_out", "quote_reserve / base_reserve", "not protocol-attributed"):
            self.assertIn(needle, text)


class TestSendPath(unittest.TestCase):
    def test_crank_once_signs_verifies_and_reads_back_through_the_indexer(self):
        rpc = FakeRpc(chain(), tx=landed_tx(1))
        result = buyback.crank_once(rpc, MINT, USER, KEYPAIR, lot_lamports=buyback.DEFAULT_LOT_LAMPORTS,
                                    slippage_bps=100, send=True, choose=first, sleep=lambda s: None)
        self.assertTrue(result["sent"])
        self.assertEqual(result["signature"], "5ignature")
        self.assertEqual(result["recorded"]["atomic"], "PASS")
        self.assertTrue(result["recorded"]["swap_present"])
        self.assertEqual(result["recorded"]["protocol_attributed"], 0)
        # what was sent carries a signature this keypair really made over this message
        wire = base64.b64decode(rpc.sent[0])
        self.assertEqual(wire[0], 1)
        signature, msg = wire[1:65], wire[65:]
        self.assertTrue(ed25519.verify(KEYPAIR.public, msg, signature))
        self.assertEqual(buyback._payer_of(msg), USER)
        self.assertEqual(rpc.calls.count("sendTransaction"), 1)

    def test_a_failed_simulation_is_never_signed(self):
        rpc = FakeRpc(chain(), sim={"err": {"InstructionError": [5, {"Custom": 6002}]}, "logs": ["Program log: ExceededSlippage"]})
        result = buyback.crank_once(rpc, MINT, USER, KEYPAIR, lot_lamports=buyback.DEFAULT_LOT_LAMPORTS, slippage_bps=100, send=True, choose=first)
        self.assertFalse(result["sent"])
        self.assertIn("pool moved", result["error"])
        self.assertEqual(rpc.sent, [])

    def test_without_send_the_unsigned_transaction_is_returned_for_a_wallet(self):
        rpc = FakeRpc(chain())
        result = buyback.crank_once(rpc, MINT, USER, None, lot_lamports=buyback.DEFAULT_LOT_LAMPORTS, slippage_bps=100, send=False, choose=first)
        self.assertFalse(result["sent"])
        self.assertIn("message_base58", result)
        self.assertEqual(base64.b64decode(result["transaction_base64"])[1:65], b"\x00" * 64)
        self.assertEqual(rpc.sent, [])

    def test_a_keypair_that_is_not_the_payer_is_refused(self):
        rpc = FakeRpc(chain())
        other = ed25519.Keypair.from_seed(bytes(range(32)))
        with self.assertRaises(buyback.BuybackError):
            buyback.crank_once(rpc, MINT, USER, other, lot_lamports=buyback.DEFAULT_LOT_LAMPORTS, slippage_bps=100, send=True, choose=first)

    def test_confirm_times_out_and_reports_on_chain_failure(self):
        rpc = FakeRpc(chain(), statuses=[None, None])
        clock = iter([0, 1, 200])
        with self.assertRaises(buyback.BuybackError):
            buyback.confirm(rpc, "sig", timeout=90, sleep=lambda s: None, clock=lambda: next(clock))
        rpc = FakeRpc(chain(), statuses=[{"confirmationStatus": "confirmed", "err": {"InstructionError": [6, "Custom"]}}])
        with self.assertRaises(buyback.BuybackError) as ctx:
            buyback.confirm(rpc, "sig", sleep=lambda s: None)
        self.assertIn("FAILED on chain", str(ctx.exception))

    def test_verify_recorded_reads_a_swapless_burn_as_not_atomic(self):
        rpc = FakeRpc(chain(), tx=landed_tx(5, with_swap=False))
        recorded = buyback.verify_recorded(rpc, "sig", MINT)
        self.assertEqual(recorded["atomic"], "FAIL")
        self.assertEqual(recorded["tokens_burned"], 5)

    def test_keeper_loop_respects_budget_and_interval(self):
        rpc = FakeRpc(chain(), tx=landed_tx(1))
        rpc.statuses = [{"confirmationStatus": "confirmed", "err": None}] * 10
        slept = []
        lines = []
        summary = buyback.run_keeper(
            rpc, MINT, KEYPAIR, lot_lamports=buyback.DEFAULT_LOT_LAMPORTS, slippage_bps=100,
            every_seconds=3600, max_total_lamports=120_000_000, log=lines.append, sleep=slept.append, choose=first,
        )
        self.assertEqual(summary["cranks"], 3)  # three 0.05 lots exhaust a 0.12 budget
        self.assertEqual(slept, [3600, 3600])
        self.assertEqual(len(lines), 3)
        self.assertLessEqual(summary["spent_lamports"], 150_000_000)

    def test_keeper_stops_after_repeated_failures(self):
        rpc = FakeRpc(chain(), sim={"err": {"x": 1}, "logs": []})
        summary = buyback.run_keeper(rpc, MINT, KEYPAIR, lot_lamports=buyback.DEFAULT_LOT_LAMPORTS, slippage_bps=100,
                                     every_seconds=1, max_total_lamports=None, log=lambda line: None, sleep=lambda s: None, choose=first)
        self.assertEqual(summary["cranks"], 0)
        self.assertEqual(summary["stopped_because"], "5 consecutive failures")


class TestExplain(unittest.TestCase):
    def test_known_patterns(self):
        self.assertIn("pool moved", buyback.explain({"logs": ["ExceededSlippage"], "err": 1}))
        self.assertIn("enough SOL", buyback.explain({"logs": ["Transfer: insufficient lamports 1, need 2"], "err": 1}))
        self.assertIn("Nothing was sent", buyback.explain({"logs": [], "err": {"weird": 1}}))


if __name__ == "__main__":
    unittest.main()

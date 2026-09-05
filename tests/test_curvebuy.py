"""The BURN leg on a coin still on its bonding curve.

pump's `buy` as the deployed program declares it (the deploy repository's
`idl` workflow, 2026-09-05): sixteen accounts in order, two u64 args and
an OptionBool, the curve's own arithmetic. Pinned so a later edit cannot
silently move an account, and so the venue choice -- curve before
graduation, pool after -- is a tested fact rather than a comment.
"""

from __future__ import annotations

import base64
import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from indexer import buyback, curvebuy, pump  # noqa: E402
from indexer.base58 import encode, pubkey_bytes  # noqa: E402
from indexer.enroll import associated_token_address  # noqa: E402
from indexer.pump import PUMP_PROGRAM, SYSTEM_PROGRAM, TOKEN_PROGRAM  # noqa: E402

from test_buyback import (  # noqa: E402
    DECIMALS, FakeRpc, KEYPAIR, MINT, SOL, SUPPLY, _account, _key, chain, mint_bytes,
    token_account_bytes,
)

USER = KEYPAIR.address
CREATOR = _key("the-coin-creator")
FEE_RECIPIENT = _key("pump-fee-recipient")
VIRTUAL_TOKEN = 1_073_000_000 * 10**DECIMALS
VIRTUAL_SOL = 30 * SOL
REAL_TOKEN = 793_100_000 * 10**DECIMALS


def curve_bytes(*, complete=False, creator=CREATOR, mayhem=False, quote_mint=None, virtual_token=VIRTUAL_TOKEN,
                virtual_sol=VIRTUAL_SOL, real_token=REAL_TOKEN, length=None) -> bytes:
    out = bytearray(pump.DISC_BONDING_CURVE)
    for value in (virtual_token, virtual_sol, real_token, 0, SUPPLY):
        out += value.to_bytes(8, "little")
    out += bytes([1 if complete else 0]) + pubkey_bytes(creator)
    out += bytes([1 if mayhem else 0, 0])
    out += pubkey_bytes(quote_mint) if quote_mint else bytes(32)
    return bytes(out[:length]) if length else bytes(out)


BUYBACK_RECIPIENTS = [_key(f"buyback-{i}") for i in range(3)]


def global_bytes(*, fee_bps=100, creator_fee_bps=30, buyback=True) -> bytes:
    out = bytearray(b"\x00" * 8)                       # disc
    out += bytes([1]) + pubkey_bytes(_key("authority")) + pubkey_bytes(FEE_RECIPIENT)
    for value in (VIRTUAL_TOKEN, VIRTUAL_SOL, REAL_TOKEN, SUPPLY, fee_bps):
        out += value.to_bytes(8, "little")
    out += pubkey_bytes(_key("withdraw")) + bytes([0]) + (0).to_bytes(8, "little")
    out += creator_fee_bps.to_bytes(8, "little")
    out += bytes(32 * 7)                               # fee_recipients -> 386
    if not buyback:
        return bytes(out)
    out += bytes(32) + bytes(32) + bytes([1])          # set/admin creator authorities, create_v2_enabled -> 451
    out += bytes(32) + bytes(32) + bytes([0])          # whitelist, reserved recipient, mayhem -> 516
    out += bytes(32 * 7) + bytes([0])                  # reserved recipients, is_cashback_enabled -> 741
    for i in range(8):
        out += pubkey_bytes(BUYBACK_RECIPIENTS[i]) if i < len(BUYBACK_RECIPIENTS) else bytes(32)
    out += (5000).to_bytes(8, "little")                # buyback_basis_points at 997
    return bytes(out)


def curve_chain(*, complete=False, user_lamports=2 * SOL, creator=CREATOR, mayhem=False,
                quote_mint=None, traded_before=True, buyback=True) -> dict:
    curve = pump.bonding_curve(MINT)
    accounts = {
        MINT: _account(TOKEN_PROGRAM, mint_bytes()),
        curve: _account(PUMP_PROGRAM, curve_bytes(complete=complete, creator=creator, mayhem=mayhem, quote_mint=quote_mint)),
        curvebuy.GLOBAL: _account(PUMP_PROGRAM, global_bytes(buyback=buyback)),
        USER: {"owner": SYSTEM_PROGRAM, "data": ["", "base64"], "lamports": user_lamports},
    }
    if traded_before:
        accounts[curvebuy.user_volume_accumulator(USER)] = _account(PUMP_PROGRAM, b"\x00" * 80)
    return accounts


class TestTheAddresses(unittest.TestCase):
    def test_pump_s_well_known_pdas(self):
        # pump's global and event authority, as every explorer shows them.
        self.assertEqual(curvebuy.GLOBAL, "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf")
        self.assertEqual(curvebuy.EVENT_AUTHORITY, "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1")

    def test_the_fee_config_is_seeded_with_pump_s_own_id(self):
        # The IDL's 32 constant bytes for the fee_config seed decode to pump.
        self.assertEqual(curvebuy.PUMP_FEE_CONFIG,
                         buyback._pda([b"fee_config", pubkey_bytes(PUMP_PROGRAM)], buyback.FEE_PROGRAM))
        self.assertNotEqual(curvebuy.PUMP_FEE_CONFIG, buyback.fee_config())

    def test_the_creator_vault_is_the_crank_s_creator_vault(self):
        from indexer import distribute
        self.assertEqual(curvebuy.creator_vault(CREATOR), distribute.creator_vault(CREATOR))


# The coin the deploy repository's probe bought in simulation on 2026-09-05,
# and the account list it resolved from the IDL that mainnet accepted.
PROBED_MINT = "22Zrdq4ia9nXni9625rc4e7JoMuLqSbv7d817P94pump"
PROBED_CREATOR = "Gd5BfwwUVbUZbu6E5NzuKREv3WSQ5REUncWxv2ub3qKD"
PROBED_USER = "burn111111111111111111111111111111111111111"
PROBED_FEE_RECIPIENT = "62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV"
PROBED_BUYBACK_RECIPIENT = "5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD"
PROBED_ACCOUNTS = [
    "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf",   # global
    PROBED_MINT,                                       # base_mint
    "So11111111111111111111111111111111111111112",    # quote_mint
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",    # base_token_program (a create_v2 coin: Token-2022)
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",    # quote_token_program
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",   # associated_token_program
    PROBED_FEE_RECIPIENT,                              # fee_recipient
    "94qWNrtmfn42h3ZjUZwWvK1MEo9uVmmrBPd2hpNjYDjb",   # associated_quote_fee_recipient
    PROBED_BUYBACK_RECIPIENT,                          # buyback_fee_recipient
    "HjQjngTDqoHE6aaGhUqfz9aQ7WZcBRjy5xB8PScLSr8i",   # associated_quote_buyback_fee_recipient
    "FbnUoPuXCkchCCXL2PyxtUFcuYWjLcHg4j3WhaePQb2U",   # bonding_curve
    "889q2pm8sXRKiL6kLsJsw7mxUDhseEAwiAWdqT8piApr",   # associated_base_bonding_curve
    "HgJv4mo7KeQ9STAjJS1dNCENBiM4YhKugdP5N9798SMq",   # associated_quote_bonding_curve
    PROBED_USER,                                       # user
    "71yM5TVcE4ivJcfzjgvkpNsDzsBzQad5bvMaDSPDnCTM",   # associated_base_user
    "GC6uA8fZAQpKb15KXQF83baZUWTFgSb1wtRTb9SQvEUZ",   # associated_quote_user
    "F23LdxWf1KD7UBMeNSHK6PpDYFa5hX6iuxQpwaxbfESq",   # creator_vault
    "9ex4bR9zE6VqMcvhaQzcQAQFKmuscqcg7DUhoWB6gqbo",   # associated_creator_vault
    "B93RbqNN2uoT9esRr6gPg64VgyXA9CW4AvCnKNYdG3Zo",   # sharing_config
    "Hq2wp8uJ9jCPsYgNHex8RtqdvMPfVGoYwjvF1ATiwn2Y",   # global_volume_accumulator
    "8Q5bfNu24mrTHMB8SagZPmK88W6aTYjUReGurC16M7cY",   # user_volume_accumulator
    "DPFk91RU4Ua4g4CNUavPA583DUpq91ojaGSPNAsSLNkR",   # associated_user_volume_accumulator
    "8Wf5TiAheLUqBrKXeYg2JtAFFMWtKdG2BSFgqUcPVwTt",   # fee_config
    "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ",    # fee_program
    "11111111111111111111111111111111",                # system_program
    "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1",   # event_authority
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",    # program
]


class TestTheInstruction(unittest.TestCase):
    def test_the_twenty_seven_accounts_mainnet_accepted_for_the_probed_coin(self):
        metas = curvebuy.buy_accounts(PROBED_MINT, PROBED_USER, PROBED_CREATOR, buyback.TOKEN_2022_PROGRAM,
                                      PROBED_FEE_RECIPIENT, PROBED_BUYBACK_RECIPIENT)
        self.assertEqual([m[0] for m in metas], PROBED_ACCOUNTS)

    def test_flags_as_the_idl_declares_them(self):
        metas = curvebuy.buy_accounts(PROBED_MINT, PROBED_USER, PROBED_CREATOR, buyback.TOKEN_2022_PROGRAM,
                                      PROBED_FEE_RECIPIENT, PROBED_BUYBACK_RECIPIENT)
        self.assertEqual([m[1] for m in metas], [False] * 13 + [True] + [False] * 13)   # only the user signs
        writable = {6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 20, 21}
        self.assertEqual([m[2] for m in metas], [i in writable for i in range(27)])

    def test_the_data_is_disc_amount_max_sol_cost(self):
        data = curvebuy.buy_data(1234, 5678)
        self.assertEqual(data[:8], bytes.fromhex("b817ee6167c5d33d"))
        self.assertEqual(int.from_bytes(data[8:16], "little"), 1234)
        self.assertEqual(int.from_bytes(data[16:24], "little"), 5678)
        self.assertEqual(len(data), 24)


class TestTheDecoders(unittest.TestCase):
    def test_the_curve_in_full(self):
        curve = curvebuy.decode_curve("x", _account(PUMP_PROGRAM, curve_bytes(quote_mint=buyback.WSOL_MINT)))
        self.assertEqual((curve.virtual_token, curve.virtual_sol, curve.real_token, curve.total_supply),
                         (VIRTUAL_TOKEN, VIRTUAL_SOL, REAL_TOKEN, SUPPLY))
        self.assertFalse(curve.complete)
        self.assertEqual(curve.creator, CREATOR)
        self.assertTrue(curve.sol_quoted)
        self.assertEqual(curve.market_cap_lamports, VIRTUAL_SOL * SUPPLY // VIRTUAL_TOKEN)

    def test_a_curve_older_than_the_appended_fields_reads_their_defaults(self):
        curve = curvebuy.decode_curve("x", _account(PUMP_PROGRAM, curve_bytes(length=81)))
        self.assertFalse(curve.mayhem)
        self.assertIsNone(curve.quote_mint)
        self.assertTrue(curve.sol_quoted)

    def test_a_zero_quote_mint_means_sol(self):
        curve = curvebuy.decode_curve("x", _account(PUMP_PROGRAM, curve_bytes()))
        self.assertEqual(curve.quote_mint, buyback.DEFAULT_PUBKEY)
        self.assertTrue(curve.sol_quoted)

    def test_global_s_fee_recipient_rates_and_buyback_recipients(self):
        g = curvebuy.decode_global(_account(PUMP_PROGRAM, global_bytes(fee_bps=95, creator_fee_bps=30)))
        self.assertEqual(g.fee_recipient, FEE_RECIPIENT)
        self.assertEqual((g.fee_bps, g.creator_fee_bps), (95, 30))
        self.assertEqual(list(g.buyback_fee_recipients), BUYBACK_RECIPIENTS)   # the zero slots dropped
        self.assertEqual(g.buyback_bps, 5000)

    def test_a_global_older_than_the_buyback_fields_names_no_recipient(self):
        g = curvebuy.decode_global(_account(PUMP_PROGRAM, global_bytes(buyback=False)))
        self.assertEqual(g.buyback_fee_recipients, ())


class TestTheArithmetic(unittest.TestCase):
    def setUp(self):
        self.curve = curvebuy.decode_curve("x", _account(PUMP_PROGRAM, curve_bytes()))
        self.fees = buyback.Fees(0, 100, 30)

    def test_cost_is_the_constant_product_quote_plus_one_then_fees_rounded_up(self):
        amount = 1_000_000 * 10**DECIMALS
        cost = curvebuy.cost_of(amount, self.curve, self.fees, True)
        sol_in = amount * VIRTUAL_SOL // (VIRTUAL_TOKEN - amount) + 1
        self.assertEqual(cost["quote_in"], sol_in)
        self.assertEqual(cost["protocol_fee"], -(-sol_in * 100 // 10_000))
        self.assertEqual(cost["creator_fee"], -(-sol_in * 30 // 10_000))
        self.assertEqual(cost["lp_fee"], 0)
        self.assertEqual(cost["total"], sol_in + cost["protocol_fee"] + cost["creator_fee"])

    def test_no_creator_fee_when_the_curve_names_no_creator(self):
        cost = curvebuy.cost_of(10**9, self.curve, self.fees, False)
        self.assertEqual(cost["creator_fee"], 0)

    def test_the_amount_for_a_lot_fits_the_lot_with_room_for_slippage(self):
        lot = 50_000_000
        amount = curvebuy.amount_for_lot(lot, self.curve, self.fees, True, 100)
        self.assertGreater(amount, 0)
        self.assertLessEqual(curvebuy.cost_of(amount, self.curve, self.fees, True)["total"], lot)
        tight = curvebuy.amount_for_lot(lot, self.curve, self.fees, True, 0)
        self.assertLessEqual(curvebuy.cost_of(tight, self.curve, self.fees, True)["total"], lot)
        self.assertLess(amount, tight)

    def test_the_amount_never_exceeds_what_the_curve_still_holds(self):
        nearly_empty = curvebuy.decode_curve("x", _account(PUMP_PROGRAM, curve_bytes(real_token=1_000)))
        amount = curvebuy.amount_for_lot(10 * SOL, nearly_empty, self.fees, True, 0)
        self.assertLessEqual(amount, 1_000)


class TestObserveAndPlan(unittest.TestCase):
    def test_observe_reads_the_curve_global_and_wallet(self):
        state = curvebuy.observe(FakeRpc(curve_chain()), MINT, USER)
        self.assertEqual(state.curve.creator, CREATOR)
        self.assertEqual(state.global_.fee_recipient, FEE_RECIPIENT)
        self.assertIsNone(state.fee_config)
        self.assertEqual(state.fees, buyback.Fees(0, 100, 30))   # Global's flat rates without a fee config
        self.assertEqual(state.user_lamports, 2 * SOL)
        self.assertIsNone(state.user_base_balance)

    def test_a_completed_curve_is_refused_here_the_pool_is_its_venue(self):
        with self.assertRaises(buyback.BuybackError) as caught:
            curvebuy.observe(FakeRpc(curve_chain(complete=True)), MINT, USER)
        self.assertIn("graduated", str(caught.exception))

    def test_a_global_without_buyback_recipients_is_refused_before_building(self):
        # The legacy buy answered 6062 BuybackFeeRecipientMissing on mainnet;
        # buy_v2 must name one, so a global that has none cannot be bought from.
        with self.assertRaises(buyback.BuybackError) as caught:
            curvebuy.observe(FakeRpc(curve_chain(buyback=False)), MINT, USER)
        self.assertIn("buyback fee recipient", str(caught.exception))

    def test_a_non_sol_quote_and_mayhem_mode_are_refused(self):
        with self.assertRaises(buyback.BuybackError):
            curvebuy.observe(FakeRpc(curve_chain(quote_mint=_key("usd-something"))), MINT, USER)
        with self.assertRaises(buyback.BuybackError):
            curvebuy.observe(FakeRpc(curve_chain(mayhem=True)), MINT, USER)

    def test_the_plan_wraps_the_lot_buys_burns_and_unwraps_in_one_transaction(self):
        state = curvebuy.observe(FakeRpc(curve_chain()), MINT, USER)
        plan = curvebuy.plan_buy_and_burn(state, lot_lamports=50_000_000, slippage_bps=100,
                                          choose=lambda options: options[0])
        self.assertEqual(plan.kind, "curve_buy_and_burn")
        programs = [ix[0] for ix in plan.instructions]
        self.assertEqual(programs, [buyback.COMPUTE_BUDGET_PROGRAM, buyback.ASSOCIATED_TOKEN_PROGRAM,
                                    buyback.ASSOCIATED_TOKEN_PROGRAM, SYSTEM_PROGRAM, TOKEN_PROGRAM,
                                    PUMP_PROGRAM, TOKEN_PROGRAM, TOKEN_PROGRAM])
        buy = plan.instructions[5]
        self.assertEqual(buy[2], curvebuy.buy_data(plan.base_out, 50_000_000))
        self.assertEqual(buy[1][8][0], BUYBACK_RECIPIENTS[0])
        self.assertEqual(plan.accounts["buyback_fee_recipient"], BUYBACK_RECIPIENTS[0])
        burn = plan.instructions[6]
        self.assertEqual(int.from_bytes(burn[2][1:9], "little"), plan.base_out)
        self.assertEqual(plan.burn_total, plan.base_out)
        self.assertLessEqual(plan.expected_cost["total"], 50_000_000)
        self.assertGreater(plan.price_after, plan.price_before)
        self.assertEqual(plan.supply_after, SUPPLY - plan.base_out)
        self.assertTrue(any("bonding curve" in n for n in plan.notes))

    def test_a_wallet_new_to_pump_gets_its_volume_accumulator_created_first(self):
        state = curvebuy.observe(FakeRpc(curve_chain(traded_before=False)), MINT, USER)
        self.assertFalse(state.user_volume_exists)
        plan = curvebuy.plan_buy_and_burn(state, lot_lamports=50_000_000)
        programs = [ix[0] for ix in plan.instructions]
        self.assertEqual(programs[:3], [buyback.COMPUTE_BUDGET_PROGRAM, PUMP_PROGRAM, buyback.ASSOCIATED_TOKEN_PROGRAM])
        self.assertEqual(len(programs), 9)
        init = plan.instructions[1]
        self.assertEqual(init[2], curvebuy.INIT_USER_VOLUME_ACCUMULATOR)
        self.assertEqual([m[0] for m in init[1]], [USER, USER, curvebuy.user_volume_accumulator(USER),
                                                   SYSTEM_PROGRAM, curvebuy.EVENT_AUTHORITY, PUMP_PROGRAM])
        self.assertEqual([m[1] for m in init[1]], [True, False, False, False, False, False])

    def test_a_poor_wallet_is_refused_before_anything_is_built(self):
        state = curvebuy.observe(FakeRpc(curve_chain(user_lamports=10_000_000)), MINT, USER)
        with self.assertRaises(buyback.BuybackError):
            curvebuy.plan_buy_and_burn(state, lot_lamports=50_000_000)

    def test_a_message_builds_and_simulates_through_the_shared_path(self):
        rpc = FakeRpc(curve_chain())
        state = curvebuy.observe(rpc, MINT, USER)
        plan = curvebuy.plan_buy_and_burn(state, lot_lamports=50_000_000)
        result = buyback._execute(rpc, plan, None, send=False)
        self.assertIsNone(result["simulation"]["err"])
        self.assertFalse(result["sent"])
        self.assertEqual(result["plan"]["instruction_count"], 8)


class TestTheVenue(unittest.TestCase):
    def test_a_coin_on_its_curve_is_planned_on_the_curve(self):
        plan = buyback.plan_for(FakeRpc(curve_chain()), MINT, USER, lot_lamports=50_000_000)
        self.assertEqual(plan.kind, "curve_buy_and_burn")

    def test_a_graduated_coin_is_planned_on_its_pool(self):
        plan = buyback.plan_for(FakeRpc(chain()), MINT, USER, lot_lamports=50_000_000)
        self.assertEqual(plan.kind, "buy_and_burn")

    def test_render_names_the_venue(self):
        plan = buyback.plan_for(FakeRpc(curve_chain()), MINT, USER, lot_lamports=50_000_000)
        text = buyback.render(plan)
        self.assertIn("pump bonding curve", text)
        self.assertIn("max_sol_cost", text)


if __name__ == "__main__":
    unittest.main()

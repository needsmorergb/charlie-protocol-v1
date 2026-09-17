"""The dev's buy at launch: pump's legacy `buy` bundled after `create`.

Pinned against pump's IDL (discriminator, the sixteen accounts in order,
the argument layout), priced by the curve's own arithmetic on a fresh curve,
and measured: create + token account + buy fit one packet with room, and
so does the variant that first creates the dev's volume accumulator.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from indexer import curvebuy, launch, launchbuy  # noqa: E402
from indexer.base58 import pubkey_bytes  # noqa: E402
from indexer.curve import find_program_address  # noqa: E402
from indexer.enroll import associated_token_address, bonding_curve_address  # noqa: E402
from test_buyback import SOL, _account, _key, fee_config_bytes  # noqa: E402
from test_curvebuy import BUYBACK_RECIPIENTS, FEE_RECIPIENT, REAL_TOKEN, VIRTUAL_SOL, VIRTUAL_TOKEN, global_bytes  # noqa: E402
from test_launch import BLOCKHASH, DEV, URI, _idl, _meta  # noqa: E402

MINT = launch.new_mint(b"\x31" * 32).address
# Measured 2026-09-16 with a pump-length URI; the mainnet simulation of the
# same shape passed. Pinned so an added account cannot silently approach 1232.
SIZE_WITH_ACCUMULATOR = 1123
SIZE_FIRST_TIME_WALLET = 1140
GLOBAL = curvebuy.decode_global(_account(curvebuy.PUMP_PROGRAM, global_bytes()))
FEE_CONFIG = curvebuy.decode_fee_config(_account(curvebuy.FEE_PROGRAM, fee_config_bytes()))


def _buy_idl():
    return next(i for i in _idl()["instructions"] if i["name"] == "buy")


class TestTheInstruction(unittest.TestCase):
    def test_the_discriminator_is_the_idl_s(self):
        self.assertEqual(list(launchbuy.BUY), _buy_idl()["discriminator"])

    def test_the_idl_s_sixteen_in_order_with_its_flags_then_the_two_remaining_accounts(self):
        idl = _buy_idl()["accounts"]
        metas = launchbuy.buy_accounts(MINT, DEV, DEV, FEE_RECIPIENT, BUYBACK_RECIPIENTS[0])
        self.assertEqual(len(idl), 16)
        self.assertEqual(len(metas), 18, "the IDL's sixteen, then bonding-curve-v2 and the buyback recipient")
        self.assertEqual([m[1] for m in metas[:16]], [bool(a.get("signer")) for a in idl])
        self.assertEqual([m[2] for m in metas[:16]], [bool(a.get("writable")) for a in idl])
        self.assertEqual(metas[6][0], DEV, "the user is the seventh account and the only signer")
        self.assertEqual(metas[16], (launchbuy.bonding_curve_v2_address(MINT), False, False))
        self.assertEqual(metas[17], (BUYBACK_RECIPIENTS[0], False, True))
        self.assertEqual([m[1] for m in metas].count(True), 1)

    def test_bonding_curve_v2_is_the_mainnet_derivation(self):
        # Two coins created on mainnet 2026-09-16 and the account their buys carried.
        self.assertEqual(launchbuy.bonding_curve_v2_address("e74VtcQyE8a3as6dRMRNM3tLmGmQDVqHgQRosFhpump"),
                         "FfsrXLmjGt8cjyzZ6AkU7MKZxiLEo6vVeetNt3coRSXf")
        self.assertEqual(launchbuy.bonding_curve_v2_address("FuAiFMHmAy4B8b4AK9EZQDzJSfHJWhVRwWN4c9ZbijHu"),
                         "33iisiT2oPraCAt2gdwGrrqjbjvUYEVJNz6A7Samwpkv")

    def test_the_pdas_follow_the_idl_s_seeds(self):
        metas = dict(zip([a["name"] for a in _buy_idl()["accounts"]], [m[0] for m in launchbuy.buy_accounts(MINT, DEV, DEV, FEE_RECIPIENT, BUYBACK_RECIPIENTS[0])]))
        self.assertEqual(metas["bonding_curve"], bonding_curve_address(MINT))
        self.assertEqual(metas["associated_bonding_curve"], associated_token_address(bonding_curve_address(MINT), MINT))
        self.assertEqual(metas["associated_user"], associated_token_address(DEV, MINT))
        self.assertEqual(metas["creator_vault"], find_program_address([b"creator-vault", pubkey_bytes(DEV)], curvebuy.PUMP_PROGRAM)[0])
        self.assertEqual(metas["user_volume_accumulator"], find_program_address([b"user_volume_accumulator", pubkey_bytes(DEV)], curvebuy.PUMP_PROGRAM)[0])
        self.assertEqual(metas["fee_config"], find_program_address([b"fee_config", pubkey_bytes(curvebuy.PUMP_PROGRAM)], curvebuy.FEE_PROGRAM)[0])
        self.assertEqual(metas["fee_recipient"], FEE_RECIPIENT)

    def test_the_data_is_disc_amount_max_sol_cost_then_one_bool_byte(self):
        data = launchbuy.buy_data(123_456, 7_890)
        self.assertEqual(data[:8], launchbuy.BUY)
        self.assertEqual(int.from_bytes(data[8:16], "little"), 123_456)
        self.assertEqual(int.from_bytes(data[16:24], "little"), 7_890)
        self.assertEqual(data[24:], b"\x01")
        self.assertEqual(len(data), 25)


class TestTheAmount(unittest.TestCase):
    def test_sol_parsing_and_its_bounds(self):
        self.assertEqual(launchbuy.parse_sol("0.5"), 500_000_000)
        self.assertEqual(launchbuy.parse_sol(" 2 "), 2 * SOL)
        self.assertEqual(launchbuy.parse_sol(".25"), 250_000_000)
        for bad in ("", "abc", "1.2.3", "0.0001", "86", "-1", "1e3"):
            with self.assertRaises(launchbuy.BuyError, msg=bad):
                launchbuy.parse_sol(bad)

    def test_a_fresh_curve_is_the_global_s_initial_state_with_the_dev_as_creator(self):
        curve = launchbuy.fresh_curve(GLOBAL, MINT, DEV)
        self.assertEqual((curve.virtual_token, curve.virtual_sol, curve.real_token, curve.real_sol),
                         (VIRTUAL_TOKEN, VIRTUAL_SOL, REAL_TOKEN, 0))
        self.assertEqual(curve.creator, DEV)
        self.assertFalse(curve.complete)

    def test_the_quote_never_costs_more_than_the_lot_and_carries_the_lot_as_the_bound(self):
        q = launchbuy.quote(SOL, GLOBAL, FEE_CONFIG, MINT, DEV)
        self.assertLessEqual(q.cost["total"], SOL)
        self.assertEqual(q.max_sol_cost, SOL)
        self.assertGreater(q.tokens, 0)
        # 1 SOL into 30 virtual SOL against 1.073B virtual tokens: about 3.3% of the 1B supply.
        self.assertAlmostEqual(q.percent_of_supply, 3.3, delta=0.3)
        self.assertEqual(q.fees, FEE_CONFIG.fee_tiers[0].fees, "a new curve is in the first tier")

    def test_describe_is_what_the_page_shows(self):
        d = launchbuy.describe(launchbuy.quote(SOL, GLOBAL, FEE_CONFIG, MINT, DEV))
        self.assertEqual(d["sol"], 1.0)
        self.assertTrue(d["before_split"])
        self.assertGreater(d["tokens"], 1_000_000)
        self.assertGreater(d["fee_lamports"], 0)


class TestTheTransaction(unittest.TestCase):
    def _message(self, accumulator_exists: bool) -> bytes:
        q = launchbuy.quote(SOL, GLOBAL, FEE_CONFIG, MINT, DEV)
        extra = launchbuy.instructions(MINT, DEV, GLOBAL, q, accumulator_exists=accumulator_exists)
        return launch.create_message(MINT, DEV, _meta(uri=URI), BLOCKHASH, extra=extra)

    def test_create_then_token_account_then_buy_fits_one_packet(self):
        message = self._message(accumulator_exists=True)
        self.assertEqual(launch.signer_addresses(message), [DEV, MINT], "still two signers")
        self.assertEqual(launch.size_of(message), SIZE_WITH_ACCUMULATOR)
        self.assertLess(launch.size_of(message), 1232)

    def test_a_first_time_pump_wallet_also_fits_with_its_accumulator_created(self):
        message = self._message(accumulator_exists=False)
        self.assertEqual(launch.size_of(message), SIZE_FIRST_TIME_WALLET)
        self.assertLess(launch.size_of(message), 1232)

    def test_a_global_without_buyback_recipients_is_refused(self):
        bare = curvebuy.decode_global(_account(curvebuy.PUMP_PROGRAM, global_bytes(buyback=False)))
        q = launchbuy.quote(SOL, bare, FEE_CONFIG, MINT, DEV)
        with self.assertRaises(launchbuy.BuyError):
            launchbuy.instructions(MINT, DEV, bare, q, accumulator_exists=True)

    def test_no_buy_is_the_old_create_message(self):
        self.assertEqual(launch.size_of(launch.create_message(MINT, DEV, _meta(uri=URI), BLOCKHASH)), 779)

    def test_observe_reads_global_fee_config_and_the_accumulator_in_one_call(self):
        class Rpc:
            def __init__(self, with_accumulator):
                self.calls = 0
                self.with_accumulator = with_accumulator

            def accounts(self, addresses):
                self.calls += 1
                self.assertion = addresses
                return [_account(curvebuy.PUMP_PROGRAM, global_bytes()),
                        _account(curvebuy.FEE_PROGRAM, fee_config_bytes()),
                        _account(curvebuy.PUMP_PROGRAM, b"\x00" * 80) if self.with_accumulator else None]

        rpc = Rpc(True)
        global_, fee_config, exists = launchbuy.observe(rpc, DEV)
        self.assertEqual(rpc.calls, 1)
        self.assertEqual(global_.fee_recipient, FEE_RECIPIENT)
        self.assertEqual(len(fee_config.fee_tiers), 4)
        self.assertTrue(exists)
        self.assertFalse(launchbuy.observe(Rpc(False), DEV)[2])


if __name__ == "__main__":
    unittest.main()

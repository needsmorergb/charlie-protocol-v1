"""Claiming the LaunchLab platform fee and ending in SOL.

Tier 1 (SOL quote) consults no price at all; tier 2 (USDC/USDT) consults the
pool's own reserves and puts the answer in the transaction as `min_out`. A
stock quote is refused rather than improvised, because the guards it needs
are not built.
"""

import base64
import re
import struct
import unittest
from pathlib import Path

from indexer import platform_fees as pf
from indexer.base58 import pubkey_bytes

ROOT = Path(__file__).resolve().parents[1]
ADMIN = "8SvEu1bvkhgaSkZW4XHLzfw8djd748KAVHMwvkYGfyr8"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
POOL = "58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2"
SPYX = "XsDoVfqeBukxuZHWhdvWHBhgEHjGNst4MLodqsJHzoB"
VAULT0 = "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R"
VAULT1 = "8HoQnePLqPj4M7PUDzfw8e3Ymdwgc7NLGnaTUapubyvu"
AMM_CONFIG = "D4FPEruKEHrG5TenZ2mpDGEfu1iUvTiqBxvpU8HLBvC2"
# 1M USDC against 5,000 SOL: about $200 a SOL.
RESERVE_USDC = 1_000_000_000_000
RESERVE_SOL = 5_000_000_000_000


def _token_account(mint: str, amount: int) -> dict:
    data = bytearray(165)
    data[0:32] = pubkey_bytes(mint)
    data[32:64] = pubkey_bytes(ADMIN)
    data[64:72] = struct.pack("<Q", amount)
    return {"owner": TOKEN, "data": [base64.b64encode(bytes(data)).decode(), "base64"]}


def _pool_account(mint0: str, mint1: str, fees0: int = 0, fees1: int = 0) -> dict:
    data = bytearray(637)
    def put(off, key):
        data[off:off + 32] = pubkey_bytes(key)
    put(8, AMM_CONFIG)
    put(72, VAULT0)
    put(104, VAULT1)
    put(136, POOL)
    put(168, mint0)
    put(200, mint1)
    put(232, TOKEN)
    put(264, TOKEN)
    put(296, POOL)
    # protocol fees at 341/349, fund 357/365, creator 397/405: split the
    # counter across all three so a decoder that skips one is caught.
    for side, total in ((0, fees0), (1, fees1)):
        a, b = total // 3, total // 3
        struct.pack_into("<Q", data, 341 + 8 * side, a)
        struct.pack_into("<Q", data, 357 + 8 * side, b)
        struct.pack_into("<Q", data, 397 + 8 * side, total - a - b)
    return {"owner": pf.CPMM, "data": [base64.b64encode(bytes(data)).decode(), "base64"]}


def _amm_config(trade_fee_rate: int, creator_fee_rate: int = 0) -> dict:
    data = bytearray(236)
    struct.pack_into("<Q", data, 12, trade_fee_rate)
    struct.pack_into("<Q", data, 20, 120_000)   # protocol share of the fee, not a swap rate
    struct.pack_into("<Q", data, 108, creator_fee_rate)
    return {"owner": pf.CPMM, "data": [base64.b64encode(bytes(data)).decode(), "base64"]}


class FakeRpc:
    def __init__(self, table):
        self.table = table

    def accounts(self, keys):
        return [self.table.get(k) for k in keys]


class TestTheClaimInstruction(unittest.TestCase):
    def test_the_discriminator_is_the_one_the_crate_asserts(self):
        rust = (ROOT / "launchlab" / "src" / "raydium.rs").read_text(encoding="utf-8")
        match = re.search(r"DISC_CLAIM_PLATFORM_FEE_FROM_VAULT:\s*\[u8;\s*8\]\s*=\s*\[([^\]]+)\]", rust)
        self.assertIsNotNone(match)
        self.assertEqual(bytes(int(b) for b in match.group(1).split(",")),
                         pf.DISC_CLAIM_PLATFORM_FEE_FROM_VAULT)

    def test_it_takes_no_arguments(self):
        """It moves what the vault holds; there is no amount to get wrong."""
        _, _, data = pf.ix_claim_platform_fee(ADMIN, pf.platform_config(ADMIN), "x", pf.WSOL, TOKEN)
        self.assertEqual(data, pf.DISC_CLAIM_PLATFORM_FEE_FROM_VAULT)
        self.assertEqual(len(data), 8)

    def test_the_fee_wallet_is_the_only_signer(self):
        _, metas, _ = pf.ix_claim_platform_fee(ADMIN, pf.platform_config(ADMIN), "x", pf.WSOL, TOKEN)
        self.assertEqual(len(metas), 9)
        self.assertEqual([m[0] for m in metas if m[1]], [ADMIN])


class TestTheSwap(unittest.TestCase):
    def test_the_discriminator_is_the_one_the_crate_asserts(self):
        rust = (ROOT / "launchlab" / "src" / "raydium.rs").read_text(encoding="utf-8")
        match = re.search(r"DISC_SWAP_BASE_INPUT:\s*\[u8;\s*8\]\s*=\s*\[([^\]]+)\]", rust)
        self.assertIsNotNone(match)
        self.assertEqual(bytes(int(b) for b in match.group(1).split(",")), pf.DISC_SWAP_BASE_INPUT)

    def test_min_out_is_in_the_instruction(self):
        pool = pf.read_pool(base64.b64decode(_pool_account(pf.USDC, pf.WSOL)["data"][0]))
        _, _, data = pf.ix_swap_base_input(ADMIN, POOL, pool, True, "a", "b", 1_000_000, 4_949)
        amount_in, min_out = struct.unpack("<QQ", data[8:24])
        self.assertEqual((amount_in, min_out), (1_000_000, 4_949))

    def test_the_constant_product_is_reduced_by_slippage(self):
        # 1e6 in against reserves 1e12 / 5e9: out is ~4999 before slippage.
        exact = 5_000_000_000 * 1_000_000 // (1_000_000_000_000 + 1_000_000)
        self.assertEqual(pf.quote_out(1_000_000, 1_000_000_000_000, 5_000_000_000, 0, fee_rate=0), exact)
        self.assertEqual(pf.quote_out(1_000_000, 1_000_000_000_000, 5_000_000_000, 100, fee_rate=0),
                         exact * 9_900 // 10_000)

    def test_an_unfunded_pool_is_refused(self):
        with self.assertRaises(pf.PlatformFeeError):
            pf.quote_out(1_000, 0, 5_000, fee_rate=0)

    def test_the_pool_fee_comes_off_the_input_rounded_up(self):
        # 2500/1e6 of 1,000,001 is 2500.0025, which the program rounds to 2501.
        net = 1_000_001 - 2_501
        exact = 5_000_000_000 * net // (1_000_000_000_000 + net)
        self.assertEqual(pf.quote_out(1_000_001, 1_000_000_000_000, 5_000_000_000, 0, fee_rate=2_500), exact)

    def test_the_fee_rate_cannot_be_left_out(self):
        """Leaving it out is the bug this argument exists to prevent."""
        with self.assertRaises(TypeError):
            pf.quote_out(1_000, 1_000_000, 5_000)  # noqa

    def test_the_amm_config_fee_is_read_from_offset_12(self):
        amm = pf.read_amm_config(base64.b64decode(_amm_config(3_000, 500)["data"][0]))
        self.assertEqual(amm, {"trade_fee_rate": 3_000, "creator_fee_rate": 500})

    def test_fee_counters_are_not_reserves(self):
        pool = pf.read_pool(base64.b64decode(_pool_account(pf.USDC, pf.WSOL, 700, 11)["data"][0]))
        self.assertEqual((pool["fees0"], pool["fees1"]), (700, 11))
        self.assertFalse(pool["enable_creator_fee"])


class TestPlan(unittest.TestCase):
    def _rpc(self, quote, vault_amount, pool=None):
        config = pf.platform_config(ADMIN)
        table = {
            quote: {"owner": TOKEN, "data": ["", "base64"]},
            pf.platform_fee_vault(config, quote): _token_account(quote, vault_amount),
        }
        if pool:
            # `_pool_account` writes VAULT0 and VAULT1 as the two vault
            # addresses, and mint0 is the quote, so vault0 holds the quote
            # and vault1 holds wSOL. Mapping them the other way round is what
            # the decoder's expect_mint check exists to catch.
            table[POOL] = pool
            table[VAULT0] = _token_account(quote, RESERVE_USDC)
            table[VAULT1] = _token_account(pf.WSOL, RESERVE_SOL)
            table[AMM_CONFIG] = _amm_config(2_500)
        return FakeRpc(table)

    def test_a_sol_quote_never_swaps(self):
        out = pf.plan(self._rpc(pf.WSOL, 2_000_000), ADMIN, pf.WSOL)
        programs = [ix[0] for ix in out["instructions"]]
        self.assertNotIn(pf.CPMM, programs)
        self.assertIn(pf.LAUNCHLAB, programs)
        self.assertEqual(out["sol_out"], 2_000_000)
        self.assertIn("no swap", " ".join(out["notes"]))

    def test_an_empty_vault_builds_nothing(self):
        out = pf.plan(self._rpc(pf.WSOL, 0), ADMIN, pf.WSOL)
        self.assertEqual(out["instructions"], [])

    def test_dust_below_the_minimum_stands_down(self):
        out = pf.plan(self._rpc(pf.WSOL, 10), ADMIN, pf.WSOL)
        self.assertEqual(out["instructions"], [])
        self.assertIn("minimum", " ".join(out["notes"]))

    def test_a_usdc_quote_without_a_pool_is_refused_rather_than_guessed(self):
        with self.assertRaises(pf.PlatformFeeError) as ctx:
            pf.plan(self._rpc(pf.USDC, 5_000_000), ADMIN, pf.USDC)
        self.assertIn("--pool", str(ctx.exception))

    def test_a_usdc_quote_swaps_once_and_unwraps(self):
        rpc = self._rpc(pf.USDC, 5_000_000, pool=_pool_account(pf.USDC, pf.WSOL))
        out = pf.plan(rpc, ADMIN, pf.USDC, pool_key=POOL)
        programs = [ix[0] for ix in out["instructions"]]
        self.assertEqual(programs.count(pf.CPMM), 1, "one hop, not a route")
        self.assertGreater(out["sol_out"], 0)

    def test_the_plan_prices_after_the_fee_and_net_of_the_counters(self):
        rpc = self._rpc(pf.USDC, 5_000_000, pool=_pool_account(pf.USDC, pf.WSOL, 9_000_000, 40_000_000))
        out = pf.plan(rpc, ADMIN, pf.USDC, pool_key=POOL)
        expected = pf.quote_out(5_000_000, RESERVE_USDC - 9_000_000, RESERVE_SOL - 40_000_000,
                                fee_rate=2_500)
        self.assertEqual(out["sol_out"], expected)
        swap = next(ix for ix in out["instructions"] if ix[0] == pf.CPMM)
        self.assertEqual(struct.unpack("<QQ", swap[2][8:24]), (5_000_000, expected))

    def test_a_usdc_claim_worth_less_than_the_floor_stands_down(self):
        # 0.1 USDC is about 0.0005 SOL, under the 0.001 SOL floor.
        rpc = self._rpc(pf.USDC, 100_000, pool=_pool_account(pf.USDC, pf.WSOL))
        out = pf.plan(rpc, ADMIN, pf.USDC, pool_key=POOL)
        self.assertEqual(out["instructions"], [])
        self.assertIn("minimum", " ".join(out["notes"]))

    def test_a_pool_that_does_not_trade_the_quote_is_refused(self):
        rpc = self._rpc(pf.USDC, 5_000_000, pool=_pool_account(pf.USDT, pf.WSOL))
        with self.assertRaises(pf.PlatformFeeError):
            pf.plan(rpc, ADMIN, pf.USDC, pool_key=POOL)

    def test_a_stock_quote_is_refused_and_says_why(self):
        with self.assertRaises(pf.PlatformFeeError) as ctx:
            pf.plan(self._rpc(SPYX, 5_000_000), ADMIN, SPYX)
        message = str(ctx.exception)
        self.assertIn("transfer-hook", message)
        self.assertIn("not built", message)


if __name__ == "__main__":
    unittest.main()

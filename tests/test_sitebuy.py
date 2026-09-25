"""The coin page's buy: `indexer/sitebuy.py` and `api/buy.py`.

Offline. The chain is the byte-built fixtures `test_buyback` and
`test_curvebuy` already serve (a curve, pump's global, a PumpSwap pool, its
configs), read through the same fake RPC. What is pinned: the venue follows
the curve's `complete` flag, each venue's accounts and data, the creator
vault is the one the LIVE curve names and not the buyer's, the slippage
bounds the SOL of an exact-out buy, nothing is burned, the inputs are
checked, and the relay takes a signed buy from either venue.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from indexer import buyback, curvebuy, launchbuy, pump, relay, sitebuy  # noqa: E402
from indexer.base58 import decode, encode  # noqa: E402
from indexer.enroll import associated_token_address as ata  # noqa: E402
from indexer.message import signed_transaction  # noqa: E402
from indexer.pump import PUMP_AMM_PROGRAM, PUMP_PROGRAM, TOKEN_2022_PROGRAM, TOKEN_PROGRAM  # noqa: E402

from test_buyback import (  # noqa: E402
    BLOCKHASH, BUYBACK, KEYPAIR, MINT, RECIPIENTS, SOL, FakeRpc, _account, _key, chain, mint_bytes,
)
from test_curvebuy import BUYBACK_RECIPIENTS, CREATOR, FEE_RECIPIENT, curve_bytes, curve_chain, global_bytes  # noqa: E402

_spec = importlib.util.spec_from_file_location("api_buy", ROOT / "api" / "buy.py")
api_buy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api_buy)

BUYER = KEYPAIR.address
LAMPORTS = 100_000_000   # 0.1 SOL


def first(options):
    return options[0]


def amm_chain(**kw) -> dict:
    """A graduated coin: the pool fixtures plus a complete curve and pump's global."""
    accounts = chain(**kw)
    accounts[pump.bonding_curve(MINT)] = _account(PUMP_PROGRAM, curve_bytes(complete=True))
    accounts[curvebuy.GLOBAL] = _account(PUMP_PROGRAM, global_bytes())
    return accounts


def ixs_by_program(buy, program):
    return [ix for ix in buy.instructions if ix[0] == program]


class TestInputs(unittest.TestCase):
    def test_sol_is_read_exactly_and_bounded(self):
        self.assertEqual(sitebuy.parse_sol("0.5"), 500_000_000)
        self.assertEqual(sitebuy.parse_sol("1"), SOL)
        self.assertEqual(sitebuy.parse_sol(".001"), 1_000_000)
        self.assertEqual(sitebuy.parse_sol("100"), 100 * SOL)
        for bad in ("", "abc", "0.0009", "100.000000001", "1e3", "-1", "1.2.3", "0.1234567891", "１"):
            with self.subTest(bad=bad), self.assertRaises(sitebuy.BuyError):
                sitebuy.parse_sol(bad)

    def test_slippage_defaults_to_five_percent_and_is_bounded(self):
        self.assertEqual(sitebuy.parse_slippage(""), 500)
        self.assertEqual(sitebuy.parse_slippage("50"), 50)
        self.assertEqual(sitebuy.parse_slippage("5000"), 5000)
        for bad in ("49", "5001", "5%", "1.5", "-100"):
            with self.subTest(bad=bad), self.assertRaises(sitebuy.BuyError):
                sitebuy.parse_slippage(bad)

    def test_addresses_must_be_32_byte_base58(self):
        self.assertEqual(sitebuy.parse_address(MINT, "coin"), MINT)
        for bad in ("", "0" * 44, MINT[:-1] + "0", "abc", encode(b"\x01" * 31), MINT + "1"):
            with self.subTest(bad=bad), self.assertRaises(sitebuy.BuyError):
                sitebuy.parse_address(bad, "coin")

    def test_the_bound_is_the_sol_plus_its_slippage(self):
        self.assertEqual(sitebuy.max_cost(LAMPORTS, 500), 105_000_000)
        self.assertEqual(sitebuy.max_cost(LAMPORTS, 50), 100_500_000)


class TestTheCurveVenue(unittest.TestCase):
    def setUp(self):
        self.buy = sitebuy.plan(FakeRpc(curve_chain()), MINT, BUYER, LAMPORTS, 500, choose=first)

    def test_an_ungraduated_coin_is_bought_on_its_curve(self):
        self.assertEqual(self.buy.venue, "curve")
        self.assertEqual(self.buy.token_program, TOKEN_PROGRAM)

    def test_the_instructions_are_budget_account_then_the_legacy_buy(self):
        programs = [ix[0] for ix in self.buy.instructions]
        self.assertEqual(programs, [buyback.COMPUTE_BUDGET_PROGRAM, buyback.ASSOCIATED_TOKEN_PROGRAM, PUMP_PROGRAM])
        self.assertEqual(self.buy.instructions[0][2], bytes([2]) + sitebuy.CURVE_COMPUTE_UNITS.to_bytes(4, "little"))
        # No priority fee, no wSOL, and never a burn.
        self.assertEqual(len(ixs_by_program(self.buy, buyback.COMPUTE_BUDGET_PROGRAM)), 1)
        self.assertEqual(ixs_by_program(self.buy, TOKEN_PROGRAM), [])

    def test_the_buyer_s_token_account_is_created_idempotently(self):
        _program, metas, data = self.buy.instructions[1]
        self.assertEqual(data, bytes([1]))
        self.assertEqual(metas[0], (BUYER, True, True))
        self.assertEqual(metas[1][0], ata(BUYER, MINT, TOKEN_PROGRAM))
        self.assertEqual(metas[5][0], TOKEN_PROGRAM)

    def test_the_accounts_are_the_launch_door_s_with_the_live_curve_s_creator(self):
        _program, metas, _data = self.buy.instructions[-1]
        self.assertEqual(metas, launchbuy.buy_accounts(MINT, BUYER, CREATOR, FEE_RECIPIENT, BUYBACK_RECIPIENTS[0]))
        self.assertEqual(len(metas), 18)
        self.assertEqual(metas[9], (curvebuy.creator_vault(CREATOR), False, True))
        self.assertNotEqual(metas[9][0], curvebuy.creator_vault(BUYER))
        # The two the IDL omits: bonding-curve-v2 (read), then a buyback recipient (written).
        self.assertEqual(metas[16], (launchbuy.bonding_curve_v2_address(MINT), False, False))
        self.assertEqual(metas[17], (BUYBACK_RECIPIENTS[0], False, True))
        self.assertEqual([m for m in metas if m[1]], [(BUYER, True, True)])

    def test_the_data_asks_for_the_quoted_tokens_for_at_most_the_bound(self):
        data = self.buy.instructions[-1][2]
        self.assertEqual(data[:8], launchbuy.BUY)
        self.assertEqual(int.from_bytes(data[8:16], "little"), self.buy.tokens)
        self.assertEqual(int.from_bytes(data[16:24], "little"), 105_000_000)
        self.assertEqual(data[24:], b"\x01")

    def test_the_quote_is_the_most_the_sol_buys_at_the_curve_s_price(self):
        curve = curvebuy.decode_curve("x", _account(PUMP_PROGRAM, curve_bytes()))
        fees = buyback.Fees(0, 100, 30)   # Global's flat rates: the fixture has no fee config
        self.assertEqual(self.buy.tokens, curvebuy.amount_for_lot(LAMPORTS, curve, fees, True, 0))
        self.assertLessEqual(self.buy.cost["total"], LAMPORTS)
        self.assertGreater(curvebuy.cost_of(self.buy.tokens + 1_000_000, curve, fees, True)["total"], LAMPORTS)
        described = self.buy.describe()
        self.assertEqual(described["expected_tokens_raw"], str(self.buy.tokens))
        self.assertEqual(described["min_tokens_raw"], str(self.buy.tokens))   # exact out
        self.assertEqual(described["max_sol_lamports"], 105_000_000)
        self.assertEqual(described["fee_bps"], 130)
        self.assertGreater(described["price_impact_bps"], 0)

    def test_a_wallet_new_to_pump_gets_its_volume_accumulator_first(self):
        buy = sitebuy.plan(FakeRpc(curve_chain(traded_before=False)), MINT, BUYER, LAMPORTS, 500, choose=first)
        self.assertEqual(buy.instructions[2], curvebuy.ix_init_user_volume_accumulator(BUYER))
        self.assertEqual(buy.instructions[3][0], PUMP_PROGRAM)

    def test_a_token_2022_coin_uses_its_own_program_throughout(self):
        accounts = curve_chain()
        accounts[MINT] = _account(TOKEN_2022_PROGRAM, mint_bytes())
        buy = sitebuy.plan(FakeRpc(accounts), MINT, BUYER, LAMPORTS, 500, choose=first)
        metas = buy.instructions[-1][1]
        self.assertEqual(metas[4][0], ata(pump.bonding_curve(MINT), MINT, TOKEN_2022_PROGRAM))
        self.assertEqual(metas[5][0], ata(BUYER, MINT, TOKEN_2022_PROGRAM))
        self.assertEqual(metas[8][0], TOKEN_2022_PROGRAM)
        self.assertEqual(buy.instructions[1][1][5][0], TOKEN_2022_PROGRAM)

    def test_mayhem_and_non_sol_curves_are_refused_plainly(self):
        for accounts in (curve_chain(mayhem=True), curve_chain(quote_mint=_key("usd"))):
            with self.assertRaises(sitebuy.BuyError) as caught:
                sitebuy.plan(FakeRpc(accounts), MINT, BUYER, LAMPORTS, 500)
            self.assertEqual(caught.exception.status, 400)
            self.assertIn("not supported yet", str(caught.exception))


class TestThePumpSwapVenue(unittest.TestCase):
    def setUp(self):
        self.state = buyback.observe(FakeRpc(amm_chain()), MINT, BUYER)
        self.buy = sitebuy.plan(FakeRpc(amm_chain()), MINT, BUYER, LAMPORTS, 500, choose=first)

    def test_a_graduated_coin_is_bought_from_its_pool(self):
        self.assertEqual(self.buy.venue, "pumpswap")
        self.assertEqual(self.buy.token_program, TOKEN_2022_PROGRAM)

    def test_wrap_buy_unwrap_and_nothing_burned(self):
        wsol = ata(BUYER, buyback.WSOL_MINT, TOKEN_PROGRAM)
        self.assertEqual(self.buy.instructions, [
            buyback.ix_compute_unit_limit(sitebuy.AMM_COMPUTE_UNITS),
            buyback.ix_create_ata_idempotent(BUYER, ata(BUYER, MINT, TOKEN_2022_PROGRAM), BUYER, MINT, TOKEN_2022_PROGRAM),
            buyback.ix_create_ata_idempotent(BUYER, wsol, BUYER, buyback.WSOL_MINT, TOKEN_PROGRAM),
            buyback.ix_system_transfer(BUYER, wsol, 105_000_000),
            buyback.ix_sync_native(wsol),
            buyback.ix_buy(self.state.pool, BUYER, self.buy.tokens, 105_000_000, base_token_program=TOKEN_2022_PROGRAM,
                           protocol_fee_recipient=RECIPIENTS[0], buyback_fee_recipient=BUYBACK[0]),
            buyback.ix_close_account(wsol, BUYER, BUYER),
        ])
        burn = bytes([8])
        self.assertFalse(any(ix[2][:1] == burn and ix[0] in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM)
                             for ix in self.buy.instructions))

    def test_the_tokens_go_to_the_buyer_s_own_account(self):
        metas = self.buy.instructions[5][1]
        self.assertEqual(metas[1], (BUYER, True, True))
        self.assertEqual(metas[5], (ata(BUYER, MINT, TOKEN_2022_PROGRAM), False, True))

    def test_the_quote_is_the_pool_s(self):
        s = self.state
        expected = buyback.base_out_for_lot(LAMPORTS, s.base_reserve, s.quote_reserve, s.fees, True, 0)
        self.assertEqual(self.buy.tokens, expected)
        self.assertLessEqual(self.buy.cost["total"], LAMPORTS)
        data = self.buy.instructions[5][2]
        self.assertEqual(int.from_bytes(data[8:16], "little"), expected)
        self.assertEqual(int.from_bytes(data[16:24], "little"), 105_000_000)

    def test_an_old_pool_is_extended_first(self):
        buy = sitebuy.plan(FakeRpc(amm_chain(pool_length=243)), MINT, BUYER, LAMPORTS, 500, choose=first)
        self.assertEqual(buy.instructions[1], buyback.ix_extend_account(buyback.canonical_pool(MINT), BUYER))

    def test_a_graduated_coin_without_its_pool_yet_asks_to_try_again(self):
        accounts = amm_chain()
        del accounts[buyback.canonical_pool(MINT)]
        with self.assertRaises(sitebuy.NotReady) as caught:
            sitebuy.plan(FakeRpc(accounts), MINT, BUYER, LAMPORTS, 500)
        self.assertEqual(caught.exception.status, 503)


class TestWhatIsNotACoin(unittest.TestCase):
    def test_no_account_is_404(self):
        accounts = curve_chain()
        del accounts[MINT]
        with self.assertRaises(sitebuy.NotFound):
            sitebuy.plan(FakeRpc(accounts), MINT, BUYER, LAMPORTS, 500)

    def test_a_token_without_a_curve_is_404(self):
        accounts = curve_chain()
        del accounts[pump.bonding_curve(MINT)]
        with self.assertRaises(sitebuy.NotFound) as caught:
            sitebuy.plan(FakeRpc(accounts), MINT, BUYER, LAMPORTS, 500)
        self.assertIn("not a pump.fun coin", str(caught.exception))

    def test_an_account_that_is_not_a_mint_is_400(self):
        accounts = curve_chain()
        accounts[MINT] = {"owner": pump.SYSTEM_PROGRAM, "data": ["", "base64"], "lamports": 1}
        with self.assertRaises(sitebuy.BuyError) as caught:
            sitebuy.plan(FakeRpc(accounts), MINT, BUYER, LAMPORTS, 500)
        self.assertEqual(caught.exception.status, 400)


class TestExplain(unittest.TestCase):
    def test_the_failures_a_buyer_can_act_on(self):
        self.assertIn("enough SOL", sitebuy.explain({"err": "AccountNotFound", "logs": []}))
        self.assertIn("enough SOL", sitebuy.explain({"err": {"InstructionError": [3, {"Custom": 1}]},
                                                     "logs": ["Transfer: insufficient lamports 5, need 10"]}))
        self.assertIn("slippage", sitebuy.explain({"err": {}, "logs": ["Error Code: TooMuchSolRequired."]}))
        self.assertIn("slippage", sitebuy.explain({"err": {}, "logs": ["Error Code: ExceededSlippage."]}))
        self.assertIn("graduated", sitebuy.explain({"err": {}, "logs": ["Error Code: BondingCurveComplete."]}))
        self.assertIn("Nothing was sent", sitebuy.explain({"err": {"x": 1}, "logs": []}))


class _Rpc(FakeRpc):
    def __init__(self, accounts, *, sim=None):
        super().__init__(accounts, sim=sim or {"err": None, "logs": [], "unitsConsumed": 94_115})
        self.params = []

    def call(self, method, params=None):
        self.params.append((method, params))
        return super().call(method, params)


class TestTheEndpoint(unittest.TestCase):
    def test_a_good_request_answers_the_simulated_unsigned_transaction(self):
        rpc = _Rpc(curve_chain())
        status, body = api_buy.build(rpc, MINT, BUYER, "0.1", "")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["venue"], "curve")
        self.assertEqual(body["sol_lamports"], LAMPORTS)
        self.assertEqual(body["slippage_bps"], 500)
        self.assertEqual(body["simulated"], {"err": None, "units": 94_115})
        raw = base64.b64decode(body["transaction"])
        self.assertEqual(decode(body["signable"]), raw)
        self.assertEqual(raw[0], 1)                          # one signature slot
        self.assertEqual(raw[1:65], b"\x00" * 64)            # unsigned
        message = raw[65:]
        self.assertEqual(encode(message[4:36]), BUYER)       # the buyer pays
        sim = [p for m, p in rpc.params if m == "simulateTransaction"][0]
        self.assertEqual(sim[0], body["transaction"])
        self.assertFalse(sim[1]["sigVerify"])

    def test_a_failed_simulation_is_422_with_the_reason_and_no_transaction(self):
        rpc = _Rpc(curve_chain(), sim={"err": "AccountNotFound", "logs": [], "unitsConsumed": 0})
        status, body = api_buy.build(rpc, MINT, BUYER, "0.1", "500")
        self.assertEqual(status, 422)
        self.assertIn("enough SOL", body["error"])
        self.assertNotIn("transaction", body)
        self.assertEqual(body["simulated"]["err"], "AccountNotFound")

    def test_bad_inputs_are_400_before_the_chain_is_read(self):
        for args in (("x", BUYER, "0.1", ""), (MINT, "x", "0.1", ""), (MINT, BUYER, "0", ""),
                     (MINT, BUYER, "101", ""), (MINT, BUYER, "0.1", "10")):
            rpc = _Rpc(curve_chain())
            with self.subTest(args=args):
                status, body = api_buy.build(rpc, *args)
                self.assertEqual(status, 400)
                self.assertTrue(body["error"])
                self.assertEqual(rpc.params, [])

    def test_not_a_coin_is_404_and_a_chain_outage_is_503(self):
        accounts = curve_chain()
        del accounts[MINT]
        self.assertEqual(api_buy.build(_Rpc(accounts), MINT, BUYER, "0.1", "")[0], 404)

        class Down(_Rpc):
            def accounts(self, addresses):
                raise api_buy.RpcError(-32000, "unreachable", "getMultipleAccounts")

        status, body = api_buy.build(Down(curve_chain()), MINT, BUYER, "0.1", "")
        self.assertEqual(status, 503)
        self.assertIn("try again", body["error"])

    def test_over_a_socket(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), api_buy.handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            with mock.patch.object(api_buy, "_rpc", lambda: _Rpc(amm_chain())):
                url = f"http://127.0.0.1:{httpd.server_address[1]}/api/buy?mint={MINT}&wallet={BUYER}&sol=0.5&slippage_bps=100"
                with urllib.request.urlopen(url, timeout=10) as response:
                    body = json.loads(response.read())
                self.assertEqual(body["venue"], "pumpswap")
                self.assertEqual(body["max_sol_lamports"], 505_000_000)
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(url.replace("sol=0.5", "sol=abc"), timeout=10)
                self.assertEqual(caught.exception.code, 400)
                self.assertIn("error", json.loads(caught.exception.read()))
        finally:
            httpd.shutdown()
            httpd.server_close()


class TestTheRelayTakesASignedBuy(unittest.TestCase):
    def _signed(self, accounts):
        buy = sitebuy.plan(FakeRpc(accounts), MINT, BUYER, LAMPORTS, 500, choose=first)
        message = buy.message(BLOCKHASH)
        return signed_transaction(message, [KEYPAIR.sign(message)]), KEYPAIR.sign(message)

    def test_both_venues_pass_the_relay_s_check(self):
        for accounts in (curve_chain(), amm_chain()):
            transaction, signature = self._signed(accounts)
            self.assertEqual(relay.check(transaction), encode(signature))

    def test_the_allowlist_gained_pumpswap_and_nothing_general(self):
        self.assertIn(PUMP_AMM_PROGRAM, relay.OURS)
        for general in (pump.SYSTEM_PROGRAM, TOKEN_PROGRAM, TOKEN_2022_PROGRAM, buyback.ASSOCIATED_TOKEN_PROGRAM,
                        buyback.COMPUTE_BUDGET_PROGRAM):
            self.assertNotIn(general, relay.OURS)


if __name__ == "__main__":
    unittest.main()

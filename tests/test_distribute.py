"""The crank that pays an enrolled coin's shareholders.

pump holds creator fees in a vault until somebody calls
distribute_creator_fees, so without this the protocol's share is owed and
never received. Every byte pinned here came from pump's on-chain IDL and the
deploy repository's mainnet simulations; these stop a later edit from
silently moving an account or a shareholder.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from indexer import distribute, legs, pump  # noqa: E402
from indexer.base58 import pubkey_bytes  # noqa: E402

from test_indexer import (  # noqa: E402
    ADMIN, CHARLIE, CHARLIE_CONFIG, FakeRpc, bonding_curve, config_account, curve_account,
    mint_account,
)
from test_buyback import _account, pool_bytes, token_account_bytes  # noqa: E402
from indexer import buyback  # noqa: E402
from indexer.pump import PUMP_AMM_PROGRAM, TOKEN_PROGRAM  # noqa: E402

BLOCKHASH = "11111111111111111111111111111111"
PAYER = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
INCINERATOR = "1nc1nerator11111111111111111111111111111111"


class _Rpc(FakeRpc):
    """FakeRpc plus the two calls the crank makes beyond account reads."""

    simulate_err = None

    def call(self, method, params=None):
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": BLOCKHASH}}
        if method == "simulateTransaction":
            self.simulated = params[0]
            return {"value": {"err": self.simulate_err, "logs": [], "unitsConsumed": 31_000}}
        raise AssertionError(method)


def crank_rpc(*, graduated=False, vault_lamports=50_000_000, amm_wsol=0, pool=True,
              coin_creator=CHARLIE_CONFIG, amm_vault_exists=True):
    """An enrolled coin: its bonding curve's creator is its config, and the
    config pays the protocol, the incinerator and an ops wallet. Graduated,
    it also has the canonical wSOL pool naming the config as coin creator
    and the AMM-side wSOL vault holding `amm_wsol`."""
    accounts = {
        bonding_curve(CHARLIE): curve_account(CHARLIE_CONFIG, graduated=graduated),
        CHARLIE_CONFIG: config_account(
            CHARLIE,
            [(legs.TOLL_DESTINATION, 500), (INCINERATOR, 2000), (ADMIN, 7500)],
            admin_revoked=True,
        ),
        CHARLIE: mint_account(1_000_000_000),
    }
    if graduated and pool:
        accounts[buyback.canonical_pool(CHARLIE)] = _account(
            PUMP_AMM_PROGRAM,
            pool_bytes(creator=buyback.pool_authority(CHARLIE), base_mint=CHARLIE, coin_creator=coin_creator),
        )
    if graduated and amm_vault_exists:
        authority = buyback.coin_creator_vault_authority(CHARLIE_CONFIG)
        accounts[distribute.amm_vault(CHARLIE_CONFIG)] = _account(
            TOKEN_PROGRAM, token_account_bytes(buyback.WSOL_MINT, authority, amm_wsol))
    return _Rpc(accounts, balances={distribute.creator_vault(CHARLIE_CONFIG): vault_lamports,
                                    PAYER: 10_000_000})


# A graduated, enrolled coin the deploy repository's `graduated` workflow
# measured on mainnet, and every address it printed for the AMM transfer.
# The derivations below are pinned to those bytes.
MEASURED_MINT = "7bRLkZEBjXhkunAhz3CLzQ5wth2vLjT41MusuZVpump"
MEASURED_CONFIG = "Ei9rqPVQpVep9DeD6DuiyWK5S2LbWuJ1EzNFMiCG1sgh"
MEASURED_POOL = "k35Vgw8KUxXiahsJHk9QZ9NS3jKJxsUJNgknoWM8i9P"
MEASURED_VAULT_AUTHORITY = "vz1StETinY7WfqyE3nnJ5xyetDgg9SvXg4uCEB1k2GW"
MEASURED_AMM_VAULT = "3uGs8p5bVKDArzPPRMcQ7TFApbSVJB8sV3vLir1yCfMB"
MEASURED_PUMP_VAULT = "31fhPvz71MKG2hD7TPpXpTvJ6k7c8YUjQiDi4otpMaD9"
MEASURED_AMM_EVENT_AUTHORITY = "GS4CU59F31iL7aR2Q8zVS8DRrcRnXX1yjQ66TqNVQnaR"


def no_config_rpc():
    """A fresh launch: the bonding curve's creator is an ordinary wallet."""
    accounts = {
        bonding_curve(CHARLIE): curve_account(ADMIN),
        ADMIN: {"owner": "11111111111111111111111111111111", "lamports": 1,
                "data": ["", "base64"], "space": 0},
    }
    return _Rpc(accounts)


class TestTheInstruction(unittest.TestCase):
    def test_the_discriminator_is_pump_s(self):
        self.assertEqual(distribute.DISTRIBUTE_CREATOR_FEES.hex(), "a572670079cef751")

    def test_accounts_are_in_the_idl_order_then_the_shareholders_in_config_order(self):
        holders = (legs.TOLL_DESTINATION, INCINERATOR, ADMIN)
        metas = distribute.accounts_for(CHARLIE, CHARLIE_CONFIG, CHARLIE_CONFIG, holders)
        names = [a for a, _s, _w in metas]
        self.assertEqual(names[0], CHARLIE)
        self.assertEqual(names[1], bonding_curve(CHARLIE))
        self.assertEqual(names[2], CHARLIE_CONFIG)
        self.assertEqual(names[3], distribute.creator_vault(CHARLIE_CONFIG))
        self.assertEqual(names[4], distribute.SYSTEM_PROGRAM)
        self.assertEqual(names[6], pump.PUMP_PROGRAM)
        # 6054: the remaining accounts are exactly the shareholders, in order.
        self.assertEqual(tuple(names[7:]), holders)
        self.assertTrue(all(w for _a, _s, w in metas[7:]))

    def test_nothing_signs(self):
        """The instruction has no signer. A wallet pays the fee and that is
        its whole involvement, which is why any wallet may run this."""
        metas = distribute.accounts_for(CHARLIE, CHARLIE_CONFIG, CHARLIE_CONFIG, (ADMIN,))
        self.assertEqual([a for a, s, _w in metas if s], [])

    def test_the_vault_is_the_pda_of_the_bonding_curve_s_creator(self):
        expected = distribute._pda([b"creator-vault", pubkey_bytes(CHARLIE_CONFIG)], pump.PUMP_PROGRAM)
        self.assertEqual(distribute.creator_vault(CHARLIE_CONFIG), expected)


class TestTheAmmTransfer(unittest.TestCase):
    """`transfer_creator_fees_to_pump`, pinned to what the deployed AMM
    declared and the mainnet simulation accepted for one real coin."""

    def test_the_discriminator_is_the_amm_s(self):
        self.assertEqual(distribute.TRANSFER_CREATOR_FEES_TO_PUMP, bytes.fromhex("8b348655e4e56cf1"))

    def test_the_ten_accounts_in_the_idl_s_order_for_the_measured_coin(self):
        metas = distribute.transfer_accounts_for(MEASURED_CONFIG)
        self.assertEqual([m[0] for m in metas], [
            buyback.WSOL_MINT,
            TOKEN_PROGRAM,
            distribute.SYSTEM_PROGRAM,
            buyback.ASSOCIATED_TOKEN_PROGRAM,
            MEASURED_CONFIG,
            MEASURED_VAULT_AUTHORITY,
            MEASURED_AMM_VAULT,
            MEASURED_PUMP_VAULT,
            MEASURED_AMM_EVENT_AUTHORITY,
            PUMP_AMM_PROGRAM,
        ])

    def test_only_the_three_vault_accounts_are_writable_and_nothing_signs(self):
        metas = distribute.transfer_accounts_for(MEASURED_CONFIG)
        self.assertEqual([m[2] for m in metas],
                         [False, False, False, False, False, True, True, True, False, False])
        self.assertFalse(any(m[1] for m in metas))

    def test_the_instruction_targets_the_amm_program(self):
        program, _metas, data = distribute.transfer_instruction(MEASURED_CONFIG)
        self.assertEqual(program, PUMP_AMM_PROGRAM)
        self.assertEqual(data, distribute.TRANSFER_CREATOR_FEES_TO_PUMP)

    def test_the_measured_coin_s_pool_and_pump_vault_derive_to_what_mainnet_holds(self):
        self.assertEqual(buyback.canonical_pool(MEASURED_MINT), MEASURED_POOL)
        self.assertEqual(distribute.creator_vault(MEASURED_CONFIG), MEASURED_PUMP_VAULT)


class TestThePlan(unittest.TestCase):
    def test_an_enrolled_coin_builds_a_message_the_payer_signs_first(self):
        built = distribute.plan(crank_rpc(), CHARLIE, PAYER)
        self.assertEqual(built.message[0], 1)
        self.assertEqual(built.message[4:36], pubkey_bytes(PAYER))
        self.assertEqual(built.shareholders, (legs.TOLL_DESTINATION, INCINERATOR, ADMIN))
        self.assertEqual(built.vault_lamports, 50_000_000)
        self.assertIn(distribute.DISTRIBUTE_CREATOR_FEES, built.message)

    def test_a_graduated_coin_moves_the_amm_vault_first_then_distributes(self):
        # Measured: distribute alone pays 0 on a graduated coin; the transfer
        # then distribute pays the whole AMM balance. So the plan is exactly
        # those two, in that order, in one transaction.
        built = distribute.plan(crank_rpc(graduated=True, amm_wsol=70_000_000), CHARLIE, PAYER)
        self.assertTrue(built.graduated)
        self.assertEqual(built.pool, buyback.canonical_pool(CHARLIE))
        self.assertEqual(built.amm_vault, distribute.amm_vault(CHARLIE_CONFIG))
        self.assertEqual(built.amm_lamports, 70_000_000)
        self.assertEqual(built.payable_lamports, 120_000_000)
        self.assertEqual(len(built.instructions), 2)
        transfer, distribution = built.instructions
        self.assertEqual(transfer, distribute.transfer_instruction(CHARLIE_CONFIG))
        self.assertEqual(distribution[2], distribute.DISTRIBUTE_CREATOR_FEES)

    def test_a_coin_on_its_curve_is_one_instruction_with_no_amm_side(self):
        built = distribute.plan(crank_rpc(), CHARLIE, PAYER)
        self.assertFalse(built.graduated)
        self.assertEqual(len(built.instructions), 1)
        self.assertEqual(built.amm_lamports, 0)
        self.assertEqual(built.payable_lamports, built.vault_lamports)

    def test_a_graduated_coin_with_an_empty_amm_vault_still_transfers(self):
        # The transfer is a no-op on an empty vault (measured on ten coins,
        # err None), and an absent vault account reads as 0 wSOL.
        built = distribute.plan(crank_rpc(graduated=True, amm_vault_exists=False), CHARLIE, PAYER)
        self.assertEqual(built.amm_lamports, 0)
        self.assertEqual(len(built.instructions), 2)

    def test_a_graduated_coin_without_its_canonical_pool_is_refused(self):
        with self.assertRaises(distribute.DistributeError) as caught:
            distribute.plan(crank_rpc(graduated=True, pool=False), CHARLIE, PAYER)
        self.assertIn("canonical wSOL pool", str(caught.exception))

    def test_a_pool_naming_another_coin_creator_is_refused_with_the_migration_named(self):
        # The one exception the AMM-side census found: a pool older than the
        # coin_creator field, reading the zero pubkey. Its fee is routed
        # elsewhere until migrated, and paying it would pay nobody.
        rpc = crank_rpc(graduated=True, coin_creator=buyback.DEFAULT_PUBKEY)
        with self.assertRaises(distribute.DistributeError) as caught:
            distribute.plan(rpc, CHARLIE, PAYER)
        self.assertIn("migrate_pool_coin_creator", str(caught.exception))

    def test_a_coin_with_no_config_is_refused(self):
        with self.assertRaises(distribute.DistributeError) as caught:
            distribute.plan(no_config_rpc(), CHARLIE, PAYER)
        self.assertIn("no fee-sharing config", str(caught.exception))


class TestTheRun(unittest.TestCase):
    def test_without_a_keypair_it_simulates_and_sends_nothing(self):
        rpc = crank_rpc()
        rows = distribute.run(rpc, [CHARLIE], payer=PAYER)
        self.assertEqual(rows[0]["outcome"], "simulated")
        self.assertEqual(rows[0]["units"], 31_000)
        self.assertTrue(hasattr(rpc, "simulated"))

    def test_a_graduated_coin_s_amm_balance_counts_toward_the_floor(self):
        # 0 in the pump vault, 50M wSOL on the AMM side: the transaction can
        # reach it, so it is worth the fee.
        rpc = crank_rpc(graduated=True, vault_lamports=0, amm_wsol=50_000_000)
        rows = distribute.run(rpc, [CHARLIE], payer=PAYER)
        self.assertEqual(rows[0]["outcome"], "simulated")
        self.assertEqual(rows[0]["amm_lamports"], 50_000_000)
        self.assertEqual(rows[0]["instructions"], 2)
        self.assertTrue(rows[0]["graduated"])

    def test_a_graduated_coin_with_dust_on_both_sides_is_skipped_naming_both(self):
        rpc = crank_rpc(graduated=True, vault_lamports=1_000, amm_wsol=2_000)
        rows = distribute.run(rpc, [CHARLIE], payer=PAYER)
        self.assertEqual(rows[0]["outcome"], "skipped")
        self.assertIn("AMM vault", rows[0]["reason"])

    def test_a_dust_vault_is_skipped_before_simulating(self):
        rpc = crank_rpc(vault_lamports=1_000)
        rows = distribute.run(rpc, [CHARLIE], payer=PAYER)
        self.assertEqual(rows[0]["outcome"], "skipped")
        self.assertIn("below", rows[0]["reason"])
        self.assertFalse(hasattr(rpc, "simulated"))

    def test_a_refused_simulation_is_reported_not_sent(self):
        rpc = crank_rpc()
        rpc.simulate_err = {"InstructionError": [0, {"Custom": 6054}]}
        sent = []
        rows = distribute.run(rpc, [CHARLIE], payer=PAYER, keypair=object(),
                              send=lambda *_a: sent.append(1))
        self.assertEqual(rows[0]["outcome"], "refused")
        self.assertEqual(sent, [])

    def test_with_a_keypair_it_sends_and_confirms(self):
        rpc = crank_rpc()
        confirmed = []
        rows = distribute.run(rpc, [CHARLIE], payer=PAYER, keypair=object(),
                              send=lambda _rpc, _kp, _msg: "sig111",
                              confirm=lambda _rpc, sig: confirmed.append(sig))
        self.assertEqual(rows[0]["outcome"], "sent")
        self.assertEqual(rows[0]["signature"], "sig111")
        self.assertEqual(confirmed, ["sig111"])

    def test_a_payer_with_no_sol_is_refused_before_the_first_coin(self):
        # The runtime answers AccountNotFound for a fee payer that does not
        # exist, before any instruction runs; every row would say that and
        # nothing about the payouts. The crank says it once, up front.
        rpc = crank_rpc()
        with self.assertRaises(distribute.DistributeError) as caught:
            distribute.run(rpc, [CHARLIE], payer="8SvEu1bvkhgaSkZW4XHLzfw8djd748KAVHMwvkYGfyr8")
        self.assertIn("holds no SOL", str(caught.exception))
        self.assertFalse(hasattr(rpc, "simulated"))

    def test_account_not_found_is_explained_as_the_payer_not_the_payout(self):
        self.assertIn("fee payer", distribute.explain({"err": "AccountNotFound", "logs": []}))

    def test_the_stand_in_payer_is_pump_s_fee_wallet(self):
        # The wallet every pump trade pays its protocol fee into: it exists
        # for as long as pump does, which is what a simulation's payer needs.
        self.assertEqual(distribute.STAND_IN_PAYER, "CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM")

    def test_one_coin_s_failure_does_not_stop_the_next(self):
        rpc = crank_rpc()
        rows = distribute.run(rpc, ["not-a-mint", CHARLIE], payer=PAYER)
        self.assertEqual([r["outcome"] for r in rows], ["skipped", "simulated"])


class TestWhichCoinsAreEnrolled(unittest.TestCase):
    def test_reads_the_passing_check_off_the_records(self):
        records = [
            {"mint": "A", "checks": [{"name": "PROTOCOL_SHARE", "status": "PASS"}]},
            {"mint": "B", "checks": [{"name": "PROTOCOL_SHARE", "status": "FAIL"}]},
            {"mint": "C", "checks": []},
            {"mint": "A", "checks": [{"name": "PROTOCOL_SHARE", "status": "PASS"}]},
        ]
        self.assertEqual(distribute.enrolled_mints(records), ["A"])


if __name__ == "__main__":
    unittest.main()

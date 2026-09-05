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


def crank_rpc(*, graduated=False, vault_lamports=50_000_000):
    """An enrolled coin: its bonding curve's creator is its config, and the
    config pays the protocol, the incinerator and an ops wallet."""
    accounts = {
        bonding_curve(CHARLIE): curve_account(CHARLIE_CONFIG, graduated=graduated),
        CHARLIE_CONFIG: config_account(
            CHARLIE,
            [(legs.TOLL_DESTINATION, 500), (INCINERATOR, 2000), (ADMIN, 7500)],
            admin_revoked=True,
        ),
        CHARLIE: mint_account(1_000_000_000),
    }
    return _Rpc(accounts, balances={distribute.creator_vault(CHARLIE_CONFIG): vault_lamports})


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


class TestThePlan(unittest.TestCase):
    def test_an_enrolled_coin_builds_a_message_the_payer_signs_first(self):
        built = distribute.plan(crank_rpc(), CHARLIE, PAYER)
        self.assertEqual(built.message[0], 1)
        self.assertEqual(built.message[4:36], pubkey_bytes(PAYER))
        self.assertEqual(built.shareholders, (legs.TOLL_DESTINATION, INCINERATOR, ADMIN))
        self.assertEqual(built.vault_lamports, 50_000_000)
        self.assertIn(distribute.DISTRIBUTE_CREATOR_FEES, built.message)

    def test_a_graduated_coin_is_refused_with_the_reason(self):
        with self.assertRaises(distribute.DistributeError) as caught:
            distribute.plan(crank_rpc(graduated=True), CHARLIE, PAYER)
        self.assertIn("transfer_creator_fees_to_pump", str(caught.exception))

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

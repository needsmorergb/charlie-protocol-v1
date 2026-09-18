"""The mainnet setup transactions compile, and the lookup table they create
takes crank_lp under the size limit."""

import unittest

from indexer import message
from indexer.launchlab_keeper import CpmmPool, Platform, Rail, Route, compute_limit, pda
from tools import launchlab_mainnet_setup as setup

WALLET = pda([b"wallet"], "11111111111111111111111111111111")
POOL = "CJEC7BywJ4fgveTPRpPbF4NxSgAzZxs6Dgo9wk1mV5qL"


class Setup(unittest.TestCase):
    def test_each_step_fits(self):
        for ix in (setup.init_platform(WALLET, POOL, setup.CHARLIE_MINT), setup.set_quote(WALLET, 10_000_000),
                   setup.create_raydium_platform(WALLET, setup.DEFAULT_CPMM_CONFIG, 20_000_000)):
            message.compile_legacy(WALLET, [ix], POOL)
        table, create = setup.lookup_table(WALLET, 123)
        message.compile_legacy(WALLET, [create, setup.extend_table(WALLET, table, setup.shared_accounts(POOL, setup.DEFAULT_CPMM_CONFIG))], POOL)

    def test_the_table_takes_crank_lp_under_the_limit(self):
        rail = Rail.__new__(Rail)
        rail.c = {"launchlab": setup.LAUNCHLAB, "cpmm": setup.CPMM, "lock": setup.LOCK, "lock_auth": setup.LOCK_AUTH}
        rail.program, rail.platform, rail.signer = setup.PROGRAM, setup.PLATFORM, setup.SIGNER
        f = lambda n: pda([b"f", bytes([n])], "11111111111111111111111111111111")
        p = Platform(WALLET, setup.RAY_PLATFORM, POOL, False)
        r = Route(f(1), f(2), setup.WSOL, WALLET, f(3), f(4), 0, 0, 0)
        pool = CpmmPool(setup.DEFAULT_CPMM_CONFIG, f(5), f(6), f(7), f(8), setup.WSOL, f(2), setup.TOKEN, setup.TOKEN_2022, f(9), 1, 1)
        ix = rail.crank_lp(WALLET, p, r, pool, f(10), f(11))
        with self.assertRaises(message.MessageError):
            message.compile_legacy(WALLET, [compute_limit(400_000), ix], POOL)
        msg = message.compile_v0(WALLET, [compute_limit(400_000), ix], POOL, f(12), setup.shared_accounts(POOL, setup.DEFAULT_CPMM_CONFIG))
        self.assertLess(len(message.unsigned_transaction(msg)), 1232)

    def test_mainnet_addresses(self):
        self.assertEqual(setup.PROGRAM, "ENZrfqk89oSQ2fPHPmi6NSMuRDo7F3txEZhi8ZVbCHym")
        # The global config the rail's launches use, read on mainnet 18 September 2026.
        self.assertIn("6s1xP3hpbAfFoNtUNF8mfHsjr2Bd97JxFJRWLbL6aHuX", setup.shared_accounts(POOL, setup.DEFAULT_CPMM_CONFIG))


class Launch(unittest.TestCase):
    def test_the_launch_transaction_fits_and_is_signed_by_dev_then_mint(self):
        from tools.launchlab_launch import launch_ix
        mint = pda([b"mint"], "11111111111111111111111111111111")
        ix = launch_ix(WALLET, mint, "Name", "SYM", "https://example.com/m.json", 85_000_000_000,
                       4000, 4000, 2000, WALLET, 10_000_000)
        self.assertEqual(len(ix[1]), 21)
        budget = ("ComputeBudget111111111111111111111111111111", [], bytes([2]) + (400_000).to_bytes(4, "little"))
        msg = message.compile_legacy(WALLET, [budget, ix], POOL)
        self.assertEqual(message.signer_count(msg), 2)
        self.assertEqual(msg[4:36], __import__("indexer.base58", fromlist=["x"]).pubkey_bytes(WALLET))


if __name__ == "__main__":
    unittest.main()

"""The keeper's derivations and message sizes, pinned to what ran on devnet
on 18 September 2026 (LAUNCHLAB-RAIL.md section 8)."""

import unittest

from indexer import message
from indexer.launchlab_keeper import WSOL, CpmmPool, Platform, Rail, Route, compute_limit, pda
from indexer.base58 import pubkey_bytes


def fake(n: int) -> str:
    """A distinct, valid-looking address for layout-only tests."""
    return pda([b"fake", bytes([n])], "11111111111111111111111111111111")


class Derivations(unittest.TestCase):
    def setUp(self):
        self.rail = Rail("devnet")

    def test_rail_pdas_match_devnet(self):
        # Prefixes as recorded when the accounts were created on devnet.
        self.assertTrue(self.rail.platform.startswith("4xdwJMKL"))
        self.assertTrue(self.rail.signer.startswith("6y373N1Q"))
        raydium_config = self.rail.ll([b"platform_config", pubkey_bytes(self.rail.signer)])
        self.assertTrue(raydium_config.startswith("CEnwM6Aj"))

    def test_launchlab_global_config(self):
        self.assertEqual(self.rail.global_config(WSOL), "7ZR4zD7PYfY2XxoG1Gxcy2EgEeGYrpxrwzPuwdUBssEt")


class Messages(unittest.TestCase):
    def setUp(self):
        self.rail = Rail("devnet")
        self.keeper = fake(0)
        self.p = Platform(fake(1), fake(2), "CJEC7BywJ4fgveTPRpPbF4NxSgAzZxs6Dgo9wk1mV5qL", False)
        self.r = Route(fake(3), fake(4), WSOL, self.keeper, fake(5), fake(6), 0, 0, 0)
        self.pool = CpmmPool(fake(7), fake(8), fake(9), fake(10), fake(11), WSOL, fake(4),
                             "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                             "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb", fake(12), 1, 1)

    def lp(self):
        return self.rail.crank_lp(self.keeper, self.p, self.r, self.pool, fake(13), fake(14))

    def test_account_counts(self):
        self.assertEqual(len(self.rail.crank_curve(self.keeper, self.p, self.r)[1]), 26)
        self.assertEqual(len(self.rail.crank_amm_creator(self.keeper, self.r, self.pool)[1]), 23)
        self.assertEqual(len(self.lp()[1]), 33)
        self.assertEqual(len(self.rail.crank_platform(self.keeper, self.p)[1]), 14)

    def test_lp_does_not_fit_legacy_with_compute_budget(self):
        # Devnet: 1,243 > 1,232 bytes.
        with self.assertRaises(message.MessageError):
            message.compile_legacy(self.keeper, [compute_limit(400_000), self.lp()], fake(15))

    def test_lp_v0_matches_the_devnet_transaction_size(self):
        ix = self.lp()
        table = []
        for address, _s, _w in ix[1]:
            if address != self.keeper and address not in table:
                table.append(address)
        self.assertEqual(len(table), 31)
        msg = message.compile_v0(self.keeper, [compute_limit(400_000), ix], fake(15), fake(16), table)
        wire = message.unsigned_transaction(msg)
        self.assertEqual(len(wire), 318)  # the size web3.js produced on devnet
        self.assertEqual(message.signer_count(msg), 1)

    def test_other_cranks_fit_legacy(self):
        for ix in (self.rail.crank_curve(self.keeper, self.p, self.r),
                   self.rail.crank_amm_creator(self.keeper, self.r, self.pool),
                   self.rail.crank_platform(self.keeper, self.p)):
            message.compile_legacy(self.keeper, [compute_limit(400_000), ix], fake(15))



class PlanAgainstDevnetState(unittest.TestCase):
    """plan() and build() on real devnet state captured 18 September 2026.
    The two transactions build() produced from this state were simulated on
    devnet and succeeded (crank_amm_creator 85,411 CU before the min_crank
    guard below existed; crank_lp 215,062 CU, v0 through the lookup table)."""

    class Rpc:
        def __init__(self, fx):
            self.fx = fx
            self.missing = []

        def accounts(self, addrs):
            out = []
            for a in addrs:
                if a in self.fx["accounts"]:
                    out.append({"data": [self.fx["accounts"][a], "base64"]})
                else:
                    self.missing.append(a)
                    out.append(None)
            return out

        def call(self, method, params=None):
            if method == "getProgramAccounts":
                return [{"pubkey": r["pubkey"], "account": {"data": [r["data"], "base64"]}} for r in self.fx["routes"]]
            if method == "getTokenAccountsByOwner":
                return {"value": [{"pubkey": t["pubkey"], "account": {"data": {"parsed": {"info": {
                    "mint": t["mint"], "tokenAmount": {"amount": t["amount"]}}}}}} for t in self.fx["signer_tokens"]]}
            if method == "getLatestBlockhash":
                return {"value": {"blockhash": self.fx["blockhash"]}}
            raise KeyError(method)

    def setUp(self):
        import json
        from pathlib import Path
        self.fx = json.loads((Path(__file__).parent / "fixtures" / "launchlab_devnet_state.json").read_text())

    def test_plan(self):
        from indexer.launchlab_keeper import build, plan
        rpc = self.Rpc(self.fx)
        rail = Rail("devnet")
        keeper = "Fdq7GiMhiitbHdjK1x7y6tLvSjwPYjNh9t4nibAzAq9V"
        jobs = plan(rpc, rail, keeper)
        self.assertEqual(rpc.missing, [])
        # Curve coin: creator vault 2,000 < min_crank 10,000. Graduated coin:
        # pool creator fee 906 < min_crank, so no creator crank; the Fee Key
        # is held, so the LP crank. Platform vault empty.
        self.assertEqual([(j.name, j.route) for j in jobs],
                         [("crank_lp", "EDAVa8fDQ664W2sXr2QHzDCBur3FPFXMJ7FmnL1g4UtR")])
        wire = message.unsigned_transaction(build(rpc, rail, keeper, jobs[0]))
        self.assertEqual(len(wire), 318)


if __name__ == "__main__":
    unittest.main()

"""`/launchlab`: renders only a recorded run, and every figure comes from it."""

import unittest

from indexer import launchlab_page
from tools.launchlab_devnet import record_run

TX = {
    "slot": 7,
    "blockTime": 1789761923,
    "transaction": {"message": {"accountKeys": [
        {"pubkey": "Fdq7GiMhiitbHdjK1x7y6tLvSjwPYjNh9t4nibAzAq9V"},
        {"pubkey": "1nc1nerator11111111111111111111111111111111"},
        {"pubkey": "CJEC7BywJ4fgveTPRpPbF4NxSgAzZxs6Dgo9wk1mV5qL"},
    ]}},
    "meta": {
        "err": None, "fee": 5000, "computeUnitsConsumed": 1234,
        "preBalances": [1_000_000, 0, 10], "postBalances": [1_095_000, 200_000, 60],
        "innerInstructions": [{"instructions": [
            {"parsed": {"type": "burn", "info": {"mint": "M", "amount": "42"}}},
            {"parsed": {"type": "burnChecked", "info": {"mint": "other", "tokenAmount": {"amount": "9"}}}},
        ]}],
    },
}


class Tool(unittest.TestCase):
    def test_figures_come_from_the_transaction(self):
        run = record_run(TX, "crank_amm_creator", "M", "sig")
        self.assertEqual(run["incinerator"], 200_000)
        self.assertEqual(run["charlie_pool"], 50)
        self.assertEqual(run["ops"], 100_000)  # keeper delta plus its fee
        self.assertEqual(run["coins_burned"], 42)  # only this coin's burns
        self.assertEqual(run["compute_units"], 1234)

    def test_a_failed_transaction_stops_the_run(self):
        failed = dict(TX, meta=dict(TX["meta"], err={"InstructionError": [0, "x"]}))
        with self.assertRaises(SystemExit):
            record_run(failed, "crank_lp", "M", "sig")


class Page(unittest.TestCase):
    def test_renders_the_recorded_runs(self):
        record = launchlab_page.load()
        html = launchlab_page.render(record)
        self.assertIn("This is devnet.", html)
        self.assertIn("upgradeable", html)
        for run in record["runs"]:
            self.assertIn(run["signature"], html)
            self.assertIn(f"{run['incinerator']:,}", html)

    def test_no_record_no_page(self):
        from pathlib import Path
        from unittest import mock
        with mock.patch.object(launchlab_page, "RECORD", Path("/nonexistent.json")):
            self.assertIsNone(launchlab_page.load())
            with self.assertRaises(FileNotFoundError):
                launchlab_page.render()


if __name__ == "__main__":
    unittest.main()

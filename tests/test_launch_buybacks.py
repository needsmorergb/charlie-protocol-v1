import tempfile
import unittest
from pathlib import Path

from indexer import launch_buybacks


class TestLaunchBuybackLedger(unittest.TestCase):
    def test_credit_then_burn_tracks_one_mint_and_refuses_overspend(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.jsonl"
            launch_buybacks.credit("mint-a", 50_000_000, "credit-sig", path=path)
            launch_buybacks.record_burn("mint-a", 40_000_000, 123, "burn-sig", path=path)
            self.assertEqual(launch_buybacks.summary(path)["mint-a"], {
                "credited_lamports": 50_000_000, "spent_lamports": 40_000_000,
                "burned_raw": 123, "available_lamports": 10_000_000,
            })
            with self.assertRaises(launch_buybacks.LedgerError):
                launch_buybacks.record_burn("mint-a", 10_000_001, 1, "too-much", path=path)

    def test_signature_cannot_be_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.jsonl"
            launch_buybacks.credit("mint-a", 1, "same", path=path)
            with self.assertRaises(launch_buybacks.LedgerError):
                launch_buybacks.credit("mint-b", 1, "same", path=path)

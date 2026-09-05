"""Enrolled coins are found on the chain, not only in committed records.

The hourly crank said "no coins to distribute for" an hour after a dev had
enrolled on the page, because the only list it read was the records the
coverage queue had written. This pins the scan that replaced that list: the
filters it sends, the slice it asks for, what it refuses to count, and the
pass that gives an enrolled coin its page without an issue.
"""

from __future__ import annotations

import base64
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from indexer import enrolled, legs, pump  # noqa: E402
from indexer.base58 import encode  # noqa: E402
from indexer.legs import Registry  # noqa: E402
from test_indexer import ADMIN, CHARLIE, FakeRpc, config_account  # noqa: E402
from test_intake import charlie_accounts  # noqa: E402

TOLL = legs.TOLL_DESTINATION
INCINERATOR = "1nc1nerator11111111111111111111111111111111"
OTHER = "22Zrdq4ia9nXni9625rc4e7JoMuLqSbv7d817P94pump"


def _sliced(account: dict, length: int) -> dict:
    raw = base64.b64decode(account["data"][0])
    return {"owner": account["owner"], "data": [base64.b64encode(raw[:length]).decode(), "base64"],
            "space": len(raw)}


class _Rpc:
    """Answers each slot's query from the configs given, applying the memcmp
    and the slice itself, so a wrong offset in the module shows up as a
    missing coin rather than a passing test."""

    def __init__(self, configs: dict):
        self.configs = configs
        self.queries = []

    def program_accounts(self, program_id, *, filters=(), data_slice=None, encoding="base64"):
        self.queries.append({"program": program_id, "filters": list(filters), "slice": data_slice})
        offset = filters[1]["memcmp"]["offset"]
        wanted = filters[1]["memcmp"]["bytes"]
        out = []
        for address, account in self.configs.items():
            raw = base64.b64decode(account["data"][0])
            if len(raw) >= offset + 32 and encode(raw[offset:offset + 32]) == wanted:
                out.append({"pubkey": address, "account": _sliced(account, data_slice["length"])})
        return out


class TestTheQuery(unittest.TestCase):
    def test_one_query_per_slot_with_the_toll_at_that_slot_and_a_slice_ending_there(self):
        rpc = _Rpc({})
        enrolled.scan(rpc, slots=3)
        self.assertEqual(len(rpc.queries), 3)
        for slot, query in enumerate(rpc.queries):
            offset = pump.SHARING_CONFIG_HEADER_BYTES + slot * pump.SHAREHOLDER_RECORD_BYTES
            self.assertEqual(query["program"], pump.PUMP_FEE_SHARE_PROGRAM)
            self.assertEqual(query["filters"][0],
                             {"memcmp": {"offset": 0, "bytes": pump.SHARING_CONFIG_DISCRIMINATOR_B58}})
            self.assertEqual(query["filters"][1], {"memcmp": {"offset": offset, "bytes": TOLL}})
            self.assertEqual(query["slice"], {"offset": 0, "length": offset + pump.SHAREHOLDER_RECORD_BYTES})

    def test_the_first_slot_is_where_the_page_puts_the_toll(self):
        self.assertEqual(enrolled.slot_offset(0), 80)
        self.assertEqual(enrolled.slot_offset(1), 114)

    def test_no_toll_wallet_configured_means_no_query_at_all(self):
        rpc = _Rpc({})
        self.assertEqual(enrolled.scan(rpc, toll=""), {})
        self.assertEqual(rpc.queries, [])


class TestWhatCounts(unittest.TestCase):
    def test_a_config_paying_the_toll_its_rate_in_the_first_slot_is_enrolled(self):
        rpc = _Rpc({"cfg": config_account(CHARLIE, [(TOLL, 500), (INCINERATOR, 2000), (ADMIN, 7500)])})
        self.assertEqual(enrolled.scan(rpc), {CHARLIE: 500})
        self.assertEqual(enrolled.mints(rpc), [CHARLIE])

    def test_the_toll_further_down_a_hand_built_split_is_still_found(self):
        rpc = _Rpc({"cfg": config_account(CHARLIE, [(ADMIN, 7500), (INCINERATOR, 2000), (TOLL, 500)])})
        self.assertEqual(enrolled.scan(rpc), {CHARLIE: 500})

    def test_paying_the_toll_below_its_rate_is_not_enrolled(self):
        rpc = _Rpc({"cfg": config_account(CHARLIE, [(TOLL, 499), (ADMIN, 9501)])})
        self.assertEqual(enrolled.scan(rpc), {})

    def test_the_toll_split_across_two_slots_is_summed(self):
        rpc = _Rpc({"cfg": config_account(CHARLIE, [(TOLL, 300), (TOLL, 200), (ADMIN, 9500)])})
        self.assertEqual(enrolled.scan(rpc), {CHARLIE: 500})

    def test_bytes_past_the_declared_count_are_not_a_shareholder(self):
        """A memcmp match is a byte comparison. A config that once named the
        toll in its second slot and now declares one shareholder still holds
        those bytes; they are nobody's share."""
        account = config_account(CHARLIE, [(ADMIN, 10_000), (TOLL, 500)])
        raw = bytearray(base64.b64decode(account["data"][0]))
        raw[76:80] = (1).to_bytes(4, "little")
        account["data"][0] = base64.b64encode(bytes(raw)).decode()
        rpc = _Rpc({"cfg": account})
        self.assertEqual(enrolled.scan(rpc), {})

    def test_beyond_the_scanned_slots_is_not_found_by_default(self):
        holders = [(ADMIN, 100)] * enrolled.DEFAULT_SLOTS + [(TOLL, 500)]
        rpc = _Rpc({"cfg": config_account(CHARLIE, holders)})
        self.assertEqual(enrolled.scan(rpc), {})
        self.assertEqual(enrolled.scan(rpc, slots=enrolled.DEFAULT_SLOTS + 1), {CHARLIE: 500})

    def test_coins_are_sorted_and_a_stranger_s_config_is_not_in_the_answer(self):
        rpc = _Rpc({
            "a": config_account(OTHER, [(TOLL, 500), (ADMIN, 9500)]),
            "b": config_account(CHARLIE, [(TOLL, 600), (ADMIN, 9400)]),
            "c": config_account("9MTfWK8chKHVJq1qnDvRZ2udovpzbP2N4tm2pFEipump", [(ADMIN, 10_000)]),
        })
        self.assertEqual(enrolled.mints(rpc), sorted([OTHER, CHARLIE]))


class TestAnEnrolledCoinGetsItsPage(unittest.TestCase):
    """A coin enrolled on the page gets a committed page without an issue.
    Measured through the same `intake.measure` the queue uses, so the two
    paths cannot drift; skipped once its record exists, so the six-hourly
    run does not re-measure every enrolled coin on top of the refresh."""

    def test_an_enrolled_coin_with_no_record_is_measured_and_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            outcomes = enrolled.index_new(
                FakeRpc(charlie_accounts()), Registry(), None, out_dir,
                site_url="https://charlieprotocol.fun", discover=lambda rpc: [CHARLIE],
            )
            self.assertEqual(len(outcomes), 1)
            self.assertTrue(outcomes[0].observed)
            self.assertIsNone(outcomes[0].issue_number)
            self.assertEqual(outcomes[0].mint, CHARLIE)
            self.assertEqual(outcomes[0].verdict_url, f"https://charlieprotocol.fun/verify/{CHARLIE}")
            self.assertTrue((out_dir / f"{CHARLIE}.json").exists())
            self.assertTrue((out_dir / f"{CHARLIE}.html").exists())

    def test_a_coin_that_already_has_a_record_is_left_to_the_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            (out_dir / f"{CHARLIE}.json").write_text("{}", encoding="utf-8")
            outcomes = enrolled.index_new(FakeRpc({}), Registry(), None, out_dir, discover=lambda rpc: [CHARLIE])
            self.assertEqual(outcomes, [])

    def test_the_cap_and_the_filename_boundary_hold(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            outcomes = enrolled.index_new(
                FakeRpc(charlie_accounts()), Registry(), None, out_dir, limit=1,
                discover=lambda rpc: ["../PWNED", CHARLIE, CHARLIE],
            )
            self.assertEqual([o.mint for o in outcomes], [CHARLIE])
            self.assertFalse((Path(tmp).parent / "PWNED.json").exists())

    def test_a_coin_the_chain_cannot_answer_for_is_a_failed_outcome_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcomes = enrolled.index_new(FakeRpc({}), Registry(), None, Path(tmp), discover=lambda rpc: [CHARLIE])
            self.assertEqual(len(outcomes), 1)
            self.assertFalse(outcomes[0].observed)
            self.assertIsNotNone(outcomes[0].reason)


if __name__ == "__main__":
    unittest.main()

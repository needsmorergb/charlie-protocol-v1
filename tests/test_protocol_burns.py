"""The protocol-burn record counts only what it can prove, and never shrinks."""

import unittest

from indexer import decode, protocol_burns as pb

WALLET = "8SvEu1bvkhgaSkZW4XHLzfw8djd748KAVHMwvkYGfyr8"
STRANGER = "9XBUCHH9ZDot9UvkBH9zCsvXSHYvUogf12zrNVsZdga9"


def _burn(authority, amount="28569506012", mint=pb.CHARLIE):
    return {"programId": decode.TOKEN_2022_PROGRAM,
            "parsed": {"type": "burn", "info": {"mint": mint, "authority": authority, "amount": amount, "account": "x"}}}


def _tx(instructions, err=None):
    return {"slot": 5, "blockTime": 1789679000, "meta": {"err": err, "innerInstructions": []},
            "transaction": {"message": {"instructions": instructions}}}


SWAP = {"programId": decode.PUMP_AMM_PROGRAM, "accounts": [], "data": ""}


class TestBurnsIn(unittest.TestCase):
    def test_a_buy_and_burn_by_the_wallet_counts(self):
        rows = pb.burns_in(_tx([SWAP, _burn(WALLET)]), "sig", WALLET)
        self.assertEqual([r["raw_amount"] for r in rows], [28569506012])

    def test_what_does_not_count(self):
        cases = {
            "a stranger's burn": _tx([SWAP, _burn(STRANGER)]),
            "a burn with no swap beside it": _tx([_burn(WALLET)]),
            "a failed transaction": _tx([SWAP, _burn(WALLET)], err={"InstructionError": [1, "x"]}),
            "another coin": _tx([SWAP, _burn(WALLET, mint="4dHdbxPANfuuvzXCTjngsqpjsUytE6KfBtQuGrT1nc1n")]),
            "a payout arriving": _tx([]),
            "unread": None,
        }
        for name, tx in cases.items():
            with self.subTest(name):
                self.assertEqual(pb.burns_in(tx, "sig", WALLET), [])


class TestRecord(unittest.TestCase):
    def test_a_short_answer_from_the_node_never_shrinks_the_record(self):
        known = [{"signature": "old", "instruction_index": 6, "raw_amount": 5, "slot": 1, "block_time": 1}]
        merged = pb.merge(known, [])
        self.assertEqual(merged, known)
        merged = pb.merge(known, [{"signature": "new", "instruction_index": 6, "raw_amount": 7, "slot": 2, "block_time": 2},
                                  {"signature": "old", "instruction_index": 6, "raw_amount": 999, "slot": 1, "block_time": 1}])
        self.assertEqual([(r["signature"], r["raw_amount"]) for r in merged], [("old", 5), ("new", 7)])

    def test_the_total_is_exact_and_says_what_it_is_not(self):
        body = pb.record([{"signature": "s", "instruction_index": 6, "raw_amount": 28569506012, "slot": 1, "block_time": 1}], WALLET, 0)
        self.assertEqual(body["total"], "28,569.506012")
        self.assertIn("not", body["is_not"])
        self.assertEqual(pb.ui(1), "0.000001")


if __name__ == "__main__":
    unittest.main()

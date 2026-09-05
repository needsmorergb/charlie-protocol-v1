"""The legacy message compiler.

`enroll.message` is the proven builder: its output was simulated against
mainnet with `err: None`. The compiler here must reproduce it byte for byte
for the same instruction, or the buyback transaction is built by an
unproven path.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import enroll, message  # noqa: E402
from indexer.base58 import encode  # noqa: E402

MINT = "JAMXU2JLraZ3RUhbgc3ttYPc18Kx4ojCnC56XR2zpump"
ADMIN = "Chx6EJ1QLRnhiyQHfpNNyiEWma8XPazbELPanPff4Nuj"
BURN = "1nc1nerator11111111111111111111111111111111"
BLOCKHASH = "11111111111111111111111111111111"
PROGRAM_A = "ComputeBudget111111111111111111111111111111"
PROGRAM_B = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


def _parse(msg: bytes) -> dict:
    """A minimal reader for the header and key list -- enough to assert on
    the grouping the runtime enforces."""
    n_signers, ro_signed, ro_unsigned = msg[0], msg[1], msg[2]
    count = msg[3]
    keys = [encode(msg[4 + 32 * i : 36 + 32 * i]) for i in range(count)]
    return {"signers": n_signers, "ro_signed": ro_signed, "ro_unsigned": ro_unsigned, "keys": keys}


class TestEquivalenceWithEnroll(unittest.TestCase):
    def test_same_bytes_as_the_proven_builder(self):
        shares = [enroll.Share(BURN, 2000), enroll.Share(ADMIN, 8000)]
        reference = enroll.message(MINT, ADMIN, shares, BLOCKHASH, current=[ADMIN])
        metas = enroll.accounts_for(MINT, ADMIN) + [(ADMIN, False, True)]
        ours = message.compile_legacy(
            ADMIN, [(enroll.FEE_SHARE_PROGRAM, metas, enroll.instruction_data(shares))], BLOCKHASH
        )
        self.assertEqual(ours, reference)


class TestGrouping(unittest.TestCase):
    def _two(self):
        ix_a = (PROGRAM_A, [], b"\x02\x01\x02\x03\x04")
        ix_b = (PROGRAM_B, [(BURN, False, True), (MINT, False, False), (ADMIN, True, False)], b"\x09")
        return message.compile_legacy(ADMIN, [ix_a, ix_b], BLOCKHASH)

    def test_payer_first_and_counts_agree(self):
        parsed = _parse(self._two())
        self.assertEqual(parsed["keys"][0], ADMIN)
        self.assertEqual(parsed["signers"], 1)
        self.assertEqual(parsed["ro_signed"], 0)
        # MINT, both programs are readonly non-signers; BURN is writable.
        self.assertEqual(parsed["ro_unsigned"], 3)
        self.assertEqual(len(parsed["keys"]), 5)
        # writable non-signer (BURN) precedes every readonly one
        self.assertLess(parsed["keys"].index(BURN), parsed["keys"].index(MINT))
        self.assertLess(parsed["keys"].index(BURN), parsed["keys"].index(PROGRAM_A))

    def test_payer_stays_writable_signer_even_when_an_instruction_asks_less(self):
        # ix_b lists ADMIN as a readonly signer; the payer must still be
        # counted as a writable signer or the fee cannot be debited.
        parsed = _parse(self._two())
        self.assertEqual(parsed["ro_signed"], 0)

    def test_a_program_used_as_a_writable_account_is_refused(self):
        with self.assertRaises(message.MessageError):
            message.compile_legacy(ADMIN, [(PROGRAM_A, [(PROGRAM_A, False, True)], b"")], BLOCKHASH)

    def test_no_instructions_is_refused(self):
        with self.assertRaises(message.MessageError):
            message.compile_legacy(ADMIN, [], BLOCKHASH)

    def test_oversized_messages_are_refused_before_the_rpc_sees_them(self):
        big = (PROGRAM_A, [], b"\x00" * 1200)
        with self.assertRaises(message.MessageError):
            message.compile_legacy(ADMIN, [big], BLOCKHASH)


class TestWireForms(unittest.TestCase):
    def test_unsigned_carries_one_zero_signature_per_signer(self):
        msg = message.compile_legacy(ADMIN, [(PROGRAM_A, [], b"\x02")], BLOCKHASH)
        wire = message.unsigned_transaction(msg)
        self.assertEqual(wire[:1], b"\x01")
        self.assertEqual(wire[1:65], b"\x00" * 64)
        self.assertEqual(wire[65:], msg)

    def test_signed_requires_exactly_the_signatures_the_header_declares(self):
        msg = message.compile_legacy(ADMIN, [(PROGRAM_A, [], b"\x02")], BLOCKHASH)
        wire = message.signed_transaction(msg, [b"\x07" * 64])
        self.assertEqual(wire[1:65], b"\x07" * 64)
        with self.assertRaises(message.MessageError):
            message.signed_transaction(msg, [])
        with self.assertRaises(message.MessageError):
            message.signed_transaction(msg, [b"\x07" * 63])


if __name__ == "__main__":
    unittest.main()

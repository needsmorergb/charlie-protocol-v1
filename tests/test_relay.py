"""The relay sends only what is truly signed and truly ours.

Offline. Transactions are built by the door's own builders and signed with
throwaway keys made here; the node is a recorder.
"""

import base64
import unittest

from indexer import ed25519, enroll, launch, relay
from indexer.base58 import encode
from indexer.message import compile_legacy, signed_transaction

BLOCKHASH = encode(bytes(range(32)))
DEV = ed25519.Keypair.from_seed(bytes([7]) * 32)
MINT = ed25519.Keypair.from_seed(bytes([9]) * 32)
TOLL = encode(bytes([3]) * 32)


def _split_message(payer=DEV) -> bytes:
    shares = [enroll.Share(TOLL, 2500), enroll.Share(payer.address, 7500)]
    return enroll.enrollment_message(MINT.address, payer.address, shares, BLOCKHASH, create=True)


class _Rpc:
    def __init__(self):
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        return "ignored"


class TestCheck(unittest.TestCase):
    def test_a_signed_split_passes_and_its_id_is_the_dev_s_signature(self):
        message = _split_message()
        signature = DEV.sign(message)
        self.assertEqual(relay.check(signed_transaction(message, [signature])), encode(signature))

    def test_the_create_needs_both_signatures(self):
        meta = launch.validate_metadata("Test", "TST", "https://ipfs.io/ipfs/x")
        message = launch.create_message(MINT.address, DEV.address, meta, BLOCKHASH)
        half = launch.partially_signed(message, MINT)
        with self.assertRaisesRegex(relay.RelayError, "signature 1 of 2 is missing"):
            relay.check(half)
        whole = relay.with_signature(half, DEV.sign(message))
        self.assertEqual(relay.check(whole), encode(DEV.sign(message)))

    def test_a_signature_over_a_rewritten_message_is_refused(self):
        # What the wallet did on 2026-09-17: it signed its own message.
        message = _split_message()
        other = _split_message(payer=ed25519.Keypair.from_seed(bytes([8]) * 32))
        with self.assertRaisesRegex(relay.RelayError, "does not match"):
            relay.check(signed_transaction(message, [DEV.sign(other)]))

    def test_a_transaction_that_is_not_the_door_s_is_refused(self):
        memo = ("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr", [], b"hi")
        message = compile_legacy(DEV.address, [memo], BLOCKHASH)
        with self.assertRaisesRegex(relay.RelayError, "not a transaction this door built"):
            relay.check(signed_transaction(message, [DEV.sign(message)]))

    def test_garbage_is_refused_not_crashed_on(self):
        for raw in (b"", b"\x01", b"\x01" + b"\x00" * 64, b"\xff" * 40, b"\x00" * 2000):
            with self.assertRaises(relay.RelayError):
                relay.check(raw)


class TestAssemble(unittest.TestCase):
    """Wallets disagree on what `signTransaction` returns. Every shape that
    is truly signed is taken; nothing else is."""

    def setUp(self):
        self.message = _split_message()
        self.built = signed_transaction(self.message, [b"\x00" * 64])
        self.signature = DEV.sign(self.message)
        self.whole = signed_transaction(self.message, [self.signature])

    def test_every_shape_of_a_true_answer(self):
        shapes = {
            "signature, base58": encode(self.signature),
            "signature, bytes": list(self.signature),
            "transaction, base58": encode(self.whole),
            "transaction, base64": base64.b64encode(self.whole).decode(),
            "transaction, bytes": list(self.whole),
        }
        for name, signed in shapes.items():
            with self.subTest(name):
                self.assertEqual(relay.assemble(self.built, signed), self.whole)

    def test_a_rewritten_transaction_the_dev_truly_signed_is_taken_as_it_is(self):
        other = _split_message(payer=ed25519.Keypair.from_seed(bytes([8]) * 32))
        signer = ed25519.Keypair.from_seed(bytes([8]) * 32)
        theirs = signed_transaction(other, [signer.sign(other)])
        self.assertEqual(relay.assemble(self.built, encode(theirs)), theirs)

    def test_a_wrong_answer_in_any_shape_is_refused(self):
        for signed in (encode(b"\x05" * 64), list(b"\x05" * 64), "not base anything!", [999], {"a": 1}, 7):
            with self.subTest(repr(signed)[:24]):
                with self.assertRaises(relay.RelayError):
                    relay.assemble(self.built, signed)


class TestSend(unittest.TestCase):
    def test_it_sends_the_same_bytes_without_preflight(self):
        message = _split_message()
        transaction = signed_transaction(message, [DEV.sign(message)])
        rpc = _Rpc()
        relay.send(rpc, transaction)
        relay.send(rpc, transaction)  # a resend is the point
        self.assertEqual(len(rpc.calls), 2)
        method, params = rpc.calls[0]
        self.assertEqual(method, "sendTransaction")
        self.assertEqual(base64.b64decode(params[0]), transaction)
        self.assertTrue(params[1]["skipPreflight"])

    def test_nothing_reaches_the_node_when_the_check_fails(self):
        rpc = _Rpc()
        with self.assertRaises(relay.RelayError):
            relay.send(rpc, relay.with_signature(signed_transaction(_split_message(), [b"\x01" * 64]), b"\x02" * 64))
        self.assertEqual(rpc.calls, [])


if __name__ == "__main__":
    unittest.main()

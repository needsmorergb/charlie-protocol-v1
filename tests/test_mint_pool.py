"""The pre-ground mint pool: parsing, and picking the first unused key."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import ed25519, mint_pool  # noqa: E402
from indexer.base58 import encode  # noqa: E402

A = ed25519.Keypair.from_seed(b"\x01" * 32)
B = ed25519.Keypair.from_seed(b"\x02" * 32)
C = ed25519.Keypair.from_seed(b"\x03" * 32)


class _Rpc:
    def __init__(self, used: set[str]):
        self.used = used
        self.calls = 0

    def accounts(self, addresses):
        self.calls += 1
        return [{"data": ["", "base64"]} if a in self.used else None for a in addresses]


class TestParse(unittest.TestCase):
    def test_seeds_and_full_keypairs_both_load(self):
        pool = mint_pool.parse(f"{encode(A.seed)}, {encode(B.seed + B.public)},")
        self.assertEqual([p.address for p in pool], [A.address, B.address])

    def test_empty_is_an_empty_pool(self):
        self.assertEqual(mint_pool.parse(""), [])
        self.assertEqual(mint_pool.parse(" , "), [])

    def test_a_bad_entry_names_its_position_not_its_value(self):
        with self.assertRaises(mint_pool.PoolError) as caught:
            mint_pool.parse(f"{encode(A.seed)},notakey")
        self.assertIn("entry 2", str(caught.exception))
        self.assertNotIn("notakey", str(caught.exception))

    def test_a_mismatched_keypair_is_refused(self):
        with self.assertRaises(mint_pool.PoolError):
            mint_pool.parse(encode(A.seed + B.public))

    def test_a_duplicate_is_refused(self):
        with self.assertRaises(mint_pool.PoolError):
            mint_pool.parse(f"{encode(A.seed)},{encode(A.seed)}")

    def test_unset_means_no_pool_and_set_means_a_pool(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(mint_pool.configured())
        with mock.patch.dict(os.environ, {mint_pool.ENV: ""}):
            self.assertEqual(mint_pool.configured(), [])
        with mock.patch.dict(os.environ, {mint_pool.ENV: encode(C.seed)}):
            self.assertEqual([p.address for p in mint_pool.configured()], [C.address])


class TestPick(unittest.TestCase):
    def test_the_first_unused_key_in_one_rpc_call(self):
        rpc = _Rpc(used={A.address})
        self.assertEqual(mint_pool.pick([A, B, C], rpc).address, B.address)
        self.assertEqual(rpc.calls, 1)

    def test_all_used_refuses_and_says_how_many(self):
        with self.assertRaises(mint_pool.PoolError) as caught:
            mint_pool.pick([A, B], _Rpc(used={A.address, B.address}))
        self.assertIn("2", str(caught.exception))

    def test_an_empty_pool_refuses(self):
        with self.assertRaises(mint_pool.PoolError):
            mint_pool.pick([], _Rpc(used=set()))


if __name__ == "__main__":
    unittest.main()

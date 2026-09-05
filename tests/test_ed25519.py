"""The signer, checked against sources that are not this code.

RFC 8032 section 7.1's published vectors pin the arithmetic; OpenSSL, when
its command line is installed, pins it against an independent implementation
on fresh random keys. Neither depends on the other.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import ed25519  # noqa: E402
from indexer.base58 import encode  # noqa: E402

# RFC 8032, 7.1, TEST 1 and TEST 2.
VECTORS = [
    (
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
]


class TestRfc8032(unittest.TestCase):
    def test_base_point(self):
        self.assertEqual(
            ed25519._BX,
            15112221349535400772501151409588531511454012693041857206046113283949847762202,
        )
        self.assertEqual(
            ed25519._BY,
            46316835694926478169428394003475163141307993866256225615783033603165251855960,
        )

    def test_published_vectors(self):
        for seed, public, msg, signature in VECTORS:
            seed, public, msg, signature = (bytes.fromhex(v) for v in (seed, public, msg, signature))
            self.assertEqual(ed25519.public_key(seed), public)
            self.assertEqual(ed25519.sign(seed, msg), signature)
            self.assertTrue(ed25519.verify(public, msg, signature))

    def test_verify_rejects_tampering(self):
        seed, public, msg, signature = (bytes.fromhex(v) for v in VECTORS[1])
        self.assertFalse(ed25519.verify(public, msg + b"!", signature))
        self.assertFalse(ed25519.verify(public, msg, signature[:-1] + bytes([signature[-1] ^ 1])))
        self.assertFalse(ed25519.verify(bytes(32), msg, signature))
        too_big = signature[:32] + (ed25519._L).to_bytes(32, "little")
        self.assertFalse(ed25519.verify(public, msg, too_big))


@unittest.skipUnless(shutil.which("openssl"), "openssl command line not installed")
class TestAgainstOpenSsl(unittest.TestCase):
    def test_random_keys_agree(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = os.path.join(tmp, "k.pem")
            for i in range(4):
                subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", key], check=True, capture_output=True)
                der = subprocess.run(["openssl", "pkey", "-in", key, "-outform", "DER"], check=True, capture_output=True).stdout
                pub = subprocess.run(["openssl", "pkey", "-in", key, "-pubout", "-outform", "DER"], check=True, capture_output=True).stdout
                seed, public = der[-32:], pub[-32:]
                msg = os.urandom(11 * i + 1)  # openssl refuses an empty input file
                path = os.path.join(tmp, "m")
                Path(path).write_bytes(msg)
                theirs = subprocess.run(
                    ["openssl", "pkeyutl", "-sign", "-inkey", key, "-rawin", "-in", path],
                    check=True, capture_output=True,
                ).stdout
                self.assertEqual(ed25519.public_key(seed), public)
                self.assertEqual(ed25519.sign(seed, msg), theirs)


class TestKeypair(unittest.TestCase):
    def setUp(self):
        self.seed = bytes.fromhex(VECTORS[0][0])
        self.public = bytes.fromhex(VECTORS[0][1])

    def test_solana_cli_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "id.json"
            path.write_text(json.dumps(list(self.seed + self.public)))
            pair = ed25519.Keypair.from_file(path)
            self.assertEqual(pair.address, encode(self.public))
            self.assertEqual(pair.sign(b""), bytes.fromhex(VECTORS[0][3]))

    def test_a_file_whose_public_half_does_not_match_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "id.json"
            path.write_text(json.dumps(list(self.seed + bytes(32))))
            with self.assertRaises(ValueError):
                ed25519.Keypair.from_file(path)

    def test_base58_secret(self):
        pair = ed25519.Keypair.from_base58(encode(self.seed + self.public))
        self.assertEqual(pair.public, self.public)
        self.assertEqual(ed25519.Keypair.from_base58(encode(self.seed)).public, self.public)

    def test_repr_never_shows_the_seed(self):
        pair = ed25519.Keypair.from_seed(self.seed)
        self.assertNotIn(self.seed.hex(), repr(pair))
        self.assertNotIn(encode(self.seed), repr(pair))
        self.assertIn(pair.address, repr(pair))

    def test_wrong_lengths_are_refused(self):
        with self.assertRaises(ValueError):
            ed25519.Keypair.from_secret_bytes(b"\x01" * 33)


if __name__ == "__main__":
    unittest.main()

"""The coin-creation transaction builder.

Every constant here was read from pump's own on-chain Anchor IDL (the copy at
the repository root), and `create` was proven by simulating it against
mainnet with a fresh mint on 2026-09-14: `err: None`, 117,654 compute units.
These tests pin the parts of that result a later edit could silently break --
the discriminator, the ACCOUNT ORDER, the argument layout, and the two-signer
shape of the transaction -- and they pin the measurement that decided the
door's shape: all three instructions do not fit one packet.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import ed25519, enroll, launch, legs  # noqa: E402
from indexer.base58 import decode, encode, pubkey_bytes  # noqa: E402
from indexer.curve import find_program_address  # noqa: E402
from indexer.message import MessageError  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEV = "Chx6EJ1QLRnhiyQHfpNNyiEWma8XPazbELPanPff4Nuj"
BLOCKHASH = "11111111111111111111111111111111"
URI = "https://ipfs.io/ipfs/bafkreibs2xlm4qm4ubh2g4wsnstlgcviephup43gq3yikzsyiltww2xpwq"
TOLL = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
BURN = "1nc1nerator11111111111111111111111111111111"


def _idl():
    return json.loads((ROOT / "_idl_6EF8rrec.json").read_text(encoding="utf-8"))


def _meta(name="Probe Coin", symbol="PROBE", uri=URI):
    return launch.validate_metadata(name, symbol, uri)


class TestTheDiscriminator(unittest.TestCase):
    def test_it_is_the_idl_s_bytes_for_create(self):
        ix = next(i for i in _idl()["instructions"] if i["name"] == "create")
        self.assertEqual(bytes(ix["discriminator"]), launch.CREATE)
        self.assertEqual(launch.CREATE.hex(), "181ec828051c0777")

    def test_it_is_not_create_v2(self):
        """The variant with the cashback flag is deliberately not built."""
        v2 = next(i for i in _idl()["instructions"] if i["name"] == "create_v2")
        self.assertNotEqual(bytes(v2["discriminator"]), launch.CREATE)


class TestTheAccounts(unittest.TestCase):
    """Positional. A list that is right in content and wrong in order is a
    transaction that fails at best."""

    def setUp(self):
        self.mint = launch.new_mint(b"\x07" * 32).address
        self.metas = launch.create_accounts_for(self.mint, DEV)
        self.idl_accounts = next(i for i in _idl()["instructions"] if i["name"] == "create")["accounts"]

    def test_fourteen_accounts_in_the_idl_s_order(self):
        self.assertEqual(len(self.metas), 14)
        self.assertEqual(len(self.idl_accounts), len(self.metas))
        # The flags, position by position, are the IDL's.
        for meta, spec in zip(self.metas, self.idl_accounts):
            self.assertEqual(meta[1], bool(spec.get("signer")), spec["name"])
            self.assertEqual(meta[2], bool(spec.get("writable")), spec["name"])

    def test_the_fixed_addresses_are_the_idl_s(self):
        by_name = {spec["name"]: meta for meta, spec in zip(self.metas, self.idl_accounts)}
        for spec in self.idl_accounts:
            if spec.get("address"):
                self.assertEqual(by_name[spec["name"]][0], spec["address"], spec["name"])
        # `program` has no address in the IDL because it is the program itself.
        self.assertEqual(by_name["program"][0], launch.PUMP_PROGRAM)
        self.assertEqual(by_name["mpl_token_metadata"][0], "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s")

    def test_the_pdas_are_derived_from_the_idl_s_seeds(self):
        by_name = {spec["name"]: meta[0] for meta, spec in zip(self.metas, self.idl_accounts)}
        self.assertEqual(by_name["mint_authority"], find_program_address([b"mint-authority"], launch.PUMP_PROGRAM)[0])
        self.assertEqual(by_name["global"], find_program_address([b"global"], launch.PUMP_PROGRAM)[0])
        self.assertEqual(by_name["event_authority"], find_program_address([b"__event_authority"], launch.PUMP_PROGRAM)[0])
        curve = find_program_address([b"bonding-curve", pubkey_bytes(self.mint)], launch.PUMP_PROGRAM)[0]
        self.assertEqual(by_name["bonding_curve"], curve)
        self.assertEqual(by_name["associated_bonding_curve"],
                         enroll.associated_token_address(curve, self.mint, launch.TOKEN_PROGRAM))
        mpl = pubkey_bytes(launch.MPL_TOKEN_METADATA)
        self.assertEqual(by_name["metadata"],
                         find_program_address([b"metadata", mpl, pubkey_bytes(self.mint)], launch.MPL_TOKEN_METADATA)[0])

    def test_the_mint_and_the_user_are_the_only_signers(self):
        signers = [a for a, s, _w in self.metas if s]
        self.assertEqual(signers, [self.mint, DEV])


class TestTheData(unittest.TestCase):
    def test_layout_is_disc_then_three_borsh_strings_then_the_creator(self):
        data = launch.create_data(_meta(), DEV)
        self.assertEqual(data[:8], launch.CREATE)
        cursor = 8
        out = []
        for _ in range(3):
            n = int.from_bytes(data[cursor:cursor + 4], "little")
            cursor += 4
            out.append(data[cursor:cursor + n].decode())
            cursor += n
        self.assertEqual(out, ["Probe Coin", "PROBE", URI])
        self.assertEqual(data[cursor:], pubkey_bytes(DEV))
        self.assertEqual(len(data), cursor + 32)

    def test_the_creator_is_the_signing_dev(self):
        """The bonding curve's creator is who pump pays and who may create
        the fee config. Any other key here would strand the second step."""
        _program, metas, data = launch.create_instruction(launch.new_mint(b"\x01" * 32).address, DEV, _meta())
        self.assertEqual(data[-32:], pubkey_bytes(DEV))
        self.assertIn((DEV, True, True), metas)


class TestValidation(unittest.TestCase):
    def test_limits_are_metaplex_s(self):
        self.assertEqual((launch.MAX_NAME_BYTES, launch.MAX_SYMBOL_BYTES, launch.MAX_URI_BYTES), (32, 10, 200))

    def test_refusals_are_addressed_to_the_dev(self):
        with self.assertRaises(launch.LaunchError) as c:
            launch.validate_metadata("", "X", URI)
        self.assertIn("name", str(c.exception))
        with self.assertRaises(launch.LaunchError):
            launch.validate_metadata("A" * 33, "X", URI)
        with self.assertRaises(launch.LaunchError):
            launch.validate_metadata("ok", "ELEVENCHARS", URI)
        with self.assertRaises(launch.LaunchError):
            launch.validate_metadata("ok", "A B", URI)
        with self.assertRaises(launch.LaunchError):
            launch.validate_metadata("ok", "X", "")
        with self.assertRaises(launch.LaunchError):
            launch.validate_metadata("ok", "X", "http://insecure")
        with self.assertRaises(launch.LaunchError):
            launch.validate_metadata("ok", "X", "https://" + "a" * 200)

    def test_bytes_not_characters(self):
        # Four 3-byte characters are 12 bytes: over the ticker's 10.
        with self.assertRaises(launch.LaunchError):
            launch.validate_metadata("ok", "€€€€", URI)
        self.assertEqual(launch.validate_metadata(" ok ", "X", URI).name, "ok")


class TestTheCreateMessage(unittest.TestCase):
    def setUp(self):
        self.mint = launch.new_mint(b"\x03" * 32)
        self.message = launch.create_message(self.mint.address, DEV, _meta(), BLOCKHASH)

    def test_two_signers_the_dev_first(self):
        self.assertEqual(self.message[0], 2)
        self.assertEqual(launch.signer_addresses(self.message), [DEV, self.mint.address])

    def test_it_is_the_measured_size(self):
        # 779 bytes with a pump-length URI: the figure the page quotes.
        self.assertEqual(launch.size_of(self.message), 779)

    def test_partially_signed_leaves_the_dev_s_slot_zero_and_signs_as_the_mint(self):
        tx = launch.partially_signed(self.message, self.mint)
        self.assertEqual(tx[0], 2)
        self.assertEqual(tx[1:65], b"\x00" * 64)
        self.assertTrue(ed25519.verify(self.mint.public, self.message, tx[65:129]))
        self.assertEqual(tx[129:], self.message)

    def test_a_stranger_keypair_cannot_partially_sign(self):
        with self.assertRaises(launch.LaunchError):
            launch.partially_signed(self.message, launch.new_mint(b"\x09" * 32))

    def test_fresh_mints_are_fresh(self):
        self.assertNotEqual(launch.new_mint().address, launch.new_mint().address)


class TestWhyTwoTransactions(unittest.TestCase):
    """The measurement that shaped the door. If pump's IDL ever loses
    enough accounts for this to fit, this test fails and the one-transaction
    version becomes a decision to make rather than a fact to record."""

    def test_all_three_instructions_do_not_fit_one_packet(self):
        shares = [enroll.Share(TOLL, 2500), enroll.Share(BURN, 2000), enroll.Share(DEV, 5500)]
        with self.subTest("real toll"):
            real = legs.TOLL_DESTINATION
            legs.TOLL_DESTINATION = TOLL
            try:
                with self.assertRaises(MessageError) as c:
                    launch.launch_message(launch.new_mint(b"\x04" * 32).address, DEV, shares, _meta(), BLOCKHASH)
            finally:
                legs.TOLL_DESTINATION = real
        self.assertIn("above Solana's 1232 byte limit", str(c.exception))
        self.assertIn("1260 bytes", str(c.exception))


class TestPreflight(unittest.TestCase):
    def test_refuses_without_the_toll_row(self):
        real = legs.TOLL_DESTINATION
        legs.TOLL_DESTINATION = TOLL
        try:
            with self.assertRaises(launch.LaunchError):
                launch.preflight(DEV, [enroll.Share(BURN, 5000), enroll.Share(DEV, 5000)], _meta())
            launch.preflight(DEV, [enroll.Share(TOLL, 2500), enroll.Share(BURN, 2000), enroll.Share(DEV, 5500)], _meta())
        finally:
            legs.TOLL_DESTINATION = real

    def test_refuses_without_the_incinerator_row(self):
        real = legs.TOLL_DESTINATION
        legs.TOLL_DESTINATION = TOLL
        try:
            with self.assertRaises(launch.LaunchError) as c:
                launch.preflight(DEV, [enroll.Share(TOLL, 2500), enroll.Share(DEV, 7500)], _meta())
            self.assertIn("incinerator", str(c.exception))
        finally:
            legs.TOLL_DESTINATION = real

    def test_refuses_while_the_door_is_closed(self):
        real = legs.TOLL_DESTINATION
        legs.TOLL_DESTINATION = None
        try:
            with self.assertRaises(launch.LaunchError) as c:
                launch.preflight(DEV, [enroll.Share(DEV, 10000)], _meta())
            self.assertIn("not open", str(c.exception))
        finally:
            legs.TOLL_DESTINATION = real


if __name__ == "__main__":
    unittest.main()

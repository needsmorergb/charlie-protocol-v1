"""Offline tests for the indexer. `python -m unittest discover -s protocol`.

No network and no dependencies. Every account the tests feed the decoders is
built byte by byte here, so a layout change in pump's program shows up as a
failing decode test rather than as a wrong number in a published post.

Two of these are regression vectors pinned against mainnet on 2026-08-29 and
noted as such: the bonding-curve PDA for $CHARLIE, and the fact that
`burn111...111` sits ON the ed25519 curve.
"""

from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import invariants
from indexer.base58 import decode, encode, pubkey_bytes
from indexer.curve import find_program_address, is_on_curve
from indexer.legs import GRANDFATHERED_SEAL, Registry, split_of
from indexer.observe import observe
from indexer.pump import (
    DISC_BONDING_CURVE,
    DISC_SHARING_CONFIG,
    PUMP_FEE_SHARE_PROGRAM,
    PUMP_PROGRAM,
    SYSTEM_PROGRAM,
    TOKEN_PROGRAM,
    DecodeError,
    bonding_curve,
    read_bonding_curve,
    read_mint,
    read_sharing_config,
)
from indexer.store import Store

CHARLIE = "8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump"
CHARLIE_CONFIG = "8cUvP3q3KqcKMT6rEowN55ZepafYLFLwY2vijETRK3E4"
CHARLIE_CURVE = "7VxCTsEknMC9ofXsddPM8piaGorGrMR8FQnDFjsQ7bjx"
BURN_VANITY = "burn111111111111111111111111111111111111111"
INCINERATOR = "1nc1nerator11111111111111111111111111111111"
ADMIN = "2CFywHXDPjDK2iRQsb95vnjgncDUZeQKJ6MceJ4ALpdc"
PROGRAM = "Charr1eProtoco11111111111111111111111111111"
WALLET = "So11111111111111111111111111111111111111112"


# -- fixtures -------------------------------------------------------------
def account(data: bytes, owner: str, lamports: int = 1_000_000) -> dict:
    return {
        "owner": owner,
        "lamports": lamports,
        "data": [base64.b64encode(data).decode(), "base64"],
    }


def curve_account(creator: str, graduated: bool = True) -> dict:
    data = DISC_BONDING_CURVE + bytes(40) + bytes([1 if graduated else 0]) + pubkey_bytes(creator)
    return account(data, PUMP_PROGRAM)


def config_account(mint: str, holders, admin_revoked: bool = True, admin: str = ADMIN) -> dict:
    data = bytearray(DISC_SHARING_CONFIG)
    data += bytes([255, 2, 1])                       # bump, version, status
    data += pubkey_bytes(mint)
    data += pubkey_bytes(admin)
    data += bytes([1 if admin_revoked else 0])
    data += len(holders).to_bytes(4, "little")
    for address, bps in holders:
        data += pubkey_bytes(address) + bps.to_bytes(2, "little")
    data += bytes(1024 - len(data))                  # pump pre-allocates the account
    return account(bytes(data), PUMP_FEE_SHARE_PROGRAM)


def mint_account(supply: int, decimals: int = 6, mint_authority=None, freeze_authority=None) -> dict:
    data = bytearray()
    data += (1 if mint_authority else 0).to_bytes(4, "little")
    data += pubkey_bytes(mint_authority) if mint_authority else bytes(32)
    data += supply.to_bytes(8, "little")
    data += bytes([decimals, 1])
    data += (1 if freeze_authority else 0).to_bytes(4, "little")
    data += pubkey_bytes(freeze_authority) if freeze_authority else bytes(32)
    return account(bytes(data), TOKEN_PROGRAM)


class FakeRpc:
    def __init__(self, accounts: dict, balances: dict | None = None, raises=None):
        self._accounts = accounts
        self._balances = balances or {}
        self._raises = raises

    def accounts(self, addresses):
        if self._raises:
            raise self._raises
        return [self._accounts.get(address) for address in addresses]

    def balance(self, address):
        return self._balances.get(address, 0)


def charlie_rpc(overrides: dict | None = None):
    """$CHARLIE as mainnet actually held it on 2026-08-29."""
    accounts = {
        CHARLIE_CURVE: curve_account(CHARLIE_CONFIG),
        CHARLIE_CONFIG: config_account(CHARLIE, [(BURN_VANITY, 10_000)]),
        CHARLIE: mint_account(956_384_474_035_955),
    }
    accounts.update(overrides or {})
    return FakeRpc(accounts, balances={BURN_VANITY: 178_734_302_038})


# -- encoding -------------------------------------------------------------
class TestBase58(unittest.TestCase):
    def test_roundtrip(self):
        for address in (CHARLIE, BURN_VANITY, SYSTEM_PROGRAM, PROGRAM):
            self.assertEqual(encode(decode(address)), address)

    def test_leading_zeros_survive(self):
        self.assertEqual(decode(SYSTEM_PROGRAM), bytes(32))
        self.assertEqual(encode(bytes(32)), SYSTEM_PROGRAM)

    def test_rejects_ambiguous_characters(self):
        with self.assertRaises(ValueError):
            decode("0OIl")

    def test_pubkey_length_is_enforced(self):
        with self.assertRaises(ValueError):
            pubkey_bytes("abc")


# -- the cryptographic core ----------------------------------------------
class TestCurve(unittest.TestCase):
    def test_pdas_are_off_curve(self):
        """The property that makes a PDA unsignable."""
        for seed in (b"seal", b"burn", b"bonding-curve"):
            address, _bump = find_program_address([seed, pubkey_bytes(CHARLIE)], PROGRAM)
            self.assertFalse(is_on_curve(address), address)

    def test_zero_key_is_on_curve(self):
        self.assertTrue(is_on_curve(bytes(32)))

    def test_burn_vanity_address_is_on_curve(self):
        """Pinned against mainnet 2026-08-29, and it is the finding, not a detail.

        `burn111...111` holds $CHARLIE's entire fee stream and a private key CAN
        exist for it. PROTOCOL.md sec.3 grandfathers the address for attribution;
        this is a second and separate weakness, and SEAL_UNSPENDABLE fails on it.
        """
        self.assertTrue(is_on_curve(BURN_VANITY))

    def test_incinerator_is_off_curve(self):
        """The contrast that makes the point: a keyless burn address is possible."""
        self.assertFalse(is_on_curve(INCINERATOR))

    def test_bonding_curve_pda_matches_mainnet(self):
        """Regression vector: the address that decoded to $CHARLIE's real curve."""
        self.assertEqual(bonding_curve(CHARLIE), CHARLIE_CURVE)


# -- decoding -------------------------------------------------------------
class TestDecoding(unittest.TestCase):
    def test_reads_charlie(self):
        rpc = charlie_rpc()
        curve = read_bonding_curve(rpc, CHARLIE)
        self.assertTrue(curve.graduated)
        self.assertEqual(curve.creator, CHARLIE_CONFIG)

        config = read_sharing_config(rpc, curve)
        self.assertEqual(config.mint, CHARLIE)
        self.assertTrue(config.admin_revoked)
        self.assertEqual(config.shareholders, ((BURN_VANITY, 10_000),))
        self.assertEqual(config.total_bps, 10_000)

    def test_wrong_owner_is_refused(self):
        """A funded stray at a derived address must not decode as a curve."""
        strays = {CHARLIE_CURVE: account(DISC_BONDING_CURVE + bytes(80), SYSTEM_PROGRAM)}
        with self.assertRaises(DecodeError) as caught:
            read_bonding_curve(FakeRpc(strays), CHARLIE)
        self.assertIn("owned by", str(caught.exception))

    def test_wrong_discriminator_is_refused(self):
        bad = {CHARLIE_CURVE: account(bytes(8) + bytes(80), PUMP_PROGRAM)}
        with self.assertRaises(DecodeError):
            read_bonding_curve(FakeRpc(bad), CHARLIE)

    def test_ordinary_creator_is_not_a_config(self):
        rpc = FakeRpc({CHARLIE_CURVE: curve_account(WALLET), WALLET: account(bytes(8), SYSTEM_PROGRAM)})
        curve = read_bonding_curve(rpc, CHARLIE)
        with self.assertRaises(DecodeError) as caught:
            read_sharing_config(rpc, curve)
        self.assertIn("not a fee-sharing config", str(caught.exception))

    def test_absurd_shareholder_count_is_refused(self):
        data = bytearray(DISC_SHARING_CONFIG + bytes([255, 2, 1]))
        data += pubkey_bytes(CHARLIE) + pubkey_bytes(ADMIN) + bytes([1])
        data += (9999).to_bytes(4, "little")
        data += bytes(64)
        rpc = FakeRpc(
            {
                CHARLIE_CURVE: curve_account(CHARLIE_CONFIG),
                CHARLIE_CONFIG: account(bytes(data), PUMP_FEE_SHARE_PROGRAM),
            }
        )
        with self.assertRaises(DecodeError):
            read_sharing_config(rpc, read_bonding_curve(rpc, CHARLIE))

    def test_mint_authority_is_decoded(self):
        rpc = FakeRpc({CHARLIE: mint_account(1_000, mint_authority=WALLET)})
        state = read_mint(rpc, CHARLIE)
        self.assertEqual(state.mint_authority, WALLET)
        self.assertIsNone(state.freeze_authority)
        self.assertEqual(state.supply, 1_000)


# -- leg attribution ------------------------------------------------------
class TestLegs(unittest.TestCase):
    def setUp(self):
        self.registry = Registry(program_id=PROGRAM, grandfathered_seal=GRANDFATHERED_SEAL)

    def config(self, holders, mint=CHARLIE):
        rpc = FakeRpc(
            {CHARLIE_CURVE: curve_account(CHARLIE_CONFIG), CHARLIE_CONFIG: config_account(mint, holders)}
        )
        return read_sharing_config(rpc, read_bonding_curve(rpc, CHARLIE))

    def test_protocol_pdas_are_seal_and_burn(self):
        seal = self.registry.seal_vault(CHARLIE)
        burn = self.registry.burn_pool(CHARLIE)
        split = split_of(self.config([(seal, 6_000), (burn, 4_000)]), self.registry)
        self.assertEqual((split.seal, split.burn, split.paid), (6_000, 4_000, 0))

    def test_grandfathered_address_is_seal(self):
        split = split_of(self.config([(BURN_VANITY, 10_000)]), self.registry)
        self.assertEqual((split.seal, split.burn, split.paid), (10_000, 0, 0))

    def test_unknown_address_is_ops_even_when_keyless(self):
        """The conservative direction: unproven is OPS, never SEAL."""
        split = split_of(self.config([(INCINERATOR, 10_000)]), self.registry)
        self.assertEqual(split.paid, 10_000)
        self.assertTrue(split.attributions[0].keyless)
        self.assertIn("Not provably sealed", split.attributions[0].reason)

    def test_ordinary_wallet_is_ops(self):
        split = split_of(self.config([(WALLET, 10_000)]), self.registry)
        self.assertEqual(split.paid, 10_000)
        self.assertFalse(split.attributions[0].keyless)

    def test_without_a_deployed_program_nothing_derives_as_seal(self):
        registry = Registry(program_id=None)
        self.assertIsNone(registry.seal_vault(CHARLIE))
        split = split_of(self.config([(WALLET, 10_000)]), registry)
        self.assertEqual(split.paid, 10_000)


# -- invariants and the silence rule -------------------------------------
class TestInvariants(unittest.TestCase):
    def test_split_must_sum_to_ten_thousand(self):
        rpc = charlie_rpc({CHARLIE_CONFIG: config_account(CHARLIE, [(BURN_VANITY, 9_000)])})
        record = observe(rpc, CHARLIE, Registry(program_id=PROGRAM), now=1.0)
        check = {c.name: c for c in record.checks}["SPLIT_SUM"]
        self.assertEqual(check.status, invariants.FAIL)
        self.assertNotIn(invariants.SPLIT, record.verdict.publishable)

    def test_config_for_another_coin_fails_every_figure(self):
        rpc = charlie_rpc({CHARLIE_CONFIG: config_account(WALLET, [(BURN_VANITY, 10_000)])})
        record = observe(rpc, CHARLIE, now=1.0)
        self.assertEqual({c.name: c for c in record.checks}["CONFIG_MINT"].status, invariants.FAIL)
        self.assertEqual(record.verdict.publishable, frozenset())

    def test_on_curve_seal_destination_fails(self):
        record = observe(charlie_rpc(), CHARLIE, now=1.0)
        check = {c.name: c for c in record.checks}["SEAL_UNSPENDABLE"]
        self.assertEqual(check.status, invariants.FAIL)
        self.assertIn(BURN_VANITY, check.actual)
        self.assertNotIn(invariants.SEAL_TOTAL, record.verdict.publishable)

    def test_off_curve_seal_destination_passes(self):
        registry = Registry(program_id=PROGRAM)
        seal = registry.seal_vault(CHARLIE)
        rpc = charlie_rpc({CHARLIE_CONFIG: config_account(CHARLIE, [(seal, 10_000)])})
        record = observe(rpc, CHARLIE, registry, now=1.0)
        self.assertEqual({c.name: c for c in record.checks}["SEAL_UNSPENDABLE"].status, invariants.PASS)

    def test_live_mint_authority_forbids_the_burn_claim(self):
        rpc = charlie_rpc({CHARLIE: mint_account(1, mint_authority=WALLET)})
        record = observe(rpc, CHARLIE, now=1.0)
        check = {c.name: c for c in record.checks}["BURN_IRREVERSIBLE"]
        self.assertEqual(check.status, invariants.FAIL)
        self.assertIn("reissued", check.detail)

    def test_unchecked_blocks_publication_exactly_like_failure(self):
        record = observe(charlie_rpc(), CHARLIE, now=1.0)
        self.assertEqual(record.verdict.publishable, frozenset({invariants.SPLIT}))
        self.assertIn(invariants.BURN_TOTAL, record.verdict.blocked)
        name, status, _detail = record.verdict.blocked[invariants.BURN_TOTAL][0]
        self.assertEqual((name, status), ("BURN_SUPPLY", invariants.UNCHECKED))

    def test_a_figure_nothing_checks_is_not_publishable(self):
        verdict = invariants.apply_silence_rule([])
        self.assertEqual(verdict.publishable, frozenset())
        self.assertEqual(verdict.blocked[invariants.SPLIT][0][0], "NO_CHECK")

    def test_all_passing_backs_publish(self):
        checks = [
            invariants.Check("A", invariants.PASS, (invariants.SPLIT,), "x == y", ""),
        ]
        self.assertIn(invariants.SPLIT, invariants.apply_silence_rule(checks).publishable)


# -- observation records --------------------------------------------------
class TestObservation(unittest.TestCase):
    def test_rpc_failure_becomes_a_record_not_an_exception(self):
        record = observe(FakeRpc({}, raises=RuntimeError("all endpoints failed")), CHARLIE, now=7.0)
        self.assertFalse(record.ok)
        self.assertIn("all endpoints failed", record.error)
        self.assertEqual(record.as_dict()["mint"], CHARLIE)
        self.assertEqual(record.as_dict()["observed_at"], 7.0)

    def test_missing_curve_becomes_a_record(self):
        record = observe(FakeRpc({}), CHARLIE, now=1.0)
        self.assertIn("no bonding curve account exists", record.error)

    def test_record_serialises_to_one_json_line(self):
        record = observe(charlie_rpc(), CHARLIE, now=1.0)
        line = json.dumps(record.as_dict(), sort_keys=True)
        self.assertNotIn("\n", line)
        parsed = json.loads(line)
        self.assertEqual(parsed["split"], {"seal": 10_000, "burn": 0, "paid": 0})
        self.assertEqual(parsed["seal_balances"][BURN_VANITY], 178_734_302_038)
        self.assertEqual(parsed["publishable"], ["split"])
        self.assertTrue(parsed["config"]["admin_revoked"])


# -- the append-only store ------------------------------------------------
class TestStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "observations.jsonl"
        self.addCleanup(self.dir.cleanup)

    def test_appends_never_overwrite(self):
        store = Store(self.path)
        store.append(observe(charlie_rpc(), CHARLIE, now=1.0))
        store.append(observe(charlie_rpc(), CHARLIE, now=2.0))
        records = store.read()
        self.assertEqual([r["observed_at"] for r in records], [1.0, 2.0])

    def test_failures_are_stored_too(self):
        store = Store(self.path)
        store.append(observe(FakeRpc({}, raises=RuntimeError("node down")), CHARLIE, now=3.0))
        self.assertIn("node down", store.read()[0]["error"])

    def test_filters_by_mint_and_returns_latest(self):
        store = Store(self.path)
        store.append(observe(charlie_rpc(), CHARLIE, now=1.0))
        store.append({"mint": WALLET, "observed_at": 2.0})
        self.assertEqual(len(store.read(mint=CHARLIE)), 1)
        self.assertEqual(store.latest(WALLET)["observed_at"], 2.0)

    def test_corrupt_lines_are_surfaced_not_skipped(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text('{"mint":"a","observed_at":1}\nnot json\n', encoding="utf-8")
        records = Store(self.path).read()
        self.assertEqual(len(records), 2)
        self.assertTrue(records[1]["corrupt"])

    def test_missing_log_reads_empty(self):
        self.assertEqual(Store(self.path).read(), [])


if __name__ == "__main__":
    unittest.main()

"""`tools/trace_collector.py` tells the two fee-routing shapes apart.

The distinction it exists for -- a config pump pays, versus an ordinary
address somebody holds the key to -- is one byte of account owner, so it is
tested against both shapes rather than only the one this project uses.
"""

from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer.base58 import pubkey_bytes
from indexer.pump import (
    DISC_BONDING_CURVE,
    DISC_SHARING_CONFIG,
    PUMP_FEE_SHARE_PROGRAM,
    PUMP_PROGRAM,
    SINGLE_SHAREHOLDER_SLICE,
    SYSTEM_PROGRAM,
    bonding_curve,
)
from tools.trace_collector import (
    ROUTE_FEE_SHARE,
    ROUTE_PLAIN_CREATOR,
    render,
    siblings,
    trace,
)

MINT_A = "8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump"
MINT_B = "So11111111111111111111111111111111111111112"
CONFIG_A = "8cUvP3q3KqcKMT6rEowN55ZepafYLFLwY2vijETRK3E4"
CONFIG_B = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
# On curve, verified by `curve.is_on_curve`: an ordinary keyed wallet,
# which is the custodial shape this tool exists to name.
BOT_WALLET = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
INCINERATOR = "1nc1nerator11111111111111111111111111111111"
ADMIN = "2CFywHXDPjDK2iRQsb95vnjgncDUZeQKJ6MceJ4ALpdc"


def account(data: bytes, owner: str, space: int | None = None) -> dict:
    """`space` is what getProgramAccounts reports for the WHOLE account, which
    is how a sliced response is told apart from a short one."""
    entry = {"owner": owner, "lamports": 1, "data": [base64.b64encode(data).decode(), "base64"]}
    if space is not None:
        entry["space"] = space
    return entry


def curve_account(creator: str) -> dict:
    return account(DISC_BONDING_CURVE + bytes(40) + bytes([1]) + pubkey_bytes(creator), PUMP_PROGRAM)


def config_data(mint: str, holders, admin_revoked: bool = False) -> bytes:
    data = bytearray(DISC_SHARING_CONFIG)
    data += bytes([255, 2, 1])
    data += pubkey_bytes(mint)
    data += pubkey_bytes(ADMIN)
    data += bytes([1 if admin_revoked else 0])
    data += len(holders).to_bytes(4, "little")
    for address, bps in holders:
        data += pubkey_bytes(address) + bps.to_bytes(2, "little")
    data += bytes(1024 - len(data))
    return bytes(data)


def config_account(mint: str, holders, admin_revoked: bool = False) -> dict:
    return account(config_data(mint, holders, admin_revoked), PUMP_FEE_SHARE_PROGRAM, space=1024)


class FakeRpc:
    def __init__(self, accounts: dict, program_accounts: list | None = None):
        self._accounts = accounts
        self._program_accounts = program_accounts or []
        self.filters_seen: list = []

    def accounts(self, addresses):
        return [self._accounts.get(address) for address in addresses]

    def program_accounts(self, program_id, *, filters=(), data_slice=None, encoding="base64"):
        self.filters_seen.append(list(filters))
        return list(self._program_accounts)


class TestFeeShareRoute(unittest.TestCase):
    def _rpc(self, holders):
        return FakeRpc(
            {
                bonding_curve(MINT_A): curve_account(CONFIG_A),
                CONFIG_A: config_account(MINT_A, holders),
                INCINERATOR: None,
                BOT_WALLET: account(b"", SYSTEM_PROGRAM),
            }
        )

    def test_single_destination_at_full_bps_is_reported_as_the_shape(self):
        result = trace(self._rpc([(BOT_WALLET, 10_000)]), MINT_A)
        self.assertEqual(result.route, ROUTE_FEE_SHARE)
        self.assertEqual(result.sole_destination, BOT_WALLET)
        self.assertEqual(result.admin, ADMIN)
        self.assertFalse(result.admin_revoked)

    def test_a_keyed_collector_is_named_on_curve(self):
        result = trace(self._rpc([(BOT_WALLET, 10_000)]), MINT_A)
        destination = result.destinations[0]
        self.assertEqual(destination.kind, "wallet")
        self.assertFalse(destination.keyless)
        self.assertIn("ON CURVE", render(result))

    def test_incinerator_is_keyless_and_never_funded_is_not_an_error(self):
        result = trace(self._rpc([(INCINERATOR, 2_000), (BOT_WALLET, 8_000)]), MINT_A)
        incinerator = result.destinations[0]
        self.assertTrue(incinerator.keyless)
        self.assertEqual(incinerator.kind, "never_funded")
        # Two destinations is not the "100% to one place" shape.
        self.assertIsNone(result.sole_destination)


class TestPlainCreatorRoute(unittest.TestCase):
    def test_an_ordinary_creator_address_is_an_answer_not_a_failure(self):
        rpc = FakeRpc(
            {
                bonding_curve(MINT_A): curve_account(BOT_WALLET),
                BOT_WALLET: account(b"", SYSTEM_PROGRAM),
            }
        )
        result = trace(rpc, MINT_A)
        self.assertEqual(result.route, ROUTE_PLAIN_CREATOR)
        self.assertEqual(result.sole_destination, BOT_WALLET)
        self.assertEqual(result.creator_kind, "wallet")
        self.assertFalse(result.creator_keyless)
        self.assertIn("no fee-sharing config", render(result))


class TestSiblings(unittest.TestCase):
    def test_finds_other_coins_paying_the_same_collector_first(self):
        entries = [
            {"pubkey": CONFIG_A, "account": account(
                config_data(MINT_A, [(BOT_WALLET, 10_000)])[:SINGLE_SHAREHOLDER_SLICE],
                PUMP_FEE_SHARE_PROGRAM, space=1024)},
            {"pubkey": CONFIG_B, "account": account(
                config_data(MINT_B, [(BOT_WALLET, 10_000)])[:SINGLE_SHAREHOLDER_SLICE],
                PUMP_FEE_SHARE_PROGRAM, space=1024)},
        ]
        rpc = FakeRpc({}, program_accounts=entries)
        found = siblings(rpc, BOT_WALLET)
        self.assertEqual(found["mints"], [MINT_A, MINT_B])
        self.assertEqual(found["truncated"], 0)

    def test_filters_on_the_discriminator_and_the_first_shareholder(self):
        rpc = FakeRpc({}, program_accounts=[])
        siblings(rpc, BOT_WALLET)
        offsets = [f["memcmp"]["offset"] for f in rpc.filters_seen[0]]
        self.assertEqual(offsets, [0, 80])
        self.assertEqual(rpc.filters_seen[0][1]["memcmp"]["bytes"], BOT_WALLET)

    def test_multi_shareholder_matches_are_counted_not_dropped(self):
        entries = [
            {"pubkey": CONFIG_A, "account": account(
                config_data(MINT_A, [(BOT_WALLET, 5_000), (INCINERATOR, 5_000)])[:SINGLE_SHAREHOLDER_SLICE],
                PUMP_FEE_SHARE_PROGRAM, space=1024)},
        ]
        found = siblings(FakeRpc({}, program_accounts=entries), BOT_WALLET)
        self.assertEqual(found["mints"], [])
        self.assertEqual(found["truncated"], 1)


if __name__ == "__main__":
    unittest.main()

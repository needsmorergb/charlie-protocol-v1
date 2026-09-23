import base64
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import coins  # noqa: E402
from indexer import enroll, launch, legs  # noqa: E402
from indexer import tag_launch as tag  # noqa: E402
from test_coin_metadata import account as meta_account  # noqa: E402
from test_coverage import full_config_bytes  # noqa: E402

TAGGED = "CNT5L1AijM92nNTJH7oiTZ4J6ZNMq7BC2yRuiNU1nc1n"
NO_LEGS = "4dHdbxPANfuuvzXCTjngsqpjsUytE6KfBtQuGrT1nc1n"
UNNAMED = "7mr9vEN4XEUDCjaDzZnVLAAE2VV5FmzBBti7t2Kpump"
FULL = tuple((s.address, s.bps) for s in tag.split_rows())


def config(mint, holders):
    return {"data": [base64.b64encode(full_config_bytes(mint, holders)).decode(), "base64"],
            "owner": enroll.FEE_SHARE_PROGRAM}


class FakeRpc:
    def __init__(self, accounts):
        self.by_address, self.calls = accounts, []

    def accounts(self, addresses):
        self.calls.append(len(addresses))
        return [self.by_address.get(a) for a in addresses]


class TestDirectory(unittest.TestCase):
    def rpc(self):
        return FakeRpc({
            enroll.sharing_config_address(TAGGED): config(TAGGED, FULL),
            launch.metadata_address(TAGGED): meta_account("Slime", "SLIME", "https://ipfs.io/ipfs/s"),
            # pays the protocol but has no other legs: not enrolled, not listed
            enroll.sharing_config_address(NO_LEGS): config(NO_LEGS, ((legs.TOLL_DESTINATION, 10_000),)),
            enroll.sharing_config_address(UNNAMED): config(UNNAMED, FULL),
        })

    def test_lists_enrolled_coins_named_from_their_metadata(self):
        rpc = self.rpc()
        with mock.patch.object(coins.enrolled, "mints", return_value=[TAGGED, NO_LEGS, UNNAMED]):
            listed = coins.directory(rpc)
        self.assertEqual(listed, [
            {"mint": TAGGED, "name": "Slime", "symbol": "SLIME", "uri": "https://ipfs.io/ipfs/s"},
            {"mint": UNNAMED, "name": "", "symbol": "", "uri": ""},
        ])
        self.assertEqual(rpc.calls, [3, 3])          # one batch for configs, one for metadata

    def test_the_leg_rule_is_the_enrollment_checks_rule(self):
        from indexer import pump
        full = pump.decode_sharing_config("x", config(TAGGED, FULL))
        self.assertTrue(coins.is_enrolled(full))
        bare = pump.decode_sharing_config("x", config(NO_LEGS, ((legs.TOLL_DESTINATION, 10_000),)))
        self.assertFalse(coins.is_enrolled(bare))

    def test_nothing_enrolled_reads_as_an_empty_list(self):
        with mock.patch.object(coins.enrolled, "mints", return_value=[]):
            self.assertEqual(coins.directory(FakeRpc({})), [])


if __name__ == "__main__":
    unittest.main()

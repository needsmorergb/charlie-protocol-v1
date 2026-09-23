import base64
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import verify  # noqa: E402
from indexer import launch  # noqa: E402

MINT = "CNT5L1AijM92nNTJH7oiTZ4J6ZNMq7BC2yRuiNU1nc1n"


def _string(value: str, pad: int) -> bytes:
    raw = value.encode() + b"\x00" * pad
    return len(raw).to_bytes(4, "little") + raw


def account(name="Slime", symbol="SLIME", uri="https://ipfs.io/ipfs/x"):
    data = b"\x04" + b"\x01" * 64 + _string(name, 20) + _string(symbol, 5) + _string(uri, 30) + b"\x00" * 40
    return {"data": [base64.b64encode(data).decode(), "base64"]}


class FakeRpc:
    def __init__(self, value):
        self.value, self.asked = value, []

    def accounts(self, addresses):
        self.asked.append(addresses)
        return [self.value]


class TestReadMetadata(unittest.TestCase):
    def test_reads_the_three_strings_without_their_padding(self):
        rpc = FakeRpc(account())
        self.assertEqual(launch.read_metadata(rpc, MINT),
                         {"name": "Slime", "symbol": "SLIME", "uri": "https://ipfs.io/ipfs/x"})
        self.assertEqual(rpc.asked, [[launch.metadata_address(MINT)]])

    def test_missing_or_short_accounts_read_as_none(self):
        self.assertIsNone(launch.read_metadata(FakeRpc(None), MINT))
        self.assertIsNone(launch.decode_metadata(b"\x04" * 70))
        self.assertIsNone(launch.decode_metadata(b"\x04" * 65 + (10_000).to_bytes(4, "little")))


class TestRecordCarriesMetadata(unittest.TestCase):
    def test_the_coin_page_record_gains_name_symbol_and_uri(self):
        body = json.dumps({"mint": MINT, "split": {"paid": 9925}})
        out = json.loads(verify.with_metadata(body, FakeRpc(account()), MINT))
        self.assertEqual((out["name"], out["symbol"], out["uri"]), ("Slime", "SLIME", "https://ipfs.io/ipfs/x"))
        self.assertEqual(out["split"], {"paid": 9925})

    def test_a_failed_read_leaves_the_record_alone(self):
        body = json.dumps({"mint": MINT})

        class Broken:
            def accounts(self, addresses):
                raise OSError("down")
        self.assertEqual(verify.with_metadata(body, Broken(), MINT), body)
        self.assertEqual(verify.with_metadata(body, FakeRpc(None), MINT), body)


if __name__ == "__main__":
    unittest.main()

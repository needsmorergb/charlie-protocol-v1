import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import t  # noqa: E402
from indexer import claim_session  # noqa: E402
from indexer.kv import KVError, MemoryKV  # noqa: E402

MINT = "CNT5L1AijM92nNTJH7oiTZ4J6ZNMq7BC2yRuiNU1nc1n"


class TestCoinLink(unittest.TestCase):
    def setUp(self):
        self.kv = MemoryKV()
        self.kv.set_json(claim_session.coin_link_key("2102897602891321416"), MINT)

    def test_a_known_tweet_goes_to_its_coin_page_and_may_be_cached(self):
        self.assertEqual(t.target("/api/t?tweet=2102897602891321416", kv=self.kv), (f"/coin/{MINT}", True))

    def test_an_unknown_tweet_goes_to_the_coin_list_uncached(self):
        self.assertEqual(t.target("/api/t?tweet=1", kv=self.kv), ("/coins", False))

    def test_a_malformed_id_never_reaches_the_store(self):
        for path in ("/api/t", "/api/t?tweet=abc", "/api/t?tweet=" + "9" * 21, "/api/t?tweet=-1"):
            self.assertEqual(t.target(path, kv=self.kv), ("/coins", False), path)

    def test_a_stored_value_that_is_not_a_mint_is_not_followed(self):
        self.kv.set_json(claim_session.coin_link_key("5"), "https://evil.example/")
        self.assertEqual(t.target("/api/t?tweet=5", kv=self.kv), ("/coins", False))

    def test_no_store_configured_or_a_store_error_falls_back(self):
        self.assertEqual(t.target("/api/t?tweet=5", env={}), ("/coins", False))

        class Broken(MemoryKV):
            def command(self, *args):
                raise KVError("down")
        self.assertEqual(t.target("/api/t?tweet=5", kv=Broken()), ("/coins", False))


if __name__ == "__main__":
    unittest.main()

"""The crowd-api client key goes to the gateway and nowhere else.

The gateway answers 401 without a key once GATEWAY_API_KEYS is set on it, so
every read here has to carry one. CHARLIE_RPC_URLS can also name a third-party
node, and a key sent there is a key that node can spend on our quota.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import rpc  # noqa: E402
from indexer.rpc import GATEWAY, GATEWAY_KEY_ENV, RpcClient  # noqa: E402


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps({"jsonrpc": "2.0", "id": 1, "result": 7}).encode()


class TestGatewayKey(unittest.TestCase):
    def _authorization_sent(self, url, env):
        sent = []

        def fake_urlopen(request, timeout):
            sent.append(request)
            return _Response()

        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(rpc.urllib.request, "urlopen", fake_urlopen):
            if GATEWAY_KEY_ENV not in env:
                os.environ.pop(GATEWAY_KEY_ENV, None)
            self.assertEqual(RpcClient([url], sleep=lambda _s: None).call("getSlot"), 7)
        return sent[0].get_header("Authorization")

    def test_the_gateway_gets_the_key_as_a_bearer_token(self):
        self.assertEqual(
            self._authorization_sent(GATEWAY, {GATEWAY_KEY_ENV: "k-123"}),
            "Bearer k-123",
        )

    def test_another_node_never_sees_the_key(self):
        self.assertIsNone(
            self._authorization_sent("https://solana-rpc.publicnode.com", {GATEWAY_KEY_ENV: "k-123"})
        )

    def test_a_lookalike_host_never_sees_the_key(self):
        self.assertIsNone(
            self._authorization_sent(
                "https://crowd-api-gateway.vercel.app.evil.example/", {GATEWAY_KEY_ENV: "k-123"}
            )
        )

    def test_no_key_configured_sends_no_header(self):
        self.assertIsNone(self._authorization_sent(GATEWAY, {}))


if __name__ == "__main__":
    unittest.main()

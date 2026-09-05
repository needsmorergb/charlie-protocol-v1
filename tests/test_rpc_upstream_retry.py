"""A gateway upstream failure is retried, not published as the coin's answer.

crowd-api chooses a provider per request. When that provider refuses, the
gateway says so with an upstream error code -- which the client used to raise,
so the visitor got "could not read the chain" for a coin the very next request
answered correctly. Observed live on 2026-09-02 against a trending coin, with
an upstream HTTP 400 that cleared as soon as the breaker opened on the bad
provider.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer.rpc import RpcClient, RpcError, UPSTREAM_FAILURE_CODES  # noqa: E402

GATEWAY = "https://gateway.example/"


class TestUpstreamFailureIsRetried(unittest.TestCase):
    def _client(self, bodies):
        client = RpcClient([GATEWAY], sleep=lambda _s: None)
        self.posted = []

        def fake_post(url, payload):
            self.posted.append(url)
            return bodies[len(self.posted) - 1]

        client._post = fake_post
        return client

    def test_an_upstream_failure_is_retried_and_can_succeed(self):
        code = sorted(UPSTREAM_FAILURE_CODES)[0]
        client = self._client([
            {"error": {"code": code, "message": "upstream HTTP 400"}},
            {"result": {"value": "answered"}},
        ])
        self.assertEqual(client.call("getMultipleAccounts"), {"value": "answered"})
        self.assertEqual(len(self.posted), 2)

    def test_a_real_rpc_error_is_still_raised_immediately(self):
        """An invalid request is the node's verdict on OUR call. Retrying it
        onto another provider just asks a second stranger the same bad
        question, and hides a bug behind timeouts.
        """
        client = self._client([{"error": {"code": -32602, "message": "invalid params"}}])
        with self.assertRaises(RpcError):
            client.call("getMultipleAccounts")
        self.assertEqual(len(self.posted), 1)

    def test_persistent_upstream_failure_ends_as_unavailable_not_a_verdict(self):
        code = sorted(UPSTREAM_FAILURE_CODES)[0]
        client = self._client([{"error": {"code": code, "message": "upstream HTTP 400"}}] * 12)
        with self.assertRaises(Exception) as caught:
            client.call("getMultipleAccounts")
        self.assertNotIsInstance(caught.exception, RpcError)


class TestTheDefaultEndpoint(unittest.TestCase):
    """Every read goes through the gateway unless told otherwise.

    The workflows always set CHARLIE_RPC_URLS to it; the deployed functions
    and the CLI did not, and read public nodes instead. One default, and it
    is the gateway, with the public nodes an explicit opt-in.
    """

    def test_the_default_is_the_gateway_alone(self):
        from indexer import rpc

        self.assertEqual(rpc.DEFAULT_ENDPOINTS, (rpc.GATEWAY,))
        self.assertEqual(rpc.GATEWAY, "https://crowd-api-gateway.vercel.app/")
        self.assertEqual(RpcClient().endpoint_urls, (rpc.GATEWAY,))

    def test_public_nodes_are_not_in_the_default(self):
        from indexer import rpc

        for url in rpc.PUBLIC_ENDPOINTS:
            self.assertNotIn(url, rpc.DEFAULT_ENDPOINTS)
        self.assertTrue(all(u.startswith("https://") for u in rpc.PUBLIC_ENDPOINTS))


if __name__ == "__main__":
    unittest.main()

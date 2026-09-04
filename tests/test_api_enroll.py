"""`api/enroll.py` over a real socket, because its bugs were in the wiring.

The two defects this covers were not logic errors inside a function, they were
what the handler ANSWERED: a coin with no fee-sharing config came back as HTTP
200 with `admin: null` and a reason the page never read, and every transport
failure was caught by a bare `except` and reported as that same absent config.
Neither is visible from a unit test of a decoder, so these drive the handler
through an actual HTTP request and assert on the status and the body.

Nothing here reaches the network: `RpcClient` and the two `pump` readers are
replaced, and the server listens on 127.0.0.1 with an ephemeral port.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("api_enroll", ROOT / "api" / "enroll.py")
api_enroll = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api_enroll)

MINT = "JAMXU2JLraZ3RUhbgc3ttYPc18Kx4ojCnC56XR2zpump"
ADMIN = "Chx6EJ1QLRnhiyQHfpNNyiEWma8XPazbELPanPff4Nuj"
WALLET = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
BURN = "1nc1nerator11111111111111111111111111111111"


class _Curve:
    def __init__(self, cashback=False, graduated=False):
        self.mint = MINT
        self.creator = ADMIN
        self.graduated = graduated
        self.cashback = cashback


class _Config:
    def __init__(self, admin=ADMIN, revoked=False):
        self.address = ADMIN
        self.admin = admin
        self.admin_revoked = revoked
        self.shareholders = ((ADMIN, 10000),)


class _Handler(api_enroll.handler):
    def log_message(self, *args):        # keep the test output readable
        pass


class ApiCase(unittest.TestCase):
    """One server per test, so a handler that hangs cannot poison the rest."""

    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def get(self, **params) -> tuple[int, dict]:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"http://127.0.0.1:{self.server.server_address[1]}/api/enroll?{query}"
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())


class TestNoSharingConfig(ApiCase):
    """~95% of fresh launches. The page has to be able to tell this apart from
    'you do not own this coin', which is what it used to say."""

    def _no_config(self, cashback=False):
        marker = pumpish = api_enroll.pump
        error = marker.DecodeError(
            f"{MINT}: its creator {ADMIN} {marker.NO_FEE_SPLIT_MARKER} "
            "(it is an ordinary creator address). There is no split to report"
        )
        return (
            mock.patch.object(pumpish, "read_bonding_curve",
                              return_value=_Curve(cashback=cashback)),
            mock.patch.object(pumpish, "read_sharing_config", side_effect=error),
        )

    def test_absent_config_is_a_200_that_names_itself(self):
        curve, config = self._no_config()
        with curve, config, mock.patch.object(api_enroll, "RpcClient"):
            status, body = self.get(mint=MINT, authority=WALLET)
        self.assertEqual(status, 200)
        self.assertEqual(body["reason"], "no_sharing_config")
        self.assertIsNone(body["admin"])
        self.assertFalse(body["owns"])
        self.assertNotIn("error", body)

    def test_the_curve_facts_the_page_needs_are_carried(self):
        curve, config = self._no_config(cashback=None)
        with curve, config, mock.patch.object(api_enroll, "RpcClient"):
            _status, body = self.get(mint=MINT, authority=WALLET)
        # Three-valued on purpose: absent is not off.
        self.assertIsNone(body["cashback"])
        self.assertIs(body["graduated"], False)


class TestFailuresAreNotFacts(ApiCase):
    """A transport failure is not a statement about the coin. The old `except
    Exception: config = None` made every one of them into the sentence 'this
    coin has no fee-sharing config'."""

    def test_a_transport_failure_is_a_503_about_us(self):
        with mock.patch.object(api_enroll.pump, "read_bonding_curve",
                               return_value=_Curve()), \
             mock.patch.object(api_enroll.pump, "read_sharing_config",
                               side_effect=OSError("connection reset")), \
             mock.patch.object(api_enroll, "RpcClient"):
            status, body = self.get(mint=MINT, authority=WALLET)
        self.assertEqual(status, 503)
        self.assertIn("Could not read the chain", body["error"])
        self.assertNotIn("no pump fee-sharing config", body["error"])

    def test_an_undecodable_config_says_so_rather_than_absent(self):
        error = api_enroll.pump.DecodeError("sharing config declares 9 shareholders")
        with mock.patch.object(api_enroll.pump, "read_bonding_curve",
                               return_value=_Curve()), \
             mock.patch.object(api_enroll.pump, "read_sharing_config",
                               side_effect=error), \
             mock.patch.object(api_enroll, "RpcClient"):
            status, body = self.get(mint=MINT, authority=WALLET)
        self.assertEqual(status, 502)
        self.assertIn("did not decode", body["error"])
        # It must not claim the coin has no config, and must not blame the dev.
        self.assertNotIn("no pump fee-sharing config", body["error"])
        self.assertNotIn("not a pump.fun coin", body["error"])


class TestCashbackNeverReachesAWallet(ApiCase):
    """A cashback coin routes its whole creator fee to traders, so every share
    is zero and enrolling spends the coin's one permanent change for nothing.
    The refusal has to happen on the shares path, before a transaction is
    built, not after a wallet has opened."""

    def test_a_cashback_coin_is_refused_with_its_reason(self):
        with mock.patch.object(api_enroll.pump, "read_bonding_curve",
                               return_value=_Curve(cashback=True)), \
             mock.patch.object(api_enroll.pump, "read_sharing_config",
                               return_value=_Config()), \
             mock.patch.object(api_enroll, "RpcClient"):
            status, body = self.get(
                mint=MINT, authority=ADMIN,
                shares=f"{BURN}:2000,{ADMIN}:8000",
            )
        self.assertEqual(status, 400)
        self.assertIn("Trader Cashback", body["error"])
        self.assertNotIn("message", body)          # nothing to sign was built

    def test_inspection_reports_cashback_without_refusing_outright(self):
        # The page decides what to do with it; the API states the fact.
        with mock.patch.object(api_enroll.pump, "read_bonding_curve",
                               return_value=_Curve(cashback=True)), \
             mock.patch.object(api_enroll.pump, "read_sharing_config",
                               return_value=_Config()), \
             mock.patch.object(api_enroll, "RpcClient"):
            status, body = self.get(mint=MINT, authority=ADMIN)
        self.assertEqual(status, 200)
        self.assertTrue(body["cashback"])


class TestGraduationIsReported(ApiCase):
    """`graduated` was answered by the API and read by nothing for a week.

    It decides whether a dev's creator fee accrues as lamports on the config
    or as wrapped SOL in an AMM vault, which changes what a payout has to do
    -- so the page says it, and these pin that the fact survives the wire.
    """

    def _inspect(self, graduated):
        with mock.patch.object(api_enroll.pump, "read_bonding_curve",
                               return_value=_Curve(graduated=graduated)), \
             mock.patch.object(api_enroll.pump, "read_sharing_config",
                               return_value=_Config()), \
             mock.patch.object(api_enroll, "RpcClient"):
            return self.get(mint=MINT, authority=ADMIN)

    def test_a_graduated_curve_is_reported_as_graduated(self):
        status, body = self._inspect(True)
        self.assertEqual(status, 200)
        self.assertIs(body["graduated"], True)

    def test_a_live_curve_is_reported_as_not_graduated(self):
        _status, body = self._inspect(False)
        self.assertIs(body["graduated"], False)

    def test_graduation_is_not_a_refusal(self):
        """The split can still be set on a graduated coin. If this ever became
        a 400 the page would stop offering the one thing it exists for.
        """
        status, body = self._inspect(True)
        self.assertEqual(status, 200)
        self.assertTrue(body["owns"])


class TestOrdinaryInspection(ApiCase):
    def test_an_owned_config_reports_ownership_and_the_current_shares(self):
        with mock.patch.object(api_enroll.pump, "read_bonding_curve",
                               return_value=_Curve()), \
             mock.patch.object(api_enroll.pump, "read_sharing_config",
                               return_value=_Config()), \
             mock.patch.object(api_enroll, "RpcClient"):
            status, body = self.get(mint=MINT, authority=ADMIN)
        self.assertEqual(status, 200)
        self.assertTrue(body["owns"])
        self.assertEqual(body["admin"], ADMIN)
        self.assertEqual(body["current"], [{"address": ADMIN, "bps": 10000}])
        self.assertIsNone(body["reason"])

    def test_a_spent_one_shot_is_reported_as_revoked(self):
        with mock.patch.object(api_enroll.pump, "read_bonding_curve",
                               return_value=_Curve()), \
             mock.patch.object(api_enroll.pump, "read_sharing_config",
                               return_value=_Config(revoked=True)), \
             mock.patch.object(api_enroll, "RpcClient"):
            status, body = self.get(mint=MINT, authority=ADMIN)
        self.assertEqual(status, 200)
        self.assertTrue(body["admin_revoked"])


if __name__ == "__main__":
    unittest.main()

"""`api/launch.py` over a real socket, like `test_api_enroll.py`.

Nothing here reaches the network: `RpcClient` is replaced with a scripted
double, pump's IPFS relay is replaced with a recorder, and the server listens
on 127.0.0.1 with an ephemeral port. What is asserted is what the handler
ANSWERS -- status and body -- because that is what the page reads.
"""

from __future__ import annotations

import importlib.util
import json
import os
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
sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling test modules, however this file is run

from indexer import ed25519, enroll, launch, launchbuy, legs, mint_pool  # noqa: E402
from indexer.base58 import decode, encode  # noqa: E402

_spec = importlib.util.spec_from_file_location("api_launch", ROOT / "api" / "launch.py")
api_launch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api_launch)

DEV = "Chx6EJ1QLRnhiyQHfpNNyiEWma8XPazbELPanPff4Nuj"
URI = "https://ipfs.io/ipfs/bafkreibs2xlm4qm4ubh2g4wsnstlgcviephup43gq3yikzsyiltww2xpwq"
BLOCKHASH = "GHtXQBsoZHVnNFa9YevAzFr17DJjgHXk3ycTKD5xD3Zi"
TOLL = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
BURN = "1nc1nerator11111111111111111111111111111111"
BUYBACK = legs.LAUNCH_BUYBACK_DESTINATION
DEFAULT_SHARES = f"{TOLL}:2500,{BURN}:2500,{BUYBACK}:2000,{DEV}:3000"


class _Rpc:
    """Answers the four methods the handler calls, from a script."""

    def __init__(self, *, simulate_err=None, status=None, curve=True, logs=(), used_mints=()):
        self.used_mints = set(used_mints)
        self.simulate_err = simulate_err
        self.status = status
        self.curve = curve
        self.logs = list(logs)
        self.calls = []

    def call(self, method, params=None):
        self.calls.append((method, params))
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": BLOCKHASH}}
        if method == "simulateTransaction":
            return {"value": {"err": self.simulate_err, "logs": self.logs, "unitsConsumed": 117654}}
        if method == "getSignatureStatuses":
            return {"value": [self.status]}
        if method == "getAccountInfo":
            return {"value": {"data": ["", "base64"]} if self.curve else None}
        if method == "getMultipleAccounts":
            return {"value": [{"data": ["", "base64"]} if a in self.used_mints else None for a in params[0]]}
        raise AssertionError(method)

    def accounts(self, addresses):
        return list(self.call("getMultipleAccounts", [addresses, {"encoding": "base64"}])["value"])


class _Server:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), api_launch.handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def post(self, body: bytes, content_type: str):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/launch", body,
                                     {"Content-Type": content_type}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class _Base(unittest.TestCase):
    def setUp(self):
        self.server = _Server()
        self.real_toll = legs.TOLL_DESTINATION
        legs.TOLL_DESTINATION = TOLL

    def tearDown(self):
        legs.TOLL_DESTINATION = self.real_toll
        self.server.close()


class TestDescribe(_Base):
    def test_the_door_describes_itself_without_a_wallet(self):
        status, body = self.server.get("/api/launch")
        self.assertEqual(status, 200)
        self.assertTrue(body["open"])
        self.assertEqual(body["toll"], {"address": TOLL, "bps": enroll.TOLL_BPS})
        self.assertEqual(body["steps"], 2)
        self.assertEqual(body["limits"]["name_bytes"], 32)
        self.assertEqual(body["mints"], {"pool": False, "suffix": mint_pool.SUFFIX, "size": 0, "left": None})

    def test_the_door_counts_the_pool_when_there_is_one(self):
        first = launch.new_mint(b"\x21" * 32)
        second = launch.new_mint(b"\x22" * 32)
        env = {mint_pool.ENV: f"{encode(first.seed)},{encode(second.seed)}"}
        with mock.patch.dict(os.environ, env), mock.patch.object(mint_pool, "SUFFIX", ""), \
                mock.patch.object(api_launch, "RpcClient", return_value=_Rpc(used_mints=[first.address])):
            status, body = self.server.get("/api/launch")
        self.assertEqual(status, 200)
        self.assertEqual(body["mints"], {"pool": True, "suffix": "", "size": 2, "left": 1})

    def test_closed_while_the_toll_is_unset(self):
        legs.TOLL_DESTINATION = None
        status, body = self.server.get("/api/launch")
        self.assertEqual(status, 200)
        self.assertFalse(body["open"])


class TestBuild(_Base):
    def _build(self, rpc, query=f"authority={DEV}&name=Probe%20Coin&symbol=PROBE&uri={URI}&shares={DEFAULT_SHARES}"):
        with mock.patch.object(api_launch, "RpcClient", return_value=rpc):
            return self.server.get(f"/api/launch?{query}")

    def test_a_clean_simulation_returns_a_two_signer_transaction_signed_by_the_mint(self):
        rpc = _Rpc()
        status, body = self._build(rpc)
        self.assertEqual(status, 200, body)
        tx = decode(body["signable"])
        self.assertEqual(tx[0], 2)
        self.assertEqual(tx[1:65], b"\x00" * 64, "the dev's slot is left for the wallet")
        message = tx[129:]
        self.assertEqual(launch.signer_addresses(message), [DEV, body["mint"]])
        mint_public = decode(body["mint"])
        self.assertTrue(ed25519.verify(mint_public, message, tx[65:129]))
        self.assertTrue(body["simulated"])
        self.assertTrue(body["split_checked"])
        self.assertEqual(body["units"], 117654)
        self.assertEqual(body["next"], f"/api/enroll?mint={body['mint']}&authority={DEV}")
        # And what was simulated is what is returned.
        simulated = next(p for m, p in rpc.calls if m == "simulateTransaction")
        self.assertEqual(simulated[0], body["transaction"])
        self.assertFalse(simulated[1]["sigVerify"])

    def test_every_build_gets_a_fresh_mint(self):
        _s1, first = self._build(_Rpc())
        _s2, second = self._build(_Rpc())
        self.assertNotEqual(first["mint"], second["mint"])

    def test_a_configured_pool_hands_out_its_first_unused_mint(self):
        first = launch.new_mint(b"\x11" * 32)
        second = launch.new_mint(b"\x12" * 32)
        env = {mint_pool.ENV: f"{encode(first.seed)},{encode(second.seed)}"}
        with mock.patch.dict(os.environ, env), mock.patch.object(mint_pool, "SUFFIX", ""):
            status, body = self._build(_Rpc(used_mints=[first.address]))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["mint"], second.address)
        tx = decode(body["signable"])
        self.assertTrue(ed25519.verify(second.public, tx[129:], tx[65:129]), "signed by the pool key")

    def test_an_exhausted_pool_pauses_launching_instead_of_minting_random(self):
        only = launch.new_mint(b"\x13" * 32)
        with mock.patch.dict(os.environ, {mint_pool.ENV: encode(only.seed)}), mock.patch.object(mint_pool, "SUFFIX", ""):
            status, body = self._build(_Rpc(used_mints=[only.address]))
        self.assertEqual(status, 503, body)
        self.assertIn("paused", body["error"])
        self.assertIn("vanity_mint", body["error"])

    def test_a_buy_at_launch_is_priced_bundled_and_described(self):
        from test_launchbuy import FEE_CONFIG, GLOBAL
        rpc = _Rpc()
        with mock.patch.object(api_launch.launchbuy, "observe", return_value=(GLOBAL, FEE_CONFIG, True)):
            status, body = self._build(rpc, query=f"authority={DEV}&name=Probe%20Coin&symbol=PROBE&uri={URI}&shares={DEFAULT_SHARES}&buy=0.5")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["buy"]["sol"], 0.5)
        self.assertTrue(body["buy"]["before_split"])
        self.assertGreater(body["buy"]["tokens"], 0)
        self.assertEqual(launch.signer_addresses(decode(body["signable"])[129:]), [DEV, body["mint"]])
        simulated = next(p for m, p in rpc.calls if m == "simulateTransaction")
        self.assertEqual(simulated[0], body["transaction"], "the bundled transaction is what was simulated")
        self.assertGreater(len(body["transaction"]), 1200, "base64 of a create-plus-buy is longer than create alone")

    def test_a_split_without_the_incinerator_row_is_refused(self):
        rpc = _Rpc()
        status, body = self._build(rpc, query=f"authority={DEV}&name=Probe%20Coin&symbol=PROBE&uri={URI}&shares={TOLL}:2500,{DEV}:7500")
        self.assertEqual(status, 400, body)
        self.assertIn("incinerator", body["error"])
        self.assertNotIn("simulateTransaction", [m for m, _ in rpc.calls])

    def test_no_buy_means_no_buy(self):
        _s, body = self._build(_Rpc())
        self.assertIsNone(body["buy"])

    def test_a_bad_buy_amount_is_refused_before_the_chain_is_read(self):
        rpc = _Rpc()
        status, body = self._build(rpc, query=f"authority={DEV}&name=Probe%20Coin&symbol=PROBE&uri={URI}&shares={DEFAULT_SHARES}&buy=abc")
        self.assertEqual(status, 400)
        self.assertIn("SOL", body["error"])
        self.assertNotIn("simulateTransaction", [m for m, _ in rpc.calls])

    def test_a_simulation_error_is_never_handed_to_the_wallet(self):
        rpc = _Rpc(simulate_err={"InstructionError": [0, {"Custom": 1}]},
                   logs=["Program 11111111111111111111111111111111 failed: custom program error: 0x1"])
        status, body = self._build(rpc)
        self.assertEqual(status, 400)
        self.assertNotIn("signable", body)
        self.assertIn("too little SOL", body["error"])
        self.assertIn("logs", body["detail"])

    def test_bad_metadata_is_refused_before_the_chain_is_read(self):
        rpc = _Rpc()
        status, body = self._build(rpc, query=f"authority={DEV}&name=&symbol=PROBE&uri={URI}&shares={DEFAULT_SHARES}")
        self.assertEqual(status, 400)
        self.assertIn("name", body["error"])
        self.assertEqual(rpc.calls, [])

    def test_a_bad_wallet_address_is_refused(self):
        status, body = self._build(_Rpc(), query=f"authority=nope&name=x&symbol=X&uri={URI}&shares={DEFAULT_SHARES}")
        self.assertEqual(status, 400)
        self.assertIn("wallet", body["error"])

    def test_closed_door_refuses_to_build(self):
        legs.TOLL_DESTINATION = None
        status, body = self._build(_Rpc())
        self.assertEqual(status, 400)
        self.assertIn("not open", body["error"])

    def test_split_is_required_and_checked_before_rpc(self):
        rpc = _Rpc()
        status, body = self._build(
            rpc, query=f"authority={DEV}&name=x&symbol=X&uri={URI}"
        )
        self.assertEqual(status, 400)
        self.assertIn("split", body["error"])
        self.assertEqual(rpc.calls, [])

        status, body = self._build(
            rpc,
            query=f"authority={DEV}&name=x&symbol=X&uri={URI}&shares={TOLL}:2500,{DEV}:9000",
        )
        self.assertEqual(status, 400)
        self.assertIn("exactly 10000", body["error"])
        self.assertEqual(rpc.calls, [])


class TestPage(unittest.TestCase):
    def test_the_page_names_the_same_mark_the_pool_enforces(self):
        from indexer import launch_page
        page = launch_page.render()
        self.assertIn(f"<code>{mint_pool.SUFFIX}</code>", page)
        self.assertIn("mints.left === 0", page, "the page closes when the pool is dry")
        self.assertNotIn("throwaway mint keypair", page)

    def test_the_fixed_incinerator_and_buyback_rows_cannot_be_removed_or_readdressed(self):
        from indexer import launch_page
        page = launch_page.render()
        self.assertIn("addRow(INCINERATOR, 30, false, true)", page, "the burn row is created as the fixed row")
        self.assertIn("if (!wallet && !fixedBurn && !fixedBuyback)", page, "no Remove button on either fixed row")
        self.assertIn(legs.LAUNCH_BUYBACK_DESTINATION, page)
        self.assertIn("fixedBuyback", page)
        self.assertNotIn("change.onclick", page, "no Change button anywhere")
        self.assertNotIn("Remove it and there is no SOL burn", page)
        self.assertNotIn("incinerator is optional", page)
        self.assertIn("The incinerator share must be at least 1% of the rest.", page)

    def test_the_protocol_s_collection_address_is_not_on_the_page(self):
        from indexer import launch_page
        page = launch_page.render()
        self.assertNotIn("protocolAddr", page)
        self.assertNotIn("state.toll.address + '   (protocol)'", page)

    def test_a_dropped_split_is_built_again_not_polled_forever(self):
        # Seen on mainnet 2026-09-17: the wallet returned a signature for
        # approval two, the chain never saw it, and "Check again" could only
        # re-poll the dead signature. The coin sat with no split.
        from indexer import launch_page
        page = launch_page.render()
        self.assertIn("return seen === false ? 'dropped' : 'unknown';", page)
        self.assertIn("outcome === 'dropped' && second", page)
        dropped = page.split("outcome === 'dropped' && second", 1)[1].split("} else", 1)[0]
        self.assertIn("state.splitSig = null;", dropped)
        self.assertIn("await prepareSplit();", dropped, "the server simulates the split again before any new signature")


class TestStatus(_Base):
    def _status(self, rpc, mint=DEV):
        with mock.patch.object(api_launch, "RpcClient", return_value=rpc):
            return self.server.get(f"/api/launch?status={'5' * 64}&mint={mint}")

    def test_ready_only_when_landed_and_the_curve_exists(self):
        status, body = self._status(_Rpc(status={"confirmationStatus": "confirmed", "err": None}, curve=True))
        self.assertEqual(status, 200)
        self.assertTrue(body["landed"])
        self.assertTrue(body["ready"])

    def test_processed_is_not_ready(self):
        _s, body = self._status(_Rpc(status={"confirmationStatus": "processed", "err": None}, curve=False))
        self.assertFalse(body["landed"])
        self.assertFalse(body["ready"])
        self.assertTrue(body["seen"])

    def test_a_failed_create_is_said(self):
        _s, body = self._status(_Rpc(status={"confirmationStatus": "confirmed", "err": {"InstructionError": [0, "x"]}}, curve=False))
        self.assertTrue(body["failed"])
        self.assertFalse(body["ready"])

    def test_unseen_is_neither(self):
        _s, body = self._status(_Rpc(status=None, curve=False))
        self.assertFalse(body["seen"])
        self.assertFalse(body["failed"])
        self.assertFalse(body["ready"])


class TestMultipart(unittest.TestCase):
    def test_round_trip(self):
        body, ctype = api_launch.build_multipart(
            {"name": "Probe", "symbol": "PRB", "description": "two\r\nlines"},
            {"file": ("t.png", "image/png", b"\x89PNG\r\n\x1a\n\x00\x01")},
        )
        fields, files = api_launch.parse_multipart(body, ctype)
        self.assertEqual(fields, {"name": "Probe", "symbol": "PRB", "description": "two\r\nlines"})
        self.assertEqual(files["file"], ("t.png", "image/png", b"\x89PNG\r\n\x1a\n\x00\x01"))

    def test_no_boundary_is_refused(self):
        with self.assertRaises(ValueError):
            api_launch.parse_multipart(b"x", "multipart/form-data")


class TestPin(_Base):
    def _post(self, fields, image, pinned=None, error=None):
        body, ctype = api_launch.build_multipart(fields, {"file": image} if image else {})
        recorded = {}

        def fake_pin(forward, img, *, opener=None):
            recorded["fields"] = forward
            recorded["image"] = img
            if error:
                raise error
            return pinned if pinned is not None else {"metadataUri": URI, "metadata": {"name": forward["name"]}}

        with mock.patch.object(api_launch, "pin_metadata", fake_pin):
            status, answer = self.server.post(body, ctype)
        return status, answer, recorded

    def test_a_good_upload_returns_the_uri_and_forwards_only_known_fields(self):
        status, body, rec = self._post(
            {"name": "Probe", "symbol": "PRB", "description": "d", "twitter": "@x", "evil": "ignored"},
            ("t.png", "image/png", b"\x89PNG"))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["metadataUri"], URI)
        self.assertEqual(rec["fields"], {"name": "Probe", "symbol": "PRB", "description": "d", "showName": "true", "twitter": "@x"})
        self.assertEqual(rec["image"], ("t.png", "image/png", b"\x89PNG"))

    def test_no_image_is_refused(self):
        status, body, _rec = self._post({"name": "Probe", "symbol": "PRB"}, None)
        self.assertEqual(status, 400)
        self.assertIn("image", body["error"].lower())

    def test_a_non_image_is_refused(self):
        status, body, rec = self._post({"name": "Probe", "symbol": "PRB"}, ("t.exe", "application/octet-stream", b"MZ"))
        self.assertEqual(status, 400)
        self.assertEqual(rec, {}, "nothing was relayed")

    def test_metadata_limits_apply_before_pinning(self):
        status, body, rec = self._post({"name": "A" * 33, "symbol": "PRB"}, ("t.png", "image/png", b"\x89PNG"))
        self.assertEqual(status, 400)
        self.assertEqual(rec, {})

    def test_pump_s_service_failing_is_a_502_not_a_400(self):
        status, body, _rec = self._post({"name": "Probe", "symbol": "PRB"}, ("t.png", "image/png", b"\x89PNG"),
                                        error=urllib.error.URLError("down"))
        self.assertEqual(status, 502)
        self.assertIn("Nothing was created", body["error"])

    def test_a_uri_pump_did_not_return_is_refused(self):
        status, body, _rec = self._post({"name": "Probe", "symbol": "PRB"}, ("t.png", "image/png", b"\x89PNG"),
                                        pinned={"metadata": {}})
        self.assertEqual(status, 502)


class TestExplain(unittest.TestCase):
    def test_rent_is_said_in_sol(self):
        words = api_launch._explain({"logs": ["Program 11111111111111111111111111111111 failed: custom program error: 0x1"]})
        self.assertIn("too little SOL", words)
        self.assertIn("0.014", words)

    def test_a_taken_mint_asks_for_a_retry(self):
        self.assertIn("Try again", api_launch._explain({"logs": ["Allocate: account X already in use"]}))

    def test_unknown_stays_generic(self):
        self.assertIn("pump refused", api_launch._explain({"logs": ["something"]}))


if __name__ == "__main__":
    unittest.main()

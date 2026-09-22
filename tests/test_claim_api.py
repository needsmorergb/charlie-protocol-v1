"""`api/claim.py` and `indexer/claim_session.py`: the sign-in, the session
cookie, the claim queue and its refusals. Driven through `route` with a
scripted X opener and `MemoryKV`; nothing reaches the network."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import unittest
import unittest.mock
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes_http import FakeOpener  # noqa: E402
from indexer import claim_session, legs, xapi  # noqa: E402
from indexer.kv import KVError, MemoryKV  # noqa: E402

_spec = importlib.util.spec_from_file_location("api_claim", ROOT / "api" / "claim.py")
api_claim = importlib.util.module_from_spec(_spec)
sys.modules["api_claim"] = api_claim  # dataclasses look their module up here
_spec.loader.exec_module(api_claim)

SECRET = "test-only-session-secret"
ENV = {
    "X_CLIENT_ID": "cid",
    "X_CLIENT_SECRET": "csecret",
    "CHARLIE_SESSION_SECRET": SECRET,
    "UPSTASH_REDIS_REST_URL": "https://example-kv.invalid",
    "UPSTASH_REDIS_REST_TOKEN": "kvtoken",
}
NOW = 1_800_000_000
WALLET = "Chx6EJ1QLRnhiyQHfpNNyiEWma8XPazbELPanPff4Nuj"
SITE = "https://charlieprotocol.fun"


def _cookie_values(response) -> dict:
    out = {}
    for header in response.cookies:
        name, _, rest = header.partition("=")
        out[name] = rest.split(";")[0]
    return out


def _session(xid="42", handle="alice", exp=NOW + 3600) -> str:
    return claim_session.sign({"xid": xid, "handle": handle, "exp": exp}, SECRET)


def _route(method="GET", path="/api/claim", *, cookies=None, body=b"", headers=None, kv=None, opener=None,
           now=NOW, env=ENV):
    h = dict(headers or {})
    if cookies:
        h["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return api_claim.route(method, path, h, body, env=env, kv=kv if kv is not None else MemoryKV(),
                           opener=opener, now=now, random=lambda n: bytes(range(n)))


class TestSessionValues(unittest.TestCase):
    def test_round_trip(self):
        value = claim_session.sign({"xid": "1", "exp": NOW + 10}, SECRET)
        self.assertEqual(value.count("."), 1)
        self.assertEqual(claim_session.verify(value, SECRET, now=NOW), {"xid": "1", "exp": NOW + 10})

    def test_expired(self):
        value = claim_session.sign({"xid": "1", "exp": NOW}, SECRET)
        self.assertIsNone(claim_session.verify(value, SECRET, now=NOW))

    def test_tampered_or_wrong_key(self):
        value = claim_session.sign({"xid": "1", "exp": NOW + 10}, SECRET)
        body, mac = value.split(".")
        forged = claim_session._b64(json.dumps({"xid": "2", "exp": NOW + 10}).encode())
        self.assertIsNone(claim_session.verify(f"{forged}.{mac}", SECRET, now=NOW))
        self.assertIsNone(claim_session.verify(value, "another-secret", now=NOW))
        self.assertIsNone(claim_session.verify(value[:-2] + "AA", SECRET, now=NOW))
        for junk in (None, "", "abc", "a.b.c", "!!.??"):
            self.assertIsNone(claim_session.verify(junk, SECRET, now=NOW))

    def test_no_expiry_is_refused(self):
        self.assertIsNone(claim_session.verify(claim_session.sign({"xid": "1"}, SECRET), SECRET, now=NOW))
        self.assertIsNone(claim_session.verify(claim_session.sign({"exp": True}, SECRET), SECRET, now=0))

    def test_empty_secret_refused(self):
        with self.assertRaises(ValueError):
            claim_session.sign({"exp": 1}, "")

    def test_cookie_headers(self):
        header = claim_session.set_cookie("cx_session", "v.s", max_age=3600)
        for piece in ("cx_session=v.s", "Path=/", "Max-Age=3600", "HttpOnly", "Secure", "SameSite=Lax"):
            self.assertIn(piece, header)
        self.assertIn("Max-Age=0", claim_session.clear_cookie("cx_session"))
        self.assertEqual(claim_session.read_cookies("a=1; cx_session=v.s; a=2"), {"a": "1", "cx_session": "v.s"})
        self.assertEqual(claim_session.read_cookies(None), {})


class TestNotOpen(unittest.TestCase):
    def test_missing_env_is_503(self):
        for missing in ENV:
            env = {k: v for k, v in ENV.items() if k != missing}
            for method, path in (("GET", "/api/claim?step=login"), ("GET", "/api/claim?step=me"),
                                 ("POST", "/api/claim")):
                response = _route(method, path, env=env)
                self.assertEqual((response.status, response.body), (503, {"error": "claims are not open yet"}))

    def test_site_default_and_override(self):
        self.assertEqual(api_claim.config(ENV).redirect_uri, f"{SITE}/api/claim?step=callback")
        other = api_claim.config({**ENV, "CHARLIE_SITE": "https://preview.example/"})
        self.assertEqual(other.redirect_uri, "https://preview.example/api/claim?step=callback")


class TestSignIn(unittest.TestCase):
    def test_login_redirects_with_pkce_and_cookie(self):
        response = _route(path="/api/claim?step=login")
        self.assertEqual(response.status, 302)
        parts = urlsplit(response.location)
        self.assertEqual(f"{parts.scheme}://{parts.netloc}{parts.path}", xapi.AUTHORIZE)
        query = parse_qs(parts.query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["redirect_uri"], [f"{SITE}/api/claim?step=callback"])
        self.assertEqual(query["client_id"], ["cid"])
        cookie = _cookie_values(response)["cx_oauth"]
        pending = claim_session.verify(cookie, SECRET, now=NOW)
        self.assertEqual(pending["state"], query["state"][0])
        self.assertEqual(pending["exp"], NOW + 600)
        verifier, challenge = xapi.pkce_pair(lambda n: bytes(range(n)))
        self.assertEqual(pending["verifier"], verifier)
        self.assertEqual(query["code_challenge"], [challenge])
        self.assertIn("HttpOnly", response.cookies[0])

    def _pending(self, state="st", exp=NOW + 600):
        return claim_session.sign({"state": state, "verifier": "VER", "exp": exp}, SECRET)

    def test_callback_signs_in(self):
        opener = FakeOpener({"/oauth2/token": {"access_token": "AT"},
                             "/users/me": {"data": {"id": "42", "username": "alice"}}})
        response = _route(path="/api/claim?step=callback&code=CODE&state=st",
                          cookies={"cx_oauth": self._pending()}, opener=opener)
        self.assertEqual((response.status, response.location), (302, "/claim"))
        cookies = _cookie_values(response)
        self.assertEqual(cookies["cx_oauth"], "")
        who = claim_session.verify(cookies["cx_session"], SECRET, now=NOW)
        self.assertEqual((who["xid"], who["handle"], who["exp"]), ("42", "alice", NOW + 3600))
        form = parse_qs(opener.requests[0].data.decode())
        self.assertEqual(form["code_verifier"], ["VER"])
        self.assertEqual(form["redirect_uri"], [f"{SITE}/api/claim?step=callback"])

    def test_callback_refusals_never_call_x(self):
        cases = [
            ("/api/claim?step=callback&code=C&state=other", {"cx_oauth": self._pending()}),
            ("/api/claim?step=callback&code=C&state=st", {}),
            ("/api/claim?step=callback&code=C&state=st", {"cx_oauth": self._pending(exp=NOW)}),
            ("/api/claim?step=callback&state=st", {"cx_oauth": self._pending()}),
            ("/api/claim?step=callback&error=access_denied&state=st", {"cx_oauth": self._pending()}),
        ]
        for path, cookies in cases:
            opener = FakeOpener({})
            response = _route(path=path, cookies=cookies, opener=opener)
            self.assertEqual(response.status, 302, path)
            self.assertTrue(response.location.startswith("/claim?error="), path)
            self.assertNotIn("cx_session", _cookie_values(response))
            self.assertEqual(opener.requests, [])

    def test_callback_x_failure(self):
        response = _route(path="/api/claim?step=callback&code=C&state=st", cookies={"cx_oauth": self._pending()},
                          opener=FakeOpener({"/oauth2/token": 400}))
        self.assertEqual((response.status, response.location), (302, "/claim?error=x"))

    def test_logout(self):
        response = _route(path="/api/claim?step=logout", cookies={"cx_session": _session()})
        self.assertEqual((response.status, response.location), (302, "/claim"))
        self.assertEqual(_cookie_values(response)["cx_session"], "")


class TestMe(unittest.TestCase):
    def test_signed_out(self):
        self.assertEqual(_route(path="/api/claim?step=me").body, {"signed_in": False})
        expired = _route(path="/api/claim?step=me", cookies={"cx_session": _session(exp=NOW)})
        self.assertEqual(expired.body, {"signed_in": False})

    def test_signed_in_reads_the_store(self):
        kv = MemoryKV()
        kv.set_json("claim:credit:42", {"handle": "alice", "claimable": 5_000_000, "burns_at": NOW + 99,
                                        "wallet": WALLET, "wallet_pending": None, "wallet_ready_at": None,
                                        "updated": NOW})
        kv.set_json("claim:status:42", {"state": "paid", "lamports": 1, "signature": "sig", "reason": None,
                                        "at": NOW})
        body = _route(path="/api/claim?step=me", cookies={"cx_session": _session()}, kv=kv).body
        self.assertEqual(body["signed_in"], True)
        self.assertEqual((body["handle"], body["claimable"], body["wallet"]), ("alice", 5_000_000, WALLET))
        self.assertEqual(body["status"]["state"], "paid")
        for key in ("burns_at", "wallet_pending", "wallet_ready_at"):
            self.assertIn(key, body)

    def test_nothing_recorded(self):
        body = _route(path="/api/claim?step=me", cookies={"cx_session": _session()}).body
        self.assertEqual((body["claimable"], body["status"]), (0, None))


class TestSubmit(unittest.TestCase):
    def setUp(self):
        self.kv = MemoryKV()
        self.kv.set_json("claim:credit:42", {"handle": "alice", "claimable": 7_000_000})

    def _post(self, wallet=WALLET, *, now=NOW, cookies=None, ctype="application/json", origin=SITE, raw=None):
        headers = {"Content-Type": ctype}
        if origin:
            headers["Origin"] = origin
        body = raw if raw is not None else json.dumps({"wallet": wallet}).encode()
        return _route("POST", "/api/claim", cookies={"cx_session": _session()} if cookies is None else cookies,
                      body=body, headers=headers, kv=self.kv, now=now)

    def test_queues(self):
        response = self._post()
        self.assertEqual((response.status, response.body), (200, {"queued": True}))
        item = self.kv.pop("claim:queue")
        self.assertEqual({k: item[k] for k in ("xid", "handle", "wallet", "at")},
                         {"xid": "42", "handle": "alice", "wallet": WALLET, "at": NOW})
        self.assertEqual(claim_session.verify_claim(item, SECRET, now=NOW),
                         {"xid": "42", "handle": "alice", "wallet": WALLET, "at": NOW})
        self.assertIsNone(claim_session.verify_claim(item, "another-secret", now=NOW))
        self.assertEqual(self.kv.get_json("claim:status:42"),
                         {"state": "queued", "lamports": 7_000_000, "signature": None, "reason": None, "at": NOW})

    def test_one_queued_claim_per_ten_minutes(self):
        self.assertEqual(self._post().status, 200)
        self.assertEqual(self._post(now=NOW + 599).status, 429)
        self.assertEqual(self._post(now=NOW + 600).status, 200)
        self.assertEqual(self.kv.command("LLEN", "claim:queue"), 2)

    def test_after_paid_may_claim_again(self):
        self.kv.set_json("claim:status:42", {"state": "paid", "at": NOW - 1})
        self.assertEqual(self._post().status, 200)

    def test_requires_session(self):
        self.assertEqual(self._post(cookies={}).status, 401)
        forged = claim_session.sign({"xid": "42", "handle": "a", "exp": NOW + 99}, "wrong")
        self.assertEqual(self._post(cookies={"cx_session": forged}).status, 401)
        self.assertIsNone(self.kv.pop("claim:queue"))

    def test_cross_site_and_non_json_refused(self):
        self.assertEqual(self._post(ctype="application/x-www-form-urlencoded").status, 415)
        self.assertEqual(self._post(origin="https://evil.example").status, 403)
        self.assertEqual(self._post(raw=b"not json").status, 400)
        self.assertEqual(self._post(raw=b"[1]").status, 400)
        self.assertIsNone(self.kv.pop("claim:queue"))

    def test_bad_wallets(self):
        for wallet in ("", None, 5, "abc", "0" * 44, WALLET + "1", "1" * 31 + "l", WALLET[:-1] + "O"):
            response = self._post(wallet)
            self.assertEqual(response.status, 400, wallet)
        self.assertIsNone(self.kv.pop("claim:queue"))

    def test_charlie_wallets_refused(self):
        for wallet in (legs.CHARLIE_PAYOUT_TREASURY, legs.CHARLIE_OPS_DESTINATION, legs.CHARLIE_LAUNCH_WALLET,
                       legs.TOLL_DESTINATION, legs.SOL_BURN_INCINERATOR, legs.LAUNCH_BUYBACK_DESTINATION):
            response = self._post(wallet)
            self.assertEqual(response.status, 400, wallet)
            self.assertIn("Charlie", response.body["error"])
        self.assertIsNone(self.kv.pop("claim:queue"))

    def test_nothing_to_claim(self):
        self.kv.delete("claim:credit:42")
        self.assertEqual(self._post().status, 400)
        self.kv.set_json("claim:credit:42", {"claimable": 0})
        self.assertEqual(self._post().status, 400)
        self.assertIsNone(self.kv.pop("claim:queue"))

    def test_store_down_is_502(self):
        class Down(MemoryKV):
            def command(self, *args):
                raise KVError("down")
        response = _route("POST", "/api/claim", cookies={"cx_session": _session()},
                          body=json.dumps({"wallet": WALLET}).encode(),
                          headers={"Content-Type": "application/json"}, kv=Down())
        self.assertEqual(response.status, 502)


class TestHandler(unittest.TestCase):
    """The thin handler writes what `route` answers: status, Location, every Set-Cookie."""

    def _serve(self, path, method="GET", body=b"", headers=None):
        handler = api_claim.handler.__new__(api_claim.handler)
        handler.path = path
        handler.headers = dict({"Content-Length": str(len(body)), **(headers or {})})
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()
        sent = {"status": None, "headers": []}
        handler.send_response = lambda status: sent.__setitem__("status", status)
        handler.send_header = lambda k, v: sent["headers"].append((k, v))
        handler.end_headers = lambda: None
        with unittest.mock.patch.dict("os.environ", ENV, clear=False):
            getattr(handler, f"do_{method}")()
        return sent, handler.wfile.getvalue()

    def test_login_through_handler(self):
        sent, body = self._serve("/api/claim?step=login")
        self.assertEqual(sent["status"], 302)
        names = [k for k, _ in sent["headers"]]
        self.assertIn("Location", names)
        self.assertIn("Set-Cookie", names)
        self.assertEqual(body, b"")

    def test_oversized_post(self):
        sent, body = self._serve("/api/claim", "POST", b"x" * 5000, {"Content-Type": "application/json"})
        self.assertEqual(sent["status"], 400)


if __name__ == "__main__":
    unittest.main()

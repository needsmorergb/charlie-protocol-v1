"""The ChatGPT-subscription moderator: sign-in, token rotation, the Codex call."""
import base64
import io
import json
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path

from indexer import codex_auth, moderation

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
IMAGE = ("image.png", "image/png", PNG)


def jwt(exp=None, account="acct-1", **auth):
    payload = {codex_auth.AUTH_CLAIM: {"chatgpt_account_id": account, **auth}} if account else {}
    if exp is not None:
        payload["exp"] = exp
    part = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"e30.{part}.sig"


class Response:
    def __init__(self, body=b"", status=200, lines=()):
        self.body, self.status, self.lines = body, status, list(lines)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body

    def __iter__(self):
        return iter(self.lines)


def http_error(code, body=b"{}"):
    return urllib.error.HTTPError("u", code, "err", {}, io.BytesIO(body))


class Opener:
    """Answers each request with the next scripted result and records it."""

    def __init__(self, *results):
        self.results, self.sent = list(results), []

    def __call__(self, request, timeout):
        self.sent.append(request)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, dict):
            return Response(json.dumps(result).encode())
        return result


def form(request):
    return dict(urllib.parse.parse_qsl(request.data.decode()))


class TestAccount(unittest.TestCase):
    def test_account_and_residency_come_from_the_access_token(self):
        self.assertEqual(codex_auth.account(jwt()), ("acct-1", ""))
        self.assertEqual(codex_auth.account(jwt(chatgpt_compute_residency="eu")), ("acct-1", "eu"))
        self.assertEqual(codex_auth.account(jwt(chatgpt_data_residency="us", chatgpt_compute_residency="eu")),
                         ("acct-1", "us"))
        for bad in (jwt(account=None), "not-a-jwt", "a.!!!.c"):
            with self.assertRaises(codex_auth.CodexAuthError):
                codex_auth.account(bad)


class TestDeviceLogin(unittest.TestCase):
    def test_the_whole_flow(self):
        opener = Opener({"device_auth_id": "dev-1", "user_code": "ABCD-1234", "interval": "1"},
                        http_error(403), http_error(404),
                        {"authorization_code": "code-1", "code_verifier": "ver-1"},
                        {"access_token": jwt(), "refresh_token": "r-1", "id_token": "i"})
        said, slept = [], []
        tokens = codex_auth.device_login(opener=opener, say=said.append, clock=lambda: 0.0, sleep=slept.append)
        self.assertEqual(tokens, {"access_token": jwt(), "refresh_token": "r-1"})
        self.assertIn("ABCD-1234", said[0])
        self.assertIn(codex_auth.VERIFY_URL, said[0])
        self.assertEqual(slept, [3, 3, 3])                    # the interval floor
        urls = [r.full_url for r in opener.sent]
        self.assertEqual(urls, [codex_auth.USERCODE] + [codex_auth.DEVICE_TOKEN] * 3 + [codex_auth.TOKEN])
        self.assertEqual(json.loads(opener.sent[0].data), {"client_id": codex_auth.CLIENT_ID})
        for request in opener.sent:                           # Python's default UA gets a Cloudflare 530
            self.assertEqual(request.get_header("User-agent"), codex_auth.USER_AGENT)
            self.assertEqual(request.get_header("Originator"), "charlie-protocol")
        self.assertEqual(json.loads(opener.sent[1].data), {"device_auth_id": "dev-1", "user_code": "ABCD-1234"})
        self.assertEqual(form(opener.sent[-1]), {
            "grant_type": "authorization_code", "code": "code-1", "redirect_uri": codex_auth.REDIRECT_URI,
            "client_id": codex_auth.CLIENT_ID, "code_verifier": "ver-1"})

    def test_a_refusal_or_a_timeout_stops_it(self):
        opener = Opener({"device_auth_id": "d", "user_code": "c"}, http_error(400))
        with self.assertRaises(codex_auth.CodexAuthError):
            codex_auth.device_login(opener=opener, say=lambda s: None, clock=lambda: 0.0, sleep=lambda s: None)
        ticks = iter([0.0, codex_auth.LOGIN_SECONDS + 1])
        opener = Opener({"device_auth_id": "d", "user_code": "c"}, http_error(403))
        with self.assertRaises(codex_auth.CodexAuthError):
            codex_auth.device_login(opener=opener, say=lambda s: None, clock=lambda: next(ticks),
                                    sleep=lambda s: None)
        with self.assertRaises(codex_auth.CodexAuthError):
            codex_auth.device_login(opener=Opener(http_error(500)), say=lambda s: None)


class TestTokenStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "codex-auth.json"

    def tearDown(self):
        self.tmp.cleanup()

    def store(self, *results, now=1000.0):
        opener = Opener(*results)
        return codex_auth.TokenStore(self.path, opener=opener, now=lambda: now), opener

    def test_a_fresh_token_needs_no_network(self):
        store, opener = self.store()
        store.save({"access_token": jwt(exp=5000), "refresh_token": "r-1"})
        self.assertEqual(store.access(), jwt(exp=5000))
        self.assertEqual(opener.sent, [])

    def test_a_token_near_expiry_is_refreshed_and_the_rotation_saved(self):
        store, opener = self.store({"access_token": jwt(exp=9000), "refresh_token": "r-2"})
        store.save({"access_token": jwt(exp=1000 + codex_auth.REFRESH_SKEW_SECONDS - 1), "refresh_token": "r-1"})
        self.assertEqual(store.access(), jwt(exp=9000))
        self.assertEqual(form(opener.sent[0]), {"grant_type": "refresh_token", "refresh_token": "r-1",
                                                "client_id": codex_auth.CLIENT_ID})
        self.assertEqual(json.loads(self.path.read_text()), {"access_token": jwt(exp=9000), "refresh_token": "r-2"})
        self.assertFalse(self.path.with_name(self.path.name + ".tmp").exists())

    def test_refusals_and_outages(self):
        store, _ = self.store(http_error(400, b'{"error": "invalid_grant"}'))
        store.save({"access_token": "", "refresh_token": "r-1"})
        with self.assertRaises(codex_auth.CodexAuthError) as refused:
            store.access()
        self.assertIn("--codex-login", str(refused.exception))
        store, _ = self.store(http_error(429))
        with self.assertRaises(codex_auth.CodexAuthError) as busy:
            store.access()
        self.assertNotIn("--codex-login", str(busy.exception))
        self.assertEqual(json.loads(self.path.read_text())["refresh_token"], "r-1")   # nothing spent

    def test_no_sign_in(self):
        store, _ = self.store()
        with self.assertRaises(codex_auth.CodexAuthError) as missing:
            store.access()
        self.assertIn("--codex-login", str(missing.exception))
        self.path.write_text('{"access_token": "x"}')
        with self.assertRaises(codex_auth.CodexAuthError):
            store.load()


def sse(*events):
    return [f"data: {json.dumps(e)}\n".encode() for e in events]


def done(text):
    return {"type": "response.output_item.done",
            "item": {"type": "message", "content": [{"type": "output_text", "text": text}]}}


class TestStream(unittest.TestCase):
    def test_items_win_and_deltas_are_the_fallback(self):
        completed = {"type": "response.completed", "response": {"output": []}}
        self.assertEqual(moderation.stream_text(sse({"type": "response.output_text.delta", "delta": "UN"},
                                                    done("SAFE"), completed)), "SAFE")
        self.assertEqual(moderation.stream_text([b"event: x\n", b"\n"] + sse(
            {"type": "response.output_text.delta", "delta": "SA"},
            {"type": "response.output_text.delta", "delta": "FE"}, completed)), "SAFE")

    def test_a_failed_or_cut_stream_is_unavailable(self):
        for lines in (sse({"type": "response.failed"}), sse({"type": "error", "message": "x"}),
                      sse(done("SAFE"))):
            with self.assertRaises(moderation.ModerationUnavailable):
                moderation.stream_text(lines)


class TestCodexModerator(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = codex_auth.TokenStore(Path(self.tmp.name) / "a.json", now=lambda: 1000.0)
        self.store.save({"access_token": jwt(exp=9000, chatgpt_data_residency="us"), "refresh_token": "r"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_request_and_the_verdict(self):
        completed = {"type": "response.completed", "response": {}}
        for answer, safe in (("SAFE", True), (" SAFE\n", True), ("UNSAFE", False), ("SAFE.", False)):
            opener = Opener(Response(lines=sse(done(answer), completed)))
            self.assertIs(moderation.codex_moderator(self.store, opener=opener)(IMAGE), safe, answer)
        request = opener.sent[0]
        self.assertEqual(request.full_url, "https://chatgpt.com/backend-api/codex/responses")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {jwt(exp=9000, chatgpt_data_residency='us')}")
        self.assertEqual(request.get_header("Chatgpt-account-id"), "acct-1")
        self.assertEqual(request.get_header("X-openai-internal-codex-residency"), "us")
        self.assertEqual(request.get_header("Originator"), "charlie-protocol")
        self.assertEqual(request.get_header("Accept"), "text/event-stream")
        body = json.loads(request.data)
        self.assertEqual((body["model"], body["stream"], body["store"]), ("gpt-5.6-luna", True, False))
        self.assertNotIn("max_output_tokens", body)
        text, image = body["input"][0]["content"]
        self.assertEqual(image, {"type": "input_image",
                                 "image_url": "data:image/png;base64," + base64.b64encode(PNG).decode()})
        for word in ("nudity", "sexual", "child", "minor", "real people, logos and violence are allowed"):
            self.assertIn(word, text["text"])

    def test_the_model_is_configurable(self):
        opener = Opener(Response(lines=sse(done("SAFE"), {"type": "response.completed"})))
        moderation.codex_moderator(self.store, model="gpt-5.5", opener=opener)(IMAGE)
        self.assertEqual(json.loads(opener.sent[0].data)["model"], "gpt-5.5")

    def test_errors_and_a_lost_sign_in_are_unavailable(self):
        for exc in (http_error(401), http_error(429), urllib.error.URLError("down"), TimeoutError("t")):
            with self.assertRaises(moderation.ModerationUnavailable):
                moderation.codex_moderator(self.store, opener=Opener(exc))(IMAGE)
        gone = codex_auth.TokenStore(Path(self.tmp.name) / "missing.json")
        with self.assertRaises(moderation.ModerationUnavailable):
            moderation.codex_moderator(gone, opener=Opener())(IMAGE)


class TestBotConfig(unittest.TestCase):
    def test_codex_moderation_config(self):
        from tools import x_bot
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "codex-auth.json"
            config = {"moderation": {"codex_auth": str(path)}}
            with self.assertRaises(x_bot.ConfigError):
                x_bot.moderator_for(config, dry_run=False)
            self.assertIsNone(x_bot.moderator_for(config, dry_run=True))
            x_bot.codex_login(path, login=lambda: {"access_token": jwt(), "refresh_token": "r"})
            self.assertTrue(callable(x_bot.moderator_for(config, dry_run=False)))
            self.assertEqual(x_bot.codex_auth_path(config, Path(tmp)), path)
            self.assertEqual(x_bot.codex_auth_path({}, Path(tmp)), Path(tmp) / "codex-auth.json")


if __name__ == "__main__":
    unittest.main()

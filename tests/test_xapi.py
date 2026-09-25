"""`indexer/xapi.py`: the OAuth 1.0a signer against X's own worked example,
the mentions shaping, writes, the size-capped fetch, and the PKCE sign-in.
Every request goes to a scripted opener; nothing reaches the network."""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes_http import FakeOpener  # noqa: E402
from indexer import xapi  # noqa: E402

# X's "Creating a signature" page (docs.x.com, authentication/oauth-1-0a),
# read 2026-09-22. The page's values are marked invalid for real requests.
EXAMPLE = dict(
    consumer_key="xvz1evFS4wEEPTGEFPHBog",
    consumer_secret="kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",
    token="370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
    token_secret="LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE",
    nonce="kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg",
    timestamp=1318622958,
)
EXAMPLE_PARAMS = {"status": "Hello Ladies + Gentlemen, a signed OAuth request!", "include_entities": "true"}


def _signature(header: str) -> str:
    for part in header[len("OAuth "):].split(", "):
        name, _, value = part.partition("=")
        if name == "oauth_signature":
            from urllib.parse import unquote
            return unquote(value.strip('"'))
    raise AssertionError("no signature")


class TestOAuth1(unittest.TestCase):
    def test_docs_example_on_api_x_com(self):
        header = xapi.oauth1_header("POST", "https://api.x.com/1.1/statuses/update.json", EXAMPLE_PARAMS, **EXAMPLE)
        self.assertEqual(_signature(header), "Ls93hJiZbQ3akF3HF3x1Bz8/zU4=")

    def test_docs_example_on_the_older_twitter_host(self):
        # The same page before the rename, api.twitter.com, published this value.
        header = xapi.oauth1_header("POST", "https://api.twitter.com/1.1/statuses/update.json",
                                    EXAMPLE_PARAMS, **EXAMPLE)
        self.assertEqual(_signature(header), "hCtSmYh+iHYCEqBWrE7C7hYmtUk=")

    def test_query_in_url_is_signed_like_params(self):
        a = xapi.oauth1_header("POST", "https://api.x.com/1.1/statuses/update.json?include_entities=true",
                               {"status": EXAMPLE_PARAMS["status"]}, **EXAMPLE)
        self.assertEqual(_signature(a), "Ls93hJiZbQ3akF3HF3x1Bz8/zU4=")

    def test_header_shape(self):
        header = xapi.oauth1_header("POST", "https://api.x.com/2/tweets", {}, **EXAMPLE)
        self.assertTrue(header.startswith("OAuth "))
        for name in ("oauth_consumer_key", "oauth_nonce", "oauth_signature", "oauth_signature_method",
                     "oauth_timestamp", "oauth_token", "oauth_version"):
            self.assertIn(f'{name}="', header)
        self.assertNotIn("secret", header.lower())
        self.assertNotIn(EXAMPLE["consumer_secret"], header)


def _mentions_page(ids, *, newest=None, next_token=None):
    return {
        "data": [{"id": i, "text": f"@CharlieSlugSOL t{i}", "author_id": "u1",
                  "attachments": {"media_keys": ["3_1"]}} for i in ids],
        "includes": {"users": [{"id": "u1", "username": "alice"}],
                     "media": [{"media_key": "3_1", "type": "photo", "url": "https://pbs.twimg.com/a.png"}],
                     "tweets": [{"id": "88", "attachments": {"media_keys": ["3_1"]}}]},
        "meta": {"newest_id": newest or (ids[0] if ids else None), "result_count": len(ids),
                 **({"next_token": next_token} if next_token else {})},
    }


class TestMentions(unittest.TestCase):
    def test_shapes_oldest_first_with_lookups(self):
        opener = FakeOpener({"/mentions": _mentions_page(["30", "9", "20"], newest="30")})
        x = xapi.XClient(bearer="B", opener=opener)
        out = x.mentions("123", "5")
        self.assertEqual([t["id"] for t in out["tweets"]], ["9", "20", "30"])
        self.assertEqual(out["users"]["u1"]["username"], "alice")
        self.assertEqual(out["media"]["3_1"]["type"], "photo")
        self.assertEqual(out["refs"]["88"]["attachments"], {"media_keys": ["3_1"]})   # the post replied to
        self.assertEqual(out["newest_id"], "30")
        request = opener.last()
        self.assertEqual(request.get_header("Authorization"), "Bearer B")
        query = parse_qs(urlsplit(request.full_url).query)
        self.assertEqual(urlsplit(request.full_url).path, "/2/users/123/mentions")
        self.assertEqual(query["since_id"], ["5"])
        self.assertEqual(query["max_results"], ["100"])
        self.assertEqual(query["expansions"], ["author_id,attachments.media_keys,referenced_tweets.id,"
                                               "referenced_tweets.id.attachments.media_keys"])
        self.assertIn("parody", query["user.fields"][0])
        self.assertIn("edit_history_tweet_ids", query["tweet.fields"][0])
        self.assertEqual(query["media.fields"], ["url,type"])

    def test_no_since_id_is_omitted(self):
        opener = FakeOpener({"/mentions": {"meta": {"result_count": 0}}})
        out = xapi.XClient(bearer="B", opener=opener).mentions("123", None)
        self.assertEqual(out, {"tweets": [], "users": {}, "media": {}, "refs": {}, "newest_id": None})
        self.assertNotIn("since_id", opener.last().full_url)

    def test_follows_pages(self):
        pages = iter([_mentions_page(["50", "40"], newest="50", next_token="N1"), _mentions_page(["30"])])
        opener = FakeOpener({"/mentions": lambda: next(pages)})
        out = xapi.XClient(bearer="B", opener=opener).mentions("1", "10")
        self.assertEqual([t["id"] for t in out["tweets"]], ["30", "40", "50"])
        self.assertEqual(out["newest_id"], "50")
        self.assertIn("pagination_token=N1", opener.requests[1].full_url)

    def test_a_query_reads_recent_search_and_pages_by_next_token(self):
        pages = iter([_mentions_page(["50"], newest="50", next_token="N1"), _mentions_page(["30"])])
        opener = FakeOpener({"/search/recent": lambda: next(pages)})
        out = xapi.XClient(bearer="B", opener=opener).mentions("1", "10", query="@Bot launch -is:retweet")
        self.assertEqual([t["id"] for t in out["tweets"]], ["30", "50"])
        first = urlsplit(opener.requests[0].full_url)
        self.assertEqual(first.path, "/2/tweets/search/recent")
        self.assertIn("query=%40Bot%20launch%20-is%3Aretweet", first.query)
        self.assertIn("since_id=10", first.query)
        self.assertIn("next_token=N1", opener.requests[1].full_url)
        self.assertNotIn("pagination_token", opener.requests[1].full_url)

    def test_a_backlog_past_the_page_cap_raises_rather_than_skip(self):
        from unittest import mock
        opener = FakeOpener({"/mentions": lambda: _mentions_page(["50"], next_token="MORE")})
        with mock.patch.object(xapi, "MAX_PAGES", 3), self.assertRaises(xapi.XError):
            xapi.XClient(bearer="B", opener=opener).mentions("1", "10")
        self.assertEqual(len(opener.requests), 3)

    def test_many_pages_are_all_followed(self):
        pages = iter([_mentions_page([str(100 - i)], next_token=f"N{i}") for i in range(7)] + [_mentions_page(["9"])])
        opener = FakeOpener({"/mentions": lambda: next(pages)})
        out = xapi.XClient(bearer="B", opener=opener).mentions("1", "5")
        self.assertEqual([t["id"] for t in out["tweets"]][0], "9")
        self.assertEqual(len(out["tweets"]), 8)

    def test_http_error_carries_status(self):
        x = xapi.XClient(bearer="B", opener=FakeOpener({"/mentions": 429}))
        with self.assertRaises(xapi.XError) as caught:
            x.mentions("1", None)
        self.assertEqual(caught.exception.status, 429)

    def test_no_bearer_refuses_before_any_request(self):
        opener = FakeOpener({})
        with self.assertRaises(xapi.XError):
            xapi.XClient(opener=opener).mentions("1", None)
        self.assertEqual(opener.requests, [])


def _user_client(opener):
    return xapi.XClient(consumer_key="ck", consumer_secret="cs", access_token="at", access_secret="as",
                        opener=opener, now=lambda: 1_700_000_000, nonce=lambda: "fixednonce")


class TestWrites(unittest.TestCase):
    def test_post_reply(self):
        opener = FakeOpener({"/2/tweets": {"data": {"id": "777", "text": "hi"}}})
        self.assertEqual(_user_client(opener).post_reply("555", "hi"), "777")
        request = opener.last()
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {"text": "hi", "reply": {"in_reply_to_tweet_id": "555"}})
        expected = xapi.oauth1_header("POST", "https://api.x.com/2/tweets", {}, consumer_key="ck",
                                      consumer_secret="cs", token="at", token_secret="as",
                                      nonce="fixednonce", timestamp=1_700_000_000)
        self.assertEqual(request.get_header("Authorization"), expected)

    def test_post_reply_without_id_raises(self):
        with self.assertRaises(xapi.XError):
            _user_client(FakeOpener({"/2/tweets": {"errors": [{"message": "no"}]}})).post_reply("1", "x")

    def test_like(self):
        opener = FakeOpener({"/likes": {"data": {"liked": True}}})
        _user_client(opener).like("42", "555")
        self.assertTrue(opener.last().full_url.endswith("/2/users/42/likes"))
        self.assertEqual(json.loads(opener.last().data), {"tweet_id": "555"})

    def test_writes_need_user_credentials(self):
        with self.assertRaises(xapi.XError):
            xapi.XClient(bearer="B", opener=FakeOpener({})).post_reply("1", "x")


class TestFetch(unittest.TestCase):
    def test_returns_type_and_bytes(self):
        opener = FakeOpener({"pbs.twimg.com": ("image/png; charset=binary", b"\x89PNG....")})
        ctype, body = xapi.XClient(opener=opener).fetch("https://pbs.twimg.com/a.png")
        self.assertEqual((ctype, body), ("image/png", b"\x89PNG...."))

    def test_size_cap(self):
        opener = FakeOpener({"pbs.twimg.com": ("image/png", b"x" * 101)})
        with self.assertRaises(xapi.XError):
            xapi.XClient(opener=opener).fetch("https://pbs.twimg.com/a.png", limit=100)
        opener = FakeOpener({"pbs.twimg.com": ("image/png", b"x" * 100)})
        self.assertEqual(len(xapi.XClient(opener=opener).fetch("https://pbs.twimg.com/a.png", limit=100)[1]), 100)

    def test_only_https(self):
        with self.assertRaises(xapi.XError):
            xapi.XClient(opener=FakeOpener({})).fetch("http://pbs.twimg.com/a.png")
        with self.assertRaises(xapi.XError):
            xapi.XClient(opener=FakeOpener({})).fetch("file:///etc/passwd")


class TestPkce(unittest.TestCase):
    def test_pair(self):
        verifier, challenge = xapi.pkce_pair(lambda n: bytes(range(n)))
        self.assertEqual(len(verifier), 43)
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        self.assertEqual(challenge, expected)
        self.assertNotIn("=", verifier + challenge)

    def test_rfc7636_appendix_b(self):
        # RFC 7636 appendix B's verifier and challenge.
        raw = bytes([116, 24, 223, 180, 151, 153, 224, 37, 79, 250, 96, 125, 216, 173, 187, 186, 22, 212,
                     37, 77, 105, 214, 191, 240, 91, 88, 5, 88, 83, 132, 141, 121])
        verifier, challenge = xapi.pkce_pair(lambda n: raw)
        self.assertEqual(verifier, "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
        self.assertEqual(challenge, "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")

    def test_authorize_url(self):
        url = xapi.authorize_url("cid", "https://charlieprotocol.fun/api/claim?step=callback", "st", "ch")
        parts = urlsplit(url)
        self.assertEqual(f"{parts.scheme}://{parts.netloc}{parts.path}", xapi.AUTHORIZE)
        query = parse_qs(parts.query)
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["redirect_uri"], ["https://charlieprotocol.fun/api/claim?step=callback"])
        self.assertEqual(query["scope"], ["users.read tweet.read"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual((query["state"], query["code_challenge"], query["client_id"]), (["st"], ["ch"], ["cid"]))

    def test_exchange_code(self):
        opener = FakeOpener({"/oauth2/token": {"access_token": "AT", "token_type": "bearer"}})
        token = xapi.exchange_code("CODE", "VER", client_id="cid", client_secret="sec",
                                   redirect_uri="https://s/cb", opener=opener)
        self.assertEqual(token, "AT")
        request = opener.last()
        self.assertEqual(request.full_url, xapi.TOKEN)
        self.assertEqual(request.get_header("Authorization"), "Basic " + base64.b64encode(b"cid:sec").decode())
        self.assertEqual(request.get_header("Content-type"), "application/x-www-form-urlencoded")
        form = parse_qs(request.data.decode())
        self.assertEqual(form["grant_type"], ["authorization_code"])
        self.assertEqual((form["code"], form["code_verifier"], form["redirect_uri"]),
                         (["CODE"], ["VER"], ["https://s/cb"]))

    def test_exchange_refused(self):
        with self.assertRaises(xapi.XError):
            xapi.exchange_code("C", "V", client_id="c", client_secret="s", redirect_uri="r",
                               opener=FakeOpener({"/oauth2/token": 400}))
        with self.assertRaises(xapi.XError):
            xapi.exchange_code("C", "V", client_id="c", client_secret="s", redirect_uri="r",
                               opener=FakeOpener({"/oauth2/token": {"error": "invalid_grant"}}))

    def test_me(self):
        opener = FakeOpener({"/users/me": {"data": {"id": "99", "username": "alice", "name": "A"}}})
        self.assertEqual(xapi.me("AT", opener=opener), {"id": "99", "username": "alice"})
        self.assertEqual(opener.last().get_header("Authorization"), "Bearer AT")
        with self.assertRaises(xapi.XError):
            xapi.me("AT", opener=FakeOpener({"/users/me": {"errors": []}}))


if __name__ == "__main__":
    unittest.main()

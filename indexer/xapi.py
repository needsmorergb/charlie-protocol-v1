"""The X API, as far as the tag launcher and the claim page need it.

Standard library only. Two ways to authenticate, one per job:

* The bot reads its mentions with the app's bearer token and writes (replies,
  likes) as its own account with OAuth 1.0a user context, signed here with
  HMAC-SHA1 exactly as X's "Creating a signature" page describes. The test
  suite checks the signer against that page's own worked example.
* The claim page signs a requester in with OAuth 2.0 and PKCE (S256), then
  asks `/users/me` who they are. That token is used once and never stored.

Every HTTP call goes through an injectable `opener` (the shape of
`urllib.request.urlopen`), so tests never reach the network.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode, urlsplit, urlunsplit, parse_qsl

API = "https://api.x.com/2"
AUTHORIZE = "https://x.com/i/oauth2/authorize"
TOKEN = API + "/oauth2/token"

USER_AGENT = "charlie-protocol tag launcher"
TIMEOUT = 20

# What the mentions poll asks for. Every name here is a documented v2 field
# (checked against docs.x.com 2026-09-22; `parody` is a real user field).
TWEET_FIELDS = "created_at,edit_history_tweet_ids,referenced_tweets,attachments,author_id,text"
USER_FIELDS = "created_at,public_metrics,verified_type,protected,profile_image_url,withheld,parody,username,name"
MEDIA_FIELDS = "url,type"
EXPANSIONS = "author_id,attachments.media_keys"
MAX_PAGES = 5


class XError(RuntimeError):
    """X answered an error, or the answer could not be used. `status` is the
    HTTP status (0 when there was no HTTP answer at all)."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


# -- transport ---------------------------------------------------------------


def _open(request: urllib.request.Request, opener=None, *, limit: int = 2_000_000) -> tuple[str, bytes]:
    """`(content-type, body)` for a request, reading at most `limit` bytes.
    An HTTP error status becomes `XError` with that status."""
    open_ = opener or urllib.request.urlopen
    try:
        with open_(request, timeout=TIMEOUT) as response:
            headers = getattr(response, "headers", None) or {}
            ctype = headers.get("Content-Type", "") if hasattr(headers, "get") else ""
            declared = headers.get("Content-Length") if hasattr(headers, "get") else None
            if declared and str(declared).isdigit() and int(declared) > limit:
                raise XError(f"answer larger than {limit} bytes", 0)
            body = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        detail = b""
        try:
            detail = exc.read(2_000)
        except Exception:
            pass
        raise XError(f"X answered HTTP {exc.code}: {detail.decode('utf-8', 'replace')[:300]}", exc.code) from None
    except urllib.error.URLError as exc:
        raise XError(f"X did not answer: {exc.reason}", 0) from None
    if len(body) > limit:
        raise XError(f"answer larger than {limit} bytes", 0)
    return ctype, body


def _json(request: urllib.request.Request, opener=None) -> dict:
    _ctype, body = _open(request, opener)
    try:
        answer = json.loads(body.decode("utf-8"))
    except ValueError:
        raise XError("X answered something that is not JSON", 0) from None
    if not isinstance(answer, dict):
        raise XError("X answered something that is not a JSON object", 0)
    return answer


# -- OAuth 1.0a (the bot's own account) ----------------------------------------


def _pct(value) -> str:
    """RFC 3986 percent-encoding, as OAuth 1.0a requires (only A-Z a-z 0-9 - . _ ~ pass)."""
    return quote(str(value), safe="-._~")


def _base_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "", ""))


def oauth1_header(method, url, params, *, consumer_key, consumer_secret, token, token_secret,
                  nonce, timestamp) -> str:
    """The `Authorization` header value for one OAuth 1.0a request.

    `params` are the request's query and form-body parameters (a JSON body is
    not signed). Query parameters already in `url` are signed too."""
    oauth = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(timestamp),
        "oauth_token": token,
        "oauth_version": "1.0",
    }
    pairs = [(_pct(k), _pct(v)) for k, v in dict(params or {}).items()]
    pairs += [(_pct(k), _pct(v)) for k, v in parse_qsl(urlsplit(url).query, keep_blank_values=True)]
    pairs += [(_pct(k), _pct(v)) for k, v in oauth.items()]
    parameter_string = "&".join(f"{k}={v}" for k, v in sorted(pairs))
    base = "&".join((method.upper(), _pct(_base_url(url)), _pct(parameter_string)))
    key = f"{_pct(consumer_secret)}&{_pct(token_secret)}".encode()
    signature = base64.b64encode(hmac.new(key, base.encode(), hashlib.sha1).digest()).decode()
    oauth["oauth_signature"] = signature
    return "OAuth " + ", ".join(f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(oauth.items()))


# -- the bot's client -----------------------------------------------------------


class XClient:
    def __init__(self, *, bearer: str = "", consumer_key="", consumer_secret="", access_token="",
                 access_secret="", opener=None, now=time.time, nonce=None):
        self.bearer = bearer
        self.consumer_key = consumer_key
        self.consumer_secret = consumer_secret
        self.access_token = access_token
        self.access_secret = access_secret
        self.opener = opener
        self.now = now
        self.nonce = nonce or (lambda: os.urandom(16).hex())

    # -- auth --

    def _bearer_get(self, url: str) -> dict:
        if not self.bearer:
            raise XError("no bearer token configured", 0)
        request = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.bearer}", "User-Agent": USER_AGENT}, method="GET")
        return _json(request, self.opener)

    def _user_post(self, url: str, payload: dict) -> dict:
        if not (self.consumer_key and self.consumer_secret and self.access_token and self.access_secret):
            raise XError("no OAuth 1.0a user credentials configured", 0)
        header = oauth1_header("POST", url, {}, consumer_key=self.consumer_key,
                               consumer_secret=self.consumer_secret, token=self.access_token,
                               token_secret=self.access_secret, nonce=self.nonce(),
                               timestamp=int(self.now()))
        request = urllib.request.Request(url, json.dumps(payload).encode(), headers={
            "Authorization": header, "Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST")
        return _json(request, self.opener)

    # -- reads --

    def mentions(self, user_id: str, since_id: str | None) -> dict:
        """Mentions of `user_id` newer than `since_id`, oldest first, with the
        authors and media the tweets reference keyed for lookup.

        Returns `{"tweets": [...], "users": {id: user}, "media": {key: media},
        "newest_id": str | None}`. Follows `next_token` for up to MAX_PAGES
        pages so a burst between polls is not skipped."""
        tweets: dict[str, dict] = {}
        users: dict[str, dict] = {}
        media: dict[str, dict] = {}
        newest: str | None = None
        token: str | None = None
        for _ in range(MAX_PAGES):
            params = {
                "max_results": "100",
                "expansions": EXPANSIONS,
                "tweet.fields": TWEET_FIELDS,
                "user.fields": USER_FIELDS,
                "media.fields": MEDIA_FIELDS,
            }
            if since_id:
                params["since_id"] = str(since_id)
            if token:
                params["pagination_token"] = token
            page = self._bearer_get(f"{API}/users/{quote(str(user_id), safe='')}/mentions?"
                                    + urlencode(params, quote_via=quote))
            shaped = shape_mentions(page)
            for tweet in shaped["tweets"]:
                tweets[str(tweet.get("id"))] = tweet
            users.update(shaped["users"])
            media.update(shaped["media"])
            newest = _max_id(newest, shaped["newest_id"])
            token = (page.get("meta") or {}).get("next_token")
            if not token:
                break
        ordered = sorted(tweets.values(), key=lambda t: _id_int(t.get("id")))
        return {"tweets": ordered, "users": users, "media": media, "newest_id": newest}

    # -- writes --

    def post_reply(self, tweet_id: str, text: str) -> str:
        """Reply to `tweet_id` as the bot. Returns the new tweet's id."""
        answer = self._user_post(f"{API}/tweets", {"text": text, "reply": {"in_reply_to_tweet_id": str(tweet_id)}})
        new_id = (answer.get("data") or {}).get("id")
        if not new_id:
            raise XError(f"X did not return the reply's id: {str(answer)[:300]}", 0)
        return str(new_id)

    def like(self, bot_user_id: str, tweet_id: str) -> None:
        self._user_post(f"{API}/users/{quote(str(bot_user_id), safe='')}/likes", {"tweet_id": str(tweet_id)})

    def fetch(self, url: str, *, limit: int = 5_000_000) -> tuple[str, bytes]:
        """`(content-type, bytes)` of a public URL (a tweet's image), refusing
        anything larger than `limit` bytes and anything not https."""
        if urlsplit(url).scheme != "https":
            raise XError("only https URLs are fetched", 0)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
        ctype, body = _open(request, self.opener, limit=limit)
        return ctype.split(";")[0].strip().lower(), body


def _id_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _max_id(a: str | None, b: str | None) -> str | None:
    if a is None:
        return b
    if b is None:
        return a
    return a if _id_int(a) >= _id_int(b) else b


def shape_mentions(page: dict) -> dict:
    """One mentions page, reshaped: tweets oldest first, users by id, media
    by media_key, and the newest id (meta's, else the largest seen)."""
    data = [t for t in (page.get("data") or []) if isinstance(t, dict)]
    includes = page.get("includes") or {}
    users = {str(u["id"]): u for u in includes.get("users") or [] if isinstance(u, dict) and "id" in u}
    media = {str(m["media_key"]): m for m in includes.get("media") or [] if isinstance(m, dict) and "media_key" in m}
    tweets = sorted(data, key=lambda t: _id_int(t.get("id")))
    newest = (page.get("meta") or {}).get("newest_id")
    if newest is None and tweets:
        newest = str(tweets[-1].get("id"))
    return {"tweets": tweets, "users": users, "media": media, "newest_id": str(newest) if newest else None}


# -- OAuth 2.0 with PKCE (the claim page's sign-in) --------------------------------


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def pkce_pair(random=os.urandom) -> tuple[str, str]:
    """`(verifier, challenge)`: a 43-character verifier and its S256 challenge."""
    verifier = _b64url(random(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def authorize_url(client_id, redirect_uri, state, challenge, scope="users.read tweet.read") -> str:
    query = urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }, quote_via=quote)
    return f"{AUTHORIZE}?{query}"


def exchange_code(code, verifier, *, client_id, client_secret, redirect_uri, opener=None) -> str:
    """Trade an authorization code for an access token (confidential client:
    HTTP Basic with the client id and secret, form body)."""
    body = urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
        "client_id": client_id,
    }).encode()
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    request = urllib.request.Request(TOKEN, body, headers={
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": USER_AGENT,
    }, method="POST")
    answer = _json(request, opener)
    token = answer.get("access_token")
    if not token or not isinstance(token, str):
        raise XError("X did not return an access token", 0)
    return token


def me(access_token, *, opener=None) -> dict:
    """`{"id", "username"}` of whoever the access token belongs to."""
    request = urllib.request.Request(f"{API}/users/me", headers={
        "Authorization": f"Bearer {access_token}", "User-Agent": USER_AGENT}, method="GET")
    data = _json(request, opener).get("data") or {}
    if not data.get("id") or not data.get("username"):
        raise XError("X did not say who signed in", 0)
    return {"id": str(data["id"]), "username": str(data["username"])}

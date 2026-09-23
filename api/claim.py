"""Sign a requester in with X and queue their claim. Never pays anything.

The claim page's server half, routed at `/api/claim`:

* `GET ?step=login`: redirect to X's authorize page (OAuth 2.0, PKCE S256),
  with the state and verifier in a signed ten-minute cookie `cx_oauth`.
* `GET ?step=callback&code&state`: check the cookie and state, trade the code
  for a token, ask X who signed in, and set a signed one-hour cookie
  `cx_session` with their numeric X id and handle. The X token is used once
  here and never stored.
* `GET ?step=me`: what the store says this requester may claim, and the state
  of their latest claim.
* `POST {"wallet": base58}`: queue a claim to that wallet. The bot pays it
  from the treasury later, by X id, after its own checks. At most one queued
  claim per requester every ten minutes.
* `GET ?step=logout`: forget the session.

Nothing here holds a key or sends a transaction. The logic lives in plain
functions returning a `Response`, so the tests drive it without a socket.
"""

from __future__ import annotations

import hmac
import json
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from indexer import claim_session, legs, xapi  # noqa: E402
from indexer.base58 import decode, encode  # noqa: E402
from indexer.kv import KV, KVError  # noqa: E402

DEFAULT_SITE = "https://charlieprotocol.fun"
QUEUE_KEY = "claim:queue"
QUEUED_SECONDS = 600
MAX_BODY = 4_000
BASE58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
NOT_OPEN = "claims are not open yet"


def credit_key(xid: str) -> str:
    return f"claim:credit:{xid}"


def status_key(xid: str) -> str:
    return f"claim:status:{xid}"


@dataclass(frozen=True)
class Config:
    client_id: str
    client_secret: str
    secret: str
    kv_url: str
    kv_token: str
    site: str

    @property
    def redirect_uri(self) -> str:
        return f"{self.site}/api/claim?step=callback"


def config(env=None) -> Config | None:
    """The configuration from the environment, or None when any part is missing."""
    env = os.environ if env is None else env
    values = {name: (env.get(name) or "").strip() for name in (
        "X_CLIENT_ID", "X_CLIENT_SECRET", "CHARLIE_SESSION_SECRET",
        "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")}
    if not all(values.values()):
        return None
    site = ((env.get("CHARLIE_SITE") or "").strip() or DEFAULT_SITE).rstrip("/")
    return Config(values["X_CLIENT_ID"], values["X_CLIENT_SECRET"], values["CHARLIE_SESSION_SECRET"],
                  values["UPSTASH_REDIS_REST_URL"], values["UPSTASH_REDIS_REST_TOKEN"], site)


@dataclass
class Response:
    status: int
    body: dict | None = None
    location: str | None = None
    cookies: list[str] = field(default_factory=list)


def _json(status: int, body: dict) -> Response:
    return Response(status, body)


def _redirect(location: str, *cookies: str) -> Response:
    return Response(302, None, location, list(cookies))


# -- wallets ------------------------------------------------------------------------


def refused_wallets() -> set[str]:
    """Charlie's own wallets and the incinerator: never a claim's destination."""
    names = (legs.CHARLIE_PAYOUT_TREASURY, legs.CHARLIE_OPS_DESTINATION, legs.CHARLIE_LAUNCH_WALLET,
             legs.TOLL_DESTINATION, legs.SOL_BURN_INCINERATOR, legs.LAUNCH_BUYBACK_DESTINATION,
             "11111111111111111111111111111111")
    return {name for name in names if name}


def check_wallet(value) -> str | None:
    """None when `value` is a wallet a claim may be paid to, else why not."""
    if not isinstance(value, str) or not value.strip():
        return "Enter the wallet address to pay."
    value = value.strip()
    if len(value) < 32 or len(value) > 44 or any(c not in BASE58 for c in value):
        return "That is not a Solana wallet address."
    try:
        raw = decode(value)
    except ValueError:
        return "That is not a Solana wallet address."
    if len(raw) != 32 or encode(raw) != value:
        return "That is not a Solana wallet address."
    if value in refused_wallets():
        return "That address belongs to Charlie. Enter your own wallet."
    return None


# -- the steps ----------------------------------------------------------------------


def session(cfg: Config, cookies: dict, *, now: float) -> dict | None:
    payload = claim_session.verify(cookies.get(claim_session.SESSION_COOKIE), cfg.secret, now=now)
    if not payload or not payload.get("xid") or not payload.get("handle"):
        return None
    return payload


def login(cfg: Config, *, now: float, random=os.urandom) -> Response:
    verifier, challenge = xapi.pkce_pair(random)
    state = random(16).hex()
    cookie = claim_session.sign({"state": state, "verifier": verifier, "exp": int(now) + claim_session.OAUTH_SECONDS},
                                cfg.secret)
    return _redirect(xapi.authorize_url(cfg.client_id, cfg.redirect_uri, state, challenge),
                     claim_session.set_cookie(claim_session.OAUTH_COOKIE, cookie,
                                              max_age=claim_session.OAUTH_SECONDS))


def callback(cfg: Config, query: dict, cookies: dict, *, now: float, opener=None) -> Response:
    """Finish the sign-in. Any failure lands back on the page with `?error=`."""
    clear = claim_session.clear_cookie(claim_session.OAUTH_COOKIE)
    if query.get("error"):
        return _redirect("/claim?error=denied", clear)
    pending = claim_session.verify(cookies.get(claim_session.OAUTH_COOKIE), cfg.secret, now=now)
    code, state = query.get("code") or "", query.get("state") or ""
    if not pending or not code or not state or not isinstance(pending.get("state"), str):
        return _redirect("/claim?error=expired", clear)
    if not hmac.compare_digest(pending["state"].encode(), state.encode()):
        return _redirect("/claim?error=expired", clear)
    try:
        token = xapi.exchange_code(code, pending.get("verifier") or "", client_id=cfg.client_id,
                                   client_secret=cfg.client_secret, redirect_uri=cfg.redirect_uri,
                                   opener=opener)
        who = xapi.me(token, opener=opener)
    except xapi.XError:
        return _redirect("/claim?error=x", clear)
    value = claim_session.sign({"xid": who["id"], "handle": who["username"],
                                "exp": int(now) + claim_session.SESSION_SECONDS}, cfg.secret)
    return _redirect("/claim", clear, claim_session.set_cookie(
        claim_session.SESSION_COOKIE, value, max_age=claim_session.SESSION_SECONDS))


def me_view(cfg: Config, kv: KV, cookies: dict, *, now: float) -> Response:
    who = session(cfg, cookies, now=now)
    if who is None:
        return _json(200, {"signed_in": False})
    credit = kv.get_json(credit_key(who["xid"]))
    credit = credit if isinstance(credit, dict) else {}
    status = kv.get_json(status_key(who["xid"]))
    return _json(200, {
        "signed_in": True,
        "handle": who["handle"],
        "claimable": int(credit.get("claimable") or 0),
        "burns_at": credit.get("burns_at"),
        "wallet": credit.get("wallet"),
        "wallet_pending": credit.get("wallet_pending"),
        "wallet_ready_at": credit.get("wallet_ready_at"),
        "status": status if isinstance(status, dict) else None,
    })


def submit(cfg: Config, kv: KV, cookies: dict, body: bytes, *, now: float,
           content_type: str = "application/json", origin: str | None = None) -> Response:
    who = session(cfg, cookies, now=now)
    if who is None:
        return _json(401, {"error": "Sign in with X first."})
    # A JSON body from this site only: a cross-site form cannot send one.
    if "application/json" not in (content_type or "").lower():
        return _json(415, {"error": "Send the claim as JSON."})
    if origin and origin.rstrip("/") != cfg.site:
        return _json(403, {"error": "Claims are made from the claim page."})
    try:
        request = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return _json(400, {"error": "That is not a claim."})
    if not isinstance(request, dict):
        return _json(400, {"error": "That is not a claim."})
    problem = check_wallet(request.get("wallet"))
    if problem:
        return _json(400, {"error": problem})
    wallet = request["wallet"].strip()
    xid = who["xid"]
    status = kv.get_json(status_key(xid))
    if (isinstance(status, dict) and status.get("state") == "queued"
            and now - float(status.get("at") or 0) < QUEUED_SECONDS):
        return _json(429, {"error": "Your last claim is still in the queue. Check back in a few minutes."})
    credit = kv.get_json(credit_key(xid))
    claimable = int((credit or {}).get("claimable") or 0) if isinstance(credit, dict) else 0
    if claimable <= 0:
        return _json(400, {"error": "There is nothing to claim for this account yet."})
    at = int(now)
    # Status first, then the queue item: the bot writes its own status only
    # after popping the item, so it can never be overwritten by "queued".
    kv.set_json(status_key(xid), {"state": "queued", "lamports": claimable, "signature": None,
                                  "reason": None, "at": at})
    try:
        kv.push(QUEUE_KEY, claim_session.sign_claim(xid, who["handle"], wallet, at, cfg.secret))
    except Exception:
        kv.set_json(status_key(xid), {"state": "refused", "lamports": 0, "signature": None,
                                      "reason": "the claim could not be queued; try again", "at": at})
        raise
    return _json(200, {"queued": True})


def logout() -> Response:
    return _redirect("/claim", claim_session.clear_cookie(claim_session.SESSION_COOKIE),
                     claim_session.clear_cookie(claim_session.OAUTH_COOKIE))


def route(method: str, path: str, headers: dict, body: bytes = b"", *, env=None, kv: KV | None = None,
          opener=None, now: float | None = None, random=os.urandom) -> Response:
    """One request in, one `Response` out. `kv`, `opener`, `now` and `random` are for tests."""
    cfg = config(env)
    if cfg is None:
        return _json(503, {"error": NOT_OPEN})
    now = time.time() if now is None else now
    query = {k: (v or [""])[0].strip() for k, v in parse_qs(urlparse(path).query).items()}
    cookies = claim_session.read_cookies(headers.get("Cookie") or headers.get("cookie"))
    store = kv or KV(cfg.kv_url, cfg.kv_token)
    try:
        if method == "POST":
            return submit(cfg, store, cookies, body, now=now,
                          content_type=headers.get("Content-Type") or headers.get("content-type") or "",
                          origin=headers.get("Origin") or headers.get("origin"))
        step = query.get("step", "")
        if step == "login":
            return login(cfg, now=now, random=random)
        if step == "callback":
            return callback(cfg, query, cookies, now=now, opener=opener)
        if step == "me":
            return me_view(cfg, store, cookies, now=now)
        if step == "logout":
            return logout()
        return _json(400, {"error": "Unknown step."})
    except KVError:
        return _json(502, {"error": "The claim store did not answer. Try again in a minute."})


# -- the handler ----------------------------------------------------------------------


class handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self._serve("GET", b"")

    def do_POST(self):  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_BODY:
            return self._write(Response(400, {"error": "That is not a claim."}))
        self._serve("POST", self.rfile.read(length))

    def _serve(self, method: str, body: bytes):
        try:
            response = route(method, self.path, dict(self.headers.items()), body)
        except Exception:  # pragma: no cover - the last line of defence
            traceback.print_exc()
            response = Response(502, {"error": "Something went wrong. Nothing was queued."})
        self._write(response)

    def _write(self, response: Response):
        payload = json.dumps(response.body).encode() if response.body is not None else b""
        self.send_response(response.status)
        if response.location:
            self.send_header("Location", response.location)
        for cookie in response.cookies:
            self.send_header("Set-Cookie", cookie)
        if response.body is not None:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

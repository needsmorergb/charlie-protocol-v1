"""ChatGPT sign-in for the image moderator, the way Hermes Agent does it.

The moderator runs on a ChatGPT subscription through OpenAI's Codex backend
instead of an API key. The bot keeps its OWN grant in its own file, never
~/.codex/auth.json: refresh tokens are single use, so two programs sharing
one grant would spend each other's token, and OpenAI revokes the whole
family when a spent token is replayed.

* `device_login` runs the device-code sign-in once
  (`python -m tools.x_bot --codex-login`): the owner opens a URL, enters a
  code, and the tokens go into the store.
* `TokenStore.access()` returns a current access token. It refreshes two
  minutes before expiry and writes the rotated refresh token back before
  anything uses the new access token.

Endpoints and client id are the Codex CLI's (openai/codex, codex-rs/login),
as Hermes Agent uses them (hermes_cli/auth_codex.py).

Stdlib only. Nothing here prints or logs a token.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ISSUER = "https://auth.openai.com"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
USERCODE = ISSUER + "/api/accounts/deviceauth/usercode"
DEVICE_TOKEN = ISSUER + "/api/accounts/deviceauth/token"
VERIFY_URL = ISSUER + "/codex/device"
TOKEN = ISSUER + "/oauth/token"
REDIRECT_URI = ISSUER + "/deviceauth/callback"
AUTH_CLAIM = "https://api.openai.com/auth"
REFRESH_SKEW_SECONDS = 120
LOGIN_SECONDS = 15 * 60
TIMEOUT_SECONDS = 30
LOGIN_HINT = "run: python -m tools.x_bot --codex-login"
ORIGINATOR = "charlie-protocol"              # third-party clients name themselves
USER_AGENT = "CharlieProtocol/1 (x-tag moderation)"


class CodexAuthError(RuntimeError):
    """No usable ChatGPT sign-in right now."""


def claims(token: str) -> dict:
    """The JWT payload of `token`, unverified (it is only read, never trusted for access)."""
    try:
        payload = token.split(".")[1]
        decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        raise CodexAuthError("the access token is not a readable JWT") from None
    if not isinstance(decoded, dict):
        raise CodexAuthError("the access token is not a readable JWT")
    return decoded


def account(token: str) -> tuple[str, str]:
    """(chatgpt_account_id, residency or "") from the access token."""
    auth = claims(token).get(AUTH_CLAIM) or {}
    account_id = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
    if not account_id:
        raise CodexAuthError("the access token names no ChatGPT account")
    residency = auth.get("chatgpt_data_residency") or auth.get("chatgpt_compute_residency") or ""
    return str(account_id), str(residency)


def _post(open_url, url: str, body: dict, *, form: bool = False) -> tuple[int, dict]:
    """(HTTP status, JSON object or {}). Network failures raise OSError."""
    if form:
        data, ctype = urllib.parse.urlencode(body).encode(), "application/x-www-form-urlencoded"
    else:
        data, ctype = json.dumps(body).encode(), "application/json"
    request = urllib.request.Request(url, data, headers={
        "Content-Type": ctype, "Accept": "application/json",
        # Cloudflare answers Python's default User-Agent with HTTP 530 (measured 2026-09-23).
        "User-Agent": USER_AGENT, "originator": ORIGINATOR}, method="POST")
    try:
        with open_url(request, timeout=TIMEOUT_SECONDS) as response:
            status, raw = getattr(response, "status", 200), response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, (exc.read() if exc.fp else b"")
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        parsed = {}
    return status, parsed if isinstance(parsed, dict) else {}


def device_login(*, opener=None, say=print, clock=time.monotonic, sleep=time.sleep) -> dict:
    """Sign in with a device code; returns {"access_token", "refresh_token"}."""
    open_url = opener or urllib.request.urlopen
    status, start = _post(open_url, USERCODE, {"client_id": CLIENT_ID})
    code = start.get("user_code") or start.get("usercode")
    device_id = start.get("device_auth_id")
    if status != 200 or not (code and device_id):
        raise CodexAuthError(f"OpenAI would not start a device sign-in (HTTP {status})")
    try:
        interval = max(3, int(start.get("interval") or 5))
    except (TypeError, ValueError):
        interval = 5
    say(f"Open {VERIFY_URL} in a browser signed in to ChatGPT and enter the code {code}")
    deadline = clock() + LOGIN_SECONDS
    while True:
        sleep(interval)
        status, grant = _post(open_url, DEVICE_TOKEN, {"device_auth_id": device_id, "user_code": code})
        if status == 200:
            break
        if status not in (403, 404, 429):            # 403/404: not approved yet; 429: slow down
            raise CodexAuthError(f"the device sign-in failed (HTTP {status})")
        if clock() > deadline:
            raise CodexAuthError("the device sign-in was not approved within 15 minutes")
    status, tokens = _post(open_url, TOKEN, {
        "grant_type": "authorization_code", "code": grant.get("authorization_code", ""),
        "redirect_uri": REDIRECT_URI, "client_id": CLIENT_ID,
        "code_verifier": grant.get("code_verifier", "")}, form=True)
    if status != 200 or not tokens.get("access_token") or not tokens.get("refresh_token"):
        raise CodexAuthError(f"OpenAI would not issue tokens (HTTP {status})")
    account(tokens["access_token"])                   # fail now, not at the first image
    return {"access_token": tokens["access_token"], "refresh_token": tokens["refresh_token"]}


class TokenStore:
    """The bot's own grant, one JSON file: {"access_token", "refresh_token"}."""

    def __init__(self, path, *, opener=None, now=time.time):
        self.path = Path(os.path.expanduser(str(path)))
        self.open_url = opener or urllib.request.urlopen
        self.now = now

    def load(self) -> dict:
        try:
            tokens = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise CodexAuthError(f"no ChatGPT sign-in at {self.path}; {LOGIN_HINT}") from None
        except ValueError:
            raise CodexAuthError(f"{self.path} is not readable JSON; {LOGIN_HINT}") from None
        if not isinstance(tokens, dict) or not tokens.get("refresh_token"):
            raise CodexAuthError(f"{self.path} holds no refresh token; {LOGIN_HINT}")
        return tokens

    def save(self, tokens: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        staging = self.path.with_name(self.path.name + ".tmp")
        staging.write_text(json.dumps({"access_token": tokens["access_token"],
                                       "refresh_token": tokens["refresh_token"]}) + "\n", encoding="utf-8")
        os.replace(staging, self.path)

    def access(self) -> str:
        tokens = self.load()
        token = tokens.get("access_token") or ""
        if token:
            expires = claims(token).get("exp")
            if not isinstance(expires, (int, float)) or expires - REFRESH_SKEW_SECONDS > self.now():
                return token
        return self.refresh(tokens)["access_token"]

    def refresh(self, tokens: dict) -> dict:
        status, fresh = _post(self.open_url, TOKEN, {
            "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
            "client_id": CLIENT_ID}, form=True)
        if status == 200 and fresh.get("access_token"):
            saved = {"access_token": fresh["access_token"],
                     "refresh_token": fresh.get("refresh_token") or tokens["refresh_token"]}
            self.save(saved)                           # the old refresh token is spent now
            return saved
        if status == 429 or status >= 500:
            raise CodexAuthError(f"OpenAI could not refresh the sign-in right now (HTTP {status})")
        raise CodexAuthError(f"the ChatGPT sign-in was refused (HTTP {status}); {LOGIN_HINT}")

"""Signed cookie values for the claim page's sign-in.

A value is `base64url(JSON payload) + "." + base64url(HMAC-SHA256(secret,
first part))`. The payload carries `exp`, an epoch second; a value past it,
or whose signature does not match (compared in constant time), reads as None.
Nothing secret goes in a payload: it is signed, not encrypted. The PKCE
verifier rides in one for ten minutes, which is what it is for; without the
client secret it cannot be exchanged by anyone else.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json

OAUTH_COOKIE = "cx_oauth"
SESSION_COOKIE = "cx_session"
OAUTH_SECONDS = 600
SESSION_SECONDS = 3600
COIN_LINK_PREFIX = "tag:coin:"


def coin_link_key(tweet_id) -> str:
    """The store key holding the mint a tag launched, by the tag's tweet id.
    The bot writes it; `/t/<tweet id>` reads it and redirects to the coin page."""
    return f"{COIN_LINK_PREFIX}{tweet_id}"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _key(secret) -> bytes:
    key = secret.encode() if isinstance(secret, str) else bytes(secret or b"")
    if not key:
        raise ValueError("a session secret is required")
    return key


def sign(payload: dict, secret) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    mac = hmac.new(_key(secret), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(mac)}"


def verify(value: str | None, secret, *, now: float) -> dict | None:
    """The payload of a value this secret signed and that has not expired, else None."""
    if not value or not isinstance(value, str) or value.count(".") != 1:
        return None
    body, mac = value.split(".")
    expected = _b64(hmac.new(_key(secret), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expected.encode(), mac.encode()):
        return None
    try:
        payload = json.loads(_unb64(body).decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool) or now >= exp:
        return None
    return payload


def set_cookie(name: str, value: str, *, max_age: int) -> str:
    """A `Set-Cookie` header value: HttpOnly, Secure, SameSite=Lax, whole site."""
    return f"{name}={value}; Path=/; Max-Age={int(max_age)}; HttpOnly; Secure; SameSite=Lax"


def clear_cookie(name: str) -> str:
    return f"{name}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax"


def read_cookies(header: str | None) -> dict[str, str]:
    """`{name: value}` from a `Cookie` request header. The first of a repeated name wins."""
    found: dict[str, str] = {}
    for piece in (header or "").split(";"):
        name, sep, value = piece.strip().partition("=")
        if sep and name and name not in found:
            found[name] = value.strip().strip('"')
    return found


# -- the claim queue ------------------------------------------------------------------
#
# The site pushes each claim onto a queue the bot pops. The bot pays only
# items this secret signed, so write access to the store alone cannot queue a
# payment. An item is stale after CLAIM_SECONDS (7 days): a replayed item
# only asks again for a payout to the user's own bound wallet, so it is harmless.

CLAIM_SECONDS = 7 * 86_400
CLAIM_FIELDS = ("xid", "handle", "wallet", "at")


def sign_claim(xid: str, handle: str, wallet: str, at: int, secret) -> dict:
    """The queue item: the four fields and `sig` over them (with `exp`)."""
    fields = {"xid": str(xid), "handle": str(handle), "wallet": str(wallet), "at": int(at)}
    return {**fields, "sig": sign({**fields, "exp": fields["at"] + CLAIM_SECONDS}, secret)}


def verify_claim(item, secret, *, now: float) -> dict | None:
    """The signed fields of a queue item, or None when it is unsigned,
    signed with another secret, altered, or older than CLAIM_SECONDS."""
    if not isinstance(item, dict) or not secret:
        return None
    payload = verify(item.get("sig"), secret, now=now)
    if not payload:
        return None
    at = payload.get("at")
    if not isinstance(at, int) or isinstance(at, bool) or payload.get("exp") != at + CLAIM_SECONDS:
        return None
    if not all(isinstance(payload.get(k), str) and payload.get(k) for k in ("xid", "wallet")):
        return None
    return {k: payload.get(k) for k in CLAIM_FIELDS}

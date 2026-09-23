"""Redirect a tag's short link to its coin page, routed at `/t/<tweet id>`.

The X-tag bot replies with `charlieprotocol.fun/t/<tweet id>` instead of the
coin address, because X refuses posts that carry a crypto address from a
newly authorised app. The bot stores the mint under
`claim_session.coin_link_key(tweet id)` before it replies; this function reads
it and redirects to `/coin/<mint>`. An unknown tweet, or no store, goes to
`/coins`. Nothing here writes.
"""
from __future__ import annotations

import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from indexer import claim_session  # noqa: E402
from indexer.kv import KV, KVError  # noqa: E402

BASE58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
FALLBACK = "/coins"
KNOWN_CACHE = "public, max-age=300, s-maxage=86400"   # a tweet's coin never changes


def target(path: str, *, env=None, kv: KV | None = None) -> tuple[str, bool]:
    """Where `/t/<tweet id>` goes, and whether that answer may be cached."""
    env = os.environ if env is None else env
    tweet = (parse_qs(urlparse(path).query).get("tweet") or [""])[0].strip()
    if not tweet.isdigit() or len(tweet) > 20:
        return FALLBACK, False
    if kv is None:
        url, token = env.get("UPSTASH_REDIS_REST_URL", ""), env.get("UPSTASH_REDIS_REST_TOKEN", "")
        if not (url and token):
            return FALLBACK, False
        kv = KV(url, token)
    try:
        mint = kv.get_json(claim_session.coin_link_key(tweet))
    except KVError:
        return FALLBACK, False
    if not isinstance(mint, str) or not 32 <= len(mint) <= 44 or not set(mint) <= BASE58:
        return FALLBACK, False
    return f"/coin/{mint}", True


class handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        try:
            location, known = target(self.path)
        except Exception:  # pragma: no cover - the last line of defence
            traceback.print_exc()
            location, known = FALLBACK, False
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", KNOWN_CACHE if known else "no-store")
        self.end_headers()

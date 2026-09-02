"""Answer for ANY contract address, live, at request time.

WHY THIS EXISTS. The site told the public to paste a CA. Only pre-generated
coins had pages, so every other paste reached a "not measured yet" page. The
promise was already public and being retweeted, so the honest options were to
retract the promise or to keep it. This keeps it.

An observation is ~1 second of RPC reads and no disk, so it fits a serverless
request comfortably. Nothing is cached and nothing is written: the page is
rendered from a reading taken while the visitor waited, which is a stronger
claim than a committed artifact, not a weaker one.

Renders through `site.render` -- the SAME function that produces the committed
pages, so a live answer and a committed one cannot disagree about how a coin
is described, and every figure stays behind `publish.Publisher`'s gate.
"""

from __future__ import annotations

import os
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from indexer import site  # noqa: E402
from indexer.base58 import decode, encode  # noqa: E402
from indexer.legs import Registry  # noqa: E402
from indexer.observe import observe  # noqa: E402
from indexer.rpc import RpcClient  # noqa: E402

BASE58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def valid_mint(value: str) -> str | None:
    """Canonicalise by reconstruction, the same rule `intake.validate_mint`
    uses: a string is a mint only if decoding and re-encoding it returns the
    string itself. Length and alphabet checks alone accept addresses that no
    key could ever produce.
    """
    if not value or len(value) < 32 or len(value) > 44:
        return None
    if any(ch not in BASE58 for ch in value):
        return None
    try:
        raw = decode(value)
    except Exception:
        return None
    if len(raw) != 32 or encode(raw) != value:
        return None
    return value


def _endpoints():
    configured = os.environ.get("CHARLIE_RPC_URLS", "").strip()
    if configured:
        return [u.strip() for u in configured.split(",") if u.strip()]
    return None


class handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 -- Vercel's Python runtime dispatches this name
        query = parse_qs(urlparse(self.path).query)
        raw = (query.get("mint") or [""])[0].strip()
        # Every coin page links "View the raw observation JSON". For a coin
        # rendered live that file has never been written, so the link 404'd --
        # on the page most visitors see. The record is generated from the same
        # observation the page was, so it costs one more read and nothing else.
        wants_json = (query.get("format") or [""])[0] == "json"

        mint = valid_mint(raw)
        if mint is None:
            return self._send(400, site.render_verify())

        if wants_json:
            committed = self._committed(mint, ".json")
            if committed is not None:
                return self._send(200, committed, "application/json")
            try:
                rpc = RpcClient(_endpoints()) if _endpoints() else RpcClient()
                record = observe(rpc, mint, Registry(), now=time.time(), evidence=None)
                return self._send(200, site.record_json(record), "application/json")
            except Exception:
                traceback.print_exc()
                return self._send(503, '{"error": "could not read the chain"}',
                                  "application/json")

        # A committed page wins. It was rendered against the evidence store, so
        # it carries figures a live read cannot reach -- recorded burns, the
        # SOL behind them, a completed walk. Serving a thinner live answer for
        # a coin we have measured properly would be a downgrade disguised as
        # freshness.
        committed = self._committed(mint, ".html")
        if committed is not None:
            return self._send(200, committed)

        try:
            rpc = RpcClient(_endpoints()) if _endpoints() else RpcClient()
            record = observe(rpc, mint, Registry(), now=time.time(), evidence=None)
            return self._send(200, site.render(record))
        except Exception:
            # An RPC that will not answer is not a verdict about the coin, and
            # must never be rendered as one. Say the read failed, in those
            # words, and keep every figure off the page.
            traceback.print_exc()
            return self._send(503, site.render_unavailable(mint))

    def _committed(self, mint: str, suffix: str) -> str | None:
        """The pre-generated page for this coin, if one is bundled.

        `site._artifact_name` is the single filename constructor (D-19), so
        this reads the same name `write()` produces and the two cannot drift
        apart into a function that looks in the wrong place.
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "web", site._artifact_name(mint, suffix))
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return None

    def _send(self, status: int, body: str, content_type: str = "text/html"):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "public, max-age=0, s-maxage=60")
        self.end_headers()
        self.wfile.write(payload)

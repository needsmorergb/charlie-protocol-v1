"""A scripted stand-in for `urllib.request.urlopen`, shared by the X, store and
claim tests. Nothing here touches the network."""

from __future__ import annotations

import io
import json
import urllib.error


class FakeResponse:
    def __init__(self, body: bytes, headers: dict | None = None):
        self._body = io.BytesIO(body)
        self.headers = headers or {}

    def read(self, n: int = -1) -> bytes:
        return self._body.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Answers each request with the first scripted answer whose key is a
    substring of the URL. An answer is a dict/list (sent as JSON), bytes,
    `(content_type, bytes)`, or an int HTTP status to raise."""

    def __init__(self, script: dict | None = None):
        self.script = dict(script or {})
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        url = request.full_url
        for key, answer in self.script.items():
            if key in url:
                return self._answer(url, answer)
        raise AssertionError(f"unscripted request to {url}")

    @staticmethod
    def _answer(url, answer):
        if callable(answer):
            answer = answer()
        if isinstance(answer, int):
            raise urllib.error.HTTPError(url, answer, "scripted", {}, io.BytesIO(b'{"title":"scripted"}'))
        if isinstance(answer, tuple):
            ctype, body = answer
            return FakeResponse(body, {"Content-Type": ctype})
        if isinstance(answer, (bytes, bytearray)):
            return FakeResponse(bytes(answer), {"Content-Type": "application/octet-stream"})
        return FakeResponse(json.dumps(answer).encode(), {"Content-Type": "application/json"})

    def last(self):
        return self.requests[-1]

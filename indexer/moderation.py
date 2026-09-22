"""Image moderation for X-tag launches, before anything is pinned.

One call to the Anthropic Messages API with the tag's image. The model must
answer exactly SAFE or UNSAFE; anything other than SAFE refuses the tag.
An API error, a timeout or an unreadable answer raises
`ModerationUnavailable`, which the bot records as `moderation_unavailable`
(not held against the requester) and posts nothing.

Stdlib only. The key comes from the bot's config (`moderation`), never from
the repo.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

API = "https://api.anthropic.com/v1/messages"
MODEL = "claude-haiku-4-5-20251001"
ANTHROPIC_VERSION = "2023-06-01"
TIMEOUT_SECONDS = 30

PROMPT = (
    "You screen images that would become the picture of a publicly traded meme coin. "
    "Does this image contain any of: sexual content or nudity; a minor in any unsafe or "
    "sexualised context; gore or graphic violence; hate symbols; a real, identifiable person; "
    "a brand logo or trademark? "
    "Answer with exactly one word: SAFE if it contains none of these, UNSAFE otherwise."
)


class ModerationUnavailable(RuntimeError):
    """The moderator could not give a verdict; nothing may be pinned."""


def verdict(answer: dict) -> bool:
    """True only for a Messages API answer whose text is exactly SAFE."""
    if not isinstance(answer, dict) or answer.get("type") == "error":
        raise ModerationUnavailable(f"the API answered an error: {str(answer)[:200]}")
    blocks = answer.get("content")
    if not isinstance(blocks, list):
        raise ModerationUnavailable("the API answer has no content")
    text = "".join(b.get("text") or "" for b in blocks if isinstance(b, dict) and b.get("type") == "text")
    return text.strip() == "SAFE"


def request_body(image: tuple[str, str, bytes]) -> dict:
    _name, ctype, data = image
    return {
        "model": MODEL,
        "max_tokens": 5,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": ctype,
                                         "data": base64.b64encode(data).decode("ascii")}},
            {"type": "text", "text": PROMPT},
        ]}],
    }


def anthropic_moderator(api_key: str, *, opener=None, timeout: float = TIMEOUT_SECONDS):
    """A `moderate(image) -> bool` for `tag_bot.Bot`: True means SAFE."""
    if not api_key:
        raise ValueError("no Anthropic API key")
    open_url = opener or urllib.request.urlopen

    def moderate(image: tuple[str, str, bytes]) -> bool:
        request = urllib.request.Request(API, json.dumps(request_body(image)).encode(), headers={
            "x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json"}, method="POST")
        try:
            with open_url(request, timeout=timeout) as response:
                body = response.read()
        except (urllib.error.URLError, OSError, ValueError) as exc:   # HTTPError, timeouts included
            raise ModerationUnavailable(f"{type(exc).__name__}: {exc}") from None
        try:
            answer = json.loads(body.decode("utf-8"))
        except ValueError:
            raise ModerationUnavailable("the API answered something that is not JSON") from None
        return verdict(answer)

    return moderate

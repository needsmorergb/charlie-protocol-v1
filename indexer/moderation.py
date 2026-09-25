"""Image moderation for X-tag launches, before anything is pinned.

One model call with the tag's image, through either of two providers:

* `codex_moderator`: OpenAI through a ChatGPT subscription (Codex backend,
  signed in with `indexer.codex_auth`, as Hermes Agent does).
* `anthropic_moderator`: the Anthropic Messages API with an API key.

The model must answer exactly SAFE or UNSAFE; anything other than SAFE
refuses the tag. An API error, a timeout, a lost sign-in or an unreadable
answer raises `ModerationUnavailable`, which the bot records as
`moderation_unavailable` (not held against the requester) and posts nothing.

Stdlib only. Keys and sign-ins come from the bot's config (`moderation`),
never from the repo.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
import uuid

from indexer import codex_auth

API = "https://api.anthropic.com/v1/messages"
MODEL = "claude-haiku-4-5-20251001"
ANTHROPIC_VERSION = "2023-06-01"
TIMEOUT_SECONDS = 30

PROMPT = (
    "You screen images that would become the picture of a publicly traded meme coin. "
    "News photos, real people, logos and violence are allowed. Does this image contain "
    "either of: nudity or sexual content; a child or minor (anyone who looks under 18)? "
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


# -- OpenAI through a ChatGPT subscription (Codex backend) ---------------------

CODEX_API = "https://chatgpt.com/backend-api/codex/responses"
CODEX_MODEL = "gpt-5.6-luna"          # listed by /codex/models for a ChatGPT account, 2026-09-23
ORIGINATOR = codex_auth.ORIGINATOR
USER_AGENT = codex_auth.USER_AGENT


def codex_request_body(image: tuple[str, str, bytes], model: str = CODEX_MODEL) -> dict:
    _name, ctype, data = image
    return {
        "model": model,
        "instructions": "You are an image screener. Answer with exactly one word.",
        "input": [{"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": PROMPT},
            {"type": "input_image", "image_url": f"data:{ctype};base64,{base64.b64encode(data).decode('ascii')}"},
        ]}],
        "store": False,
        "stream": True,                      # the backend only streams
        "include": [],
        "reasoning": {"effort": "low"},
        "text": {"verbosity": "low"},
    }


def stream_text(lines) -> str:
    """The answer text from a Responses SSE stream. Finished message items win
    over deltas, because `response.completed` may carry an empty output."""
    deltas, items, completed = [], [], False
    for raw in lines:
        line = (raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw).strip()
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[5:].strip())
        except ValueError:
            continue
        kind = event.get("type") if isinstance(event, dict) else None
        if kind == "response.output_text.delta":
            deltas.append(str(event.get("delta") or ""))
        elif kind == "response.output_item.done":
            item = event.get("item") or {}
            if item.get("type") == "message":
                items.append("".join(part.get("text") or "" for part in item.get("content") or ()
                                     if isinstance(part, dict) and part.get("type") == "output_text"))
        elif kind == "response.completed":
            completed = True
            break
        elif kind in ("error", "response.failed", "response.incomplete"):
            raise ModerationUnavailable(f"the model stream ended with {kind}: {line[5:205]}")
    if not completed:
        raise ModerationUnavailable("the model stream ended before response.completed")
    return "".join(items) if items else "".join(deltas)


def codex_moderator(store: codex_auth.TokenStore, *, model: str = CODEX_MODEL, opener=None,
                    timeout: float = TIMEOUT_SECONDS):
    """A `moderate(image) -> bool` for `tag_bot.Bot`: True means SAFE."""
    open_url = opener or urllib.request.urlopen

    def moderate(image: tuple[str, str, bytes]) -> bool:
        try:
            token = store.access()
            account_id, residency = codex_auth.account(token)
        except (codex_auth.CodexAuthError, OSError, ValueError) as exc:
            raise ModerationUnavailable(f"ChatGPT sign-in: {exc}") from None
        headers = {"Authorization": f"Bearer {token}", "ChatGPT-Account-ID": account_id,
                   "originator": ORIGINATOR, "User-Agent": USER_AGENT, "session_id": str(uuid.uuid4()),
                   "Content-Type": "application/json", "Accept": "text/event-stream"}
        if residency:
            headers["x-openai-internal-codex-residency"] = residency
        request = urllib.request.Request(CODEX_API, json.dumps(codex_request_body(image, model)).encode(),
                                         headers=headers, method="POST")
        try:
            with open_url(request, timeout=timeout) as response:
                text = stream_text(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace") if exc.fp else ""
            raise ModerationUnavailable(f"HTTP {exc.code}: {detail}") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:   # timeouts included
            raise ModerationUnavailable(f"{type(exc).__name__}: {exc}") from None
        return text.strip() == "SAFE"

    return moderate

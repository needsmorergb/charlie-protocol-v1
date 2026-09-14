"""The Telegram front desk for the launch door. It never signs anything.

A dev's key never leaves the dev's wallet. That rule is written into
`indexer/enroll.py` and it does not bend for Telegram: a bot cannot hold a
key, so a bot cannot create a coin. What it can do is the part before the
wallet opens -- collect the name, ticker, description, image and links, pin
them through the site's own `/api/launch` (which relays to pump's metadata
service), and hand back ONE link to `/launch` with the fields filled in. The
page is where the wallet opens and the two approvals happen.

Stdlib only, long polling, no framework. Per-chat state lives in memory in
this process; a restart forgets half-finished drafts, and that is fine
because nothing about a draft is worth keeping.

    TELEGRAM_BOT_TOKEN=... python -m tools.launch_bot
    TELEGRAM_BOT_TOKEN=... python -m tools.launch_bot --site https://charlieprotocol.fun

If any message from any account ever asks for a seed phrase or a private
key, it is not this bot. The intro says so in as many words.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import launch  # noqa: E402

DEFAULT_SITE = "https://charlieprotocol.fun"
TELEGRAM = "https://api.telegram.org"
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
MAX_IMAGE_BYTES = 4_000_000

INTRO = (
    "This is the Charlie Protocol launch desk.\n\n"
    "It helps you describe a coin, then hands you one link to the launch page, "
    "where your own wallet creates the coin and sets its fee split in two "
    "approvals. A share of the creator fee goes to Solana's incinerator, a "
    "quarter goes to the protocol, the rest wherever you say.\n\n"
    "Before anything else: pump lets a coin's fee split be changed exactly "
    "once. Launching this way spends that one change at creation, on purpose, "
    "on a split you chose. After that no key can change it, including yours.\n\n"
    "This bot never asks for a seed phrase or a private key, and never will. "
    "Anything that does is not us.\n\n"
    "Send /launch to start, /cancel to stop."
)

STEPS = ("name", "symbol", "description", "image", "links")


@dataclass
class Draft:
    step: str = "name"
    name: str = ""
    symbol: str = ""
    description: str = ""
    image: tuple[str, str, bytes] | None = None
    links: dict[str, str] = field(default_factory=dict)


class Desk:
    """The conversation, with its two side effects injected: `download(file_id)
    -> (filename, content_type, bytes)` and `pin(fields, image) -> uri`."""

    def __init__(self, *, site: str, download, pin):
        self.site = site.rstrip("/")
        self.download = download
        self.pin = pin
        self.drafts: dict[int, Draft] = {}

    def handle(self, message: dict) -> list[str]:
        chat = message.get("chat", {}).get("id")
        if chat is None:
            return []
        text = (message.get("text") or "").strip()
        if text.startswith("/start") or text.startswith("/help"):
            return [INTRO]
        if text.startswith("/cancel"):
            self.drafts.pop(chat, None)
            return ["Cancelled. Nothing was created, nothing was kept. /launch to start again."]
        if text.startswith("/launch"):
            self.drafts[chat] = Draft()
            return ["What is the coin's name? Up to 32 bytes. It cannot be changed after creation."]
        draft = self.drafts.get(chat)
        if draft is None:
            return ["Send /launch to describe a coin, or /start to read what this is."]
        return getattr(self, "_" + draft.step)(chat, draft, message, text)

    # -- steps --

    def _name(self, chat, draft, message, text):
        if not text:
            return ["Type the coin's name as text."]
        try:
            launch.validate_metadata(text, "X", "https://x/")
        except launch.LaunchError as exc:
            return [str(exc)]
        draft.name = text
        draft.step = "symbol"
        return [f"Name: {text}\n\nTicker? Up to 10 bytes, no spaces."]

    def _symbol(self, chat, draft, message, text):
        if not text:
            return ["Type the ticker as text."]
        try:
            launch.validate_metadata(draft.name, text, "https://x/")
        except launch.LaunchError as exc:
            return [str(exc)]
        draft.symbol = text.upper() if text.isascii() else text
        draft.step = "description"
        return [f"Ticker: {draft.symbol}\n\nDescription? One message. Or send - for none."]

    def _description(self, chat, draft, message, text):
        if not text:
            return ["Type the description as text, or - for none."]
        draft.description = "" if text == "-" else text[:1000]
        draft.step = "image"
        return ["Now the image: send it as a photo or as a file. PNG, JPEG, GIF or WebP, up to 4 MB. pump shows it beside the coin everywhere and it cannot be changed."]

    def _image(self, chat, draft, message, text):
        file_id, hint_type = _image_reference(message)
        if file_id is None:
            return ["Send the image as a photo or a file. Nothing else is needed at this step."]
        try:
            filename, content_type, data = self.download(file_id)
        except Exception as exc:  # noqa: BLE001 - the dev needs the words, not a stack
            return [f"Could not fetch that file from Telegram ({exc}). Send it again."]
        content_type = content_type or hint_type or "image/jpeg"
        if content_type not in IMAGE_TYPES:
            return ["That is not a PNG, JPEG, GIF or WebP. Send one of those."]
        if len(data) > MAX_IMAGE_BYTES:
            return ["That image is over 4 MB. Send a smaller one."]
        draft.image = (filename or "image", content_type, data)
        draft.step = "links"
        return ["Got it.\n\nLinks, optional: send them as lines like\n\ntwitter https://x.com/yourcoin\ntelegram https://t.me/yourcoin\nwebsite https://yourcoin.xyz\n\nor send - to skip."]

    def _links(self, chat, draft, message, text):
        if not text:
            return ["Send the links as text lines, or - to skip."]
        if text != "-":
            for line in text.splitlines():
                key, _sep, value = line.strip().partition(" ")
                key = key.lower().strip(":")
                if key in ("twitter", "x"):
                    key = "twitter"
                if key in ("twitter", "telegram", "website") and value.strip():
                    draft.links[key] = value.strip()[:200]
        fields = {"name": draft.name, "symbol": draft.symbol, "description": draft.description, **draft.links}
        try:
            uri = self.pin(fields, draft.image)
        except Exception as exc:  # noqa: BLE001
            return [f"Pinning the metadata failed ({exc}). Nothing was created. Send - or the links again to retry, or /cancel."]
        self.drafts.pop(chat, None)
        link = self.link(draft.name, draft.symbol, uri)
        return [
            "Pinned. This is the metadata pump will show:\n" + uri,
            "Your launch link:\n" + link +
            "\n\nOpen it in a browser with your wallet. It will ask for two approvals: "
            "first the coin, then its split. Read the split before the second one; "
            "it is permanent. Nothing here has created anything yet, and nothing "
            "will until your wallet signs.",
        ]

    def link(self, name: str, symbol: str, uri: str) -> str:
        return f"{self.site}/launch?" + urllib.parse.urlencode({"name": name, "symbol": symbol, "uri": uri})


def _image_reference(message: dict) -> tuple[str | None, str | None]:
    photos = message.get("photo") or []
    if photos:
        # Telegram sends several sizes; the last is the largest.
        return photos[-1].get("file_id"), "image/jpeg"
    document = message.get("document") or {}
    if document.get("file_id"):
        return document["file_id"], document.get("mime_type")
    return None, None


# -- the wires ------------------------------------------------------------------------


class Telegram:
    def __init__(self, token: str):
        self.token = token

    def call(self, method: str, **params):
        body = json.dumps(params).encode()
        request = urllib.request.Request(f"{TELEGRAM}/bot{self.token}/{method}", body,
                                         {"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=60) as response:
            answer = json.loads(response.read().decode())
        if not answer.get("ok"):
            raise RuntimeError(answer.get("description") or "telegram refused")
        return answer["result"]

    def download(self, file_id: str) -> tuple[str, str | None, bytes]:
        info = self.call("getFile", file_id=file_id)
        path = info["file_path"]
        with urllib.request.urlopen(f"{TELEGRAM}/file/bot{self.token}/{path}", timeout=60) as response:
            return Path(path).name, None, response.read()


def site_pin(site: str):
    """`pin(fields, image) -> uri` through the site's own `/api/launch`, so
    the limits and the relay to pump live in one place."""
    def pin(fields, image):
        boundary = "charlie" + uuid.uuid4().hex
        out = bytearray()
        for name, value in fields.items():
            out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n".encode()
            out += str(value).encode("utf-8") + b"\r\n"
        filename, ctype, data = image
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                f"filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n").encode()
        out += data + b"\r\n" + f"--{boundary}--\r\n".encode()
        request = urllib.request.Request(site.rstrip("/") + "/api/launch", bytes(out),
                                         {"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                answer = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            answer = json.loads(exc.read().decode() or "{}")
        if answer.get("error"):
            raise RuntimeError(answer["error"])
        return answer["metadataUri"]
    return pin


def run(token: str, site: str) -> None:  # pragma: no cover - the network loop
    tg = Telegram(token)
    desk = Desk(site=site, download=tg.download, pin=site_pin(site))
    offset = None
    print(f"launch desk up; linking to {site}/launch")
    while True:
        try:
            updates = tg.call("getUpdates", timeout=30, **({"offset": offset} if offset else {}))
        except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
            print(f"getUpdates: {exc}; retrying")
            time.sleep(5)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            message = update.get("message") or update.get("edited_message")
            if not message:
                continue
            for reply in desk.handle(message):
                try:
                    tg.call("sendMessage", chat_id=message["chat"]["id"], text=reply, disable_web_page_preview=False)
                except (urllib.error.URLError, RuntimeError) as exc:
                    print(f"sendMessage: {exc}")


def main(argv=None) -> int:  # pragma: no cover
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--site", default=os.environ.get("CHARLIE_SITE", DEFAULT_SITE))
    args = parser.parse_args(argv)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set", file=sys.stderr)
        return 2
    run(token, args.site)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

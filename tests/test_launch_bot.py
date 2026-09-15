"""The Telegram front desk, driven end to end with its two side effects faked.

What matters is what a dev is told and what the link carries. The bot's one
hard rule -- it never signs and never asks for a key -- is pinned as text,
because a bot that drifted into asking for one would pass every other test.
"""

from __future__ import annotations

import sys
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import launch_bot  # noqa: E402

URI = "https://ipfs.io/ipfs/bafkreibs2xlm4qm4ubh2g4wsnstlgcviephup43gq3yikzsyiltww2xpwq"


def _msg(text=None, chat=7, **extra):
    m = {"chat": {"id": chat}}
    if text is not None:
        m["text"] = text
    m.update(extra)
    return m


class _Desk(launch_bot.Desk):
    def __init__(self, *, pin_error=None):
        self.pinned = []
        self.downloads = []

        def download(file_id):
            self.downloads.append(file_id)
            if file_id == "bad":
                raise RuntimeError("no such file")
            return ("photo.jpg", None, b"\xff\xd8" + b"x" * 10)

        def pin(fields, image):
            if pin_error:
                raise pin_error
            self.pinned.append((fields, image))
            return URI

        super().__init__(site="https://charlieprotocol.fun/", download=download, pin=pin)


def _walk(desk, chat=7, links="-"):
    out = []
    out += desk.handle(_msg("/launch", chat))
    out += desk.handle(_msg("Probe Coin", chat))
    out += desk.handle(_msg("probe", chat))
    out += desk.handle(_msg("a coin that burns", chat))
    out += desk.handle(_msg(None, chat, photo=[{"file_id": "small"}, {"file_id": "big"}]))
    out += desk.handle(_msg(links, chat))
    return out


class TestTheRule(unittest.TestCase):
    def test_the_intro_says_it_never_asks_for_a_key(self):
        text = "\n".join(_Desk().handle(_msg("/start")))
        self.assertIn("never asks for a seed phrase or a private key", text)
        self.assertIn("exactly once", text)

    def test_no_reply_anywhere_asks_for_a_key(self):
        desk = _Desk()
        replies = _walk(desk)
        for r in replies:
            low = r.lower()
            self.assertNotIn("seed phrase", low.replace("never asks for a seed phrase", ""))
            self.assertNotIn("private key", low.replace("never asks for a seed phrase or a private key", ""))


class TestTheWalk(unittest.TestCase):
    def test_a_full_walk_ends_in_one_link_carrying_the_fields(self):
        desk = _Desk()
        replies = _walk(desk, links="twitter https://x.com/probe\nwebsite https://probe.xyz\nnonsense line")
        link_reply = replies[-1]
        self.assertIn("https://charlieprotocol.fun/launch?", link_reply)
        url = link_reply.split("\n")[1]
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(query, {"name": ["Probe Coin"], "symbol": ["PROBE"], "uri": [URI]})
        self.assertIn("two approvals", link_reply)
        self.assertIn("permanent", link_reply)
        # What was pinned: the fields, the links that parsed, the LARGEST photo.
        fields, image = desk.pinned[0]
        self.assertEqual(fields, {"name": "Probe Coin", "symbol": "PROBE", "description": "a coin that burns",
                                  "twitter": "https://x.com/probe", "website": "https://probe.xyz"})
        self.assertEqual(desk.downloads, ["big"])
        self.assertEqual(image[1], "image/jpeg")
        # And the draft is gone.
        self.assertEqual(desk.drafts, {})

    def test_the_ticker_is_upper_cased_and_validated(self):
        desk = _Desk()
        desk.handle(_msg("/launch"))
        desk.handle(_msg("Probe"))
        self.assertIn("spaces", desk.handle(_msg("A B"))[0])
        self.assertIn("10 bytes", desk.handle(_msg("ELEVENCHARS"))[0])
        self.assertIn("Ticker: PROBE", desk.handle(_msg("probe"))[0])

    def test_the_name_limit_is_the_chain_s(self):
        desk = _Desk()
        desk.handle(_msg("/launch"))
        self.assertIn("32 bytes", desk.handle(_msg("A" * 33))[0])
        self.assertEqual(desk.drafts[7].step, "name")

    def test_a_document_upload_with_a_wrong_type_is_refused(self):
        desk = _Desk()
        desk.handle(_msg("/launch")); desk.handle(_msg("Probe")); desk.handle(_msg("PRB")); desk.handle(_msg("-"))
        reply = desk.handle(_msg(None, document={"file_id": "doc", "mime_type": "application/pdf"}))[0]
        self.assertIn("not a PNG", reply)
        self.assertEqual(desk.drafts[7].step, "image")

    def test_a_download_failure_is_said_and_the_step_stays(self):
        desk = _Desk()
        desk.handle(_msg("/launch")); desk.handle(_msg("Probe")); desk.handle(_msg("PRB")); desk.handle(_msg("-"))
        reply = desk.handle(_msg(None, photo=[{"file_id": "bad"}]))[0]
        self.assertIn("Could not fetch", reply)
        self.assertEqual(desk.drafts[7].step, "image")

    def test_a_pin_failure_keeps_the_draft_so_the_dev_can_retry(self):
        desk = _Desk(pin_error=RuntimeError("pump down"))
        replies = _walk(desk)
        self.assertIn("Pinning the metadata failed", replies[-1])
        self.assertIn("Nothing was created", replies[-1])
        self.assertEqual(desk.drafts[7].step, "links")

    def test_cancel_forgets_everything(self):
        desk = _Desk()
        desk.handle(_msg("/launch")); desk.handle(_msg("Probe"))
        self.assertIn("Cancelled", desk.handle(_msg("/cancel"))[0])
        self.assertEqual(desk.drafts, {})
        self.assertIn("/launch", desk.handle(_msg("hello"))[0])

    def test_chats_do_not_share_drafts(self):
        desk = _Desk()
        desk.handle(_msg("/launch", chat=1)); desk.handle(_msg("One", chat=1))
        desk.handle(_msg("/launch", chat=2))
        self.assertEqual(desk.drafts[1].name, "One")
        self.assertEqual(desk.drafts[2].step, "name")

    def test_the_link_is_url_encoded(self):
        desk = _Desk()
        link = desk.link("Sp ace & Co", "A/B", "https://ipfs.io/ipfs/x?y=1")
        self.assertIn("name=Sp+ace+%26+Co", link)
        self.assertIn("symbol=A%2FB", link)
        self.assertTrue(link.startswith("https://charlieprotocol.fun/launch?"))


if __name__ == "__main__":
    unittest.main()

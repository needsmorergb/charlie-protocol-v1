"""Offline tests for the X-tag bot (`indexer.tag_bot`). Every outside effect is a fake."""
from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import claim_session, legs
from indexer import tag_bot
from indexer import tag_launch as tag
from indexer import tag_ledger
from indexer.base58 import decode, encode

try:
    from indexer.kv import MemoryKV
except ImportError:   # built alongside; the same methods
    MemoryKV = None

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
TS = tag.epoch(NOW)
SOL = 1_000_000_000
FEE = tag_ledger.TX_FEE_LAMPORTS
BLOCKHASH = "11111111111111111111111111111111"
URI = "https://ipfs.io/ipfs/" + "Q" * 46
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
WALLET = "So11111111111111111111111111111111111111112"
WALLET_2 = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SECRET = "test-only-claim-secret"
CONFIRMED = {"err": None, "confirmationStatus": "confirmed"}


class Crash(BaseException):
    """The process dying mid-step: not caught by the bot's `except Exception`."""


class FakeKV:
    def __init__(self):
        self.data, self.lists = {}, {}

    def get_json(self, key):
        return self.data.get(key)

    def set_json(self, key, value, *, ex=None):
        self.data[key] = value

    def push(self, key, value):
        self.lists.setdefault(key, []).append(value)

    def pop(self, key):
        items = self.lists.get(key) or []
        return items.pop(0) if items else None


def make_kv():
    if MemoryKV is not None:
        try:
            return MemoryKV()
        except TypeError:
            pass
    return FakeKV()


def tweet(**over):
    base = {"id": "100", "edit_history_tweet_ids": ["100"], "author_id": "7",
            "text": "@CharlieSlugSOL launch Moon Dog $MDOG https://t.co/Ab12",
            "created_at": (NOW - timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
            "attachments": {"media_keys": ["3_1"]}}
    base.update(over)
    return base


def user(**over):
    base = {"id": "7", "username": "alice", "created_at": "2024-01-01T00:00:00Z",
            "public_metrics": {"followers_count": 200, "tweet_count": 500},
            "verified_type": "none", "protected": False,
            "profile_image_url": "https://pbs.twimg.com/profile_images/1/a.jpg"}
    base.update(over)
    return base


class FakeX:
    def __init__(self, tweets=(), users=None, media=None, image=("image/png", PNG)):
        self.tweets = list(tweets)
        self.users = users if users is not None else {"7": user()}
        self.media = media if media is not None else {"3_1": {"type": "photo", "url": "https://pbs/x.png"}}
        self.image = image
        self.replies, self.since = [], []

    def mentions(self, user_id, since_id):
        self.since.append(since_id)
        tweets, self.tweets = self.tweets, []
        return {"tweets": tweets, "users": self.users, "media": self.media,
                "newest_id": tweets[-1]["id"] if tweets else None}

    def fetch(self, url, *, limit=5_000_000):
        if isinstance(self.image, Exception):
            raise self.image
        return self.image

    def post_reply(self, tweet_id, text):
        self.replies.append((tweet_id, text))
        return "reply1"


class FakeRpc:
    def __init__(self, balances=None, sim_errors=()):
        self.balances = {legs.CHARLIE_LAUNCH_WALLET: 1 * SOL, legs.CHARLIE_PAYOUT_TREASURY: 100 * SOL,
                         legs.CHARLIE_OPS_DESTINATION: 0}
        self.balances.update(balances or {})
        self.sim_errors = list(sim_errors)
        self.sims = 0
        self.signatures, self.txs = [], {}
        self.existing = set()
        self.statuses = {}
        self.last_valid = 1_000         # what getLatestBlockhash says
        self.height = 900               # the finalized block height
        self.on_height = None           # called when the height is read
        self.account_data = {}          # address -> a full account dict

    def call(self, method, params=None):
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": BLOCKHASH, "lastValidBlockHeight": self.last_valid}}
        if method == "getBlockHeight":
            if self.on_height:
                self.on_height()
            return self.height
        if method == "simulateTransaction":
            self.sims += 1
            err = self.sim_errors.pop(0) if self.sim_errors else None
            return {"value": {"err": err, "logs": [], "unitsConsumed": 1000}}
        if method == "getSignatureStatuses":
            return {"value": [self.statuses.get(s) for s in params[0]]}
        raise AssertionError(f"unexpected RPC {method}")

    def accounts(self, addresses):
        return [self.account_data.get(a) or ({"lamports": 1} if a in self.existing else None)
                for a in addresses]

    def balance(self, address):
        return self.balances.get(address, 0)

    def signatures_for_address(self, address, before=None, until=None, limit=1000):
        sigs = list(self.signatures)
        if until in sigs:
            sigs = sigs[:sigs.index(until)]
        if before in sigs:
            sigs = sigs[sigs.index(before) + 1:]
        return [{"signature": s, "err": None} for s in sigs]

    def transaction(self, signature):
        return self.txs.get(signature)


class Harness:
    def __init__(self, *, x=None, rpc=None, dry_run=False, stop_file=None, now=NOW, db=":memory:"):
        self.x = x or FakeX()
        self.rpc = rpc or FakeRpc()
        self.kv = make_kv()
        self.book = tag.Book(db)
        self.ledger = tag_ledger.Ledger(self.book.db)
        self.sent, self.wires, self.pins, self.lines, self.distributed, self.events = [], [], [], [], [], []
        self.on_transmit = None          # None: lands confirmed; else a callable(wire, signature)
        self.clock = [now]
        self.bot = self.make_bot(dry_run=dry_run, stop_file=stop_file)

    def make_bot(self, *, dry_run=False, stop_file=None):
        return tag_bot.Bot(
            self.x, self.rpc, self.kv, self.book, self.ledger, None, dry_run=dry_run,
            now=lambda: self.clock[0], bot_id="999", signer=self.signer, transmit=self.transmit, pin=self.pin,
            confirm=lambda signature: None, distribute_run=self.distribute_run,
            stop_file=stop_file, claim_secret=SECRET, log=self.lines.append)

    def signer(self, name, message, cosigners=()):
        self.sent.append((name, message, tuple(c.address for c in cosigners)))
        self.events.append(("sign", name))
        signature = hashlib.sha512(f"{len(self.sent)}".encode()).digest()
        return bytes([1]) + signature + message

    def transmit(self, wire):
        signature = encode(wire[1:65])
        self.wires.append(signature)
        if self.on_transmit is not None:
            self.on_transmit(wire, signature)
        else:
            self.rpc.statuses[signature] = CONFIRMED
        return signature

    def pin(self, fields, image):
        self.pins.append((fields, image[1]))
        return {"metadataUri": URI}

    def distribute_run(self, rpc, mints, *, payer, keypair=None, send=None, confirm=None, **_):
        self.distributed.append((tuple(mints), payer, keypair))
        rows = []
        for mint in mints:
            if keypair is None:
                rows.append({"mint": mint, "outcome": "simulated"})
            else:
                signature = send(rpc, keypair, b"distribute-message")
                confirm(rpc, signature)
                rows.append({"mint": mint, "outcome": "sent", "signature": signature})
        return rows

    def outcome(self, key="100"):
        return self.book.db.execute("SELECT outcome, code, ticker, mint FROM tag_requests WHERE tweet_key = ?",
                                    (key,)).fetchone()

    def queue(self, xid="7", wallet=WALLET, at=TS, secret=SECRET):
        self.kv.push("claim:queue", claim_session.sign_claim(xid, "alice", wallet, at, secret))


def lamports_in(message: bytes, destination: str, lamports: int) -> bool:
    return decode(destination) in message and lamports.to_bytes(8, "little") in message


class TestMentions(unittest.TestCase):
    def test_a_good_tag_launches_records_and_replies(self):
        h = Harness(x=FakeX([tweet()]))
        rows = h.bot.tick_mentions()
        self.assertEqual(rows[0]["outcome"], "launched")
        mint = rows[0]["mint"]
        self.assertEqual([(s[0], s[2]) for s in h.sent], [("launch", (mint,)), ("launch", ())])
        self.assertEqual(h.outcome(), ("launched", None, "MDOG", mint))
        self.assertEqual(h.ledger.coin(mint), {"mint": mint, "user_id": "7", "handle": "alice",
                                               "tweet_id": "100", "at": TS})
        self.assertEqual(h.x.replies, [("100", tag.reply_text(tag.TagRequest("Moon Dog", "MDOG"), mint))])
        self.assertEqual(h.pins[0][0]["twitter"], "https://x.com/alice/status/100")
        self.assertEqual(h.pins[0][1], "image/png")
        self.assertEqual(h.ledger.state(tag_bot.SINCE_KEY), "100")
        self.assertEqual(h.ledger.state(tag_bot.UNCONFIRMED_KEY), {})
        h.x.tweets = [tweet()]                          # seen: never twice
        self.assertEqual(h.bot.tick_mentions(), [])
        self.assertEqual(h.x.since[-1], "100")

    def test_each_refusal_is_recorded_and_silent(self):
        cases = {
            "not_original": dict(tweet=tweet(referenced_tweets=[{"type": "replied_to", "id": "1"}])),
            "stale": dict(tweet=tweet(created_at="2026-09-22T11:00:00Z")),
            "no_image": dict(tweet=tweet(attachments={})),
            "too_new": dict(users={"7": user(created_at="2026-09-01T00:00:00Z")}),
            "no_user": dict(users={}),
            "ignored": dict(users={"7": user(username="grok")}),
            "ticker_taken": dict(tweet=tweet(text="@CharlieSlugSOL launch Moon Dog $CHARLIE")),
            "impersonation": dict(tweet=tweet(text="@CharlieSlugSOL launch Elon Musk $EM")),
            "wallet_low": dict(balance=100_000_000),
            "bad_image": dict(image=("text/html", b"<html>")),
            "no_photo": dict(media={"3_1": {"type": "video", "url": "https://v"}}),
            "image_refused": dict(moderate=lambda image: False),
            "unsafe_text": dict(moderate=lambda image: "SAFE."),
        }
        for code, case in cases.items():
            with self.subTest(code=code):
                x = FakeX([case.get("tweet", tweet())], users=case.get("users"), media=case.get("media"),
                          image=case.get("image", ("image/png", PNG)))
                h = Harness(x=x, rpc=FakeRpc({legs.CHARLIE_LAUNCH_WALLET: case.get("balance", SOL)}))
                h.bot.moderate = case.get("moderate")
                h.bot.tick_mentions()
                self.assertEqual((h.sent, h.x.replies, h.pins), ([], [], []))
                expected = {"no_photo": "no_image", "unsafe_text": "image_refused"}.get(code, code)
                outcome = "failed" if code == "wallet_low" else "refused"
                self.assertEqual(h.outcome()[:2], (outcome, expected))

    def test_moderation_sees_the_image_before_anything_is_pinned(self):
        h = Harness(x=FakeX([tweet()]))
        seen = []
        h.bot.moderate = lambda image: seen.append((image[1], image[2], list(h.pins))) or True
        self.assertEqual(h.bot.tick_mentions()[0]["outcome"], "launched")
        self.assertEqual(seen, [("image/png", PNG, [])])

    def test_moderation_unavailable_is_not_the_requester_s_fault(self):
        h = Harness(x=FakeX([tweet()]))

        def down(image):
            raise TimeoutError("timed out")
        h.bot.moderate = down
        row = h.bot.tick_mentions()[0]
        self.assertEqual(row["outcome"], "error")
        self.assertEqual((h.sent, h.x.replies, h.pins), ([], [], []))
        self.assertFalse(h.book.seen("100"))                       # retried next poll
        self.assertIsNone(h.ledger.state(tag_bot.SINCE_KEY))       # cursor held
        self.assertIsNone(h.book.limit_refusal("7", TS))

    def test_a_split_that_keeps_failing_still_uses_the_day_s_launch(self):
        second = tweet(id="200", edit_history_tweet_ids=["200"], text="@CharlieSlugSOL launch Moon Cat $MCAT")
        err = {"InstructionError": [0, "x"]}
        h = Harness(x=FakeX([tweet(), second]), rpc=FakeRpc(sim_errors=[None, err, err]))
        h.bot.tick_mentions()
        self.assertEqual(h.outcome()[:2], ("failed", "split_pending"))
        self.assertEqual(h.outcome("200")[:2], ("refused", "daily_limit"))
        self.assertEqual(len([s for s in h.sent if s[2]]), 1)           # one create only

    def test_a_crash_after_the_create_lands_still_ends_with_the_split(self):
        h = Harness(x=FakeX([tweet()]))
        real = h.book.record

        def crash_at_split_pending(key, user_id, now, outcome, **kw):
            if kw.get("code") == "split_pending":
                raise Crash()
            return real(key, user_id, now, outcome, **kw)
        h.book.record = crash_at_split_pending
        with self.assertRaises(Crash):
            h.bot.tick_mentions()
        mint = h.outcome()[3]
        self.assertEqual(h.outcome()[0], "launching")
        self.assertIn(mint, h.ledger.state(tag_bot.UNCONFIRMED_KEY))   # kept until split_pending is written
        h.book.record = real
        h.rpc.existing.add(mint)
        h.x.tweets = []
        h.bot = h.make_bot()
        rows = h.bot.tick_mentions()
        self.assertEqual([(r["outcome"], r["mint"]) for r in rows], [("launched", mint)])
        self.assertEqual([s[2] for s in h.sent], [(mint,), ()])          # one create, one split

    def test_an_orphan_launching_row_is_recovered(self):
        h = Harness()
        known, on_chain = encode(bytes([5] * 32)), encode(bytes([6] * 32))
        h.book.record("100", "7", TS, "launching", ticker="MDOG", mint=known)
        h.ledger.add_coin(known, "7", "alice", "100", TS)
        h.book.record("101", "8", TS, "launching", ticker="ABC", mint=on_chain)
        h.rpc.existing.add(on_chain)
        rows = h.bot.tick_mentions()
        self.assertEqual(sorted(r["mint"] for r in rows if r["outcome"] == "launched"), sorted([known, on_chain]))
        self.assertEqual(h.ledger.coin(on_chain)["user_id"], "8")
        self.assertEqual(h.book.launching(), [])

    def test_limits_are_refusals_too(self):
        h = Harness(x=FakeX([tweet(id="200", edit_history_tweet_ids=["200"])]))
        h.book.record("100", "7", TS - 3600, "launched", ticker="ABC", mint="m")
        h.bot.tick_mentions()
        self.assertEqual(h.outcome("200")[:2], ("refused", "daily_limit"))
        self.assertEqual(h.sent, [])

    def test_a_ticker_already_launched_is_taken(self):
        h = Harness(x=FakeX([tweet()]))
        h.book.record("50", "8", TS - 40 * 86_400, "launched", ticker="MDOG", mint="m")
        h.bot.tick_mentions()
        self.assertEqual(h.outcome()[:2], ("refused", "ticker_taken"))

    def test_text_that_is_not_a_tag_is_ignored_and_not_recorded(self):
        h = Harness(x=FakeX([tweet(text="@CharlieSlugSOL gm")]))
        self.assertEqual(h.bot.tick_mentions(), [])
        self.assertIsNone(h.outcome())

    def test_a_failed_split_is_pending_and_retried_next_tick(self):
        h = Harness(x=FakeX([tweet()]), rpc=FakeRpc(sim_errors=[None, {"InstructionError": [0, "x"]}]))
        rows = h.bot.tick_mentions()
        mint = rows[0]["mint"]
        self.assertEqual(h.outcome(), ("failed", "split_pending", "MDOG", mint))
        self.assertEqual(len(h.sent), 1)
        self.assertEqual(h.x.replies, [])
        self.assertIsNotNone(h.ledger.coin(mint))
        self.assertIn("MDOG", h.book.taken_tickers())
        rows = h.bot.tick_mentions()
        self.assertEqual(rows[0]["outcome"], "launched")
        self.assertEqual(h.outcome(), ("launched", None, "MDOG", mint))
        self.assertEqual(len(h.sent), 2)
        self.assertEqual(h.x.replies[0][0], "100")
        self.assertEqual(h.book.pending_splits(), [])

    def _unconfirmed_create(self):
        h = Harness(x=FakeX([tweet()]))

        def failing(wire, signature):
            raise RuntimeError("confirm timed out")
        h.on_transmit = failing
        mint = h.bot.tick_mentions()[0]["mint"]
        self.assertEqual(h.outcome(), ("failed", "create_failed", "MDOG", mint))
        self.assertIsNone(h.ledger.coin(mint))
        h.on_transmit = None
        return h, mint

    def test_an_unconfirmed_create_that_landed_gets_its_split(self):
        h, mint = self._unconfirmed_create()
        h.rpc.existing.add(mint)
        rows = h.bot.tick_mentions()
        self.assertEqual(rows[0]["outcome"], "launched")
        self.assertEqual(h.outcome(), ("launched", None, "MDOG", mint))
        self.assertEqual(h.ledger.coin(mint)["handle"], "alice")
        self.assertEqual(h.x.replies[0][0], "100")
        self.assertEqual(h.ledger.state(tag_bot.UNCONFIRMED_KEY), {})

    def test_an_unconfirmed_create_that_never_appears_is_finally_failed(self):
        h, mint = self._unconfirmed_create()
        h.clock[0] = NOW + timedelta(seconds=599)
        h.bot.tick_mentions()
        self.assertEqual(h.outcome()[:2], ("failed", "create_failed"))
        h.clock[0] = NOW + timedelta(seconds=600)
        h.bot.tick_mentions()
        self.assertEqual(h.outcome(), ("failed", "create_lost", "MDOG", mint))
        self.assertIsNone(h.ledger.coin(mint))
        self.assertEqual(h.ledger.state(tag_bot.UNCONFIRMED_KEY), {})
        self.assertIsNone(h.book.limit_refusal("7", tag.epoch(h.clock[0])))

    def test_a_stale_unconfirmed_entry_never_overwrites_a_newer_row(self):
        h = Harness()
        pending = {"key": "100", "user_id": "7", "handle": "alice", "tweet_id": "100", "at": TS, "ticker": "MDOG"}
        h.ledger.set_state(tag_bot.UNCONFIRMED_KEY, {"oldMint": dict(pending), "neverSent": dict(pending, key="300")})
        h.book.record("100", "7", TS, "launched", ticker="MDOG", mint="newMint")    # the relaunch won
        h.rpc.existing.add("oldMint")
        h.bot.tick_mentions()
        self.assertEqual(h.outcome("100"), ("launched", None, "MDOG", "newMint"))
        self.assertIsNone(h.outcome("300"))                     # the crash came before `launching`
        self.assertIsNone(h.ledger.coin("oldMint"))
        self.assertEqual(h.ledger.state(tag_bot.UNCONFIRMED_KEY), {})

    def test_the_tweet_is_launching_before_the_create_is_sent(self):
        h = Harness(x=FakeX([tweet()]))
        seen = []

        def check(wire, signature):
            if not seen:
                seen.append((h.outcome()[:2], h.book.seen("100")))
            h.rpc.statuses[signature] = CONFIRMED
        h.on_transmit = check
        h.bot.tick_mentions()
        self.assertEqual(seen, [(("launching", None), True)])

    def test_a_crash_mid_create_never_launches_the_tweet_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "book.db")
            second = tweet(id="101", edit_history_tweet_ids=["101"], author_id="8",
                           text="@CharlieSlugSOL launch Moon Cat $MCAT")
            x = FakeX([tweet(), second], users={"7": user(), "8": user(id="8", username="bob")})
            h = Harness(x=x, db=db)

            def crash_on_second(wire, signature):
                if len(h.wires) == 3:          # create 1, split 1, create 2
                    raise Crash()
                h.rpc.statuses[signature] = CONFIRMED
            h.on_transmit = crash_on_second
            with self.assertRaises(Crash):
                h.bot.tick_mentions()
            self.assertEqual(h.ledger.state(tag_bot.SINCE_KEY), "100")     # saved per tweet
            self.assertEqual(h.outcome("101")[0], "launching")
            mint = h.outcome("101")[3]
            h.book.db.close()

            # the restart: the same database, the tweet redelivered, the coin on chain
            h2 = Harness(x=FakeX([second], users={"8": user(id="8", username="bob")}), db=db)
            h2.rpc.existing.add(mint)
            rows = h2.bot.tick_mentions()
            self.assertEqual([(r["outcome"], r["mint"]) for r in rows], [("launched", mint)])
            self.assertEqual(h2.outcome("101"), ("launched", None, "MCAT", mint))
            self.assertEqual([s[2] for s in h2.sent], [()])                # only the split; no second create
            self.assertEqual(h2.ledger.coin(mint)["handle"], "bob")
            h2.book.db.close()

    def test_a_failed_create_simulation_sends_nothing(self):
        h = Harness(x=FakeX([tweet()]), rpc=FakeRpc(sim_errors=[{"InstructionError": [0, "x"]}]))
        h.bot.tick_mentions()
        self.assertEqual(h.outcome()[:2], ("failed", "create_simulation"))
        self.assertEqual((h.sent, h.x.replies), ([], []))

    def test_dry_run_signs_sends_posts_pins_and_records_nothing(self):
        h = Harness(x=FakeX([tweet()]), dry_run=True)
        h.bot.signer = lambda *a: self.fail("dry run signed")
        h.bot.transmit = lambda *a: self.fail("dry run sent")
        h.x.post_reply = lambda *a: self.fail("dry run posted")
        rows = h.bot.tick_mentions()
        self.assertEqual(rows[0]["outcome"], "simulated")
        self.assertEqual(h.rpc.sims, 1)
        self.assertEqual((h.pins, h.outcome()), ([], None))
        self.assertTrue(any(line.startswith("would_launch") for line in h.lines))

    def test_stop_pauses_launches_but_not_money(self):
        with tempfile.TemporaryDirectory() as tmp:
            stop = Path(tmp) / "STOP"
            stop.write_text("")
            h = Harness(x=FakeX([tweet()]), stop_file=stop)
            h.ledger.add_coin("mintA", "7", "alice", "1", TS)
            self.assertEqual(h.bot.tick_mentions(), [])
            self.assertEqual((h.x.since, h.sent), ([], []))
            h.bot.tick_money()
            self.assertEqual(h.distributed, [(("mintA",), legs.CHARLIE_LAUNCH_WALLET, "launch")])
            stop.unlink()
            self.assertEqual(h.bot.tick_mentions()[0]["outcome"], "launched")


class TestMoney(unittest.TestCase):
    @staticmethod
    def paid(h, name="treasury"):
        """What the bot signed with one key."""
        return [s for s in h.sent if s[0] == name]

    def credit(self, h, lamports, at=TS, signature="c1", user_id="7"):
        h.ledger.add_coin(f"mint{user_id}", user_id, "alice", "1", at)
        h.ledger.credit(signature, f"mint{user_id}", lamports, at)

    def test_distribute_pays_fees_from_the_launch_wallet_through_the_signer(self):
        h = Harness()
        h.ledger.add_coin("mintA", "7", "alice", "1", TS)
        h.bot.tick_money()
        self.assertEqual(h.distributed, [(("mintA",), legs.CHARLIE_LAUNCH_WALLET, "launch")])
        self.assertEqual(h.sent[0][:2], ("launch", b"distribute-message"))

    def test_the_scan_attributes_treasury_inflows_to_the_requester(self):
        from test_tag_ledger import distribute_tx, MINT
        h = Harness()
        h.ledger.add_coin(MINT, "7", "alice", "1", TS)
        h.rpc.signatures = ["s1"]
        h.rpc.txs = {"s1": distribute_tx(treasury_gain=7_000_000)}
        h.bot.tick_money()
        self.assertEqual(h.ledger.balance("7"), 7_000_000)
        self.assertEqual(h.kv.get_json("claim:credit:7")["claimable"], 7_000_000)

    def test_a_claim_pays_the_balance_less_the_fee_and_writes_the_status(self):
        h = Harness()
        self.credit(h, 2 * SOL)
        h.queue()
        h.bot.tick_money()
        _, message, _ = self.paid(h)[0]
        self.assertTrue(lamports_in(message, WALLET, 2 * SOL - FEE))
        status = h.kv.get_json("claim:status:7")
        self.assertEqual((status["state"], status["lamports"], status["signature"]), ("paid", 2 * SOL - FEE, h.wires[-1]))
        self.assertEqual(h.ledger.balance("7"), 0)
        self.assertEqual(h.ledger.pending(), {})
        self.assertEqual(h.ledger.wallet("7")[0], WALLET)
        self.assertEqual(h.kv.get_json("claim:credit:7")["wallet"], WALLET)

    def test_an_identical_payment_in_flight_is_not_a_second_payment(self):
        h = Harness()
        self.credit(h, 2 * SOL)
        h.ledger.debit_pending("claim", [("8", 1)], wallet=WALLET, signature="same", at=0, last_valid=10**9)
        with mock.patch.object(tag_bot, "wire_signature", return_value="same"):
            status = h.bot.claim("7", WALLET)
        status = status[0] if isinstance(status, tuple) else status
        self.assertEqual(status["state"], "refused")
        self.assertIn("identical payment", status["reason"])
        self.assertEqual(h.wires, [])
        self.assertEqual(h.ledger.pending()["same"]["rows"], [("8", 1, WALLET)])

    def test_a_claim_that_fails_midway_is_requeued_and_nothing_burns(self):
        h = Harness()
        self.credit(h, 2 * SOL)
        h.queue()
        with mock.patch.object(tag_bot.Bot, "claim", side_effect=RuntimeError("rpc down")),                 mock.patch.object(tag_bot.Bot, "_burn") as burn:
            out = h.bot.tick_money()
        self.assertIn("error", out["claims"])
        self.assertIn("skipped", out["burn"])
        burn.assert_not_called()
        self.assertIsNotNone(h.kv.pop(tag_bot.QUEUE_KEY))

    def test_the_debit_is_pending_before_the_send(self):
        h = Harness()
        self.credit(h, 2 * SOL)
        seen = []

        def check(wire, signature):
            seen.append((h.ledger.has_pending("7"), list(h.ledger.pending()), h.ledger.balance("7")))
            h.rpc.statuses[signature] = CONFIRMED
        h.on_transmit = check
        h.bot.claim("7", WALLET)
        self.assertEqual(seen, [(True, [h.wires[0]], 0)])

    def test_an_ambiguous_send_is_never_paid_twice_and_expires_back_to_credit(self):
        h = Harness()
        self.credit(h, 2 * SOL)

        def lost(wire, signature):
            raise RuntimeError("connection reset")
        h.on_transmit = lost
        self.assertEqual(h.bot.claim("7", WALLET)["state"], "queued")
        self.assertEqual(h.ledger.balance("7"), 0)              # the pending debit stays
        h.on_transmit = None
        self.assertEqual(h.bot.claim("7", WALLET)["state"], "queued")   # no second payment
        self.assertEqual(len(self.paid(h)), 1)
        h.clock[0] = NOW + timedelta(hours=1)
        h.bot.reconcile()
        self.assertEqual(h.ledger.balance("7"), 0)              # unknown for an hour: still never dropped
        h.rpc.height = 1_000
        h.bot.reconcile()
        self.assertEqual(h.ledger.balance("7"), 0)              # at last_valid it may still land
        h.rpc.height = 1_001
        h.bot.reconcile()
        self.assertEqual(h.ledger.balance("7"), 2 * SOL)
        self.assertEqual(h.kv.get_json("claim:status:7")["reason"], tag_bot.PAYMENT_FAILED)
        self.assertEqual(h.bot.claim("7", WALLET)["state"], "paid")

    def test_a_status_seen_on_the_second_read_is_never_dropped(self):
        h = Harness()
        self.credit(h, 2 * SOL)
        h.on_transmit = lambda wire, signature: None             # the node has not seen it yet
        h.bot.claim("7", WALLET)
        (signature,) = h.ledger.pending()
        h.rpc.height = 5_000

        def lands_meanwhile():
            h.rpc.statuses[signature] = CONFIRMED
        h.rpc.on_height = lands_meanwhile
        h.bot.reconcile()
        self.assertEqual((h.ledger.balance("7"), h.ledger.pending()), (0, {}))
        self.assertEqual(h.kv.get_json("claim:status:7")["state"], "paid")

    def test_a_pending_debit_without_a_height_is_never_dropped(self):
        h = Harness()
        self.credit(h, 2 * SOL)
        h.rpc.last_valid = None
        h.on_transmit = lambda wire, signature: None
        h.bot.claim("7", WALLET)
        h.rpc.height = 10 ** 9
        h.bot.reconcile()
        self.assertEqual(h.ledger.balance("7"), 0)
        self.assertEqual(len(h.ledger.pending()), 1)

    def test_an_unseen_burn_is_dropped_only_past_its_height(self):
        h = Harness()
        self.credit(h, 30_000_000, at=TS - 8 * 86_400)
        h.on_transmit = lambda wire, signature: None
        h.bot.tick_money()
        h.clock[0] = NOW + timedelta(days=1)
        h.bot.reconcile()
        self.assertTrue(h.ledger.pending_burn())
        h.rpc.height = 1_001
        h.bot.reconcile()
        self.assertFalse(h.ledger.pending_burn())
        self.assertIsNone(h.ledger.state(tag_bot.LAST_BURN_KEY))
        self.assertEqual(h.ledger.balance("7"), 30_000_000)

    def test_an_ambiguous_send_that_did_land_is_final(self):
        h = Harness()
        self.credit(h, 2 * SOL)

        def landed_but_raised(wire, signature):
            h.rpc.statuses[signature] = CONFIRMED
            raise RuntimeError("timeout after forwarding")
        h.on_transmit = landed_but_raised
        self.assertEqual(h.bot.claim("7", WALLET)["state"], "paid")
        self.assertEqual((h.ledger.balance("7"), h.ledger.pending()), (0, {}))

    def test_a_failed_payment_restores_the_credit(self):
        h = Harness()
        self.credit(h, 2 * SOL)
        h.on_transmit = lambda wire, signature: h.rpc.statuses.__setitem__(
            signature, {"err": {"InstructionError": [0, "x"]}, "confirmationStatus": "confirmed"})
        status = h.bot.claim("7", WALLET)
        self.assertEqual((status["state"], status["reason"]), ("refused", tag_bot.PAYMENT_FAILED))
        self.assertEqual(h.ledger.balance("7"), 2 * SOL)

    def test_unsigned_altered_or_stale_queue_items_are_dropped(self):
        h = Harness()
        self.credit(h, 2 * SOL)
        h.kv.push("claim:queue", {"xid": "7", "handle": "alice", "wallet": WALLET, "at": TS})
        h.queue(secret="another-secret")
        forged = claim_session.sign_claim("7", "alice", WALLET, TS, SECRET)
        forged["wallet"] = WALLET_2          # the plain field is ignored; the signed one pays
        h.kv.push("claim:queue", dict(forged, sig=forged["sig"][:-2] + "AA"))
        h.queue(at=TS - 7 * 86_400 - 1)
        h.bot.tick_money()
        self.assertEqual(self.paid(h), [])
        self.assertEqual(sum(1 for line in h.lines if line.startswith("claim_dropped")), 4)

    def test_a_signed_item_days_old_still_pays(self):
        h = Harness()
        self.credit(h, 2 * SOL)
        h.queue(at=TS - 6 * 86_400)
        h.bot.tick_money()
        self.assertEqual(h.kv.get_json("claim:status:7")["state"], "paid")

    def test_a_claim_over_the_cap_is_held_approved_and_paid_by_the_loop(self):
        h = Harness()
        self.credit(h, 6 * SOL, at=TS - 8 * 86_400)
        h.queue()
        h.bot.tick_money()
        self.assertEqual(h.kv.get_json("claim:status:7")["state"], "held")
        self.assertEqual(self.paid(h), [])                      # nor burnt, though a week old
        self.assertEqual(h.ledger.balance("7"), 6 * SOL)
        h.bot.tick_money()
        self.assertEqual(self.paid(h), [])                      # held stays held
        self.assertEqual(h.ledger.approve_held("7"), 1)
        h.bot.tick_money()
        self.assertTrue(lamports_in(self.paid(h)[0][1], WALLET, 6 * SOL - FEE))
        self.assertEqual(h.kv.get_json("claim:status:7")["state"], "paid")
        self.assertEqual(h.ledger.approved(), [])

    def test_an_approved_hold_pays_at_most_what_was_held(self):
        h = Harness()
        self.credit(h, 6 * SOL)
        h.queue()
        h.bot.tick_money()
        self.credit(h, 3 * SOL, signature="c2")                 # more arrives after the approval was asked
        h.ledger.approve_held("7")
        h.bot.tick_money()
        self.assertEqual(len(self.paid(h)), 1)
        self.assertTrue(lamports_in(self.paid(h)[0][1], WALLET, 6 * SOL - FEE))
        self.assertEqual(h.ledger.balance("7"), 3 * SOL)

    def test_an_approved_hold_closes_only_once_its_payment_is_final(self):
        h = Harness()
        self.credit(h, 6 * SOL, at=TS - 8 * 86_400)
        h.queue()
        h.bot.tick_money()
        h.ledger.approve_held("7")

        def lost(wire, signature):
            raise RuntimeError("connection reset")
        h.on_transmit = lost
        h.bot.tick_money()
        self.assertEqual(len(self.paid(h)), 1)
        held = "SELECT state, signature IS NOT NULL FROM tag_held"
        self.assertEqual(h.ledger.db.execute(held).fetchall(), [("approved", 1)])
        h.rpc.height = 1_001                                    # it can never land now
        h.on_transmit = None
        h.bot.tick_money()                                      # dropped, then paid again under the approval
        self.assertEqual(len(self.paid(h)), 2)                  # no burn of the week-old held credit
        self.assertEqual(h.ledger.db.execute(held).fetchall(), [("paid", 1)])
        self.assertEqual(h.ledger.balance("7"), 0)

    def test_a_new_wallet_waits_before_it_is_paid(self):
        h = Harness()
        self.credit(h, 2 * SOL)
        h.ledger.apply_wallet("7", tag_ledger.Decision("pay", 0, bind=WALLET))
        self.assertEqual(h.bot.claim("7", WALLET_2)["state"], "refused")
        self.assertEqual(h.ledger.wallet("7"), (WALLET, WALLET_2, TS + 72 * 3600))
        h.clock[0] = NOW + timedelta(hours=72)
        self.assertEqual(h.bot.claim("7", WALLET_2)["state"], "paid")
        self.assertEqual(h.ledger.wallet("7"), (WALLET_2, None, None))

    def test_a_claim_to_the_bound_wallet_cancels_a_pending_change(self):
        h = Harness()
        self.credit(h, 2 * SOL)
        h.ledger.apply_wallet("7", tag_ledger.Decision("pay", 0, bind=WALLET))
        h.bot.claim("7", WALLET_2)
        self.assertEqual(h.bot.claim("7", WALLET)["state"], "paid")
        self.assertEqual(h.ledger.wallet("7"), (WALLET, None, None))

    def test_a_charlie_wallet_or_junk_is_refused(self):
        h = Harness()
        self.credit(h, 2 * SOL)
        for wallet in (legs.CHARLIE_PAYOUT_TREASURY, "not-a-wallet"):
            self.assertEqual(h.bot.claim("7", wallet)["state"], "refused")
        self.assertEqual(h.sent, [])

    def test_week_old_credit_burns_once_a_day_less_the_fee(self):
        h = Harness()
        self.credit(h, 30_000_000, at=TS - 8 * 86_400)
        self.credit(h, 5_000_000, at=TS - 86_400, signature="c2")
        h.bot.tick_money()
        burns = self.paid(h)
        self.assertEqual(len(burns), 1)
        self.assertTrue(lamports_in(burns[0][1], legs.TOLL_DESTINATION, 30_000_000 - FEE))
        self.assertEqual(h.ledger.balance("7"), 5_000_000)
        self.assertEqual(h.ledger.state(tag_bot.LAST_BURN_KEY), TS)
        h.clock[0] = NOW + timedelta(hours=23)
        self.credit(h, 30_000_000, at=TS - 8 * 86_400, signature="c3")
        h.bot.tick_money()
        self.assertEqual(len(self.paid(h)), 1)

    def test_a_burn_stamps_its_day_only_once_final(self):
        h = Harness()
        self.credit(h, 30_000_000, at=TS - 8 * 86_400)
        h.on_transmit = lambda wire, signature: None            # sent, not yet seen
        h.bot.tick_money()
        self.assertIsNone(h.ledger.state(tag_bot.LAST_BURN_KEY))
        self.assertTrue(h.ledger.pending_burn())
        h.bot.tick_money()
        self.assertEqual(len(self.paid(h)), 1)                  # never a second burn while one is pending
        (burn_signature,) = h.ledger.pending()
        h.rpc.statuses[burn_signature] = CONFIRMED
        h.clock[0] = NOW + timedelta(minutes=30)
        h.bot.tick_money()
        self.assertEqual(h.ledger.state(tag_bot.LAST_BURN_KEY), TS + 1800)
        self.assertEqual(h.ledger.balance("7"), 0)

    def test_credit_is_dated_when_scanned_so_it_shows_a_week_before_burning(self):
        from test_tag_ledger import distribute_tx, MINT
        h = Harness()
        h.ledger.add_coin(MINT, "7", "alice", "1", TS - 30 * 86_400)
        h.rpc.signatures = ["s1"]
        old = distribute_tx(treasury_gain=30_000_000)
        old["blockTime"] = TS - 30 * 86_400                    # landed long ago, seen only now
        h.rpc.txs = {"s1": old}
        h.bot.tick_money()
        self.assertEqual(self.paid(h), [])
        self.assertEqual(h.kv.get_json("claim:credit:7")["burns_at"], TS + 7 * 86_400)

    def test_the_site_sees_the_credit_before_anything_burns(self):
        h = Harness()
        self.credit(h, 30_000_000, at=TS - 8 * 86_400)
        real_set = h.kv.set_json

        def recording(key, value, **kw):
            h.events.append(("kv", key))
            return real_set(key, value, **kw)
        h.kv.set_json = recording
        h.bot.tick_money()
        order = [e for e in h.events if e in (("kv", "claim:credit:7"), ("sign", "treasury"))]
        self.assertEqual(order[:2], [("kv", "claim:credit:7"), ("sign", "treasury")])

    def test_burn_waits_below_the_minimum(self):
        h = Harness()
        self.credit(h, 9_999_999, at=TS - 8 * 86_400)
        h.bot.tick_money()
        self.assertEqual(self.paid(h), [])

    def test_ops_fills_a_low_launch_wallet_up_to_half_a_sol(self):
        reserve = tag_ledger.TREASURY_RESERVE_LAMPORTS
        for launch_balance, ops, amount in ((100_000_000, 2 * SOL, 400_000_000),
                                            (100_000_000, 300_000_000, 300_000_000 - reserve),
                                            (100_000_000, 5_000_000, None),
                                            (295_000_000, 2 * SOL, 205_000_000),
                                            (300_000_000, 2 * SOL, None)):
            with self.subTest(launch=launch_balance, ops=ops):
                h = Harness(rpc=FakeRpc({legs.CHARLIE_LAUNCH_WALLET: launch_balance,
                                         legs.CHARLIE_OPS_DESTINATION: ops}))
                h.bot.tick_money()
                sweeps = self.paid(h, "ops")
                if amount is None:
                    self.assertEqual(sweeps, [])
                else:
                    self.assertEqual(len(sweeps), 1)
                    self.assertTrue(lamports_in(sweeps[0][1], legs.CHARLIE_LAUNCH_WALLET, amount))

    def test_dry_run_money_signs_nothing_and_leaves_the_queue(self):
        h = Harness(dry_run=True, rpc=FakeRpc({legs.CHARLIE_LAUNCH_WALLET: 100_000_000,
                                               legs.CHARLIE_OPS_DESTINATION: 2 * SOL}))
        h.bot.signer = lambda *a: self.fail("dry run signed")
        self.credit(h, 30_000_000, at=TS - 8 * 86_400)
        h.queue()
        out = h.bot.tick_money()
        self.assertEqual(out["burn"]["state"], "simulated")
        self.assertEqual(out["sweep"]["state"], "simulated")
        self.assertIsNotNone(h.kv.pop("claim:queue"))
        self.assertIsNone(h.kv.get_json("claim:credit:7"))
        self.assertEqual(h.distributed[0][2], None)

    def test_one_failing_step_does_not_stop_the_rest(self):
        h = Harness(rpc=FakeRpc({legs.CHARLIE_LAUNCH_WALLET: 100_000_000, legs.CHARLIE_OPS_DESTINATION: 2 * SOL}))
        h.ledger.add_coin("mintA", "7", "alice", "1", TS)
        h.bot.distribute_run = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        out = h.bot.tick_money()
        self.assertIn("error", out["distribute"])
        self.assertEqual(out["sweep"]["state"], "sent")


class TestLiveSigner(unittest.TestCase):
    def test_the_key_signer_signs_in_the_message_s_order_with_cosigners(self):
        from indexer.ed25519 import Keypair, verify
        launcher = Keypair.from_seed(bytes(range(32)))
        built = tag.build(tag.TagRequest("Moon Dog", "MDOG"), URI, BLOCKHASH, launcher=launcher.address)
        wire = tag_bot.key_signer({"launch": launcher})("launch", built.create, (built.mint,))
        self.assertEqual(wire[0], 2)
        signers = tag.launch.signer_addresses(built.create)
        for i, address in enumerate(signers):
            self.assertTrue(verify(decode(address), built.create, wire[1 + 64 * i: 65 + 64 * i]))
        self.assertEqual(tag_bot.wire_signature(wire), encode(wire[1:65]))
        with self.assertRaises(tag.launch.LaunchError):
            tag_bot.sign_with(built.create, (launcher,))


class TestXBotCli(unittest.TestCase):
    def test_a_key_that_is_not_its_legs_wallet_is_refused(self):
        from tools import x_bot

        class Key:
            def __init__(self, address):
                self.address = address

        good = {"launch": legs.CHARLIE_LAUNCH_WALLET, "treasury": legs.CHARLIE_PAYOUT_TREASURY,
                "ops": legs.CHARLIE_OPS_DESTINATION}
        keys = x_bot.load_keys({n: n for n in good}, reader=lambda path: Key(good[path]))
        self.assertEqual(set(keys), set(good))
        with self.assertRaises(x_bot.ConfigError):
            x_bot.load_keys({n: n for n in good}, reader=lambda path: Key(WALLET))
        with self.assertRaises(x_bot.ConfigError):
            x_bot.load_keys({"launch": "launch"}, reader=lambda path: Key(good[path]))

    def test_the_config_may_not_live_in_the_repo(self):
        from tools import x_bot
        with self.assertRaises(x_bot.ConfigError):
            x_bot.load_config(x_bot.REPO / "tools" / "x_bot.example.json")

    def test_a_dry_run_gets_its_own_database(self):
        from tools import x_bot
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"bot_user_id": "1"}', encoding="utf-8")
            config = x_bot.load_config(path)
            self.assertEqual(Path(config["db_dry"]).name, "book-dry.db")
            self.assertNotEqual(config["db"], config["db_dry"])
            same = str(Path(tmp) / "one.db").replace("\\", "/")
            path.write_text('{"bot_user_id": "1", "db": "%s", "db_dry": "%s"}' % (same, same), encoding="utf-8")
            with self.assertRaises(x_bot.ConfigError):
                x_bot.load_config(path)

    def test_a_live_bot_needs_the_claim_secret(self):
        import contextlib
        import io
        from tools import x_bot
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"bot_user_id": "1", "claim_secret": ""}', encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(x_bot.main(["--config", str(path), "--once"]), 2)
            self.assertIn("claim_secret is empty", err.getvalue())
            self.assertFalse((Path(tmp) / "book.db.lock").exists())

    def test_moderation_config(self):
        from tools import x_bot
        self.assertIsNone(x_bot.moderator_for({"moderation": "off"}, dry_run=False))
        self.assertTrue(callable(x_bot.moderator_for({"moderation": {"anthropic_api_key": "k"}}, dry_run=False)))
        self.assertIsNone(x_bot.moderator_for({}, dry_run=True))
        for setting in (None, "", "OFF", {}, {"anthropic_api_key": ""}):
            with self.assertRaises(x_bot.ConfigError, msg=repr(setting)):
                x_bot.moderator_for({"moderation": setting}, dry_run=False)

    def test_a_live_bot_needs_moderation_or_an_explicit_off(self):
        import contextlib
        import io
        from tools import x_bot
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"bot_user_id": "1", "claim_secret": "s"}', encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(x_bot.main(["--config", str(path), "--once"]), 2)
            self.assertIn("anthropic_api_key", err.getvalue())
            self.assertFalse((Path(tmp) / "book.db.lock").exists())

    def test_a_second_bot_cannot_take_the_lock(self):
        from tools import x_bot
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "book.db")
            first = x_bot.acquire_lock(db)
            try:
                with self.assertRaises(x_bot.Locked):
                    x_bot.acquire_lock(db)
            finally:
                first.close()
            x_bot.acquire_lock(db).close()          # free again once the first is gone

    def test_approve_held_only_marks_the_row(self):
        from tools import x_bot
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "book.db")
            book = tag.Book(db)
            tag_ledger.Ledger(book.db).hold("7", 6 * SOL, WALLET, TS)
            book.db.close()
            self.assertEqual(x_bot.approve_held(db, "7"), 1)
            self.assertEqual(x_bot.approve_held(db, "7"), 0)
            book = tag.Book(db)
            self.assertEqual(tag_ledger.Ledger(book.db).approved(), [("7", WALLET, 6 * SOL)])
            book.db.close()

    def test_once_runs_each_tick_one_time(self):
        from tools import x_bot
        calls = []

        class Bot:
            def tick_mentions(self):
                calls.append("mentions")

            def tick_money(self):
                calls.append("money")

            def log(self, *a, **k):
                calls.append("log")

        x_bot.run(Bot(), once=True, clock=lambda: 0.0, sleep=lambda s: self.fail("slept"))
        self.assertEqual(calls, ["mentions", "money"])


class TestImage(unittest.TestCase):
    def test_the_bytes_decide_the_type(self):
        self.assertEqual(tag_bot.sniff(PNG), "image/png")
        self.assertEqual(tag_bot.sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "image/webp")
        self.assertIsNone(tag_bot.sniff(b"<svg"))

    def test_too_big_is_bad(self):
        x = FakeX(image=("image/png", PNG + b"\x00" * tag_bot.MAX_IMAGE_BYTES))
        self.assertIsNone(tag_bot.image_of(x, tweet(), x.media))

    def test_a_gone_image_is_bad_but_a_network_failure_is_retried(self):
        from indexer.xapi import XError
        x = FakeX(image=XError("X answered HTTP 404: gone", 404))
        self.assertIsNone(tag_bot.image_of(x, tweet(), x.media))
        for exc in (XError("X did not answer: timed out", 0), XError("X answered HTTP 503", 503),
                    XError("X answered HTTP 429", 429)):
            x = FakeX(image=exc)
            with self.assertRaises(XError):
                tag_bot.image_of(x, tweet(), x.media)



class TestModeration(unittest.TestCase):
    IMAGE = ("image.png", "image/png", PNG)

    def answer(self, text):
        return {"type": "message", "content": [{"type": "text", "text": text}]}

    def test_only_exactly_safe_passes(self):
        from indexer import moderation
        self.assertTrue(moderation.verdict(self.answer("SAFE")))
        self.assertTrue(moderation.verdict(self.answer(" SAFE\n")))
        for text in ("UNSAFE", "safe", "SAFE.", "Probably SAFE", ""):
            self.assertFalse(moderation.verdict(self.answer(text)), text)
        with self.assertRaises(moderation.ModerationUnavailable):
            moderation.verdict({"type": "error", "error": {"type": "overloaded_error"}})

    def test_the_request(self):
        import base64
        import json as jsonlib
        from indexer import moderation
        sent = []

        class Response:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return self.body

        def opener(request, timeout):
            sent.append((request, timeout))
            return Response(jsonlib.dumps(self.answer("UNSAFE")).encode())
        self.assertFalse(moderation.anthropic_moderator("key-1", opener=opener)(self.IMAGE))
        request, timeout = sent[0]
        self.assertEqual(request.full_url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(request.get_header("X-api-key"), "key-1")
        self.assertEqual(request.get_header("Anthropic-version"), "2023-06-01")
        body = jsonlib.loads(request.data)
        self.assertEqual(body["model"], "claude-haiku-4-5-20251001")
        image, text = body["messages"][0]["content"]
        self.assertEqual(image["source"], {"type": "base64", "media_type": "image/png",
                                           "data": base64.b64encode(PNG).decode()})
        for word in ("sexual", "minor", "gore", "hate symbols", "real, identifiable person", "brand logo",
                     "SAFE", "UNSAFE"):
            self.assertIn(word, text["text"])
        self.assertEqual(timeout, moderation.TIMEOUT_SECONDS)

    def test_errors_and_timeouts_are_unavailable(self):
        import socket
        import urllib.error
        from indexer import moderation
        for exc in (urllib.error.HTTPError("u", 529, "overloaded", {}, None), socket.timeout("t"),
                    urllib.error.URLError("down")):
            def opener(request, timeout, exc=exc):
                raise exc
            with self.assertRaises(moderation.ModerationUnavailable):
                moderation.anthropic_moderator("k", opener=opener)(self.IMAGE)


def sharing_config(mint, rows):
    """A pump fee-sharing config account holding `rows` [(address, bps)]."""
    import base64
    from indexer import pump
    data = (pump.DISC_SHARING_CONFIG + bytes([255, 1, 0]) + decode(mint) + decode(legs.CHARLIE_LAUNCH_WALLET)
            + bytes([0]) + len(rows).to_bytes(4, "little")
            + b"".join(decode(a) + bps.to_bytes(2, "little") for a, bps in rows))
    return {"owner": pump.PUMP_FEE_SHARE_PROGRAM, "lamports": 1,
            "data": [base64.b64encode(data).decode(), "base64"]}


class TestCursor(unittest.TestCase):
    def two(self):
        return [tweet(), tweet(id="101", edit_history_tweet_ids=["101"], author_id="8",
                               text="@CharlieSlugSOL launch Big Cat $BCAT https://t.co/Cd34")]

    def harness(self):
        users = {"7": user(), "8": user(id="8", username="bob")}
        h = Harness(x=FakeX(self.two(), users=users))
        real, self.fails = h.pin, [1]

        def pin(fields, image):
            if self.fails and "MDOG" in str(fields):             # only tweet 100
                self.fails.pop()
                raise RuntimeError("pinning service down")
            return real(fields, image)
        h.bot.pin = pin
        return h

    def test_a_tweet_that_raised_is_fetched_again(self):
        h = self.harness()
        rows = h.bot.tick_mentions()
        self.assertEqual([r["outcome"] for r in rows], ["error", "launched"])
        self.assertIsNone(h.outcome("100"))
        self.assertIsNone(h.ledger.state(tag_bot.SINCE_KEY))     # held before 100
        self.assertEqual(len(h.sent), 2)
        h.x.tweets = self.two()                                   # the next poll returns both again
        rows = h.bot.tick_mentions()
        self.assertEqual(h.x.since[-1], None)
        self.assertEqual(h.outcome("100")[0], "launched")
        self.assertEqual(h.outcome("101")[0], "launched")
        self.assertEqual(len(h.sent), 4)                          # 101 was not launched twice
        self.assertEqual(h.ledger.state(tag_bot.SINCE_KEY), "101")

    def test_a_tweet_that_keeps_raising_ages_out_as_stale(self):
        h = self.harness()
        self.fails = [1] * 100
        h.bot.tick_mentions()
        h.x.tweets = self.two()
        h.bot.tick_mentions()
        self.assertIsNone(h.outcome("100"))
        self.assertIsNone(h.ledger.state(tag_bot.SINCE_KEY))
        h.clock[0] = NOW + timedelta(minutes=11)
        h.x.tweets = self.two()
        h.bot.tick_mentions()
        self.assertEqual(h.outcome("100")[:2], ("refused", "stale"))
        self.assertEqual(h.ledger.state(tag_bot.SINCE_KEY), "101")


class TestSplitRetry(unittest.TestCase):
    def landed_unconfirmed(self):
        h = Harness(x=FakeX([tweet()]))

        def split_unconfirmed(wire, signature):
            if len(h.wires) == 2:
                raise RuntimeError("confirm timed out")
            h.rpc.statuses[signature] = CONFIRMED
        h.on_transmit = split_unconfirmed
        mint = h.bot.tick_mentions()[0]["mint"]
        self.assertEqual(h.outcome(), ("failed", "split_pending", "MDOG", mint))
        h.on_transmit = None
        return h, mint

    def test_a_split_that_landed_is_marked_launched_without_a_resend(self):
        from indexer import enroll
        h, mint = self.landed_unconfirmed()
        rows = [(r.address, r.bps) for r in tag.split_rows()]
        h.rpc.account_data[enroll.sharing_config_address(mint)] = sharing_config(mint, rows[::-1])
        out = h.bot.tick_mentions()
        self.assertEqual(out[0]["outcome"], "launched")
        self.assertEqual(h.outcome(), ("launched", None, "MDOG", mint))
        self.assertEqual(len(h.sent), 2)                          # no second split
        self.assertEqual(h.x.replies[0][0], "100")
        self.assertEqual(h.book.pending_splits(), [])

    def test_a_config_with_other_rows_is_a_mismatch_and_never_retried(self):
        from indexer import enroll
        h, mint = self.landed_unconfirmed()
        rows = [(r.address, r.bps) for r in tag.split_rows()]
        rows[0] = (rows[0][0], rows[0][1] + 1)
        rows[-1] = (rows[-1][0], rows[-1][1] - 1)
        h.rpc.account_data[enroll.sharing_config_address(mint)] = sharing_config(mint, rows)
        out = h.bot.tick_mentions()
        self.assertEqual(out[0]["code"], "split_mismatch")
        self.assertEqual(h.outcome(), ("failed", "split_mismatch", "MDOG", mint))
        self.assertTrue(any("SPLIT_MISMATCH" in str(line) for line in h.lines))
        self.assertEqual(h.book.pending_splits(), [])
        self.assertIn("MDOG", h.book.taken_tickers())
        h.bot.tick_mentions()
        self.assertEqual(len(h.sent), 2)
        self.assertEqual(h.x.replies, [])

    def test_an_absent_config_is_retried(self):
        h, mint = self.landed_unconfirmed()
        out = h.bot.tick_mentions()
        self.assertEqual(out[0]["outcome"], "launched")
        self.assertEqual(len(h.sent), 3)


if __name__ == "__main__":
    unittest.main()

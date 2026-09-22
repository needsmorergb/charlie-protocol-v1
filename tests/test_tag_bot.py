"""Offline tests for the X-tag bot (`indexer.tag_bot`). Every outside effect is a fake."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import legs
from indexer import tag_bot
from indexer import tag_launch as tag
from indexer import tag_ledger
from indexer.base58 import decode

try:
    from indexer.kv import MemoryKV
except ImportError:   # built alongside; the same methods
    MemoryKV = None

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
TS = tag.epoch(NOW)
SOL = 1_000_000_000
BLOCKHASH = "11111111111111111111111111111111"
URI = "https://ipfs.io/ipfs/" + "Q" * 46
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
WALLET = "So11111111111111111111111111111111111111112"
WALLET_2 = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


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

    def accounts(self, addresses):
        return [{"lamports": 1} if a in self.existing else None for a in addresses]

    def call(self, method, params=None):
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": BLOCKHASH}}
        if method == "simulateTransaction":
            self.sims += 1
            err = self.sim_errors.pop(0) if self.sim_errors else None
            return {"value": {"err": err, "logs": [], "unitsConsumed": 1000}}
        raise AssertionError(f"unexpected RPC {method}")

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
    def __init__(self, *, x=None, rpc=None, dry_run=False, stop_file=None, now=NOW):
        self.x = x or FakeX()
        self.rpc = rpc or FakeRpc()
        self.kv = make_kv()
        self.book = tag.Book()
        self.ledger = tag_ledger.Ledger(self.book.db)
        self.sent, self.pins, self.lines, self.distributed = [], [], [], []
        self.clock = [now]
        self.bot = tag_bot.Bot(
            self.x, self.rpc, self.kv, self.book, self.ledger, None, dry_run=dry_run,
            now=lambda: self.clock[0], bot_id="999", sender=self.sender, pin=self.pin,
            confirm=lambda signature: None, distribute_run=self.distribute_run,
            stop_file=stop_file, log=self.lines.append)

    def sender(self, name, message, cosigners=()):
        self.sent.append((name, message, tuple(c.address for c in cosigners)))
        return f"sig{len(self.sent)}"

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
        }
        for code, case in cases.items():
            with self.subTest(code=code):
                x = FakeX([case.get("tweet", tweet())], users=case.get("users"), media=case.get("media"),
                          image=case.get("image", ("image/png", PNG)))
                h = Harness(x=x, rpc=FakeRpc({legs.CHARLIE_LAUNCH_WALLET: case.get("balance", SOL)}))
                h.bot.tick_mentions()
                self.assertEqual((h.sent, h.x.replies, h.pins), ([], [], []))
                expected = "bad_image" if code == "no_photo" else code
                outcome = "failed" if code == "wallet_low" else "refused"
                self.assertEqual(h.outcome()[:2], (outcome, expected))

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

        def failing(name, message, cosigners=()):
            h.sent.append((name, message, ()))
            raise RuntimeError("confirm timed out")
        h.bot.sender = failing
        mint = h.bot.tick_mentions()[0]["mint"]
        self.assertEqual(h.outcome(), ("failed", "create_failed", "MDOG", mint))
        self.assertIsNone(h.ledger.coin(mint))
        h.bot.sender = h.sender
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

    def test_a_failed_create_simulation_sends_nothing(self):
        h = Harness(x=FakeX([tweet()]), rpc=FakeRpc(sim_errors=[{"InstructionError": [0, "x"]}]))
        h.bot.tick_mentions()
        self.assertEqual(h.outcome()[:2], ("failed", "create_simulation"))
        self.assertEqual((h.sent, h.x.replies), ([], []))

    def test_dry_run_sends_posts_pins_and_records_nothing(self):
        h = Harness(x=FakeX([tweet()]), dry_run=True)
        h.bot.sender = lambda *a: self.fail("dry run sent")
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
    def paid(h):
        """What the bot sent, less the distribute crank's own transactions."""
        return [s for s in h.sent if s[1] != b"distribute-message"]

    def credit(self, h, lamports, at=TS, signature="c1", user_id="7"):
        h.ledger.add_coin(f"mint{user_id}", user_id, "alice", "1", at)
        h.ledger.credit(signature, f"mint{user_id}", lamports, at)

    def test_distribute_pays_fees_from_the_launch_wallet_through_the_sender(self):
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

    def test_a_claim_pays_the_balance_and_writes_the_status(self):
        h = Harness()
        self.credit(h, 2 * SOL)
        h.kv.push("claim:queue", {"xid": "7", "handle": "alice", "wallet": WALLET, "at": TS})
        h.bot.tick_money()
        name, message, _ = self.paid(h)[0]
        self.assertEqual(name, "treasury")
        self.assertTrue(lamports_in(message, WALLET, 2 * SOL))
        self.assertEqual(h.kv.get_json("claim:status:7")["state"], "paid")
        self.assertEqual(h.ledger.balance("7"), 0)
        self.assertEqual(h.ledger.wallet("7")[0], WALLET)
        self.assertEqual(h.kv.get_json("claim:credit:7")["wallet"], WALLET)

    def test_a_claim_over_the_cap_is_held_and_the_owner_pays_it(self):
        h = Harness()
        self.credit(h, 6 * SOL)
        h.kv.push("claim:queue", {"xid": "7", "handle": "alice", "wallet": WALLET, "at": TS})
        h.bot.tick_money()
        self.assertEqual(h.kv.get_json("claim:status:7")["state"], "held")
        self.assertEqual(self.paid(h), [])
        self.assertEqual(h.ledger.balance("7"), 6 * SOL)
        self.assertEqual(h.bot.pay_held("7")["state"], "paid")
        self.assertTrue(lamports_in(self.paid(h)[0][1], WALLET, 6 * SOL))

    def test_a_new_wallet_waits_before_it_is_paid(self):
        h = Harness()
        self.credit(h, 2 * SOL)
        h.ledger.apply_wallet("7", tag_ledger.Decision("pay", 0, bind=WALLET))
        self.assertEqual(h.bot.claim("7", WALLET_2)["state"], "refused")
        self.assertEqual(h.ledger.wallet("7"), (WALLET, WALLET_2, TS + 72 * 3600))
        h.clock[0] = NOW + timedelta(hours=72)
        self.assertEqual(h.bot.claim("7", WALLET_2)["state"], "paid")
        self.assertEqual(h.ledger.wallet("7"), (WALLET_2, None, None))

    def test_a_charlie_wallet_or_junk_is_refused(self):
        h = Harness()
        self.credit(h, 2 * SOL)
        for wallet in (legs.CHARLIE_PAYOUT_TREASURY, "not-a-wallet"):
            self.assertEqual(h.bot.claim("7", wallet)["state"], "refused")
        self.assertEqual(h.sent, [])

    def test_week_old_credit_burns_once_a_day_to_the_collection_wallet(self):
        h = Harness()
        self.credit(h, 30_000_000, at=TS - 8 * 86_400)
        self.credit(h, 5_000_000, at=TS - 86_400, signature="c2")
        h.bot.tick_money()
        burns = [s for s in h.sent if s[0] == "treasury"]
        self.assertEqual(len(burns), 1)
        self.assertTrue(lamports_in(burns[0][1], legs.TOLL_DESTINATION, 30_000_000))
        self.assertEqual(h.ledger.balance("7"), 5_000_000)
        h.clock[0] = NOW + timedelta(hours=23)
        self.credit(h, 30_000_000, at=TS - 8 * 86_400, signature="c3")
        h.bot.tick_money()
        self.assertEqual(len([s for s in h.sent if s[0] == "treasury"]), 1)

    def test_burn_waits_below_the_minimum(self):
        h = Harness()
        self.credit(h, 9_999_999, at=TS - 8 * 86_400)
        h.bot.tick_money()
        self.assertEqual(self.paid(h), [])

    def test_ops_tops_up_a_low_launch_wallet(self):
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
                sweeps = [s for s in h.sent if s[0] == "ops"]
                if amount is None:
                    self.assertEqual(sweeps, [])
                else:
                    self.assertEqual(len(sweeps), 1)
                    self.assertTrue(lamports_in(sweeps[0][1], legs.CHARLIE_LAUNCH_WALLET, amount))

    def test_dry_run_money_sends_nothing_and_leaves_the_queue(self):
        h = Harness(dry_run=True, rpc=FakeRpc({legs.CHARLIE_LAUNCH_WALLET: 100_000_000,
                                               legs.CHARLIE_OPS_DESTINATION: 2 * SOL}))
        h.bot.sender = lambda *a: self.fail("dry run sent")
        self.credit(h, 30_000_000, at=TS - 8 * 86_400)
        h.kv.push("claim:queue", {"xid": "7", "wallet": WALLET})
        out = h.bot.tick_money()
        self.assertEqual(out["burn"]["state"], "simulated")
        self.assertEqual(out["sweep"]["state"], "simulated")
        self.assertEqual(h.kv.pop("claim:queue"), {"xid": "7", "wallet": WALLET})
        self.assertIsNone(h.kv.get_json("claim:credit:7"))
        self.assertEqual(h.distributed[0][2], None)

    def test_one_failing_step_does_not_stop_the_rest(self):
        h = Harness(rpc=FakeRpc({legs.CHARLIE_LAUNCH_WALLET: 100_000_000, legs.CHARLIE_OPS_DESTINATION: 2 * SOL}))
        h.ledger.add_coin("mintA", "7", "alice", "1", TS)
        h.bot.distribute_run = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        out = h.bot.tick_money()
        self.assertIn("error", out["distribute"])
        self.assertEqual(out["sweep"]["state"], "sent")


class TestLiveSender(unittest.TestCase):
    def test_the_key_sender_signs_in_the_message_s_order_with_cosigners(self):
        from indexer.ed25519 import Keypair, verify
        launcher = Keypair.from_seed(bytes(range(32)))
        built = tag.build(tag.TagRequest("Moon Dog", "MDOG"), URI, BLOCKHASH, launcher=launcher.address)
        wires = []

        class Rpc:
            def call(self, method, params):
                import base64
                wires.append(base64.b64decode(params[0]))
                return "sig"

        send = tag_bot.key_sender(Rpc(), {"launch": launcher})
        self.assertEqual(send("launch", built.create, (built.mint,)), "sig")
        wire = wires[0]
        self.assertEqual(wire[0], 2)
        signers = tag.launch.signer_addresses(built.create)
        for i, address in enumerate(signers):
            self.assertTrue(verify(decode(address), built.create, wire[1 + 64 * i: 65 + 64 * i]))
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


if __name__ == "__main__":
    unittest.main()

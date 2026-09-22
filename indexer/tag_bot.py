"""The X-tag bot: reads tags, launches coins, pays and burns what they earn.

Two ticks, each safe to run again after a crash:

- `tick_mentions` (every minute): the bot's new mentions, oldest first. Each
  is parsed (`tag_launch.parse`; anything else is ignored and not recorded),
  deduped, run through the refusals, and its image fetched and checked.
  A tag that passes is launched: metadata pinned, create simulated, signed,
  sent and confirmed, then the split. Refusals are recorded and silent. A
  create that landed without its split is recorded `split_pending` and the
  split is retried every tick. Success is the only thing the bot says.
- `tick_money` (every 30 minutes): the distribute crank for tag coins (the
  launch wallet pays the fees), the treasury scan into the ledger, the claim
  queue, the daily burn of credit older than seven days, the OPS top-up of
  the launch wallet, and the mirror of every balance to the site's KV.

Every outside effect is injected: the X client, the RPC, the KV, `sender`
(signs a message with a named key and sends it), `pin` (pump's metadata
pin), the clock and the log. `dry_run` simulates and never sends, posts,
pins, pops the claim queue or writes the KV. A file named STOP in the config
directory pauses launches; money ticks carry on.

The keys are named "launch", "treasury" and "ops".
"""
from __future__ import annotations

import base64
from pathlib import Path

from . import buyback, distribute, launch, legs
from . import tag_launch as tag
from . import tag_ledger
from .base58 import decode
from .message import compile_legacy, signed_transaction

# -- the numbers ----------------------------------------------------------------------

LAMPORTS_PER_SOL = 1_000_000_000
MAX_IMAGE_BYTES = 5_000_000
IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
PLACEHOLDER_URI = "https://ipfs.io/ipfs/" + "Q" * 46   # dry runs never pin

MIN_BURN_LAMPORTS = 10_000_000             # 0.01 SOL; below this the day's burn waits
BURN_EVERY_SECONDS = tag_ledger.DAY        # one burn transaction per day at most
SWEEP_BELOW_LAMPORTS = 300_000_000         # OPS tops up the launch wallet below 0.30 SOL
SWEEP_TARGET_LAMPORTS = 500_000_000        # filling it up to 0.50 SOL
SWEEP_MIN_LAMPORTS = 10_000_000            # and not bothering below 0.01 SOL
OPS_RESERVE_LAMPORTS = tag_ledger.TREASURY_RESERVE_LAMPORTS   # OPS keeps rent + a fee
MAX_CLAIMS_PER_TICK = 50
CREATE_LANDING_SECONDS = 600               # an unconfirmed create that has not appeared by then never will

SINCE_KEY = "mentions_since_id"
LAST_BURN_KEY = "last_burn_at"
UNCONFIRMED_KEY = "unconfirmed_creates"   # mint -> what tag_coins needs if it lands
QUEUE_KEY = "claim:queue"

KEY_ADDRESSES = {
    "launch": legs.CHARLIE_LAUNCH_WALLET,
    "treasury": legs.CHARLIE_PAYOUT_TREASURY,
    "ops": legs.CHARLIE_OPS_DESTINATION,
}


# -- the RPC steps tools/tag_launcher.py shares ------------------------------------------


def simulate_wire(rpc, wire: bytes) -> dict:
    """simulateTransaction on a wire transaction, signatures unchecked."""
    result = rpc.call("simulateTransaction", [base64.b64encode(wire).decode(), {
        "encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True, "commitment": "processed",
    }])
    return (result or {}).get("value") or {}


def send_wire(rpc, wire: bytes) -> str:
    return rpc.call("sendTransaction", [base64.b64encode(wire).decode(), {
        "encoding": "base64", "skipPreflight": False, "preflightCommitment": "confirmed", "maxRetries": 5,
    }])


def blockhash(rpc) -> str:
    return rpc.call("getLatestBlockhash", [{"commitment": "finalized"}])["value"]["blockhash"]


def pin_uri(pin, request: tag.TagRequest, image: tuple[str, str, bytes], handle: str, tweet_id: str) -> str:
    """Pin the metadata with `pin` (`api.launch.pin_metadata`) and return a
    URI the launch builder accepts."""
    pinned = pin(tag.metadata_fields(request, handle, tweet_id), image)
    uri = (pinned or {}).get("metadataUri") or ""
    launch.validate_metadata(request.name, request.ticker, uri)
    return uri


def sign_with(message: bytes, keypairs) -> bytes:
    """The wire transaction, each signer's signature in the message's order."""
    by_address = {k.address: k for k in keypairs}
    signers = launch.signer_addresses(message)
    missing = [a for a in signers if a not in by_address]
    if missing:
        raise launch.LaunchError(f"no key for signer(s) {missing}")
    return signed_transaction(message, [by_address[a].sign(message) for a in signers])


def key_sender(rpc, keys: dict):
    """The live `sender`: sign with `keys[name]` (plus any cosigners) and send."""
    def send(name: str, message: bytes, cosigners=()) -> str:
        return send_wire(rpc, sign_with(message, (keys[name], *cosigners)))
    return send


# -- the image -------------------------------------------------------------------------------

_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def sniff(data: bytes) -> str | None:
    for magic, ctype in _MAGIC:
        if data.startswith(magic):
            return ctype
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def image_of(x, tweet: dict, media: dict) -> tuple[str, str, bytes] | None:
    """The tweet's first photo as `(filename, content type, bytes)`, or None
    when there is none or it is not a PNG, JPEG, GIF or WebP of at most
    MAX_IMAGE_BYTES. The bytes decide the type, not the header."""
    for key in (tweet.get("attachments") or {}).get("media_keys") or []:
        item = media.get(key) or {}
        if item.get("type") != "photo" or not item.get("url"):
            continue
        try:
            declared, data = x.fetch(item["url"], limit=MAX_IMAGE_BYTES)
        except Exception:  # noqa: BLE001 -- any fetch failure is a bad image
            return None
        ctype = sniff(data or b"")
        declared = (declared or "").split(";")[0].strip().lower()
        if ctype is None or len(data) > MAX_IMAGE_BYTES or (declared and declared not in IMAGE_TYPES):
            return None
        return (f"image.{ctype.split('/')[1]}", ctype, data)
    return None


def valid_wallet(address: str) -> bool:
    try:
        return len(decode(address or "")) == 32 and address not in tag_ledger.CHARLIE_WALLETS
    except ValueError:
        return False


def transfer_message(source: str, destination: str, lamports: int, recent_blockhash: str) -> bytes:
    return compile_legacy(source, [buyback.ix_system_transfer(source, destination, lamports)], recent_blockhash)


# -- the bot ---------------------------------------------------------------------------------


class Bot:
    def __init__(self, x, rpc, kv, book: tag.Book, ledger: tag_ledger.Ledger, keys: dict | None, *,
                 dry_run: bool, now=tag.now_utc, bot_id: str = "", sender=None, pin=None,
                 confirm=None, distribute_run=distribute.run, stop_file: Path | str | None = None,
                 log=None):
        self.x, self.rpc, self.kv, self.book, self.ledger = x, rpc, kv, book, ledger
        self.keys = keys or {}
        self.dry_run = dry_run
        self.now = now
        self.bot_id = str(bot_id)
        self.sender = sender or key_sender(rpc, self.keys)
        self.pin = pin
        self.confirm = confirm or (lambda signature: buyback.confirm(rpc, signature))
        self.distribute_run = distribute_run
        self.stop_file = Path(stop_file) if stop_file else None
        self._log = log or (lambda line: print(line, flush=True))

    # -- plumbing --

    def log(self, event: str, **fields) -> None:
        parts = [event] + [f"{k}={v}" for k, v in fields.items() if v is not None]
        self._log(" ".join(str(p) for p in parts))

    def stopped(self) -> bool:
        return self.stop_file is not None and self.stop_file.exists()

    def _ts(self) -> int:
        return tag.epoch(self.now())

    def _send(self, name: str, message: bytes, cosigners=()) -> str:
        if self.dry_run:
            raise RuntimeError("dry run: nothing is sent")   # never reached; callers check first
        return self.sender(name, message, cosigners)

    def _simulate(self, message: bytes, cosigner=None) -> dict:
        wire = launch.partially_signed(message, cosigner) if cosigner else None
        return simulate_wire(self.rpc, wire) if wire else buyback.simulate(self.rpc, message)

    # -- mentions --

    def tick_mentions(self) -> list[dict]:
        """One pass over new mentions. Returns one row per tag acted on."""
        if self.stopped():
            self.log("paused", reason="STOP file present")
            return []
        self._check_unconfirmed()
        rows = [self._retry_split(*pending) for pending in self.book.pending_splits()]
        page = self.x.mentions(self.bot_id, self.ledger.state(SINCE_KEY))
        for tweet in page.get("tweets") or []:
            try:
                row = self._one(tweet, page.get("users") or {}, page.get("media") or {})
            except Exception as exc:  # noqa: BLE001 -- one bad tag never stops the rest
                row = {"tweet": tweet.get("id"), "outcome": "error", "reason": f"{type(exc).__name__}: {exc}"}
                self.log("error", tweet=tweet.get("id"), reason=row["reason"])
            if row:
                rows.append(row)
        if page.get("newest_id"):
            self.ledger.set_state(SINCE_KEY, str(page["newest_id"]))
        return rows

    def _refuse(self, key: str, user_id: str, ts: int, refused: tag.TagRefused, ticker: str) -> dict:
        # A paused wallet or the daily ceiling is not the requester's doing,
        # so it is not held against them (only `refused` counts toward a timeout).
        outcome = "failed" if refused.code in ("wallet_low", "ceiling") else "refused"
        if not self.dry_run:
            self.book.record(key, user_id, ts, outcome, code=refused.code, ticker=ticker)
        self.log("refused", tweet=key, user=user_id, code=refused.code)
        return {"tweet": key, "outcome": outcome, "code": refused.code}

    def _one(self, tweet: dict, users: dict, media: dict) -> dict | None:
        request = tag.parse(tweet.get("text") or "")
        if request is None:
            return None
        key = tag.dedupe_key(tweet)
        if self.book.seen(key):
            return None
        user_id = str(tweet.get("author_id") or "")
        user = users.get(user_id)
        now = self.now()
        ts = tag.epoch(now)
        refused = tag.tweet_refusal(tweet, now)
        if refused is None and user is None:
            refused = tag.TagRefused("no_user", "The author could not be read.")
        refused = (refused
                   or tag.account_refusal(user, now, bot_id=self.bot_id)
                   or self.book.limit_refusal(user_id, ts)
                   or tag.content_refusal(request, taken=self.book.taken_tickers())
                   or tag.wallet_refusal(self.rpc.balance(legs.CHARLIE_LAUNCH_WALLET)))
        if refused:
            return self._refuse(key, user_id, ts, refused, request.ticker)
        image = image_of(self.x, tweet, media)
        if image is None:
            return self._refuse(key, user_id, ts, tag.TagRefused("bad_image", "The image is not usable."),
                                request.ticker)
        return self._launch(key, tweet, user, request, image, ts)

    def _launch(self, key: str, tweet: dict, user: dict, request: tag.TagRequest, image, ts: int) -> dict:
        user_id, handle, tweet_id = str(user["id"]), user.get("username") or "", str(tweet["id"])
        if self.dry_run:
            uri = PLACEHOLDER_URI
            self.log("would_pin", tweet=key, ticker=request.ticker, bytes=len(image[2]))
        else:
            uri = pin_uri(self.pin, request, image, handle, tweet_id)
        built = tag.build(request, uri, blockhash(self.rpc))
        mint = built.mint.address
        simulated = self._simulate(built.create, built.mint)
        if simulated.get("err") is not None:
            if not self.dry_run:
                self.book.record(key, user_id, ts, "failed", code="create_simulation", ticker=request.ticker)
            self.log("create_refused", tweet=key, err=simulated.get("err"))
            return {"tweet": key, "outcome": "failed", "code": "create_simulation"}
        if self.dry_run:
            self.log("would_launch", tweet=key, user=user_id, ticker=request.ticker, mint=mint,
                     units=simulated.get("unitsConsumed"))
            return {"tweet": key, "outcome": "simulated", "mint": mint}
        try:
            signature = self._send("launch", built.create, (built.mint,))
            self.confirm(signature)
        except Exception as exc:  # noqa: BLE001
            self.book.record(key, user_id, ts, "failed", code="create_failed", ticker=request.ticker, mint=mint)
            unconfirmed = self.ledger.state(UNCONFIRMED_KEY, {})
            unconfirmed[mint] = {"key": key, "user_id": user_id, "handle": handle, "tweet_id": tweet_id,
                                 "at": ts, "ticker": request.ticker}
            self.ledger.set_state(UNCONFIRMED_KEY, unconfirmed)
            self.log("create_failed", tweet=key, mint=mint, reason=f"{type(exc).__name__}: {exc}")
            return {"tweet": key, "outcome": "failed", "code": "create_failed", "mint": mint}
        self.ledger.add_coin(mint, user_id, handle, tweet_id, ts)
        self.log("created", tweet=key, mint=mint, signature=signature)
        return self._finish(key, user_id, ts, request.ticker, mint)

    def _check_unconfirmed(self) -> None:
        """A create whose send or confirm failed may still have landed. One
        whose mint now exists becomes a tag coin with its split pending; one
        still absent after CREATE_LANDING_SECONDS is finally failed, which
        is not held against the requester (only `refused` counts)."""
        unconfirmed = self.ledger.state(UNCONFIRMED_KEY, {})
        if not unconfirmed or self.dry_run:
            return
        ts = self._ts()
        for mint, row in list(unconfirmed.items()):
            if self.rpc.accounts([mint])[0]:
                self.ledger.add_coin(mint, row["user_id"], row["handle"], row["tweet_id"], row["at"])
                self.book.record(row["key"], row["user_id"], row["at"], "failed", code="split_pending",
                                 ticker=row["ticker"], mint=mint)
                self.log("create_landed", tweet=row["key"], mint=mint)
            elif ts - row["at"] >= CREATE_LANDING_SECONDS:
                self.book.record(row["key"], row["user_id"], row["at"], "failed", code="create_lost",
                                 ticker=row["ticker"], mint=mint)
                self.log("create_lost", tweet=row["key"], mint=mint)
            else:
                continue
            del unconfirmed[mint]
        self.ledger.set_state(UNCONFIRMED_KEY, unconfirmed)

    def _finish(self, key: str, user_id: str, ts: int, ticker: str, mint: str) -> dict:
        """The split, then the record and the reply; a failed split leaves
        the coin `split_pending` for the next tick."""
        try:
            signature = self._split(mint)
        except Exception as exc:  # noqa: BLE001
            self.book.record(key, user_id, ts, "failed", code="split_pending", ticker=ticker, mint=mint)
            self.log("split_pending", tweet=key, mint=mint, reason=f"{type(exc).__name__}: {exc}")
            return {"tweet": key, "outcome": "failed", "code": "split_pending", "mint": mint}
        self.book.record(key, user_id, ts, "launched", ticker=ticker, mint=mint)
        self.log("launched", tweet=key, mint=mint, signature=signature)
        coin = self.ledger.coin(mint) or {}
        reply = None
        try:
            reply = self.x.post_reply(coin.get("tweet_id") or key,
                                      tag.reply_text(tag.TagRequest(ticker, ticker), mint))
        except Exception as exc:  # noqa: BLE001 -- the coin is live either way
            self.log("reply_failed", tweet=key, reason=f"{type(exc).__name__}: {exc}")
        return {"tweet": key, "outcome": "launched", "mint": mint, "reply": reply}

    def _split(self, mint: str) -> str:
        message = tag.split_message(mint, blockhash(self.rpc))
        checked = buyback.simulate(self.rpc, message)
        if checked.get("err") is not None:
            raise launch.LaunchError(f"split simulation: {checked.get('err')}")
        signature = self._send("launch", message)
        self.confirm(signature)
        return signature

    def _retry_split(self, key: str, user_id: str, at: int, ticker: str, mint: str) -> dict:
        if self.dry_run:
            checked = buyback.simulate(self.rpc, tag.split_message(mint, blockhash(self.rpc)))
            self.log("would_retry_split", tweet=key, mint=mint, err=checked.get("err"))
            return {"tweet": key, "outcome": "simulated", "mint": mint}
        return self._finish(key, user_id, at, ticker, mint)

    # -- money --

    def tick_money(self) -> dict:
        """Every money step, each on its own: one failing never stops the next."""
        out = {}
        for name, step in (("distribute", self._distribute), ("scan", self._scan), ("claims", self._claims),
                           ("burn", self._burn), ("sweep", self._sweep), ("mirror", self._mirror)):
            try:
                out[name] = step()
            except Exception as exc:  # noqa: BLE001
                out[name] = {"error": f"{type(exc).__name__}: {exc}"}
                self.log("money_error", step=name, reason=out[name]["error"])
        return out

    def _distribute(self) -> list[dict]:
        mints = self.ledger.tag_mints()
        if not mints:
            return []
        live = not self.dry_run
        rows = self.distribute_run(
            self.rpc, mints, payer=legs.CHARLIE_LAUNCH_WALLET, keypair="launch" if live else None,
            send=lambda _rpc, _key, message: self._send("launch", message),
            confirm=lambda _rpc, signature: self.confirm(signature))
        for row in rows:
            self.log("distribute", mint=row.get("mint"), outcome=row.get("outcome"), signature=row.get("signature"))
        return rows

    def _scan(self) -> list[dict]:
        rows = self.ledger.scan(self.rpc, legs.CHARLIE_PAYOUT_TREASURY, now=self._ts())
        for row in rows:
            self.log("credit", user=row["user_id"], mint=row["mint"], lamports=row["lamports"])
        return rows

    def _status(self, xid: str, state: str, lamports: int, signature=None, reason=None) -> dict:
        status = {"state": state, "lamports": int(lamports), "signature": signature, "reason": reason,
                  "at": self._ts()}
        self.kv.set_json(f"claim:status:{xid}", status)
        self.log("claim", user=xid, state=state, lamports=lamports, signature=signature, reason=reason)
        return status

    def _claims(self) -> list[dict]:
        if self.dry_run:
            self.log("would_process_claims")
            return []
        done = []
        for _ in range(MAX_CLAIMS_PER_TICK):
            item = self.kv.pop(QUEUE_KEY)
            if item is None:
                break
            done.append(self.claim(str(item.get("xid") or ""), str(item.get("wallet") or "")))
        return done

    def claim(self, xid: str, wallet: str, *, caps: bool = True) -> dict:
        """Decide and, when the decision is to pay, pay one claim."""
        if not xid or not valid_wallet(wallet):
            return self._status(xid or "unknown", "refused", 0, reason="that wallet cannot receive claims")
        ts = self._ts()
        bound, pending, ready_at = self.ledger.wallet(xid)
        decision = tag_ledger.claim_decision(
            balance=self.ledger.balance(xid), wallet=wallet, bound=bound, pending=pending, ready_at=ready_at,
            now=ts, recipient_lamports=self.rpc.balance(wallet),
            treasury_lamports=self.rpc.balance(legs.CHARLIE_PAYOUT_TREASURY),
            paid_today=self.ledger.claimed_since(ts - tag_ledger.DAY), caps=caps)
        self.ledger.apply_wallet(xid, decision)
        if decision.state != "pay":
            return self._status(xid, decision.state, decision.lamports, reason=decision.reason)
        message = transfer_message(legs.CHARLIE_PAYOUT_TREASURY, wallet, decision.lamports, blockhash(self.rpc))
        checked = buyback.simulate(self.rpc, message)
        if checked.get("err") is not None:
            return self._status(xid, "held", decision.lamports, reason="the payment did not simulate")
        signature = self._send("treasury", message)
        # Debit on send, before confirming: an unconfirmed payment may still
        # land, and paying twice is worse than a claim the owner re-checks.
        self.ledger.debit(xid, "claim", decision.lamports, wallet=wallet, signature=signature, at=ts)
        try:
            self.confirm(signature)
        except Exception:  # noqa: BLE001
            return self._status(xid, "held", decision.lamports, signature,
                                reason="sent but not confirmed; the owner checks it")
        return self._status(xid, "paid", decision.lamports, signature)

    def pay_held(self, xid: str) -> dict:
        """The owner's command for a held claim: pay the bound wallet without
        the per-claim and per-day caps. Rent and the treasury reserve still apply."""
        bound = self.ledger.wallet(xid)[0]
        if bound is None:
            return {"state": "refused", "reason": "no wallet is bound"}
        if self.dry_run:
            self.log("would_pay_held", user=xid, wallet=bound, lamports=self.ledger.balance(xid))
            return {"state": "simulated", "lamports": self.ledger.balance(xid)}
        return self.claim(xid, bound, caps=False)

    def _burn(self) -> dict | None:
        ts = self._ts()
        last = self.ledger.state(LAST_BURN_KEY)
        if last is not None and ts - int(last) < BURN_EVERY_SECONDS:
            return None
        owed = {uid: self.ledger.burnable(uid, ts) for uid in self.ledger.requesters()}
        owed = {uid: amount for uid, amount in owed.items() if amount > 0}
        total = sum(owed.values())
        if total < MIN_BURN_LAMPORTS:
            return None
        treasury = self.rpc.balance(legs.CHARLIE_PAYOUT_TREASURY)
        if treasury - total < tag_ledger.TREASURY_RESERVE_LAMPORTS:
            self.log("burn_held", lamports=total, treasury=treasury)
            return {"state": "held", "lamports": total}
        message = transfer_message(legs.CHARLIE_PAYOUT_TREASURY, legs.TOLL_DESTINATION, total, blockhash(self.rpc))
        checked = buyback.simulate(self.rpc, message)
        if checked.get("err") is not None or self.dry_run:
            self.log("would_burn" if self.dry_run else "burn_refused", lamports=total, err=checked.get("err"))
            return {"state": "simulated" if self.dry_run else "refused", "lamports": total}
        signature = self._send("treasury", message)
        for uid, amount in owed.items():
            self.ledger.debit(uid, "burn", amount, wallet=legs.TOLL_DESTINATION, signature=signature, at=ts)
        self.ledger.set_state(LAST_BURN_KEY, ts)
        self.log("burn", lamports=total, users=len(owed), signature=signature)
        self.confirm(signature)
        return {"state": "sent", "lamports": total, "signature": signature, "users": len(owed)}

    def _sweep(self) -> dict | None:
        launch_balance = self.rpc.balance(legs.CHARLIE_LAUNCH_WALLET)
        if launch_balance >= SWEEP_BELOW_LAMPORTS:
            return None
        ops = self.rpc.balance(legs.CHARLIE_OPS_DESTINATION)
        amount = min(SWEEP_TARGET_LAMPORTS - launch_balance, ops - OPS_RESERVE_LAMPORTS)
        if amount < SWEEP_MIN_LAMPORTS:
            self.log("sweep_short", launch=launch_balance, ops=ops)
            return None
        message = transfer_message(legs.CHARLIE_OPS_DESTINATION, legs.CHARLIE_LAUNCH_WALLET, amount,
                                   blockhash(self.rpc))
        checked = buyback.simulate(self.rpc, message)
        if checked.get("err") is not None or self.dry_run:
            self.log("would_sweep" if self.dry_run else "sweep_refused", lamports=amount, err=checked.get("err"))
            return {"state": "simulated" if self.dry_run else "refused", "lamports": amount}
        signature = self._send("ops", message)
        self.log("sweep", lamports=amount, signature=signature)
        self.confirm(signature)
        return {"state": "sent", "lamports": amount, "signature": signature}

    def _mirror(self) -> int:
        if self.dry_run:
            return 0
        return self.ledger.mirror(self.kv, self._ts())

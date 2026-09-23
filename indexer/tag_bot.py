"""The X-tag bot: reads tags, launches coins, pays and burns what they earn.

Two ticks, each safe to run again after a crash:

- `tick_mentions` (every minute): the bot's new mentions, oldest first. Each
  is parsed (`tag_launch.parse`; anything else is ignored and not recorded),
  deduped, run through the refusals, and its image fetched and checked.
  A tag that passes is launched: metadata pinned, create simulated, the tweet
  recorded `launching` with its mint BEFORE the create is sent (so a restart
  can never launch it twice), then signed, sent and confirmed, then the
  split. Refusals are recorded and silent. A create that landed without its
  split is `split_pending` and the split is retried every tick; a create
  whose outcome is unknown is checked on chain every tick. `since_id` is
  saved after each tweet. Success is the only thing the bot says.
- `tick_money` (every 30 minutes): the distribute crank for tag coins (the
  launch wallet pays the fees), the treasury scan into the ledger, the
  reconcile of pending payments, the mirror of every balance to the site's
  KV, the signed claim queue, approved held claims, the daily burn of credit
  older than seven days, the OPS top-up of the launch wallet, and the mirror
  again.

Every payment out of the treasury is written ahead: signed first, its debit
recorded `pending` under the transaction's own signature, then sent.
`reconcile` settles each pending debit from `getSignatureStatuses`. The
network fee comes out of the amount sent, so the treasury never pays a fee
the ledger does not see.

Every outside effect is injected: the X client, the RPC, the KV, `signer`
(signs a message with a named key, returning the wire transaction),
`transmit` (sends a wire transaction), `pin` (pump's metadata pin), the
clock and the log. `dry_run` simulates and never signs, sends, posts, pins,
pops the claim queue or writes the KV. A file named STOP in the config
directory pauses launches; money ticks carry on.

The keys are named "launch", "treasury" and "ops".
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

from . import buyback, claim_session, distribute, enroll, launch, legs, mint_pool, pump
from . import tag_launch as tag
from . import tag_ledger
from .base58 import decode, encode
from .message import compile_legacy, signed_transaction
from .xapi import XError

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
TX_FEE_LAMPORTS = tag_ledger.TX_FEE_LAMPORTS
MAX_CLAIMS_PER_TICK = 50
CLAIM_RETRIES = 3          # ticks a failing claim is retried before it is dropped (credit untouched)
CREATE_LANDING_SECONDS = 600               # an unconfirmed create that has not appeared by then never will
STATUS_BATCH = 256                         # getSignatureStatuses takes at most this many

SINCE_KEY = "mentions_since_id"
LAST_BURN_KEY = "last_burn_at"
UNCONFIRMED_KEY = "unconfirmed_creates"   # mint -> what tag_coins needs if it lands
QUEUE_KEY = "claim:queue"
PAYMENT_FAILED = "payment failed, will retry on next claim"

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


def latest_blockhash(rpc) -> tuple[str, int | None]:
    """`(blockhash, lastValidBlockHeight)`: past that height a transaction
    built on this blockhash can never land."""
    value = rpc.call("getLatestBlockhash", [{"commitment": "finalized"}])["value"]
    last_valid = value.get("lastValidBlockHeight")
    return value["blockhash"], int(last_valid) if isinstance(last_valid, int) else None


def blockhash(rpc) -> str:
    return latest_blockhash(rpc)[0]


def pin_uri(pin, request: tag.TagRequest, image: tuple[str, str, bytes], handle: str, tweet_id: str,
            mint: str) -> str:
    """Pin the metadata with `pin` (`api.launch.pin_metadata`) and return a
    URI the launch builder accepts."""
    pinned = pin(tag.metadata_fields(request, handle, tweet_id, mint), image)
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


def key_signer(keys: dict):
    """The live `signer`: sign with `keys[name]` plus any cosigners."""
    def sign(name: str, message: bytes, cosigners=()) -> bytes:
        return sign_with(message, (keys[name], *cosigners))
    return sign


def wire_signature(wire: bytes) -> str:
    """The transaction's ID: its first signature (the fee payer's)."""
    if not wire or wire[0] < 1 or wire[0] >= 0x80:
        raise ValueError("not a wire transaction with 1 to 127 signatures")
    return encode(wire[1:65])


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
        except XError as exc:
            # Too large, or gone (4xx other than 429): the image is bad.
            # Anything else is the network or X, so it raises and the
            # mention stays retryable instead of counting against the user.
            if "larger than" in str(exc) or (400 <= exc.status < 500 and exc.status != 429):
                return None
            raise
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
                 dry_run: bool, now=tag.now_utc, bot_id: str = "", signer=None, transmit=None, pin=None,
                 confirm=None, distribute_run=distribute.run, stop_file: Path | str | None = None,
                 claim_secret: str = "", moderate=None, mint_keys=None, log=None):
        self.x, self.rpc, self.kv, self.book, self.ledger = x, rpc, kv, book, ledger
        self.keys = keys or {}
        self.dry_run = dry_run
        self.now = now
        self.bot_id = str(bot_id)
        self.signer = signer or key_signer(self.keys)
        self.transmit = transmit or (lambda wire: send_wire(rpc, wire))
        self.pin = pin
        self.confirm = confirm or (lambda signature: buyback.confirm(rpc, signature))
        self.distribute_run = distribute_run
        self.stop_file = Path(stop_file) if stop_file else None
        self.claim_secret = claim_secret
        self._claim_failures: dict[str, int] = {}
        self._claims_backlog = False
        self.moderate = moderate            # image -> True when SAFE; None only when the owner turned it off
        self.mint_keys = list(mint_keys or ())   # the 1nc1n pool, shared with the /launch door
        self._log = log or (lambda line: print(line, flush=True))

    # -- plumbing --

    def log(self, event: str, **fields) -> None:
        parts = [event] + [f"{k}={v}" for k, v in fields.items() if v is not None]
        self._log(" ".join(str(p) for p in parts))

    def stopped(self) -> bool:
        return self.stop_file is not None and self.stop_file.exists()

    def _ts(self) -> int:
        return tag.epoch(self.now())

    def _sign(self, name: str, message: bytes, cosigners=()) -> bytes:
        if self.dry_run:
            raise RuntimeError("dry run: nothing is signed")   # never reached; callers check first
        return self.signer(name, message, cosigners)

    def _send(self, name: str, message: bytes, cosigners=()) -> str:
        return self.transmit(self._sign(name, message, cosigners))

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
        # The cursor stops at the first tweet that raised before it was
        # recorded, so the next poll fetches it again. Tweets after it are
        # still acted on now; recorded ones are skipped next time (`seen`).
        # One that keeps raising ages out as `stale`, which is recorded.
        held = False
        for tweet in page.get("tweets") or []:
            try:
                row = self._one(tweet, page.get("users") or {}, page.get("media") or {})
            except Exception as exc:  # noqa: BLE001 -- one bad tag never stops the rest
                row = {"tweet": tweet.get("id"), "outcome": "error", "reason": f"{type(exc).__name__}: {exc}"}
                self.log("error", tweet=tweet.get("id"), reason=row["reason"])
                held = True
            if row:
                rows.append(row)
            if tweet.get("id") and not held:
                self.ledger.set_state(SINCE_KEY, str(tweet["id"]))
        if page.get("newest_id") and not held:
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
        refused = tag.tweet_refusal(tweet, now, media)
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
        if self.moderate is not None:
            try:
                safe = self.moderate(image)
            except Exception as exc:  # noqa: BLE001 -- no verdict, no launch, and not the requester's fault
                # Nothing is recorded: the raise holds the mention cursor, so
                # the tag is tried again next poll (and ages out as `stale`).
                self.log("moderation_unavailable", tweet=key, reason=f"{type(exc).__name__}: {exc}")
                raise
            if safe is not True:
                return self._refuse(key, user_id, ts, tag.TagRefused(
                    "image_refused", "That image cannot be a coin's picture."), request.ticker)
        return self._launch(key, tweet, user, request, image, ts)

    def _new_mint(self):
        """The next unused 1nc1n key, shared with the /launch door (the chain
        says which are spent, so the two never launch the same key), or a
        random mint once the pool is empty."""
        if self.mint_keys:
            try:
                return mint_pool.pick(self.mint_keys, self.rpc)
            except mint_pool.PoolError as exc:
                self.log("mint_pool_empty", reason=str(exc))
        return launch.new_mint()

    def _launch(self, key: str, tweet: dict, user: dict, request: tag.TagRequest, image, ts: int) -> dict:
        user_id, handle, tweet_id = str(user["id"]), user.get("username") or "", str(tweet["id"])
        mint_key = self._new_mint()             # first: the metadata links the coin's own page
        if self.dry_run:
            uri = PLACEHOLDER_URI
            self.log("would_pin", tweet=key, ticker=request.ticker, bytes=len(image[2]))
        else:
            uri = pin_uri(self.pin, request, image, handle, tweet_id, mint_key.address)
        built = tag.build(request, uri, blockhash(self.rpc), mint=mint_key)
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
        # Written ahead: the tweet is seen and the mint is checked on chain
        # after any crash from here on.
        unconfirmed = self.ledger.state(UNCONFIRMED_KEY, {})
        unconfirmed[mint] = {"key": key, "user_id": user_id, "handle": handle, "tweet_id": tweet_id,
                             "at": ts, "ticker": request.ticker}
        self.ledger.set_state(UNCONFIRMED_KEY, unconfirmed)
        self.book.record(key, user_id, ts, "launching", ticker=request.ticker, mint=mint)
        try:
            signature = self._send("launch", built.create, (built.mint,))
            self.confirm(signature)
        except Exception as exc:  # noqa: BLE001
            self.book.record(key, user_id, ts, "failed", code="create_failed", ticker=request.ticker, mint=mint)
            self.log("create_failed", tweet=key, mint=mint, reason=f"{type(exc).__name__}: {exc}")
            return {"tweet": key, "outcome": "failed", "code": "create_failed", "mint": mint}
        # The coin exists. Its row reaches `split_pending` before the recovery
        # entry goes, so a crash anywhere here still ends with the split retried.
        self.ledger.add_coin(mint, user_id, handle, tweet_id, ts)
        self.book.record(key, user_id, ts, "failed", code="split_pending", ticker=request.ticker, mint=mint)
        self._forget_unconfirmed(mint)
        self.log("created", tweet=key, mint=mint, signature=signature)
        return self._finish(key, user_id, ts, request.ticker, mint)

    def _forget_unconfirmed(self, mint: str) -> None:
        unconfirmed = self.ledger.state(UNCONFIRMED_KEY, {})
        if unconfirmed.pop(mint, None) is not None:
            self.ledger.set_state(UNCONFIRMED_KEY, unconfirmed)

    def _check_unconfirmed(self) -> None:
        """A create recorded `launching` or `create_failed` may have landed.
        One whose mint now exists becomes a tag coin with its split pending;
        one still absent after CREATE_LANDING_SECONDS is finally failed,
        which is not held against the requester (only `refused` counts)."""
        if self.dry_run:
            return
        unconfirmed = self.ledger.state(UNCONFIRMED_KEY, {})
        self._recover_launching(unconfirmed)
        ts = self._ts()
        for mint, row in list(unconfirmed.items()):
            current = self.book.entry(row["key"])
            if current is None or current["mint"] != mint or not (
                    current["outcome"] == "launching" or current["code"] == "create_failed"):
                # Never sent (the crash came before `launching`), or the tweet
                # has a newer row since: this entry must not overwrite it.
                self.log("unconfirmed_stale", tweet=row["key"], mint=mint)
                del unconfirmed[mint]
                continue
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

    def _recover_launching(self, unconfirmed: dict) -> None:
        """A `launching` row with no recovery entry (a database from before
        the entry was kept until split_pending): a known coin gets its split
        retried; any other goes back through the existence check."""
        for key, user_id, at, ticker, mint in self.book.launching():
            if mint in unconfirmed:
                continue
            coin = self.ledger.coin(mint)
            if coin is not None:
                self.book.record(key, user_id, at, "failed", code="split_pending", ticker=ticker, mint=mint)
                self.log("launching_recovered", tweet=key, mint=mint)
                continue
            unconfirmed[mint] = {"key": key, "user_id": user_id, "handle": "", "tweet_id": key,
                                 "at": at, "ticker": ticker}
            self.ledger.set_state(UNCONFIRMED_KEY, unconfirmed)
            self.log("launching_rechecked", tweet=key, mint=mint)

    def _finish(self, key: str, user_id: str, ts: int, ticker: str, mint: str) -> dict:
        """The split, then the record and the reply; a failed split leaves
        the coin `split_pending` for the next tick."""
        try:
            signature = self._split(mint)
        except Exception as exc:  # noqa: BLE001
            self.book.record(key, user_id, ts, "failed", code="split_pending", ticker=ticker, mint=mint)
            self.log("split_pending", tweet=key, mint=mint, reason=f"{type(exc).__name__}: {exc}")
            return {"tweet": key, "outcome": "failed", "code": "split_pending", "mint": mint}
        return self._announce(key, user_id, ts, ticker, mint, signature)

    def _announce(self, key: str, user_id: str, ts: int, ticker: str, mint: str, signature) -> dict:
        """The coin has its split: record it launched and reply."""
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

    def split_state(self, mint: str) -> str:
        """What the chain says of the coin's fee-sharing config: `absent`,
        `ours` (its rows are exactly `tag.split_rows()`), or `mismatch`."""
        address = enroll.sharing_config_address(mint)
        account = self.rpc.accounts([address])[0]
        if not account:
            return "absent"
        try:
            config = pump.decode_sharing_config(address, account)
        except pump.DecodeError:
            return "mismatch"
        ours = sorted((r.address, r.bps) for r in tag.split_rows())
        return "ours" if sorted(config.shareholders) == ours else "mismatch"

    def _retry_split(self, key: str, user_id: str, at: int, ticker: str, mint: str) -> dict:
        """A split that may have landed unconfirmed is never rebuilt blind:
        `create=True` cannot succeed twice. The config on chain decides."""
        try:
            state = self.split_state(mint)
        except Exception as exc:  # noqa: BLE001 -- unread: leave it pending for the next tick
            self.log("split_unread", tweet=key, mint=mint, reason=f"{type(exc).__name__}: {exc}")
            return {"tweet": key, "outcome": "failed", "code": "split_pending", "mint": mint}
        if state == "mismatch":
            if not self.dry_run:
                self.book.record(key, user_id, at, "failed", code="split_mismatch", ticker=ticker, mint=mint)
            self.log("SPLIT_MISMATCH", tweet=key, mint=mint,
                     reason="the coin's fee-sharing config does not hold the tag split; not retried, owner to act")
            return {"tweet": key, "outcome": "failed", "code": "split_mismatch", "mint": mint}
        if state == "ours":
            if self.dry_run:
                self.log("would_mark_launched", tweet=key, mint=mint)
                return {"tweet": key, "outcome": "simulated", "mint": mint}
            self.log("split_found", tweet=key, mint=mint)
            return self._announce(key, user_id, at, ticker, mint, None)
        if self.dry_run:
            checked = buyback.simulate(self.rpc, tag.split_message(mint, blockhash(self.rpc)))
            self.log("would_retry_split", tweet=key, mint=mint, err=checked.get("err"))
            return {"tweet": key, "outcome": "simulated", "mint": mint}
        return self._finish(key, user_id, at, ticker, mint)

    # -- money --

    def tick_money(self) -> dict:
        """Every money step, each on its own: one failing never stops the next.
        The site sees every credit (the first mirror) before anything burns."""
        out = {}
        for name, step in (("distribute", self._distribute), ("scan", self._scan),
                           ("reconcile", self.reconcile), ("mirror", self._mirror),
                           ("claims", self._claims), ("approved", self._pay_approved),
                           ("burn", self._burn), ("sweep", self._sweep),
                           ("settle", self.reconcile), ("mirror_after", self._mirror)):
            if name == "burn" and ("error" in (out.get("claims") or {}) or self._claims_backlog):
                # A claim still waiting in the queue (back after an error, or
                # past this tick's limit) must not see its credit burn first.
                out[name] = {"skipped": "claims are still waiting in the queue"}
                self.log("burn_skipped", reason=out[name]["skipped"])
                continue
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
            if row["kind"] == "credit":
                self.log("credit", user=row["user_id"], mint=row["mint"], lamports=row["lamports"])
            else:
                self.log(row["kind"], signature=row["signature"], lamports=row.get("lamports"),
                         reason=row.get("reason"))
        return rows

    def _status(self, xid: str, state: str, lamports: int, signature=None, reason=None) -> dict:
        status = {"state": state, "lamports": int(lamports), "signature": signature, "reason": reason,
                  "at": self._ts()}
        if not self.dry_run:
            self.kv.set_json(f"claim:status:{xid}", status)
        self.log("claim", user=xid, state=state, lamports=lamports, signature=signature, reason=reason)
        return status

    # -- paying out of the treasury, written ahead --

    def _pay_out(self, destination: str, amount: int, debits, kind: str, *,
                 holder: str | None = None) -> tuple[str | None, str | None]:
        """Send `amount` less the network fee from the treasury, with every
        `(user_id, lamports)` in `debits` written pending BEFORE the send.
        Returns `(signature, None)`, or `(None, why)` when nothing was sent."""
        recent, last_valid = latest_blockhash(self.rpc)
        message = transfer_message(legs.CHARLIE_PAYOUT_TREASURY, destination, amount - TX_FEE_LAMPORTS, recent)
        checked = buyback.simulate(self.rpc, message)
        if checked.get("err") is not None:
            return None, f"simulation: {checked.get('err')}"
        if self.dry_run:
            return None, "dry run"
        wire = self._sign("treasury", message)
        signature = wire_signature(wire)
        if self.ledger.signature_known(signature):
            # Same payer, destination, amount and blockhash: the very same
            # transaction. It lands once, so it must not stand for two debits.
            return None, "an identical payment is already in flight"
        # Any open hold is linked in the same write, and paid only once final.
        self.ledger.debit_pending(kind, debits, wallet=destination, signature=signature, at=self._ts(),
                                  last_valid=last_valid, held=holder)
        try:
            self.transmit(wire)
        except Exception as exc:  # noqa: BLE001 -- it may still land; reconcile decides
            self.log("send_uncertain", kind=kind, signature=signature, reason=f"{type(exc).__name__}: {exc}")
            return signature, None
        try:
            self.confirm(signature)
        except Exception as exc:  # noqa: BLE001
            self.log("confirm_uncertain", kind=kind, signature=signature, reason=f"{type(exc).__name__}: {exc}")
        return signature, None

    def _statuses(self, signatures: list[str]) -> dict:
        out = {}
        for start in range(0, len(signatures), STATUS_BATCH):
            batch = signatures[start:start + STATUS_BATCH]
            result = self.rpc.call("getSignatureStatuses", [batch, {"searchTransactionHistory": True}])
            values = list((result or {}).get("value") or [])
            values += [None] * (len(batch) - len(values))
            out.update(zip(batch, values))
        return out

    @staticmethod
    def _outcome(status) -> str | None:
        if status and status.get("err") is not None:
            return "failed"
        if status and status.get("confirmationStatus") in ("confirmed", "finalized"):
            return "final"
        return None

    def reconcile(self) -> list[dict]:
        """Settle every pending debit from the chain: confirmed becomes final;
        failed is deleted, which restores the credit. An unknown signature is
        deleted only on proof it can never land: the finalized block height
        is past its blockhash's `last_valid`, and a second status read, taken
        after that height, still does not know it. An unknown status alone
        never drops a debit. A burn stamps `last_burn_at` only once final."""
        pending = self.ledger.pending()
        if not pending:
            return []
        ts = self._ts()
        settled = []

        def settle(signature, outcome):
            self._settle(signature, pending[signature], outcome, ts)
            settled.append({"signature": signature, "kind": pending[signature]["kind"], "outcome": outcome})

        unknown = []
        for signature, status in self._statuses(list(pending)).items():
            outcome = self._outcome(status)
            if outcome:
                settle(signature, outcome)
            elif status is None and pending[signature]["last_valid"] is not None:
                unknown.append(signature)
        if unknown:
            height = self.rpc.call("getBlockHeight", [{"commitment": "finalized"}])
            expired = [s for s in unknown if isinstance(height, int) and height > pending[s]["last_valid"]]
            if expired:
                for signature, status in self._statuses(expired).items():
                    outcome = self._outcome(status) or ("expired" if status is None else None)
                    if outcome:
                        settle(signature, outcome)
        return settled

    def _settle(self, signature: str, entry: dict, outcome: str, ts: int) -> None:
        if outcome == "final":
            self.ledger.finalize(signature)
        else:
            self.ledger.drop_pending(signature)
        self.log("settled", kind=entry["kind"], signature=signature, outcome=outcome)
        if entry["kind"] == "burn":
            if outcome == "final":
                self.ledger.set_state(LAST_BURN_KEY, ts)
            return
        for user_id, lamports, _wallet in entry["rows"]:
            if outcome == "final":
                self._status(user_id, "paid", lamports - TX_FEE_LAMPORTS, signature)
            else:
                self._status(user_id, "refused", 0, signature, reason=PAYMENT_FAILED)

    # -- claims --

    def _claims(self) -> list[dict]:
        if self.dry_run:
            self.log("would_process_claims")
            return []
        done = []
        self._claims_backlog = True     # until the queue is seen empty
        for _ in range(MAX_CLAIMS_PER_TICK):
            item = self.kv.pop(QUEUE_KEY)
            if item is None:
                self._claims_backlog = False
                break
            fields = claim_session.verify_claim(item, self.claim_secret, now=self._ts())
            if fields is None:
                self.log("claim_dropped", reason="unsigned, altered or stale")
                continue
            try:
                done.append(self.claim(fields["xid"], fields["wallet"]))
            except Exception:
                # The error stops this tick's burn (tick_money). A claim whose
                # payment is already written ahead is done: reconcile settles
                # it, and running it again would overwrite its status. Any
                # other goes back for the next tick, a few times at most; a
                # dropped claim leaves the credit untouched to claim again.
                self._requeue(fields["xid"], item)
                raise
        return done

    def _requeue(self, xid: str, item) -> None:
        if self.ledger.has_pending(xid):
            self.log("claim_in_flight", user=xid)
            return
        key = json.dumps(item, sort_keys=True)
        tries = self._claim_failures[key] = self._claim_failures.get(key, 0) + 1
        if tries >= CLAIM_RETRIES:
            self._claim_failures.pop(key, None)
            self._status(xid, "refused", 0, reason="the claim could not be processed; claim again")
            return
        try:
            self.kv.push(QUEUE_KEY, item)
        except Exception as exc:  # noqa: BLE001 -- the credit is untouched; they claim again
            self.log("claim_lost", user=xid, reason=f"{type(exc).__name__}: {exc}")

    def claim(self, xid: str, wallet: str, *, caps: bool = True, most: int | None = None) -> dict:
        """Decide and, when the decision is to pay, pay one claim (written ahead)."""
        return self._claim(xid, wallet, caps=caps, most=most)[0]

    def _claim(self, xid: str, wallet: str, *, caps: bool, most: int | None) -> tuple[dict, bool]:
        """`(status, refused_outright)`. `most` caps the amount (an approved
        hold pays at most what was held)."""
        if not xid or not valid_wallet(wallet):
            return self._status(xid or "unknown", "refused", 0, reason="that wallet cannot receive claims"), True
        if self.ledger.has_pending(xid):
            return self._status(xid, "queued", 0, reason="a payment is still confirming"), False
        ts = self._ts()
        bound, pending, ready_at = self.ledger.wallet(xid)
        balance = self.ledger.balance(xid)
        if most is not None:
            balance = min(balance, int(most))
        decision = tag_ledger.claim_decision(
            balance=balance, wallet=wallet, bound=bound, pending=pending, ready_at=ready_at,
            now=ts, recipient_lamports=self.rpc.balance(wallet),
            treasury_lamports=self.rpc.balance(legs.CHARLIE_PAYOUT_TREASURY),
            paid_today=self.ledger.claimed_since(ts - tag_ledger.DAY), caps=caps)
        self.ledger.apply_wallet(xid, decision)
        if decision.state == "held":
            self.ledger.hold(xid, decision.lamports, wallet, ts)
            return self._status(xid, "held", decision.lamports, reason=decision.reason), False
        if decision.state != "pay":
            return self._status(xid, decision.state, decision.lamports, reason=decision.reason), True
        signature, why = self._pay_out(wallet, decision.lamports, [(xid, decision.lamports)], "claim",
                                       holder=xid)
        if signature is None:
            return self._status(xid, "refused", 0,
                                reason=f"the payment could not be made ({why}); claim again"), False
        status = self._status(xid, "queued", decision.lamports - TX_FEE_LAMPORTS, signature, reason="sending")
        for row in self.reconcile():
            if row["signature"] == signature:
                return self.kv.get_json(f"claim:status:{xid}") or status, False
        return status, False

    def _pay_approved(self) -> list[dict]:
        """Held claims the owner approved, paid without the caps through the
        same written-ahead path. `--approve-held` only marks them."""
        if self.dry_run:
            for user_id, wallet, lamports in self.ledger.approved():
                self.log("would_pay_approved", user=user_id, wallet=wallet, lamports=lamports)
            return []
        done = []
        for user_id, wallet, lamports in self.ledger.approved():
            status, refused_outright = self._claim(user_id, wallet, caps=False, most=lamports)
            if refused_outright:
                self.ledger.drop_held(user_id)
            done.append(status)
        return done

    # -- the burn and the top-up --

    def _burn(self) -> dict | None:
        ts = self._ts()
        if self.ledger.pending_burn():
            return None
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
        signature, why = self._pay_out(legs.TOLL_DESTINATION, total, list(owed.items()), "burn")
        if signature is None:
            self.log("would_burn" if self.dry_run else "burn_refused", lamports=total, reason=why)
            return {"state": "simulated" if self.dry_run else "refused", "lamports": total}
        self.log("burn", lamports=total, users=len(owed), signature=signature)
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

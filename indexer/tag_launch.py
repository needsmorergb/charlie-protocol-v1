"""Launch a pump coin from an X tag: `@CharlieSlugSOL launch <Name> $TICKER` + image.

The tweet's author is the only beneficiary. Charlie's launch wallet
(`legs.CHARLIE_LAUNCH_WALLET`) signs and pays for both transactions, so it is
the coin's pump creator; the split it sets sends the requester's share to the
payout treasury, which credits it to the author's numeric X user ID.

The split, fixed here and nowhere else:

    protocol row        legs.TOLL_BPS to legs.TOLL_DESTINATION
    incinerator         the floor, 1% of the rest
    Charlie OPS         legs.CHARLIE_OPS_PERCENT of the rest
    payout treasury     everything left, credited to the requester

What decides whether a tag becomes a coin, in the order it is checked:

1. `parse`: a strict pattern, never a language model. Anything that does not
   match is ignored, so no text in a tweet can steer the launcher.
2. `tweet_refusal`: an original post (no reply, quote or retweet), first
   version only, recent, with an image.
3. `account_refusal`: the author's X account is old enough, followed enough
   (or verified), has posted, has a profile image, is public, and is not a
   known bot or parody.
4. `content_refusal`: the ticker and name are not reserved or impersonating.
5. `Book.limit_refusal`: one launch per account per day and three per thirty
   days; repeat refusals and bans are ignored.
6. `wallet_refusal`: the launch wallet holds its floor plus one launch. The
   pace is set by the wallet, not a calendar: OPS refills it when coins earn,
   and launching pauses when they do not. `DAILY_CEILING` is only a backstop.

Image moderation is not in this module; the caller must pass the image
through it before pinning metadata.
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone

from . import enroll, launch, legs
from .ed25519 import Keypair
from .message import signed_transaction

# -- the numbers (chosen 2026-09-22; see the project notes for the reasoning) ----

MIN_ACCOUNT_AGE_DAYS = 90
MIN_FOLLOWERS = 50
MIN_POSTS = 20
MAX_TWEET_AGE_SECONDS = 600

PER_DAY = 1
PER_THIRTY_DAYS = 3
REFUSALS_BEFORE_TIMEOUT = 3
REFUSAL_TIMEOUT_DAYS = 30

LAUNCH_COST_LAMPORTS = 30_000_000        # ~0.03 SOL: create rent + split rent + fees
WALLET_FLOOR_LAMPORTS = 150_000_000      # launching pauses below this plus one launch
DAILY_CEILING = 50

# Accounts the launcher never acts for, whatever they post: AI and launch bots
# that can be talked into tagging (Grok set off 17 launches on Bankr, 2025).
IGNORED_HANDLES = frozenset({
    "grok", "bankrbot", "clanker", "aixbt_agent", "truth_terminal", "launchcoin",
    "believeapp", "pumpdotfun", "chatgptapp", "perplexity_ai",
})

# Tickers no requester may take. Active Charlie coins' tickers are added by
# the caller (`content_refusal(..., taken=...)`).
RESERVED_TICKERS = frozenset({
    "CHARLIE", "SOL", "WSOL", "BTC", "WBTC", "ETH", "WETH", "USDC", "USDT", "USD",
    "PUMP", "BONK", "WIF", "JUP", "PYTH", "JTO", "RAY", "TRUMP", "MELANIA", "PENGU",
    "FARTCOIN", "POPCAT", "MEW", "GOAT", "USELESS", "SPX", "AI16Z", "XRP", "DOGE",
    "SHIB", "PEPE", "BNB", "TON", "ADA", "HYPE", "SUI", "LINK", "X", "GROK",
})

# Names a coin may not be, or be within two edits of, once lowercased and
# stripped to letters and digits. People, brands and projects a coin could be
# mistaken for. The caller adds more (`protected=`).
PROTECTED_NAMES = frozenset({
    "charlie", "charlieprotocol", "charlieslug", "charlieslugsol", "pumpfun", "pump", "solana", "bitcoin", "ethereum",
    "coinbase", "binance", "phantom", "jupiter", "raydium", "tether", "circle",
    "elonmusk", "elon", "tesla", "spacex", "twitter", "xai", "grok", "openai",
    "anthropic", "claude", "google", "apple", "microsoft", "amazon", "nvidia", "meta",
    "donaldtrump", "trump", "melania", "barron", "bidenjoe", "biden", "kamala",
    "obama", "vitalik", "cz", "saylor", "blackrock", "whitehouse", "federalreserve",
})

SITE = "https://charlieprotocol.fun"

# Charlie's X account, the one a tag must address.
BOT_HANDLE = "CharlieSlugSOL"


class TagRefused(ValueError):
    """A tag that does not become a coin. `code` is stable for the log."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TagRequest:
    name: str
    ticker: str


# -- 1. the words ----------------------------------------------------------------

_MEDIA_LINKS = re.compile(r"(?:\s+https://t\.co/\w+)+\s*$")
_TAG = re.compile(
    r"^@(?P<bot>[A-Za-z0-9_]{1,15})\s+launch\s+"
    r"(?P<name>[A-Za-z0-9](?:[A-Za-z0-9 ]{0,30}[A-Za-z0-9])?)\s+"
    r"\$(?P<ticker>[A-Za-z0-9]{2,10})\s*$",
    re.IGNORECASE,
)


def parse(text: str, bot_handle: str = BOT_HANDLE) -> TagRequest | None:
    """The request in a tweet's text, or None when the text is not exactly a
    launch tag addressed to `bot_handle`. X appends a t.co link for attached
    media; those are dropped first. Nothing else is tolerated."""
    body = _MEDIA_LINKS.sub("", unicodedata.normalize("NFKC", text or "")).strip()
    match = _TAG.match(body)
    if match is None or match["bot"].lower() != bot_handle.lstrip("@").lower():
        return None
    name = " ".join(match["name"].split())
    return TagRequest(name=name, ticker=match["ticker"].upper())


# -- 2. the tweet ------------------------------------------------------------------


def _when(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def dedupe_key(tweet: dict) -> str:
    """The first ID in the tweet's edit history, so an edited tweet is the
    same request as its original and can never launch twice."""
    history = tweet.get("edit_history_tweet_ids") or []
    return str(history[0] if history else tweet["id"])


PHOTO_TYPES = ("photo",)   # the bot pins stills only (tag_bot.image_of)


def tweet_refusal(tweet: dict, now: datetime, media: dict | None = None) -> TagRefused | None:
    """X API v2 tweet object (with `created_at`, `referenced_tweets`,
    `attachments`, `edit_history_tweet_ids`). With `media` (the includes,
    keyed by media_key), at least one attached item must be a photo: media
    keys alone are not enough."""
    if tweet.get("referenced_tweets"):
        return TagRefused("not_original", "Only an original post can launch, not a reply, quote or repost.")
    if dedupe_key(tweet) != str(tweet["id"]):
        return TagRefused("edited", "An edited post cannot launch; the first version is the request.")
    age = (now - _when(tweet["created_at"])).total_seconds()
    if age > MAX_TWEET_AGE_SECONDS:
        return TagRefused("stale", "The post is too old to act on.")
    keys = (tweet.get("attachments") or {}).get("media_keys") or []
    if not keys:
        return TagRefused("no_image", "Attach the coin's image to the post.")
    if media is not None and not any((media.get(k) or {}).get("type") in PHOTO_TYPES for k in keys):
        return TagRefused("no_image", "Attach the coin's image to the post.")
    return None


# -- 3. the account ----------------------------------------------------------------


def account_refusal(user: dict, now: datetime, *, bot_id: str = "") -> TagRefused | None:
    """X API v2 user object (with `created_at`, `public_metrics`,
    `verified_type`, `protected`, `profile_image_url`, `withheld`)."""
    handle = (user.get("username") or "").lower()
    if str(user.get("id")) == str(bot_id) or handle in IGNORED_HANDLES:
        return TagRefused("ignored", "This account is not served.")
    if user.get("parody") or user.get("is_parody"):
        return TagRefused("parody", "Parody accounts cannot launch.")
    if user.get("protected"):
        return TagRefused("protected", "Protected accounts cannot launch.")
    if user.get("withheld"):
        return TagRefused("withheld", "This account is withheld.")
    avatar = user.get("profile_image_url")
    if not isinstance(avatar, str) or not avatar.strip() or "default_profile_images" in avatar:
        return TagRefused("no_avatar", "Set a profile image before launching.")
    if (now - _when(user["created_at"])).days < MIN_ACCOUNT_AGE_DAYS:
        return TagRefused("too_new", f"The account must be at least {MIN_ACCOUNT_AGE_DAYS} days old.")
    metrics = user.get("public_metrics") or {}
    verified = (user.get("verified_type") or "none") in ("blue", "business", "government")
    if not verified and metrics.get("followers_count", 0) < MIN_FOLLOWERS:
        return TagRefused("few_followers", f"The account needs {MIN_FOLLOWERS} followers or a verified badge.")
    if metrics.get("tweet_count", 0) < MIN_POSTS:
        return TagRefused("few_posts", f"The account needs at least {MIN_POSTS} posts.")
    return None


# -- 4. the words, again ---------------------------------------------------------


def _squash(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def _within_two(a: str, b: str) -> bool:
    if abs(len(a) - len(b)) > 2:
        return False
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        if min(current) > 2:
            return False
        previous = current
    return previous[-1] <= 2


def content_refusal(request: TagRequest, *, taken=(), protected=()) -> TagRefused | None:
    """Reserved and active tickers, and names that impersonate. Short names
    (four letters or fewer) must match exactly to be refused; two edits on a
    short word would refuse half the dictionary."""
    if request.ticker in RESERVED_TICKERS or request.ticker in {t.upper() for t in taken}:
        return TagRefused("ticker_taken", f"${request.ticker} is taken. Pick another ticker.")
    words = [w for w in (*PROTECTED_NAMES, *(_squash(p) for p in protected)) if w]
    for value in (_squash(request.name), _squash(request.ticker)):   # $OPENAI impersonates as well
        for word in words:
            close = value == word if len(word) <= 4 or len(value) <= 4 else _within_two(value, word)
            if close or (len(word) >= 5 and word in value):
                return TagRefused("impersonation", "That name is too close to a real person, brand or project.")
    return None


# -- 5. the account's history ------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tag_requests (
    tweet_key   TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    at          INTEGER NOT NULL,
    outcome     TEXT NOT NULL,          -- launched | refused | failed | launching
    code        TEXT,
    ticker      TEXT,
    mint        TEXT
);
CREATE INDEX IF NOT EXISTS tag_requests_user ON tag_requests (user_id, at);
CREATE TABLE IF NOT EXISTS tag_bans (
    user_id     TEXT PRIMARY KEY,
    at          INTEGER NOT NULL,
    reason      TEXT NOT NULL
);
"""

DAY = 86_400


class Book:
    """Every tag the launcher acted on, by the author's numeric X user ID.
    Handles change; IDs do not."""

    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path)
        self.db.executescript(_SCHEMA)

    def seen(self, tweet_key: str) -> bool:
        return self.db.execute("SELECT 1 FROM tag_requests WHERE tweet_key = ?", (tweet_key,)).fetchone() is not None

    def _count(self, user_id: str, outcome: str, since: int) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM tag_requests WHERE user_id = ? AND outcome = ? AND at > ?",
            (user_id, outcome, since)).fetchone()[0]

    # A coin counts toward the limits once it exists or may: launched, being
    # launched, created with its split still pending, or a create not yet
    # known to have failed. A split that keeps failing must not let one
    # account create coin after coin.
    def _launches(self, user_id: str, since: int) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM tag_requests WHERE user_id = ? AND at > ? "
            "AND (outcome IN ('launched', 'launching') OR code IN ('split_pending', 'split_mismatch', 'create_failed'))",
            (user_id, since)).fetchone()[0]

    def launched_since(self, since: int) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM tag_requests WHERE at > ? "
            "AND (outcome IN ('launched', 'launching') OR code IN ('split_pending', 'split_mismatch', 'create_failed'))",
            (since,)).fetchone()[0]

    def limit_refusal(self, user_id: str, now: int) -> TagRefused | None:
        if self.db.execute("SELECT 1 FROM tag_bans WHERE user_id = ?", (user_id,)).fetchone():
            return TagRefused("banned", "This account is not served.")
        if self._count(user_id, "refused", now - REFUSAL_TIMEOUT_DAYS * DAY) >= REFUSALS_BEFORE_TIMEOUT:
            return TagRefused("timeout", "Too many refused requests; try again later.")
        if self._launches(user_id, now - DAY) >= PER_DAY:
            return TagRefused("daily_limit", f"{PER_DAY} launch per account per day.")
        if self._launches(user_id, now - 30 * DAY) >= PER_THIRTY_DAYS:
            return TagRefused("monthly_limit", f"{PER_THIRTY_DAYS} launches per account per 30 days.")
        if self.launched_since(now - DAY) >= DAILY_CEILING:
            return TagRefused("ceiling", "Launching is paused for today.")
        return None

    def record(self, tweet_key: str, user_id: str, now: int, outcome: str, *,
               code: str | None = None, ticker: str | None = None, mint: str | None = None) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO tag_requests VALUES (?, ?, ?, ?, ?, ?, ?)",
                (tweet_key, user_id, now, outcome, code, ticker, mint))

    def entry(self, tweet_key: str) -> dict | None:
        """The tweet's current row: `{"outcome", "code", "ticker", "mint"}`, or None."""
        row = self.db.execute("SELECT outcome, code, ticker, mint FROM tag_requests WHERE tweet_key = ?",
                              (tweet_key,)).fetchone()
        return dict(zip(("outcome", "code", "ticker", "mint"), row)) if row else None

    def taken_tickers(self) -> set[str]:
        """Tickers of coins that exist or may: launched, launching, created
        with the split pending, or a create not yet known to have failed."""
        return {r[0] for r in self.db.execute(
            "SELECT DISTINCT ticker FROM tag_requests WHERE ticker IS NOT NULL AND mint IS NOT NULL "
            "AND (outcome IN ('launched', 'launching') OR code IN ('split_pending', 'split_mismatch', 'create_failed'))")}

    def launching(self) -> list[tuple]:
        """`(tweet_key, user_id, at, ticker, mint)` for rows still `launching`."""
        return self.db.execute(
            "SELECT tweet_key, user_id, at, ticker, mint FROM tag_requests "
            "WHERE outcome = 'launching' AND mint IS NOT NULL ORDER BY at").fetchall()

    def pending_splits(self) -> list[tuple]:
        """`(tweet_key, user_id, at, ticker, mint)` for coins whose create
        landed and whose split did not."""
        return self.db.execute(
            "SELECT tweet_key, user_id, at, ticker, mint FROM tag_requests "
            "WHERE outcome = 'failed' AND code = 'split_pending' AND mint IS NOT NULL ORDER BY at").fetchall()

    def ban(self, user_id: str, now: int, reason: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO tag_bans VALUES (?, ?, ?)", (user_id, now, reason))


# -- 6. the wallet -----------------------------------------------------------------


def wallet_refusal(balance_lamports: int) -> TagRefused | None:
    if balance_lamports < WALLET_FLOOR_LAMPORTS + LAUNCH_COST_LAMPORTS:
        return TagRefused("wallet_low", "Launching is paused until the launch wallet is refilled.")
    return None


# -- the split and the transactions -------------------------------------------------


def split_rows(rate: int | None = None) -> tuple[enroll.Share, ...]:
    """The one split every tag-launched coin gets. Refuses to exist when a
    wallet it needs is unset or the rows fail the tag-launch leg rule."""
    rate = legs.TOLL_BPS if rate is None else rate
    if legs.TOLL_DESTINATION is None or legs.CHARLIE_OPS_DESTINATION is None:
        raise launch.LaunchError("Tag launches are closed: a protocol wallet is not set.")
    rows = [
        enroll.Share(legs.TOLL_DESTINATION, rate),
        enroll.Share(legs.SOL_BURN_INCINERATOR, legs.min_incinerator_bps(rate)),
        enroll.Share(legs.CHARLIE_OPS_DESTINATION, legs.charlie_ops_bps(rate)),
    ]
    rows.append(enroll.Share(legs.CHARLIE_PAYOUT_TREASURY, 10_000 - sum(r.bps for r in rows)))
    validated = enroll.validate(rows)
    enroll.require_toll(validated)
    rest = [(r.address, r.bps) for r in validated if r.address != legs.TOLL_DESTINATION]
    missing = legs.tag_launch_missing_legs(rest, rate)
    if missing:
        raise launch.LaunchError(f"The tag-launch split lacks {', '.join(missing)}.")
    return validated


@dataclass(frozen=True)
class Built:
    mint: Keypair
    create: bytes        # message: pump `create`; signers launch wallet + mint
    split: bytes         # message: fee config + split; signer launch wallet


def build(request: TagRequest, uri: str, blockhash: str, *,
          launcher: str | None = None, mint: Keypair | None = None) -> Built:
    """Both messages. A random mint, never the /launch door's 1nc1n pool.
    The split message reuses `blockhash`; `split_message` rebuilds it with a
    fresh one once the coin exists."""
    launcher = launcher or legs.CHARLIE_LAUNCH_WALLET
    mint = mint or launch.new_mint()
    meta = launch.validate_metadata(request.name, request.ticker, uri)
    shares = split_rows()
    create = launch.create_message(mint.address, launcher, meta, blockhash)
    return Built(mint, create, split_message(mint.address, blockhash, launcher=launcher, shares=shares))


def split_message(mint: str, blockhash: str, *, launcher: str | None = None, shares=None) -> bytes:
    return enroll.enrollment_message(mint, launcher or legs.CHARLIE_LAUNCH_WALLET,
                                     shares or split_rows(), blockhash, create=True)


def sign_create(built: Built, wallet: Keypair) -> bytes:
    """The create transaction with both signatures, in the message's order."""
    signers = launch.signer_addresses(built.create)
    keys = {wallet.address: wallet, built.mint.address: built.mint}
    if set(signers) != set(keys):
        raise launch.LaunchError(f"the create message expects {signers}, not {sorted(keys)}")
    return signed_transaction(built.create, [keys[a].sign(built.create) for a in signers])


def sign_split(message: bytes, wallet: Keypair) -> bytes:
    signers = launch.signer_addresses(message)
    if signers != [wallet.address]:
        raise launch.LaunchError(f"the split message expects {signers}, not {wallet.address}")
    return signed_transaction(message, [wallet.sign(message)])


# -- what the coin and the reply say ---------------------------------------------------


def metadata_fields(request: TagRequest, handle: str, tweet_id: str, mint: str) -> dict[str, str]:
    """Fields for pump's metadata pin (`api.launch.pin_metadata`). The website is
    the coin's own page, so the mint is chosen before anything is pinned."""
    return {
        "name": request.name,
        "symbol": request.ticker,
        "description": (f"Requested by @{handle} on X and launched by Charlie Protocol. "
                        "Not affiliated with any person or brand."),
        "twitter": f"https://x.com/{handle}/status/{tweet_id}",
        "website": f"{SITE}/coin/{mint}",
        "showName": "true",
    }


def reply_text(request: TagRequest, mint: str) -> str:
    """The only thing the bot says on success. Nothing from the tweet but
    the ticker reaches it. One link only, the coin page; the claim is
    reached from that page rather than linked here."""
    return (f"${request.ticker} is live \N{SNAIL}\n\n"
            f"CA: {mint}\n\n"
            "your share of its creator fees is yours to claim. sign in with X on the "
            "coin page. after 7 days, unclaimed fees go to the $CHARLIE buy-and-burn.\n\n"
            f"{SITE}/coin/{mint}")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def epoch(moment: datetime) -> int:
    return int(moment.timestamp())

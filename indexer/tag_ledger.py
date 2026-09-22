"""What each X-tag requester is owed, read off the payout treasury's history.

Every tag-launched coin's split pays one row to `legs.CHARLIE_PAYOUT_TREASURY`.
That wallet is shared, so the ledger attributes each SOL inflow to a coin, and
each coin to the numeric X user ID that requested it (`tag_coins`, written at
launch). An inflow counts when its transaction runs pump's
`distribute_creator_fees` for a known tag mint; the amount is the treasury's
own balance change in that transaction. Anything else that reaches the
treasury is nobody's credit.

Credits leave by two roads, both written as debits: a claim to the wallet the
requester signs in with, or the burn once a credit is seven days old
(`burnable`; debits use up the oldest credits first). `claim_decision` is the
whole claim policy and does no I/O.

The tables live in the same SQLite file as `tag_launch.Book`.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from . import distribute, legs
from .base58 import decode

# -- the numbers ----------------------------------------------------------------------

DAY = 86_400
BURN_AFTER_SECONDS = 7 * DAY               # unclaimed credit burns after this
WALLET_CHANGE_DELAY_SECONDS = 72 * 3_600   # a bound wallet changes only after this
MIN_CLAIM_LAMPORTS = 1_000_000             # 0.001 SOL; below this a claim is refused
RENT_EXEMPT_MIN_LAMPORTS = 890_880         # a 0-data system account's rent-exempt minimum
TX_FEE_LAMPORTS = 5_000                    # one signature
TREASURY_RESERVE_LAMPORTS = RENT_EXEMPT_MIN_LAMPORTS + TX_FEE_LAMPORTS
PER_CLAIM_MAX_LAMPORTS = 5_000_000_000     # 5 SOL; larger claims are held for the owner
PER_DAY_MAX_LAMPORTS = 20_000_000_000      # 20 SOL of claims per day, all requesters together
SCAN_PAGE = 1_000                          # signatures per getSignaturesForAddress page
SCAN_MAX_PAGES = 20                        # one scan reads at most this many pages

PUMP_PROGRAM = distribute.PUMP_PROGRAM
DISTRIBUTE_DISCRIMINATOR = distribute.DISTRIBUTE_CREATOR_FEES

CURSOR_KEY = "treasury_cursor"

# Wallets a claim may never pay: Charlie's own.
CHARLIE_WALLETS = frozenset(a for a in (
    legs.TOLL_DESTINATION, legs.CHARLIE_OPS_DESTINATION, legs.CHARLIE_PAYOUT_TREASURY,
    legs.CHARLIE_LAUNCH_WALLET, legs.SOL_BURN_INCINERATOR,
) if a)


# -- one transaction ----------------------------------------------------------------------


def _key(entry) -> str:
    return entry.get("pubkey", "") if isinstance(entry, dict) else str(entry)


def _is_distribute(ix: dict) -> bool:
    if ix.get("programId") != PUMP_PROGRAM or not ix.get("accounts") or not isinstance(ix.get("data"), str):
        return False
    try:
        return decode(ix["data"])[:8] == DISTRIBUTE_DISCRIMINATOR
    except ValueError:
        return False


def inflow(tx: dict | None, treasury: str, known=None) -> list[tuple[str, int]]:
    """`[(mint, lamports)]` the treasury received in a jsonParsed transaction
    through pump's distribute instruction, or `[]`.

    The mint is account 0 of the distribute instruction; the amount is the
    treasury's post minus pre balance, and only a gain counts. A failed
    transaction, one with no distribute instruction, one distributing more
    than one mint (the gain could not be split between them), or a mint not
    in `known` (when given) yields `[]`.
    """
    if not tx or (tx.get("meta") or {}).get("err") is not None:
        return []
    message = (tx.get("transaction") or {}).get("message") or {}
    mints = {ix["accounts"][0] for ix in message.get("instructions") or [] if _is_distribute(ix)}
    if len(mints) != 1:
        return []
    mint = mints.pop()
    if known is not None and mint not in known:
        return []
    keys = [_key(k) for k in message.get("accountKeys") or []]
    if treasury not in keys:
        return []
    at = keys.index(treasury)
    meta = tx["meta"]
    try:
        gain = int(meta["postBalances"][at]) - int(meta["preBalances"][at])
    except (KeyError, IndexError, TypeError, ValueError):
        return []
    return [(mint, gain)] if gain > 0 else []


# -- the claim policy --------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    state: str                    # pay | held | refused
    lamports: int
    reason: str | None = None
    bind: str | None = None       # the wallet to bind now
    pending: str | None = None    # a new wallet to start waiting on
    ready_at: int | None = None   # when `pending` becomes usable


def claim_decision(*, balance: int, wallet: str, bound: str | None, pending: str | None,
                   ready_at: int | None, now: int, recipient_lamports: int,
                   treasury_lamports: int, paid_today: int, caps: bool = True) -> Decision:
    """What to do with one claim. Pure: every number is passed in.

    The first wallet binds. A different wallet waits WALLET_CHANGE_DELAY
    from the request that names it (a new different wallet restarts the
    wait), then binds. The amount is the whole balance. A claim below
    MIN_CLAIM, or one that would leave a new recipient below rent exemption,
    is refused. One the treasury cannot pay while keeping its reserve, above
    PER_CLAIM_MAX, or past PER_DAY_MAX today, is held for the owner, who
    pays it with a command (`caps=False` skips the two caps).
    """
    bind = None
    if wallet in CHARLIE_WALLETS:
        return Decision("refused", 0, "that wallet cannot receive claims")
    if bound is None or (pending == wallet and ready_at is not None and ready_at <= now):
        bind = wallet
    elif wallet != bound:
        if pending == wallet:
            return Decision("refused", 0, "the new wallet is still waiting", pending=wallet, ready_at=ready_at)
        return Decision("refused", 0, "a new wallet waits 72 hours before it can receive a claim",
                        pending=wallet, ready_at=now + WALLET_CHANGE_DELAY_SECONDS)
    amount = balance
    if amount < MIN_CLAIM_LAMPORTS:
        return Decision("refused", 0, "nothing to claim yet", bind=bind)
    if recipient_lamports + amount < RENT_EXEMPT_MIN_LAMPORTS:
        return Decision("refused", 0, "the amount would not keep a new wallet open", bind=bind)
    if treasury_lamports - amount < TREASURY_RESERVE_LAMPORTS:
        return Decision("held", amount, "the treasury cannot cover this claim yet", bind=bind)
    if caps and amount > PER_CLAIM_MAX_LAMPORTS:
        return Decision("held", amount, "large claims are paid by hand", bind=bind)
    if caps and paid_today + amount > PER_DAY_MAX_LAMPORTS:
        return Decision("held", amount, "the daily claim limit is reached", bind=bind)
    return Decision("pay", amount, bind=bind)


# -- the ledger ----------------------------------------------------------------------------


class Ledger:
    """Credits and debits by X user ID, in the Book's SQLite file."""

    def __init__(self, db: sqlite3.Connection | str = ":memory:"):
        self.db = sqlite3.connect(db) if isinstance(db, str) else db
        self.db.executescript("""
CREATE TABLE IF NOT EXISTS tag_coins (
    mint        TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    handle      TEXT NOT NULL,
    tweet_id    TEXT NOT NULL,
    at          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS tag_coins_user ON tag_coins (user_id);
CREATE TABLE IF NOT EXISTS tag_credits (
    signature   TEXT NOT NULL,
    mint        TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    lamports    INTEGER NOT NULL,
    at          INTEGER NOT NULL,
    PRIMARY KEY (signature, mint)
);
CREATE INDEX IF NOT EXISTS tag_credits_user ON tag_credits (user_id, at);
CREATE TABLE IF NOT EXISTS tag_debits (
    id          INTEGER PRIMARY KEY,
    user_id     TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- claim | burn
    lamports    INTEGER NOT NULL,
    wallet      TEXT,
    signature   TEXT,
    at          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS tag_debits_user ON tag_debits (user_id, at);
CREATE TABLE IF NOT EXISTS tag_wallets (
    user_id     TEXT PRIMARY KEY,
    wallet      TEXT,
    pending     TEXT,
    ready_at    INTEGER
);
CREATE TABLE IF NOT EXISTS tag_state (
    key         TEXT PRIMARY KEY,
    value       TEXT
);
""")

    # -- state --

    def state(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM tag_state WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_state(self, key: str, value) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO tag_state VALUES (?, ?)", (key, json.dumps(value)))

    # -- coins --

    def add_coin(self, mint: str, user_id: str, handle: str, tweet_id: str, at: int) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO tag_coins VALUES (?, ?, ?, ?, ?)",
                            (mint, str(user_id), handle, str(tweet_id), at))

    def coin(self, mint: str) -> dict | None:
        row = self.db.execute("SELECT mint, user_id, handle, tweet_id, at FROM tag_coins WHERE mint = ?",
                              (mint,)).fetchone()
        return dict(zip(("mint", "user_id", "handle", "tweet_id", "at"), row)) if row else None

    def tag_mints(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT mint FROM tag_coins ORDER BY at, mint")]

    def requesters(self) -> dict[str, str]:
        """user_id -> the handle of their latest coin."""
        return {r[0]: r[1] for r in self.db.execute(
            "SELECT user_id, handle FROM tag_coins ORDER BY at, mint")}

    # -- credits --

    def credit(self, signature: str, mint: str, lamports: int, at: int) -> bool:
        """One credit per (signature, mint); False when it is already there
        or the mint is not a tag coin."""
        coin = self.coin(mint)
        if coin is None or lamports <= 0:
            return False
        with self.db:
            cur = self.db.execute("INSERT OR IGNORE INTO tag_credits VALUES (?, ?, ?, ?, ?)",
                                  (signature, mint, coin["user_id"], int(lamports), at))
        return cur.rowcount == 1

    def scan(self, rpc, treasury: str, *, now: int = 0) -> list[dict]:
        """Walk the treasury's signatures newer than the saved cursor, oldest
        first, and credit each tag-mint inflow once. The cursor moves past a
        signature only once its transaction was read; one the node cannot
        return yet stops the walk, to be read next time."""
        cursor = self.state(CURSOR_KEY)
        entries, before = [], None
        for _ in range(SCAN_MAX_PAGES):
            page = rpc.signatures_for_address(treasury, before=before, until=cursor, limit=SCAN_PAGE)
            entries.extend(page)
            if len(page) < SCAN_PAGE:
                break
            before = page[-1]["signature"]
        known = set(self.tag_mints())
        credited = []
        for entry in reversed(entries):
            signature = entry["signature"]
            if entry.get("err") is None:
                tx = rpc.transaction(signature)
                if tx is None:
                    break
                at = int(tx.get("blockTime") or entry.get("blockTime") or now)
                for mint, lamports in inflow(tx, treasury, known):
                    if self.credit(signature, mint, lamports, at):
                        credited.append({"signature": signature, "mint": mint, "lamports": lamports,
                                         "user_id": self.coin(mint)["user_id"], "at": at})
            self.set_state(CURSOR_KEY, signature)
        return credited

    # -- debits --

    def debit(self, user_id: str, kind: str, lamports: int, *, wallet: str | None = None,
              signature: str | None = None, at: int) -> None:
        if kind not in ("claim", "burn"):
            raise ValueError(f"unknown debit kind {kind!r}")
        with self.db:
            self.db.execute(
                "INSERT INTO tag_debits (user_id, kind, lamports, wallet, signature, at) VALUES (?, ?, ?, ?, ?, ?)",
                (str(user_id), kind, int(lamports), wallet, signature, at))

    def _credited(self, user_id: str) -> int:
        return self.db.execute("SELECT COALESCE(SUM(lamports), 0) FROM tag_credits WHERE user_id = ?",
                               (str(user_id),)).fetchone()[0]

    def _debited(self, user_id: str) -> int:
        return self.db.execute("SELECT COALESCE(SUM(lamports), 0) FROM tag_debits WHERE user_id = ?",
                               (str(user_id),)).fetchone()[0]

    def balance(self, user_id: str) -> int:
        return self._credited(user_id) - self._debited(user_id)

    def burnable(self, user_id: str, now: int) -> int:
        """Credits older than BURN_AFTER not yet covered by debits (debits
        use up the oldest credits first)."""
        old = self.db.execute(
            "SELECT COALESCE(SUM(lamports), 0) FROM tag_credits WHERE user_id = ? AND at <= ?",
            (str(user_id), now - BURN_AFTER_SECONDS)).fetchone()[0]
        return max(0, old - self._debited(user_id))

    def burns_at(self, user_id: str) -> int | None:
        """When the oldest credit not yet covered by debits burns."""
        covered = self._debited(user_id)
        for lamports, at in self.db.execute(
                "SELECT lamports, at FROM tag_credits WHERE user_id = ? ORDER BY at, signature, mint",
                (str(user_id),)):
            if covered < lamports:
                return at + BURN_AFTER_SECONDS
            covered -= lamports
        return None

    def claimed_since(self, since: int) -> int:
        return self.db.execute(
            "SELECT COALESCE(SUM(lamports), 0) FROM tag_debits WHERE kind = 'claim' AND at > ?",
            (since,)).fetchone()[0]

    # -- wallets --

    def wallet(self, user_id: str) -> tuple[str | None, str | None, int | None]:
        """(bound wallet, pending wallet, when the pending one is usable)."""
        row = self.db.execute("SELECT wallet, pending, ready_at FROM tag_wallets WHERE user_id = ?",
                              (str(user_id),)).fetchone()
        return tuple(row) if row else (None, None, None)

    def apply_wallet(self, user_id: str, decision: Decision) -> None:
        """Persist the wallet part of a claim decision."""
        if decision.bind:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO tag_wallets VALUES (?, ?, NULL, NULL)",
                                (str(user_id), decision.bind))
        elif decision.pending:
            bound = self.wallet(user_id)[0]
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO tag_wallets VALUES (?, ?, ?, ?)",
                                (str(user_id), bound, decision.pending, decision.ready_at))

    # -- the site's copy --

    def credit_record(self, user_id: str, handle: str, now: int) -> dict:
        wallet, pending, ready_at = self.wallet(user_id)
        return {"handle": handle, "claimable": self.balance(user_id), "burns_at": self.burns_at(user_id),
                "wallet": wallet, "wallet_pending": pending, "wallet_ready_at": ready_at, "updated": now}

    def mirror(self, kv, now: int) -> int:
        """Write `claim:credit:{xid}` for every requester. Returns the count."""
        people = self.requesters()
        for user_id, handle in people.items():
            kv.set_json(f"claim:credit:{user_id}", self.credit_record(user_id, handle, now))
        return len(people)

"""What each X-tag requester is owed, read off the payout treasury's history.

Every tag-launched coin's split pays one row to `legs.CHARLIE_PAYOUT_TREASURY`.
That wallet is shared, so the ledger attributes each SOL inflow to a coin, and
each coin to the numeric X user ID that requested it (`tag_coins`, written at
launch). Attribution is per pump `distribute_creator_fees` instruction,
top-level or inner (a CPI): pump pays each shareholder row with a system
`transfer` it makes itself, so the transfers to the treasury nested under one
distribute are exactly what that coin paid (measured on mainnet, fixture
`tests/fixtures/pump_distribute_mainnet.json`). A transaction with several
distributes therefore credits each known tag mint exactly its own share.
Anything else that reaches the treasury (a distribute of another coin, a
plain transfer, a CPI transfer beside a distribute) is unattributed: nobody's
credit, and logged. If the nested transfers ever add up to more than the
treasury actually gained, nothing is credited.

Credits leave by two roads, both written as debits: a claim to the wallet the
requester signs in with, or the burn once a credit is seven days old
(`burnable`; debits use up the oldest credits first). A debit is written
`pending` BEFORE its transaction is sent (a write-ahead intent) and becomes
`final` once the chain confirms it, or is deleted when the transaction failed
or provably never landed (`reconcile` in the bot: its blockhash's
`last_valid` height has passed and the signature is still unknown). A pending
debit already lowers the balance, so an ambiguous send can never be paid twice.

A claim held for the owner sits in `tag_held` (held, then approved, then
paid); held and approved amounts do not burn. A payment that covers a hold is
linked to it by signature: the hold is paid only once that payment is final,
and returns to its state (approved stays approved) if it fails. `claim_decision` is the whole
claim policy and does no I/O.

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
BURN_AFTER_SECONDS = 7 * DAY               # unclaimed credit burns this long after it was recorded
WALLET_CHANGE_DELAY_SECONDS = 72 * 3_600   # a bound wallet changes only after this
MIN_CLAIM_LAMPORTS = 1_000_000             # 0.001 SOL; below this a claim is refused
RENT_EXEMPT_MIN_LAMPORTS = 890_880         # a 0-data system account's rent-exempt minimum
TX_FEE_LAMPORTS = 5_000                    # one signature; taken out of the amount sent
TREASURY_RESERVE_LAMPORTS = RENT_EXEMPT_MIN_LAMPORTS + TX_FEE_LAMPORTS
PER_CLAIM_MAX_LAMPORTS = 5_000_000_000     # 5 SOL; larger claims are held for the owner
PER_DAY_MAX_LAMPORTS = 20_000_000_000      # 20 SOL of claims per day, all requesters together
SCAN_PAGE = 1_000                          # signatures per getSignaturesForAddress page

PUMP_PROGRAM = distribute.PUMP_PROGRAM
SYSTEM_PROGRAM = "11111111111111111111111111111111"
DISTRIBUTE_DISCRIMINATOR = distribute.DISTRIBUTE_CREATOR_FEES

CURSOR_KEY = "treasury_cursor"

# Wallets a claim may never pay: Charlie's own.
CHARLIE_WALLETS = frozenset(a for a in (
    legs.TOLL_DESTINATION, legs.CHARLIE_OPS_DESTINATION, legs.CHARLIE_PAYOUT_TREASURY,
    legs.CHARLIE_LAUNCH_WALLET, legs.SOL_BURN_INCINERATOR, legs.LAUNCH_BUYBACK_DESTINATION,
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


def _paid_to(instructions, treasury: str) -> int:
    """Lamports the parsed system `transfer`s in `instructions` sent to `treasury`."""
    total = 0
    for ix in instructions:
        parsed = ix.get("parsed")
        if ix.get("program") != "system" and ix.get("programId") != SYSTEM_PROGRAM:
            continue
        if not isinstance(parsed, dict) or parsed.get("type") not in ("transfer", "transferWithSeed"):
            continue
        info = parsed.get("info") or {}
        if info.get("destination") == treasury:
            try:
                total += int(info.get("lamports") or 0)
            except (TypeError, ValueError):
                continue
    return total


def distributes(tx: dict, treasury: str) -> list[tuple[str, int | None]]:
    """`(mint, lamports)` for every pump distribute instruction, top-level and
    inner alike, in order: the mint is account 0, the lamports are the system
    transfers to `treasury` nested under that instruction (None when an
    inner distribute's nesting cannot be read, i.e. no stackHeight)."""
    message = (tx.get("transaction") or {}).get("message") or {}
    groups = {}
    for group in (tx.get("meta") or {}).get("innerInstructions") or []:
        groups.setdefault(group.get("index"), []).extend(group.get("instructions") or [])
    out = []
    for index, ix in enumerate(message.get("instructions") or []):
        inner = groups.get(index, [])
        if _is_distribute(ix):
            out.append((ix["accounts"][0], _paid_to(inner, treasury)))
            continue
        for at, cix in enumerate(inner):
            if not _is_distribute(cix):
                continue
            height = cix.get("stackHeight")
            if not isinstance(height, int):
                out.append((cix["accounts"][0], None))
                continue
            under = []
            for nxt in inner[at + 1:]:
                nested = nxt.get("stackHeight")
                if not isinstance(nested, int) or nested <= height:
                    break
                under.append(nxt)
            out.append((cix["accounts"][0], _paid_to(under, treasury)))
    return out


def gain(tx: dict, treasury: str) -> int:
    """The treasury's post minus pre balance in this transaction (0 if absent)."""
    message = (tx.get("transaction") or {}).get("message") or {}
    keys = [_key(k) for k in message.get("accountKeys") or []]
    if treasury not in keys:
        return 0
    at = keys.index(treasury)
    meta = tx.get("meta") or {}
    try:
        return int(meta["postBalances"][at]) - int(meta["preBalances"][at])
    except (KeyError, IndexError, TypeError, ValueError):
        return 0


def classify(tx: dict | None, treasury: str, known=None) -> tuple[list[tuple[str, int]], int, str | None]:
    """`(credits, unattributed, why)`: `[(mint, lamports)]` per known mint
    (summed over its distributes), the rest of the treasury's gain that is
    nobody's credit, and why that rest was not attributed."""
    if not tx or (tx.get("meta") or {}).get("err") is not None:
        return [], 0, "failed"
    amount = gain(tx, treasury)
    found = distributes(tx, treasury)
    if any(lamports is None for _mint, lamports in found):
        return [], max(amount, 0), "unreadable_nesting"
    credits: dict[str, int] = {}
    unknown = False
    for mint, lamports in found:
        if lamports <= 0:
            continue
        if known is not None and mint not in known:
            unknown = True
            continue
        credits[mint] = credits.get(mint, 0) + lamports
    total = sum(credits.values())
    if total > max(amount, 0):
        return [], max(amount, 0), "inconsistent"
    rest = amount - total
    if rest <= 0:
        return list(credits.items()), 0, None
    why = "unknown_mint" if unknown else ("no_distribute" if not found else "not_distributed")
    return list(credits.items()), rest, why


def inflow(tx: dict | None, treasury: str, known=None) -> list[tuple[str, int]]:
    """`[(mint, lamports)]` the treasury received through pump distribute
    instructions (top-level or inner) in a jsonParsed transaction, one entry
    per mint. With `known`, only those mints."""
    return classify(tx, treasury, known)[0]


# -- the claim policy --------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    state: str                    # pay | held | refused
    lamports: int                 # debited from the credit; the wallet receives this less TX_FEE
    reason: str | None = None
    bind: str | None = None       # the wallet to bind now (clears any pending change)
    pending: str | None = None    # a new wallet to start waiting on
    ready_at: int | None = None   # when `pending` becomes usable


def claim_decision(*, balance: int, wallet: str, bound: str | None, pending: str | None,
                   ready_at: int | None, now: int, recipient_lamports: int,
                   treasury_lamports: int, paid_today: int, caps: bool = True) -> Decision:
    """What to do with one claim. Pure: every number is passed in.

    The first wallet binds. A claim to the bound wallet keeps it bound and
    clears any pending change. A different wallet waits WALLET_CHANGE_DELAY
    from the request that names it (a new different wallet restarts the
    wait), then binds. The amount is the whole balance; the network fee comes
    out of it, so the wallet receives the amount less TX_FEE. A claim below
    MIN_CLAIM, or one that would leave a new recipient below rent exemption,
    is refused. One the treasury cannot pay while keeping its reserve, above
    PER_CLAIM_MAX, or past PER_DAY_MAX today, is held for the owner, who
    approves it (`caps=False` then skips the two caps).
    """
    if wallet in CHARLIE_WALLETS:
        return Decision("refused", 0, "that wallet cannot receive claims")
    if bound is None or wallet == bound or (pending == wallet and ready_at is not None and ready_at <= now):
        bind = wallet
    elif pending == wallet:
        return Decision("refused", 0, "the new wallet is still waiting", pending=wallet, ready_at=ready_at)
    else:
        return Decision("refused", 0, "a new wallet waits 72 hours before it can receive a claim",
                        pending=wallet, ready_at=now + WALLET_CHANGE_DELAY_SECONDS)
    amount = balance
    if amount < MIN_CLAIM_LAMPORTS:
        return Decision("refused", 0, "nothing to claim yet", bind=bind)
    if recipient_lamports + amount - TX_FEE_LAMPORTS < RENT_EXEMPT_MIN_LAMPORTS:
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
    at          INTEGER NOT NULL,       -- when the ledger recorded it, not the block time
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
    at          INTEGER NOT NULL,       -- when it was written, right after the blockhash was fetched
    state       TEXT NOT NULL DEFAULT 'final',  -- pending | final
    last_valid  INTEGER                 -- the blockhash's lastValidBlockHeight (pending rows)
);
CREATE INDEX IF NOT EXISTS tag_debits_user ON tag_debits (user_id, at);
CREATE INDEX IF NOT EXISTS tag_debits_state ON tag_debits (state, signature);
CREATE TABLE IF NOT EXISTS tag_held (
    id          INTEGER PRIMARY KEY,
    user_id     TEXT NOT NULL,
    lamports    INTEGER NOT NULL,
    wallet      TEXT NOT NULL,
    at          INTEGER NOT NULL,
    state       TEXT NOT NULL,          -- held | approved | paid
    signature   TEXT                    -- the pending payment covering it, if any
);
CREATE INDEX IF NOT EXISTS tag_held_user ON tag_held (user_id, state);
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
        # A database made before these columns existed gains them.
        if "last_valid" not in {r[1] for r in self.db.execute("PRAGMA table_info(tag_debits)")}:
            with self.db:
                self.db.execute("ALTER TABLE tag_debits ADD COLUMN last_valid INTEGER")
        if "signature" not in {r[1] for r in self.db.execute("PRAGMA table_info(tag_held)")}:
            with self.db:
                self.db.execute("ALTER TABLE tag_held ADD COLUMN signature TEXT")

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

    def scan(self, rpc, treasury: str, *, now: int) -> list[dict]:
        """Walk the treasury's signatures back to the saved cursor, then read
        them oldest first. Each tag-mint inflow is credited once, dated `now`
        (when the ledger learnt of it), so a credit always has its full week
        on the site before it can burn. The cursor moves only after every
        signature was read; one the node cannot return yet stops the scan
        without moving it (credits are idempotent, so the rescan is safe).

        Rows: `{"kind": "credit", ...}` or `{"kind": "unattributed", ...}`
        for a treasury gain that is nobody's credit."""
        cursor = self.state(CURSOR_KEY)
        entries, before = [], None
        while True:
            page = rpc.signatures_for_address(treasury, before=before, until=cursor, limit=SCAN_PAGE)
            entries.extend(page)
            if len(page) < SCAN_PAGE:
                break
            before = page[-1]["signature"]
        if not entries:
            return []
        known = set(self.tag_mints())
        rows = []
        for entry in reversed(entries):
            if entry.get("err") is not None:
                continue
            signature = entry["signature"]
            tx = rpc.transaction(signature)
            if tx is None:
                rows.append({"kind": "unread", "signature": signature})
                return rows
            credits, rest, why = classify(tx, treasury, known)
            for mint, lamports in credits:
                if self.credit(signature, mint, lamports, now):
                    rows.append({"kind": "credit", "signature": signature, "mint": mint, "lamports": lamports,
                                 "user_id": self.coin(mint)["user_id"], "at": now})
            if rest > 0:
                rows.append({"kind": "unattributed", "signature": signature, "lamports": rest, "reason": why})
        self.set_state(CURSOR_KEY, entries[0]["signature"])
        return rows

    # -- debits --

    def debit(self, user_id: str, kind: str, lamports: int, *, wallet: str | None = None,
              signature: str | None = None, at: int, state: str = "final") -> None:
        if kind not in ("claim", "burn"):
            raise ValueError(f"unknown debit kind {kind!r}")
        if state not in ("pending", "final"):
            raise ValueError(f"unknown debit state {state!r}")
        with self.db:
            self.db.execute(
                "INSERT INTO tag_debits (user_id, kind, lamports, wallet, signature, at, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (str(user_id), kind, int(lamports), wallet, signature, at, state))

    def debit_pending(self, kind: str, rows, *, wallet: str, signature: str, at: int,
                      last_valid: int | None) -> None:
        """The write-ahead intent: every `(user_id, lamports)` of one signed
        transaction, pending, in one database transaction, before it is sent.
        `last_valid` is its blockhash's lastValidBlockHeight: past it, a
        signature still unknown can never land."""
        if kind not in ("claim", "burn"):
            raise ValueError(f"unknown debit kind {kind!r}")
        with self.db:
            self.db.executemany(
                "INSERT INTO tag_debits (user_id, kind, lamports, wallet, signature, at, state, last_valid) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                [(str(uid), kind, int(lamports), wallet, signature, at, last_valid) for uid, lamports in rows])

    def has_pending(self, user_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM tag_debits WHERE user_id = ? AND state = 'pending' LIMIT 1",
                               (str(user_id),)).fetchone() is not None

    def pending(self) -> dict[str, dict]:
        """signature -> {"kind", "at", "last_valid", "rows": [(user_id, lamports, wallet)]}
        for pending debits."""
        out: dict[str, dict] = {}
        for signature, kind, at, last_valid, user_id, lamports, wallet in self.db.execute(
                "SELECT signature, kind, at, last_valid, user_id, lamports, wallet FROM tag_debits "
                "WHERE state = 'pending' ORDER BY id"):
            entry = out.setdefault(signature, {"kind": kind, "at": at, "last_valid": last_valid, "rows": []})
            entry["rows"].append((user_id, lamports, wallet))
        return out

    def finalize(self, signature: str) -> None:
        """The transaction landed: its debits are final and any hold it covered is paid."""
        with self.db:
            self.db.execute("UPDATE tag_debits SET state = 'final' WHERE signature = ? AND state = 'pending'",
                            (signature,))
            self.db.execute("UPDATE tag_held SET state = 'paid' WHERE signature = ?", (signature,))

    def drop_pending(self, signature: str) -> None:
        """The transaction failed or never landed: the credit is restored, and
        any hold it covered is open again in the state it had (approved stays approved)."""
        with self.db:
            self.db.execute("DELETE FROM tag_debits WHERE signature = ? AND state = 'pending'", (signature,))
            self.db.execute("UPDATE tag_held SET signature = NULL WHERE signature = ? AND state != 'paid'",
                            (signature,))

    def pending_burn(self) -> bool:
        return self.db.execute(
            "SELECT 1 FROM tag_debits WHERE kind = 'burn' AND state = 'pending' LIMIT 1").fetchone() is not None

    def _credited(self, user_id: str) -> int:
        return self.db.execute("SELECT COALESCE(SUM(lamports), 0) FROM tag_credits WHERE user_id = ?",
                               (str(user_id),)).fetchone()[0]

    def _debited(self, user_id: str) -> int:
        return self.db.execute("SELECT COALESCE(SUM(lamports), 0) FROM tag_debits WHERE user_id = ?",
                               (str(user_id),)).fetchone()[0]

    def balance(self, user_id: str) -> int:
        """Credits less every debit, pending ones included."""
        return self._credited(user_id) - self._debited(user_id)

    def burnable(self, user_id: str, now: int) -> int:
        """Credits older than BURN_AFTER not yet covered by debits (debits
        use up the oldest credits first), less any held or approved claim."""
        old = self.db.execute(
            "SELECT COALESCE(SUM(lamports), 0) FROM tag_credits WHERE user_id = ? AND at <= ?",
            (str(user_id), now - BURN_AFTER_SECONDS)).fetchone()[0]
        return max(0, old - self._debited(user_id) - self.held_lamports(user_id))

    def burns_at(self, user_id: str) -> int | None:
        """When the oldest credit not yet covered by debits burns."""
        covered = self._debited(user_id) + self.held_lamports(user_id)
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

    # -- held claims --

    def hold(self, user_id: str, lamports: int, wallet: str, at: int) -> None:
        """Hold a claim for the owner. An open, unapproved one is updated, not
        doubled; one the owner already approved (or one being paid) is left
        exactly as it was, so an approval never grows after the fact."""
        with self.db:
            if self.db.execute("SELECT 1 FROM tag_held WHERE user_id = ? AND state IN ('held', 'approved') "
                               "AND (state = 'approved' OR signature IS NOT NULL) LIMIT 1",
                               (str(user_id),)).fetchone():
                return
            cur = self.db.execute(
                "UPDATE tag_held SET lamports = ?, wallet = ? WHERE user_id = ? AND state = 'held'",
                (int(lamports), wallet, str(user_id)))
            if cur.rowcount == 0:
                self.db.execute("INSERT INTO tag_held (user_id, lamports, wallet, at, state) VALUES (?, ?, ?, ?, ?)",
                                (str(user_id), int(lamports), wallet, at, "held"))

    def held_lamports(self, user_id: str) -> int:
        """Held and approved amounts not already covered by a pending payment
        (a pending debit lowers the balance by itself)."""
        return self.db.execute(
            "SELECT COALESCE(SUM(lamports), 0) FROM tag_held WHERE user_id = ? AND state IN ('held', 'approved') "
            "AND signature IS NULL", (str(user_id),)).fetchone()[0]

    def link_held(self, user_id: str, signature: str) -> None:
        """A pending payment for this user covers their open hold, if any."""
        with self.db:
            self.db.execute("UPDATE tag_held SET signature = ? WHERE user_id = ? AND state IN ('held', 'approved') "
                            "AND signature IS NULL", (signature, str(user_id)))

    def approve_held(self, user_id: str) -> int:
        """The owner's approval. Only marks the row; the running bot pays it."""
        with self.db:
            return self.db.execute("UPDATE tag_held SET state = 'approved' WHERE user_id = ? AND state = 'held'",
                                   (str(user_id),)).rowcount

    def approved(self) -> list[tuple[str, str, int]]:
        """`(user_id, wallet, lamports)` of every approved held claim not being paid."""
        return self.db.execute("SELECT user_id, wallet, lamports FROM tag_held WHERE state = 'approved' "
                               "AND signature IS NULL ORDER BY id").fetchall()

    def drop_held(self, user_id: str) -> None:
        """The claim no longer stands (refused outright): its open, unpaid hold goes."""
        with self.db:
            self.db.execute("DELETE FROM tag_held WHERE user_id = ? AND state IN ('held', 'approved') "
                            "AND signature IS NULL", (str(user_id),))

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

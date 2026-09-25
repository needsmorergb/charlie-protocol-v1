"""Run the X-tag bot (`indexer.tag_bot`): read tags, launch, pay and burn.

    python -m tools.x_bot --dry-run --once          # one simulated pass, nothing sent
    python -m tools.x_bot                           # the loop: mentions every 60 s, money every 30 min
    python -m tools.x_bot --approve-held <x id>     # approve a held claim; the running bot pays it
    python -m tools.x_bot --codex-login             # sign the image moderator in to ChatGPT, once

The config lives OUTSIDE the repo, by default in %USERPROFILE%\\.charlie-bot\\config.json
(see tools/x_bot.example.json for every key). The keys are loaded only when
not in a dry run, and each must be the wallet its name says (legs). A dry
run uses its own database (`db_dry`), so it never consumes live tags.

One bot at a time: the bot holds an exclusive lock file next to its
database, and a second one exits. `--approve-held` only updates a row and
does not take the lock.

A file named STOP in the config directory pauses launches; the money tick
carries on. Every event is one line on stdout and in the log file.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from indexer import codex_auth, mint_pool, moderation, tag_bot
from indexer import tag_launch as tag
from indexer import tag_ledger
from indexer.ed25519 import Keypair
from indexer.rpc import DEFAULT_ENDPOINTS, RpcClient

REPO = Path(__file__).resolve().parents[1]
CONFIG_DIR = Path.home() / ".charlie-bot"
DEFAULT_CONFIG = CONFIG_DIR / "config.json"
MENTIONS_EVERY_SECONDS = 60
MONEY_EVERY_SECONDS = 30 * 60


class ConfigError(ValueError):
    pass


class Locked(RuntimeError):
    pass


def load_config(path: Path) -> dict:
    path = Path(path).resolve()
    if path == REPO or REPO in path.parents:
        raise ConfigError(f"{path} is inside the repo; the bot's config lives outside it")
    config = json.loads(path.read_text(encoding="utf-8"))
    config.setdefault("db", str(path.parent / "book.db"))
    config.setdefault("db_dry", str(path.parent / "book-dry.db"))
    config.setdefault("log", str(path.parent / "bot.log"))
    for field in ("bot_user_id",):
        if not config.get(field):
            raise ConfigError(f"the config has no {field}")
    if Path(config["db"]).resolve() == Path(config["db_dry"]).resolve():
        raise ConfigError("db and db_dry must be different files")
    return config


def load_keys(paths: dict, *, reader=Keypair.from_file) -> dict:
    """Each named key, refused unless its address is the legs wallet of that name."""
    keys = {}
    for name, address in tag_bot.KEY_ADDRESSES.items():
        if not paths.get(name):
            raise ConfigError(f"the config has no keypairs.{name}")
        key = reader(paths[name])
        if key.address != address:
            raise ConfigError(f"keypairs.{name} is {key.address}, not the {name} wallet {address}")
        keys[name] = key
    return keys


def acquire_lock(db_path: str):
    """An exclusive, non-blocking lock on `<db>.lock`. Returns the open file,
    which holds the lock until it is closed or the process ends."""
    path = f"{db_path}.lock"
    handle = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise Locked(f"another x_bot holds {path}; only one may run against this database") from None
    return handle


def codex_auth_path(config: dict, config_dir: Path) -> Path:
    """Where the moderator's own ChatGPT sign-in lives (never ~/.codex)."""
    setting = config.get("moderation")
    path = setting.get("codex_auth") if isinstance(setting, dict) else None
    return Path(os.path.expanduser(path)) if path else Path(config_dir) / "codex-auth.json"


def moderator_for(config: dict, *, dry_run: bool):
    """The image moderator, from `moderation` in the config: an object with
    `codex_auth` (a ChatGPT sign-in file from --codex-login; optional `model`)
    or `anthropic_api_key`, or the string "off". A live bot refuses to start
    with none of them, or with a codex_auth file that is missing; a dry run
    without one simply does not moderate."""
    setting = config.get("moderation")
    if setting == "off":
        return None
    setting = setting if isinstance(setting, dict) else {}
    if setting.get("codex_auth"):
        store = codex_auth.TokenStore(setting["codex_auth"])
        try:
            store.load()
        except codex_auth.CodexAuthError as exc:
            if dry_run:
                return None
            raise ConfigError(f"moderation: {exc}") from None
        return moderation.codex_moderator(store, model=setting.get("model") or moderation.CODEX_MODEL)
    if setting.get("anthropic_api_key"):
        return moderation.anthropic_moderator(setting["anthropic_api_key"])
    if dry_run:
        return None
    raise ConfigError('moderation has neither codex_auth (sign in with --codex-login) nor anthropic_api_key; '
                      'set one, or "moderation": "off" to launch unmoderated')


def codex_login(path: Path, *, login=None) -> None:
    """Sign the moderator in to ChatGPT and keep the grant at `path`."""
    def say(line: str) -> None:
        print(line, flush=True)
        webbrowser.open(codex_auth.VERIFY_URL)
    tokens = (login or (lambda: codex_auth.device_login(say=say)))()
    codex_auth.TokenStore(path).save(tokens)
    print(f"signed in; the moderator's ChatGPT sign-in is saved to {path}")


def file_logger(path: str):
    def log(line: str) -> None:
        stamped = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {line}"
        print(stamped, flush=True)
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(stamped + "\n")
    return log


def build_bot(config: dict, *, dry_run: bool, config_dir: Path) -> tag_bot.Bot:
    from api.launch import pin_metadata
    from indexer.kv import KV
    from indexer.xapi import XClient

    x = XClient(**{k: config.get("x", {}).get(k, "") for k in
                   ("bearer", "consumer_key", "consumer_secret", "access_token", "access_secret")})
    rpc = RpcClient(tuple(config.get("rpc_urls") or ()) or DEFAULT_ENDPOINTS)
    kv = KV(config["upstash"]["url"], config["upstash"]["token"])
    book = tag.Book(config["db_dry"] if dry_run else config["db"])
    ledger = tag_ledger.Ledger(book.db)
    keys = None if dry_run else load_keys(config.get("keypairs") or {})
    pool = mint_pool.from_file(os.path.expanduser(config["mint_pool"])) if config.get("mint_pool") else None
    return tag_bot.Bot(x, rpc, kv, book, ledger, keys, dry_run=dry_run, bot_id=str(config["bot_user_id"]),
                       pin=pin_metadata, stop_file=config_dir / "STOP", claim_secret=config.get("claim_secret", ""),
                       moderate=moderator_for(config, dry_run=dry_run), mint_keys=pool,
                       log=file_logger(config["log"]),
                       priority_micro_lamports=int(config.get("priority_micro_lamports",
                                                              tag.PRIORITY_MICRO_LAMPORTS)),
                       announce=config.get("announce", "quote"))


def approve_held(db_path: str, xid: str) -> int:
    """Mark a user's held claim approved. The running bot pays it."""
    book = tag.Book(db_path)
    try:
        return tag_ledger.Ledger(book.db).approve_held(xid)
    finally:
        book.db.close()


def run(bot: tag_bot.Bot, *, once: bool, clock=time.monotonic, sleep=time.sleep) -> None:
    """Tags as often as X's rate limit allows (at most MENTIONS_EVERY_SECONDS
    apart), money every MONEY_EVERY_SECONDS. A due tag search also runs
    between money steps, so a tag never waits out a whole money tick."""
    due = {"mentions": clock()}

    def mentions_if_due() -> None:
        if clock() >= due["mentions"]:
            _guarded(bot, bot.tick_mentions)
            due["mentions"] = clock() + bot.poll_seconds(MENTIONS_EVERY_SECONDS)

    next_money = clock()
    while True:
        mentions_if_due()
        if clock() >= next_money:
            _guarded(bot, lambda: bot.tick_money(between=None if once else mentions_if_due), "tick_money")
            next_money = clock() + MONEY_EVERY_SECONDS
        if once:
            return
        sleep(max(0.5, min(due["mentions"], next_money) - clock()))


def _guarded(bot, step, name: str | None = None) -> None:
    try:
        step()
    except Exception as exc:  # noqa: BLE001 -- the loop outlives any one tick
        bot.log("tick_error", step=name or step.__name__, reason=f"{type(exc).__name__}: {exc}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.x_bot", description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--dry-run", action="store_true", help="simulate on its own database; never send, post, pin or write the KV")
    ap.add_argument("--once", action="store_true", help="one mentions tick and one money tick, then exit")
    ap.add_argument("--approve-held", metavar="XID", help="approve a held claim; the running bot pays it without the caps")
    ap.add_argument("--replay", metavar="TWEET_ID",
                    help="rerun one missed tag with every rule but its age, then exit (stop the running bot first)")
    ap.add_argument("--bypass-account", action="store_true",
                    help="with --replay: the owner's one-time exception to the account age, follower, "
                         "post and profile-image rules for that tag")
    ap.add_argument("--codex-login", action="store_true",
                    help="sign the image moderator in to ChatGPT (device code) and save the grant")
    args = ap.parse_args(argv)
    if args.bypass_account and not args.replay:
        ap.error("--bypass-account needs --replay")
    if args.codex_login:
        try:
            loose = json.loads(args.config.read_text(encoding="utf-8")) if args.config.exists() else {}
            codex_login(codex_auth_path(loose, args.config.resolve().parent))
        except (codex_auth.CodexAuthError, OSError, ValueError) as exc:
            print(f"x_bot: {exc}", file=sys.stderr)
            return 2
        return 0
    try:
        config = load_config(args.config)
        if args.approve_held:
            count = approve_held(config["db"], args.approve_held)
            print(f"approved {count} held claim(s) for {args.approve_held}")
            return 0 if count else 1
        if not args.dry_run and not config.get("claim_secret"):
            raise ConfigError("claim_secret is empty: set it to the site's CHARLIE_SESSION_SECRET before running live")
        moderator_for(config, dry_run=args.dry_run)          # refuse before the lock, not after
        lock = acquire_lock(config["db_dry"] if args.dry_run else config["db"])
        bot = build_bot(config, dry_run=args.dry_run, config_dir=Path(args.config).resolve().parent)
    except (ConfigError, Locked, OSError, KeyError, ValueError) as exc:
        print(f"x_bot: {exc}", file=sys.stderr)
        return 2
    try:
        if args.replay:
            row = bot.replay(args.replay, bypass_account=args.bypass_account)
            print(json.dumps(row))
            return 0 if row and row.get("outcome") in ("launched", "simulated") else 1
        bot.log("start", dry_run=args.dry_run, once=args.once)
        run(bot, once=args.once)
    finally:
        lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

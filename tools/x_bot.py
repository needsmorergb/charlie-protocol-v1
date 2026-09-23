"""Run the X-tag bot (`indexer.tag_bot`): read tags, launch, pay and burn.

    python -m tools.x_bot --dry-run --once          # one simulated pass, nothing sent
    python -m tools.x_bot                           # the loop: mentions every 60 s, money every 30 min
    python -m tools.x_bot --approve-held <x id>     # approve a held claim; the running bot pays it

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
from datetime import datetime, timezone
from pathlib import Path

from indexer import moderation, tag_bot
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


def moderator_for(config: dict, *, dry_run: bool):
    """The image moderator, from `moderation` in the config: an object with
    `anthropic_api_key`, or the string "off". A live bot refuses to start
    with neither; a dry run without a key simply does not moderate."""
    setting = config.get("moderation")
    if setting == "off":
        return None
    key = setting.get("anthropic_api_key") if isinstance(setting, dict) else None
    if key:
        return moderation.anthropic_moderator(key)
    if dry_run:
        return None
    raise ConfigError('moderation has no anthropic_api_key; set one, or "moderation": "off" to launch unmoderated')


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
    return tag_bot.Bot(x, rpc, kv, book, ledger, keys, dry_run=dry_run, bot_id=str(config["bot_user_id"]),
                       pin=pin_metadata, stop_file=config_dir / "STOP", claim_secret=config.get("claim_secret", ""),
                       moderate=moderator_for(config, dry_run=dry_run), log=file_logger(config["log"]))


def approve_held(db_path: str, xid: str) -> int:
    """Mark a user's held claim approved. The running bot pays it."""
    book = tag.Book(db_path)
    try:
        return tag_ledger.Ledger(book.db).approve_held(xid)
    finally:
        book.db.close()


def run(bot: tag_bot.Bot, *, once: bool, clock=time.monotonic, sleep=time.sleep) -> None:
    next_mentions = next_money = clock()
    while True:
        now = clock()
        if now >= next_mentions:
            _guarded(bot, bot.tick_mentions)
            next_mentions = now + MENTIONS_EVERY_SECONDS
        if now >= next_money:
            _guarded(bot, bot.tick_money)
            next_money = now + MONEY_EVERY_SECONDS
        if once:
            return
        sleep(max(1.0, min(next_mentions, next_money) - clock()))


def _guarded(bot, step) -> None:
    try:
        step()
    except Exception as exc:  # noqa: BLE001 -- the loop outlives any one tick
        bot.log("tick_error", step=step.__name__, reason=f"{type(exc).__name__}: {exc}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.x_bot", description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--dry-run", action="store_true", help="simulate on its own database; never send, post, pin or write the KV")
    ap.add_argument("--once", action="store_true", help="one mentions tick and one money tick, then exit")
    ap.add_argument("--approve-held", metavar="XID", help="approve a held claim; the running bot pays it without the caps")
    args = ap.parse_args(argv)
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
        bot.log("start", dry_run=args.dry_run, once=args.once)
        run(bot, once=args.once)
    finally:
        lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

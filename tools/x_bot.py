"""Run the X-tag bot (`indexer.tag_bot`): read tags, launch, pay and burn.

    python -m tools.x_bot --dry-run --once          # one simulated pass, nothing sent
    python -m tools.x_bot                           # the loop: mentions every 60 s, money every 30 min
    python -m tools.x_bot --pay-held <x user id>    # pay a held claim to its bound wallet

The config lives OUTSIDE the repo, by default in %USERPROFILE%\\.charlie-bot\\config.json
(see tools/x_bot.example.json for every key). The keys are loaded only when
not in a dry run, and each must be the wallet its name says (legs).

A file named STOP in the config directory pauses launches; the money tick
carries on. Every event is one line on stdout and in the log file.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from indexer import tag_bot
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


def load_config(path: Path) -> dict:
    path = Path(path).resolve()
    if path == REPO or REPO in path.parents:
        raise ConfigError(f"{path} is inside the repo; the bot's config lives outside it")
    config = json.loads(path.read_text(encoding="utf-8"))
    config.setdefault("db", str(path.parent / "book.db"))
    config.setdefault("log", str(path.parent / "bot.log"))
    for field in ("bot_user_id",):
        if not config.get(field):
            raise ConfigError(f"the config has no {field}")
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
    book = tag.Book(config["db"])
    ledger = tag_ledger.Ledger(book.db)
    keys = None if dry_run else load_keys(config.get("keypairs") or {})
    return tag_bot.Bot(x, rpc, kv, book, ledger, keys, dry_run=dry_run, bot_id=str(config["bot_user_id"]),
                       pin=pin_metadata, stop_file=config_dir / "STOP", log=file_logger(config["log"]))


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
    ap.add_argument("--dry-run", action="store_true", help="simulate; never send, post, pin or write the KV")
    ap.add_argument("--once", action="store_true", help="one mentions tick and one money tick, then exit")
    ap.add_argument("--pay-held", metavar="XID", help="pay a held claim to the user's bound wallet, without caps")
    args = ap.parse_args(argv)
    try:
        config = load_config(args.config)
        bot = build_bot(config, dry_run=args.dry_run, config_dir=Path(args.config).resolve().parent)
    except (ConfigError, OSError, KeyError, ValueError) as exc:
        print(f"x_bot: {exc}", file=sys.stderr)
        return 2
    if args.pay_held:
        print(json.dumps(bot.pay_held(args.pay_held), indent=2))
        return 0
    bot.log("start", dry_run=args.dry_run, once=args.once)
    run(bot, once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

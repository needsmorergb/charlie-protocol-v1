"""Command line for the indexer.

    python -m indexer observe <mint> [<mint> ...]   read the chain, run the checks
    python -m indexer log                           replay the append-only record
    python -m indexer derive <mint> --program <id>  the vault PDAs for a coin

Exit codes are meant to be usable from a cron line or a CI step:

    0   every check that ran passed
    1   at least one check FAILED
    2   at least one coin could not be observed at all

`UNCHECKED` does not set a non-zero code -- today most legs are unchecked by
construction and a permanently red exit code teaches whoever runs it to stop
reading. It does still block publication; see the silence rule in the report.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from . import invariants
from .legs import GRANDFATHERED_SEAL, Registry
from .observe import observe
from .report import render
from .rpc import DEFAULT_ENDPOINTS, RpcClient
from .store import DEFAULT_PATH, Store


def _endpoints(value: str | None) -> tuple:
    raw = value or os.environ.get("CHARLIE_RPC_URLS") or ""
    urls = tuple(url.strip() for url in raw.split(",") if url.strip())
    return urls or DEFAULT_ENDPOINTS


def _registry(program_id: str | None) -> Registry:
    return Registry(
        program_id=program_id or os.environ.get("CHARLIE_PROGRAM_ID") or None,
        grandfathered_seal=GRANDFATHERED_SEAL,
    )


def _observe(args) -> int:
    rpc = RpcClient(_endpoints(args.rpc))
    registry = _registry(args.program)
    store = Store(args.store) if args.store else None

    worst = 0
    for index, mint in enumerate(args.mints):
        record = observe(rpc, mint, registry)
        if store:
            store.append(record)
        if args.json:
            print(json.dumps(record.as_dict(), sort_keys=True))
        else:
            if index:
                print("\n" + "=" * 78 + "\n")
            print(render(record))
        if record.error and record.config is None:
            worst = max(worst, 2)
        elif record.failures:
            worst = max(worst, 1)
    if store and not args.json:
        print(f"\nappended {len(args.mints)} observation(s) to {store.path}")
    return worst


def _stamp(value) -> str:
    """Epoch seconds are what the record stores; humans get UTC to the second."""
    if not isinstance(value, (int, float)):
        return str(value)
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _log(args) -> int:
    records = Store(args.path).read(mint=args.mint, limit=args.limit)
    if args.json:
        for record in records:
            print(json.dumps(record, sort_keys=True))
        return 0
    if not records:
        print(f"no observations in {args.path}")
        return 0
    for record in records:
        stamp = _stamp(record.get("observed_at"))
        split = record.get("split") or {}
        failed = [c["name"] for c in record.get("checks", []) if c["status"] == invariants.FAIL]
        state = "ERROR" if record.get("error") and not split else ("FAIL" if failed else "ok")
        summary = (
            f"seal {split.get('seal')} burn {split.get('burn')} paid {split.get('paid')}"
            if split
            else (record.get("error") or "-")
        )
        print(f"{stamp}  {state:<5}  {record.get('mint')}  {summary}")
        if failed:
            print(f"{'':<21}  failed: {', '.join(failed)}")
    return 0


def _derive(args) -> int:
    registry = _registry(args.program)
    if not registry.program_id:
        print(
            "no program id. The protocol program is not deployed, so seal and burn\n"
            "vault PDAs cannot be derived yet. Pass --program once it is, or set\n"
            "CHARLIE_PROGRAM_ID.",
            file=sys.stderr,
        )
        return 2
    for mint in args.mints:
        print(f"mint  {mint}")
        print(f"  seal  {registry.seal_vault(mint)}")
        print(f"  burn  {registry.burn_pool(mint)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="indexer",
        description="Charlie Protocol indexer -- read a coin's fee split and check it.",
    )
    # Shared options hang off every subcommand rather than off the top level,
    # so `derive <mint> --program X` -- the order people actually type -- works.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--rpc", help="comma-separated RPC endpoints (env CHARLIE_RPC_URLS)")
    common.add_argument("--program", help="protocol program id (env CHARLIE_PROGRAM_ID)")
    sub = parser.add_subparsers(dest="command", required=True)

    observe_cmd = sub.add_parser("observe", parents=[common],
                                 help="read the chain and run the checks")
    observe_cmd.add_argument("mints", nargs="+")
    observe_cmd.add_argument(
        "--store",
        nargs="?",
        const=str(DEFAULT_PATH),
        help=f"append each observation to this log (default {DEFAULT_PATH})",
    )
    observe_cmd.add_argument("--json", action="store_true", help="one JSON record per line")
    observe_cmd.set_defaults(handler=_observe)

    log_cmd = sub.add_parser("log", parents=[common],
                             help="replay the append-only observation log")
    log_cmd.add_argument("--path", default=str(DEFAULT_PATH))
    log_cmd.add_argument("--mint")
    log_cmd.add_argument("--limit", type=int)
    log_cmd.add_argument("--json", action="store_true")
    log_cmd.set_defaults(handler=_log)

    derive_cmd = sub.add_parser("derive", parents=[common],
                                help="show the vault PDAs for a coin")
    derive_cmd.add_argument("mints", nargs="+")
    derive_cmd.set_defaults(handler=_derive)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)

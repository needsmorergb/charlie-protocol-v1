"""Command line for the indexer.

    python -m indexer observe <mint> [<mint> ...]   read the chain, run the checks
    python -m indexer log                           replay the append-only record
    python -m indexer derive <mint> --program <id>  the vault PDAs for a coin
    python -m indexer scan <mint> [--evidence] [--pages N]   walk SEAL/OPS inflows into evidence
    python -m indexer export [--db] [--out]         write the deterministic committed text export
    python -m indexer reconcile <mint> [--evidence PATH] [--write]   EVID-10's residual, as of an observation
    python -m indexer site <mint> [--evidence PATH] [--write] [--out]   WEB-02/WEB-03/WEB-06: the HTML page + raw JSON

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
from pathlib import Path

from . import invariants, publish, site
from .evidence import DEFAULT_DB_PATH, Evidence
from .export import DEFAULT_EXPORT_DIR, export_all
from .legs import GRANDFATHERED_SEAL, Registry, split_of
from .observe import observe
from .pump import read_bonding_curve, read_mint, read_sharing_config
from .reconcile import DEFAULT_OUTPUT_PATH, reconcile, record as record_reconciliation, render as render_reconciliation
from .report import render
from .rpc import DEFAULT_ENDPOINTS, RpcClient
from .scan import BACKFILL_PAGES_PER_RUN, derive_initial_supply, scan_burns, scan_inflows_all_endpoints
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
    evidence = Evidence(args.evidence) if getattr(args, "evidence", None) else None

    worst = 0
    for index, mint in enumerate(args.mints):
        record = observe(rpc, mint, registry, evidence=evidence)
        if store:
            store.append(record)
        if args.json:
            # PUB-01/PUB-02: the --json surface shares the exact same
            # publication boundary as report.render()'s text -- neither
            # reads a figure straight off the Observation.
            print(json.dumps(publish.public_record(record), sort_keys=True))
        else:
            if index:
                print("\n" + "=" * 78 + "\n")
            print(render(record))
        if record.error and record.config is None:
            worst = max(worst, 2)
        elif record.failures:
            worst = max(worst, 1)
    if evidence is not None:
        evidence.close()
    if store and not args.json:
        print(f"\nappended {len(args.mints)} observation(s) to {store.path}")
    return worst


def _scan(args) -> int:
    rpc = RpcClient(_endpoints(args.rpc))
    registry = _registry(args.program)
    evidence = Evidence(args.evidence or DEFAULT_DB_PATH)
    pages = args.pages or BACKFILL_PAGES_PER_RUN

    worst = 0
    try:
        for mint in args.mints:
            try:
                curve = read_bonding_curve(rpc, mint)
                config = read_sharing_config(rpc, curve)
            except Exception as exc:
                print(f"{mint}: cannot read split -- {type(exc).__name__}: {exc}")
                worst = max(worst, 2)
                continue
            split = split_of(config, registry)
            leg_of = {a.address: a.leg for a in split.attributions}
            destinations = {addr for addr, leg in leg_of.items() if leg in ("seal", "paid")}
            if not destinations:
                print(f"{mint}: no SEAL or OPS destination -- nothing to scan for inflows")
            for destination in sorted(destinations):
                # D-13: every configured endpoint is walked deliberately and
                # unioned -- which inflows get recorded must not depend on
                # which endpoint happened to answer.
                newest, oldest, complete, endpoints_contributed = scan_inflows_all_endpoints(
                    rpc, evidence, mint, destinations, leg_of.get, destination,
                    pages=pages, grandfathered=registry.grandfathered_seal,
                )
                state = "backfill complete" if complete else "backfill incomplete"
                print(
                    f"{mint}  {leg_of[destination]:<4}  {destination}  {state}  "
                    f"reached {oldest or '-'}  newest {newest or '-'}  "
                    f"({endpoints_contributed} endpoint(s) contributed)"
                )

            # The burn walk and initial-supply derivation apply to every mint
            # regardless of whether it has a SEAL/OPS destination configured --
            # EVID-06/09/10: the mint-wide burn walk -- every burn against the
            # mint, by anyone, not just one known actor's transactions (D-09).
            burn_newest, burn_oldest, burn_complete = scan_burns(rpc, evidence, mint, pages=pages)
            burn_state = "backfill complete" if burn_complete else "backfill incomplete"
            print(
                f"{mint}  burn  {mint}  {burn_state}  "
                f"reached {burn_oldest or '-'}  newest {burn_newest or '-'}"
            )

            # EVID-07/08: derive once, cache forever.
            supply_row = derive_initial_supply(rpc, evidence, mint)
            if supply_row is None:
                print(f"{mint}  initial_supply  walk unfinished this run -- try again")
            elif supply_row.get("raw_supply") is not None:
                print(
                    f"{mint}  initial_supply  {supply_row['raw_supply']} raw units "
                    f"(decimals {supply_row['decimals']}, from {supply_row['creation_signature']})"
                )
            else:
                print(f"{mint}  initial_supply  UNCHECKED -- {supply_row['unchecked_reason']}")
    finally:
        evidence.close()
    return worst


def _reconcile(args) -> int:
    rpc = RpcClient(_endpoints(args.rpc))
    evidence = Evidence(args.evidence or DEFAULT_DB_PATH)
    try:
        mint_state = read_mint(rpc, args.mint)
        result = reconcile(evidence, args.mint, mint_state)
        row = record_reconciliation(evidence, result)
    finally:
        evidence.close()

    text = render_reconciliation(row)
    if args.write:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text + "\n")
        print(f"wrote {out_path}")
    else:
        print(text)
    return 0


def _site(args) -> int:
    """WEB-02/WEB-03/WEB-06: build one Observation (real RPC + a pre-populated
    Evidence store, same as `_observe`/`_reconcile`) and render it through
    `site.render`/`site.record_json` -- both already-classified `SURFACES`
    targets. `--write` writes both artifacts to sibling paths under `--out`;
    without it, the HTML is printed to stdout (matching `_reconcile`'s shape).
    """
    rpc = RpcClient(_endpoints(args.rpc))
    registry = _registry(args.program)
    evidence = Evidence(args.evidence or DEFAULT_DB_PATH)
    try:
        record = observe(rpc, args.mint, registry, evidence=evidence)
    finally:
        evidence.close()

    if args.write:
        html_path, json_path = site.write(record, Path(args.out))
        print(f"wrote {html_path}")
        print(f"wrote {json_path}")
    else:
        print(site.render(record))
    return 0


def _export(args) -> int:
    evidence = Evidence(args.db)
    try:
        written = export_all(evidence, args.out)
    finally:
        evidence.close()
    for path in written:
        print(f"wrote {path}")
    return 0


def _stamp(value) -> str:
    """Epoch seconds are what the record stores; humans get UTC to the second."""
    if not isinstance(value, (int, float)):
        return str(value)
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _log_lines(records) -> list[str]:
    """The pure line-producing half of the text `log` surface -- every
    record routed through `publish.gate_stored_record()` first, so a legacy
    schema-2 record whose split was not publishable is redacted on replay
    rather than reprinted verbatim (PUB-01, `01-VERIFICATION.md`).
    """
    lines: list[str] = []
    for record in records:
        gated = publish.gate_stored_record(record)
        stamp = _stamp(gated.get("observed_at"))
        split = gated.get("split") or {}
        failed = [c["name"] for c in gated.get("checks", []) if c["status"] == invariants.FAIL]
        blocked = gated.get("blocked") or {}
        state = "ERROR" if gated.get("error") and not split else ("FAIL" if failed else "ok")
        if split:
            summary = f"seal {split.get('seal')} burn {split.get('burn')} paid {split.get('paid')}"
        elif invariants.SPLIT in blocked and blocked[invariants.SPLIT]:
            reason = blocked[invariants.SPLIT][0]
            summary = f"withheld -- {reason['check']} ({reason['status']})"
        else:
            summary = gated.get("error") or "-"
        lines.append(f"{stamp}  {state:<5}  {gated.get('mint')}  {summary}")
        if failed:
            lines.append(f"{'':<21}  failed: {', '.join(failed)}")
        if gated.get("_redacted"):
            lines.append(
                f"{'':<21}  redacted on replay (schema {gated.get('schema')}): "
                + ", ".join(gated["_redacted"])
            )
    return lines


def _log_json_lines(records) -> list[str]:
    """The pure line-producing half of the `log --json` surface -- same
    `gate_stored_record()` pass as `_log_lines`, one JSON line per record.
    """
    return [json.dumps(publish.gate_stored_record(record), sort_keys=True) for record in records]


def _log(args) -> int:
    records = Store(args.path).read(mint=args.mint, limit=args.limit)
    if args.json:
        for line in _log_json_lines(records):
            print(line)
        return 0
    if not records:
        print(f"no observations in {args.path}")
        return 0
    for line in _log_lines(records):
        print(line)
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
    observe_cmd.add_argument(
        "--evidence",
        nargs="?",
        const=str(DEFAULT_DB_PATH),
        help=f"read/write recorded inflows from this SQLite store (default {DEFAULT_DB_PATH})",
    )
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

    scan_cmd = sub.add_parser("scan", parents=[common],
                              help="walk a coin's SEAL/OPS destinations and record inflows")
    scan_cmd.add_argument("mints", nargs="+")
    scan_cmd.add_argument(
        "--evidence",
        nargs="?",
        const=str(DEFAULT_DB_PATH),
        help=f"SQLite evidence store to read/write (default {DEFAULT_DB_PATH})",
    )
    scan_cmd.add_argument("--pages", type=int, help=f"backfill pages per run (default {BACKFILL_PAGES_PER_RUN})")
    scan_cmd.set_defaults(handler=_scan)

    export_cmd = sub.add_parser(
        "export", help="write the deterministic committed text export of the evidence store"
    )
    export_cmd.add_argument("--db", default=str(DEFAULT_DB_PATH), help=f"default {DEFAULT_DB_PATH}")
    export_cmd.add_argument("--out", default=str(DEFAULT_EXPORT_DIR), help=f"default {DEFAULT_EXPORT_DIR}")
    export_cmd.set_defaults(handler=_export)

    reconcile_cmd = sub.add_parser(
        "reconcile", parents=[common],
        help="EVID-10: record and render the mint's exact residual, as of an observation",
    )
    reconcile_cmd.add_argument("mint")
    reconcile_cmd.add_argument(
        "--evidence", default=str(DEFAULT_DB_PATH), help=f"SQLite evidence store to read (default {DEFAULT_DB_PATH})"
    )
    reconcile_cmd.add_argument(
        "--write", action="store_true", help=f"write to --out (default {DEFAULT_OUTPUT_PATH}) instead of printing"
    )
    reconcile_cmd.add_argument("--out", default=str(DEFAULT_OUTPUT_PATH), help=f"default {DEFAULT_OUTPUT_PATH}")
    reconcile_cmd.set_defaults(handler=_reconcile)

    site_cmd = sub.add_parser(
        "site", parents=[common],
        help="WEB-02/WEB-03/WEB-06: render the HTML page + raw observation JSON for a coin",
    )
    site_cmd.add_argument("mint")
    site_cmd.add_argument(
        "--evidence", default=str(DEFAULT_DB_PATH), help=f"SQLite evidence store to read (default {DEFAULT_DB_PATH})"
    )
    site_cmd.add_argument(
        "--write", action="store_true", help=f"write to --out (default {site.DEFAULT_OUTPUT_DIR}) instead of printing"
    )
    site_cmd.add_argument("--out", default=str(site.DEFAULT_OUTPUT_DIR), help=f"default {site.DEFAULT_OUTPUT_DIR}")
    site_cmd.set_defaults(handler=_site)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)

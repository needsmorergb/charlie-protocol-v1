"""Command line for the indexer.

    python -m indexer observe <mint> [<mint> ...]   read the chain, run the checks
    python -m indexer log                           replay the append-only record
    python -m indexer derive <mint> --program <id>  the vault PDAs for a coin
    python -m indexer scan <mint> [--evidence] [--pages N]   walk SOL-burn/OPS inflows into evidence
    python -m indexer export [--db] [--out]         write the deterministic committed text export
    python -m indexer reconcile <mint> [--evidence PATH] [--write]   EVID-10's residual, as of an observation
    python -m indexer site <mint> [--evidence PATH] [--write] [--out]   WEB-02/WEB-03/WEB-06: the HTML page + raw JSON
    python -m indexer intake [--repo OWNER/REPO] [--limit N] [--dry-run]   D-34: read the public issue queue, measure submissions

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

from . import coverage, intake, invariants, publish, site
from .evidence import DEFAULT_DB_PATH, Evidence
from .export import DEFAULT_EXPORT_DIR, export_all
from .legs import GRANDFATHERED_SOL_BURN, Registry, split_of
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
        grandfathered_sol_burn=GRANDFATHERED_SOL_BURN,
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
            destinations = {addr for addr, leg in leg_of.items() if leg in ("sol_burn", "paid")}
            if not destinations:
                print(f"{mint}: no SOL burn or OPS destination -- nothing to scan for inflows")
            for destination in sorted(destinations):
                # D-13: every configured endpoint is walked deliberately and
                # unioned -- which inflows get recorded must not depend on
                # which endpoint happened to answer.
                newest, oldest, complete, endpoints_contributed = scan_inflows_all_endpoints(
                    rpc, evidence, mint, destinations, leg_of.get, destination,
                    pages=pages, grandfathered=registry.grandfathered_sol_burn,
                )
                state = "backfill complete" if complete else "backfill incomplete"
                print(
                    f"{mint}  {leg_of[destination]:<4}  {destination}  {state}  "
                    f"reached {oldest or '-'}  newest {newest or '-'}  "
                    f"({endpoints_contributed} endpoint(s) contributed)"
                )

            # The burn walk and initial-supply derivation apply to every mint
            # regardless of whether it has a SOL burn/OPS destination configured --
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
    """WEB-02/WEB-03/WEB-06/QT-01/QT-02/QT-03: build one Observation (real
    RPC + a pre-populated Evidence store, same as `_observe`/`_reconcile`)
    and render it through `site.render`/`site.record_json`/
    `site.render_landing` -- all already-classified `SURFACES` targets. One
    RPC read produces every artifact. `--write` writes the coin page + record
    to sibling paths under `--out`, plus `index.html` there too when
    `--landing` is also given; without `--write`, the relevant HTML is
    printed to stdout (matching `_reconcile`'s shape).
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
        if args.landing:
            landing_path = site.write_landing(record, Path(args.out))
            print(f"wrote {landing_path}")
    else:
        if args.landing:
            print(site.render_landing(record))
        else:
            print(site.render(record))
    return 0


def _enumerate(args) -> int:
    """COV-02: one `getProgramAccounts` sweep against the fee-sharing
    program, on a DEDICATED client -- never the shared one a per-coin
    observation loop uses, per RESEARCH.md's verified endpoint-penalty
    pitfall.
    """
    rpc = RpcClient(_endpoints(args.rpc), timeout=60.0)
    evidence = Evidence(args.evidence or DEFAULT_DB_PATH)
    revoked = True if args.revoked else (False if args.not_revoked else None)
    try:
        result = coverage.sweep(rpc, evidence, holders=args.holders, revoked=revoked, on_progress=print)
    finally:
        evidence.close()
    print(
        f"returned {result['returned']}  decoded {result['decoded']}  "
        f"truncated {result['truncated']}  refused {result['refused']}"
    )
    if result["truncated"]:
        print(
            f"{result['truncated']} account(s) truncated by the 114-byte slice -- "
            "these need a full fetch (03-02's second pass) before they can be recorded"
        )
    return 0


def _index_inputs(out_dir: Path) -> tuple[list[dict], set[str]]:
    """The two things `write_index` needs, read fresh off disk every time:
    every committed record under `out_dir`, and which mints have a sibling
    page. Factored out of `_index` so `_index` and `_intake` (03-02) can
    never disagree about the page set or the known-pages set -- both call
    this exact function rather than each re-deriving it.
    """
    records = []
    for path in sorted(out_dir.glob("*.json")):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue

    reserved_html_stems = {"index"} | {
        p.stem for p in out_dir.glob(site.INDEX_FILENAME_TEMPLATE.format(page="*"))
    }
    known_pages = {
        p.stem for p in out_dir.glob("*.html") if p.stem not in reserved_html_stems
    }
    return records, known_pages


def _write_index(out_dir: Path, *, extra_counts: dict | None = None) -> list[Path]:
    """Writes the index pages from whatever is committed under `out_dir`
    right now. `extra_counts` lets `_intake` (03-02) fold in the failed-
    attempt count from the submission store without `_index` (which has no
    submission store to read) ever needing to know that count exists.
    """
    records, known_pages = _index_inputs(out_dir)
    # D-33/D-35: no enumeration, so no denominator. The only count this
    # command can back on its own is how many records it actually read.
    counts = {"observed": len(records)}
    if extra_counts:
        counts.update(extra_counts)
    written = list(site.write_index(records, out_dir, counts=counts, known_pages=known_pages))
    # `/verify` with no mint after it. Written alongside the index rather than
    # by a command of its own because it is the same surface from the other
    # side: the index says which coins are measured, this says how a coin
    # becomes one. Both callers of this helper therefore keep the two in step.
    written.append(site.write_verify(out_dir))
    return written


def _index(args) -> int:
    """WEB-01: read the committed records under `--out` and write the index
    pages. A record's mint is treated as "has a page" when a sibling
    `<mint>.html` exists under the same directory.
    """
    out_dir = Path(args.out)
    written = _write_index(out_dir)
    for path in written:
        print(f"wrote {path}")
    return 0


def _intake(args) -> int:
    """D-34: the front door. Reads the public issue queue (no credential),
    measures every submission up to the per-run cap, writes the artifacts
    for what it could observe, records every attempt to the evidence store,
    and rebuilds the index from what is now on disk -- through the exact
    same `_write_index` helper `_index` uses, so a standalone index build
    and an intake run can never disagree about the page set.

    The reply to GitHub (a comment, a close) needs `gh` and its credential,
    so it is behind its own explicit `--answer` flag (D-23) -- omitted, this
    handler never spawns a process and never requires a token to run. This
    is what lets the read half run in a deployment holding no secret at all.
    `--dry-run` additionally skips every LOCAL write this handler performs
    (the coin artifacts, the index, the evidence rows) and only reports what
    `run()` would find.
    """
    rpc = RpcClient(_endpoints(args.rpc))
    registry = _registry(args.program)
    evidence = Evidence(args.evidence or DEFAULT_DB_PATH)
    out_dir = Path(args.out)
    try:
        issues = intake.open_issues(args.repo, limit=max(args.limit * 4, intake.DEFAULT_ISSUE_PAGE_LIMIT))
        if args.dry_run:
            submissions = [issue for issue in issues if intake.is_submission(issue)]
            submissions.sort(key=lambda issue: issue.get("number") or 0)
            for issue in submissions[: args.limit]:
                try:
                    mint = intake.submitted_mint(issue)
                    print(f"issue #{issue.get('number')}  {mint}  (dry run -- not observed)")
                except intake.InvalidMint as exc:
                    print(f"issue #{issue.get('number')}  invalid  {exc.reason}")
            print("dry run -- no artifact written, no evidence recorded, no reply sent")
            return 0

        outcomes = intake.run(
            issues, rpc, registry, evidence, out_dir,
            repo=args.repo, site_url=args.site_url, limit=args.limit,
        )

        for outcome in outcomes:
            if outcome.observed:
                print(f"issue #{outcome.issue_number}  {outcome.mint}  observed  {outcome.verdict_url or ''}")
            else:
                print(f"issue #{outcome.issue_number}  {outcome.mint or '(no mint)'}  failed  {outcome.reason}")

        counts_extra = {"failed": evidence.submission_counts()["failed"]}
        written = _write_index(out_dir, extra_counts=counts_extra)
        for path in written:
            print(f"wrote {path}")

        if args.answer:
            results = intake.reply(evidence, site_url=args.site_url, repo=args.repo)
            for result in results:
                print(f"issue #{result['issue_number']}  reply  {result}")
    finally:
        evidence.close()

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
            summary = f"sol_burn {split.get('sol_burn')} burn {split.get('burn')} paid {split.get('paid')}"
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
            "no program id. The protocol program is not deployed, so the SOL-burn and token-burn\n"
            "vault PDAs cannot be derived yet. Pass --program once it is, or set\n"
            "CHARLIE_PROGRAM_ID.",
            file=sys.stderr,
        )
        return 2
    for mint in args.mints:
        print(f"mint  {mint}")
        print(f"  sol_burn  {registry.sol_burn_vault(mint)}")
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
                              help="walk a coin's SOL-burn/OPS destinations and record inflows")
    scan_cmd.add_argument("mints", nargs="+")
    scan_cmd.add_argument(
        "--evidence",
        nargs="?",
        const=str(DEFAULT_DB_PATH),
        help=f"SQLite evidence store to read/write (default {DEFAULT_DB_PATH})",
    )
    scan_cmd.add_argument("--pages", type=int, help=f"backfill pages per run (default {BACKFILL_PAGES_PER_RUN})")
    scan_cmd.set_defaults(handler=_scan)

    enumerate_cmd = sub.add_parser(
        "enumerate", parents=[common],
        help="COV-02: enumerate sharing configs from the fee-sharing program",
    )
    enumerate_cmd.add_argument(
        "--evidence", default=str(DEFAULT_DB_PATH), help=f"SQLite evidence store to read/write (default {DEFAULT_DB_PATH})"
    )
    enumerate_cmd.add_argument("--holders", type=int, help="narrow to exactly this many shareholders")
    enumerate_cmd.add_argument("--revoked", action="store_true", help="narrow to admin_revoked configs only")
    enumerate_cmd.add_argument(
        "--not-revoked", action="store_true", dest="not_revoked", help="narrow to non-admin_revoked configs only"
    )
    enumerate_cmd.set_defaults(handler=_enumerate)

    index_cmd = sub.add_parser(
        "index", help="WEB-01: build the coin index pages from the committed records under --out"
    )
    index_cmd.add_argument(
        "--evidence", default=str(DEFAULT_DB_PATH), help=f"SQLite evidence store to read (default {DEFAULT_DB_PATH})"
    )
    index_cmd.add_argument("--out", default=str(site.DEFAULT_OUTPUT_DIR), help=f"default {site.DEFAULT_OUTPUT_DIR}")
    index_cmd.set_defaults(handler=_index)

    intake_cmd = sub.add_parser(
        "intake", parents=[common],
        help="D-34/COV-02/COV-03: the front door -- read the public issue queue and measure submissions",
    )
    intake_cmd.add_argument(
        "--repo", default="needsmorergb/charlie-protocol-site",
        help="owner/repo carrying the public submission queue (default needsmorergb/charlie-protocol-site)",
    )
    intake_cmd.add_argument(
        "--limit", type=int, default=intake.DEFAULT_RUN_LIMIT,
        help=f"submissions attempted per run (default {intake.DEFAULT_RUN_LIMIT}) -- a cap, not a skip",
    )
    intake_cmd.add_argument(
        "--evidence", default=str(DEFAULT_DB_PATH), help=f"SQLite evidence store to read/write (default {DEFAULT_DB_PATH})"
    )
    intake_cmd.add_argument("--out", default=str(site.DEFAULT_OUTPUT_DIR), help=f"default {site.DEFAULT_OUTPUT_DIR}")
    intake_cmd.add_argument(
        "--site-url", dest="site_url", default=None,
        help="the deployed site's base URL, for composing each verdict link",
    )
    intake_cmd.add_argument(
        "--dry-run", action="store_true",
        help="read the queue and report what would be measured; write nothing",
    )
    intake_cmd.add_argument(
        "--answer", action="store_true",
        help="D-23: also reply on GitHub (comment, close) for every unanswered row -- "
             "needs a logged-in gh credential; defaults to off so the read half never requires one",
    )
    intake_cmd.set_defaults(handler=_intake)

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
    site_cmd.add_argument(
        "--landing", action="store_true",
        help="also emit the landing page (QT-01/QT-02/QT-03) at index.html",
    )
    site_cmd.set_defaults(handler=_site)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)

"""The publication boundary (PUB-01/PUB-02): the single accessor every
surface obtains a figure through.

`invariants.apply_silence_rule()` already computes which figures are
publishable and why the rest are withheld; that boundary computation stays
there. This module is the *gate* that makes bypassing it require deleting
code rather than forgetting a rule -- a direct read of an `Observation`
field for a figure's value, anywhere outside `FIGURE_SOURCES`, is exactly the
bypass PUB-01 forbids.

`FIGURE_SOURCES` is the only place in the codebase that knows how to turn an
`Observation` into a number a human might read. Every name in
`invariants.FIGURES` must appear here, or a figure has no source at all --
that is a bug in this module, not a silence the protocol intends.

`report.py` (the text surface) and `publish.public_record()` (the `--json`
surface, called from `cli.py`) both render every figure through
`Publisher.figure()` -- there is exactly one boundary, used by both.
"""

from __future__ import annotations

import importlib
import json

from . import invariants
from . import legs


def _split_value(observation):
    split = observation.split
    if split is None:
        return None
    return {"sol_burn": split.sol_burn, "burn": split.burn, "paid": split.paid}


def _leg_total(observation, leg: str):
    """Sum of recorded lamports across every destination of `leg` (`sol_burn` or
    `paid`) in this observation's split -- `None` when there is nothing to
    sum (no evidence handle was consulted, or no destination of this leg
    exists in the split).
    """
    if observation.split is None or observation.evidence is None:
        return None
    destinations = [a.address for a in observation.split.attributions if a.leg == leg]
    if not destinations:
        return None
    total = 0
    seen = False
    for address in destinations:
        value = observation.evidence.get(address)
        if value is None:
            continue
        seen = True
        total += value
    return total if seen else None


def _sol_burn_total(observation):
    return _leg_total(observation, "sol_burn")


def _ops_total(observation):
    return _leg_total(observation, "paid")


def _burn_total(observation):
    """BURN_TOTAL: SOL spent at a BURN destination (PROTOCOL.md sec.4's
    `burn_spend` equation) -- distinct from SUPPLY_DESTROYED's token count.
    No coin has a BURN destination while `Registry.program_id` is `None`
    (the protocol program is not deployed), so this always resolves to
    `None` today; `invariants.burn_spend()` keeps `BURN_TOTAL` correctly
    `UNCHECKED` regardless of what this returns.
    """
    return None


def _supply_destroyed(observation):
    """D-09: every burn against the mint, by anyone -- the evidence store's
    own running total (`Evidence.total_burned`, surfaced onto the
    observation's `evidence` block as `"burn_total"` by `observe.py`).
    """
    if observation.evidence is None:
        return None
    return observation.evidence.get("burn_total")


FIGURE_SOURCES = {
    invariants.SPLIT: _split_value,
    invariants.SOL_BURN_TOTAL: _sol_burn_total,
    invariants.BURN_TOTAL: _burn_total,
    invariants.OPS_TOTAL: _ops_total,
    invariants.SUPPLY_DESTROYED: _supply_destroyed,
}

# `durable_record()`'s figure -> field-path map (PUB-01, the gap the verifier
# reproduced against `Observation.as_dict()`). A figure added to
# `invariants.FIGURES` later that has a simple, single-field durable
# representation is gated by construction the moment it is added here -- the
# loop in `durable_record()` never writes a figure's value except through
# this map, so an unlisted figure simply never reaches the record.
#
# SOL_BURN_TOTAL and OPS_TOTAL are deliberately absent: neither occupies one
# field. Each is the aggregate of a *per-destination* breakdown living under
# `evidence`, and `durable_record()` gates that breakdown separately, one
# destination at a time, keyed by which leg (`sol_burn`/`paid`) it belongs to --
# see the dedicated section below. BURN_TOTAL is also absent: `_burn_total()`
# always resolves to `None` (no BURN destination exists this phase), so it
# never has a value to place.
DURABLE_FIGURE_FIELDS = {
    invariants.SPLIT: ("split",),
    invariants.SUPPLY_DESTROYED: ("evidence", "burn_total"),
}


def classification(value):
    """D-26's five-bucket label, computed from the SPLIT figure's already-
    gated `{sol_burn, burn, paid}` value -- never from an `Observation`. This
    signature is what makes it structurally impossible to label a withheld
    split: there is no observation here to reach past the gate for, only
    the value a caller already obtained through `Publisher.figure()`.
    `None` in, `None` out -- a withheld split cannot produce a label.
    """
    if value is None:
        return None
    return legs.classify_split(value)


class Withheld(Exception):
    """Raised by `Publisher.figure()` when a figure's backing checks have
    not all passed. Carries the figure name and the blocking reasons
    (`(check_name, status, detail)` tuples, exactly `Verdict.blocked`'s
    shape) so a caller can render *why* without recomputing the silence rule
    itself.
    """

    def __init__(self, figure: str, reasons):
        self.figure = figure
        self.reasons = list(reasons)
        detail = "; ".join(f"{name} ({status}): {detail}" for name, status, detail in self.reasons)
        super().__init__(f"{figure} is withheld -- {detail}")


class Publisher:
    """The single accessor every surface obtains a figure through.

    Wraps one `Observation`. `apply_silence_rule()`'s verdict (already
    computed onto `observation.verdict` by `observe.observe()`) decides
    publishability; this class is only the gate over it.
    """

    def __init__(self, observation):
        self.observation = observation
        self.verdict = observation.verdict

    def figure(self, name: str):
        """`(value, backing_check_names)` when `name` is publishable.

        Raises `Withheld` otherwise, carrying the blocking check names and
        their statuses/details -- exactly what stopped it.
        """
        if self.verdict is None or not self.verdict.may_publish(name):
            reasons = (self.verdict.blocked.get(name) if self.verdict is not None else None) or [
                ("NO_CHECK", invariants.UNCHECKED, "no observation to check this figure against")
            ]
            raise Withheld(name, reasons)
        source = FIGURE_SOURCES.get(name)
        value = source(self.observation) if source is not None else None
        backing = tuple(
            check.name
            for check in self.observation.checks
            if name in check.backs and check.passed
        )
        return value, backing

    def publishable(self) -> frozenset:
        return self.verdict.publishable if self.verdict is not None else frozenset()

    def withheld(self) -> dict:
        return dict(self.verdict.blocked) if self.verdict is not None else {}


def public_record(observation) -> dict:
    """The one JSON shape a human-facing surface may emit -- the `--json`
    console path's counterpart to `report.render()`'s text (PUB-01/PUB-02).

    Every figure in `invariants.FIGURES` is obtained through
    `Publisher.figure()`: present under `"figures"` with its value and
    backing check names when publishable, present under `"blocked"` (naming
    the check that stopped it, never the value) when withheld. Non-figure
    facts (mint, error, the raw check list) are unaffected -- the rule is
    about the names in `invariants.FIGURES`, not about every field an
    observation carries.
    """
    publisher = Publisher(observation)
    figures: dict = {}
    blocked: dict = {}
    for name in invariants.FIGURES:
        try:
            value, backs = publisher.figure(name)
            figures[name] = {"value": value, "backed_by": list(backs)}
        except Withheld as exc:
            blocked[name] = [
                {"check": check_name, "status": status, "detail": detail}
                for check_name, status, detail in exc.reasons
            ]
    return {
        "schema": observation.schema,
        "mint": observation.mint,
        "observed_at": observation.observed_at,
        "error": observation.error,
        "checks": [c.as_dict() for c in observation.checks],
        "figures": figures,
        "blocked": blocked,
    }


def _set_path(record: dict, path: tuple, value) -> None:
    node = record
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value


def _pop_path(record: dict, path: tuple) -> bool:
    """Remove `path` from `record` if present. Returns True iff it removed
    something -- used by `gate_stored_record()` to know whether a redaction
    actually happened, so a record that never had the field in the first
    place is not reported as "redacted".
    """
    node = record
    for key in path[:-1]:
        if not isinstance(node, dict) or key not in node:
            return False
        node = node[key]
    if not isinstance(node, dict) or path[-1] not in node:
        return False
    del node[path[-1]]
    return True


def durable_record(observation) -> dict:
    """The append-only counterpart to `public_record()` -- what
    `Observation.as_dict()` delegates to, and therefore what `Store.append()`
    persists to the committed `state/observations.jsonl` and what `log`/`log
    --json` replay (PUB-01/PUB-02, `01-VERIFICATION.md`'s reproduced gap).

    Builds the same non-figure facts `as_dict()` always built -- schema,
    mint, observed_at, error, config, graduated, mint_state, sol_burn_balances,
    evidence_coverage, checks, publishable, blocked -- untouched by the
    silence rule, because none of them is a name in `invariants.FIGURES`.

    Every name in `invariants.FIGURES` is then obtained through
    `Publisher.figure()`, exactly as `public_record()` does: a figure whose
    checks have not all passed contributes nothing but its `blocked` entry
    (already present above). A publishable figure is written through
    `DURABLE_FIGURE_FIELDS`'s field path, in its existing shape, alongside
    the names of the checks backing it (`record["backed_by"][figure]`) --
    PUB-02 on a surface that has never carried backing names before.

    `SPLIT`'s per-address breakdown (`attribution`, carrying each
    shareholder's own bps) travels with `SPLIT` itself: with one shareholder
    a leaked `attribution` entry reconstitutes the exact aggregate a withheld
    `split` must not leak, so it is gated on the same figure rather than
    treated as a separate non-figure fact.

    `SOL_BURN_TOTAL`/`OPS_TOTAL` have no single field of their own: each is the
    aggregate of a per-destination breakdown under `evidence`. A destination
    is gated by the leg it belongs to (looked up from the observation's own
    split attributions) -- a SOL burn destination's recorded lamports appear only
    when `SOL_BURN_TOTAL` is publishable, a paid destination's only when
    `OPS_TOTAL` is publishable. With one destination on a leg (`$CHARLIE`'s
    shape today) the per-destination entry IS the aggregate, so this is not
    an optional refinement -- leaving it ungated would be the exact bypass
    `01-VERIFICATION.md` reproduced, moved one field over.

    `initial_supply` is chain-derived evidence, not a figure this protocol
    checks or publishes (it is not a member of `invariants.FIGURES`), so it
    is carried through unconditionally whenever an evidence handle was
    consulted -- the same judgment `report.py` and `public_record()` already
    make by never mentioning it as a figure at all.
    """
    record = {
        "schema": observation.schema,
        "mint": observation.mint,
        "observed_at": observation.observed_at,
        "error": observation.error,
    }
    if observation.config is not None:
        record["config"] = {
            "address": observation.config.address,
            "mint": observation.config.mint,
            "version": observation.config.version,
            "status": observation.config.status,
            "admin": observation.config.admin,
            "admin_revoked": observation.config.admin_revoked,
            "shareholders": [
                {"address": who, "bps": bps} for who, bps in observation.config.shareholders
            ],
        }
    if observation.graduated is not None:
        record["graduated"] = observation.graduated
    if observation.mint_state is not None:
        record["mint_state"] = {
            "supply": observation.mint_state.supply,
            "decimals": observation.mint_state.decimals,
            "mint_authority": observation.mint_state.mint_authority,
            "freeze_authority": observation.mint_state.freeze_authority,
            "token_program": observation.mint_state.program,
        }
    if observation.sol_burn_balances:
        record["sol_burn_balances"] = observation.sol_burn_balances
    if observation.evidence_coverage is not None:
        record["evidence_coverage"] = observation.evidence_coverage

    record["checks"] = [c.as_dict() for c in observation.checks]
    if observation.verdict is not None:
        record["publishable"] = sorted(observation.verdict.publishable)
        record["blocked"] = {
            figure: [
                {"check": name, "status": status, "detail": detail}
                for name, status, detail in reasons
            ]
            for figure, reasons in sorted(observation.verdict.blocked.items())
        }

    publisher = Publisher(observation)
    backed_by: dict = {}

    for name in invariants.FIGURES:
        path = DURABLE_FIGURE_FIELDS.get(name)
        if path is None:
            continue
        try:
            value, backs = publisher.figure(name)
        except Withheld:
            continue
        _set_path(record, path, value)
        backed_by[name] = list(backs)

    if invariants.SPLIT in backed_by and observation.split is not None:
        record["attribution"] = [
            {
                "address": a.address,
                "bps": a.bps,
                "leg": a.leg,
                "keyless": a.keyless,
                "reason": a.reason,
            }
            for a in observation.split.attributions
        ]

    if observation.evidence is not None and observation.split is not None:
        leg_by_address = {a.address: a.leg for a in observation.split.attributions}
        evidence_out: dict = {}
        for figure_name, leg in ((invariants.SOL_BURN_TOTAL, "sol_burn"), (invariants.OPS_TOTAL, "paid")):
            try:
                _value, backs = publisher.figure(figure_name)
            except Withheld:
                continue
            backed_by[figure_name] = list(backs)
            for address, value in observation.evidence.items():
                if leg_by_address.get(address) == leg:
                    evidence_out[address] = value
        if "initial_supply" in observation.evidence:
            evidence_out["initial_supply"] = observation.evidence["initial_supply"]
        if evidence_out:
            record.setdefault("evidence", {}).update(evidence_out)

    if backed_by:
        record["backed_by"] = backed_by

    return record


# `gate_stored_record()`'s figure -> field-path(s) map. Deliberately the same
# shape as `DURABLE_FIGURE_FIELDS` plus SPLIT's attribution breakdown -- a
# STORED record (schema 2 or 3) carries its own `publishable`/`blocked`
# fields regardless of which schema wrote it (both existed before this
# phase), so redaction at read time needs no knowledge of the schema that
# produced the record, only of where a figure's value lives.
_STORED_FIGURE_PATHS = {
    invariants.SPLIT: (("split",), ("attribution",)),
    invariants.SUPPLY_DESTROYED: (("evidence", "burn_total"),),
}


def gate_stored_record(record: dict) -> dict:
    """Read-time redaction of an already-STORED record (schema 2 or later),
    using that record's own `publishable`/`blocked` fields -- never the
    live silence rule, because a stored record IS the thing being replayed.

    This is what lets `log`/`log --json` redact a legacy schema-2 record
    written before `durable_record()` existed: schema 2's `as_dict()` always
    wrote `publishable`/`blocked` correctly (the verifier's gap was never in
    the verdict computation, only in which fields obeyed it), so gating on
    those two fields at read time closes the gap on replay without rewriting
    the append-only file itself.

    Returns a new dict; `record` is never mutated. A record this function
    actually redacted carries `record["_redacted"]`, a sorted list of the
    figure names it removed -- present only when a redaction happened, so a
    reader can tell "the tool redacted this" apart from "the field was never
    there".
    """
    gated = dict(record)
    blocked = record.get("blocked") or {}
    if not blocked:
        return gated

    redacted = []
    for figure, paths in _STORED_FIGURE_PATHS.items():
        if figure not in blocked:
            continue
        for path in paths:
            if _pop_path(gated, path):
                redacted.append(figure)

    # SOL_BURN_TOTAL/OPS_TOTAL: a stored record's `evidence` dict has no leg label
    # of its own (that lives in `split.attributions`, which may itself be
    # absent from a redacted record) -- so redaction here is conservative:
    # either leg being withheld strips every per-destination entry except
    # `burn_total`/`initial_supply`, rather than guessing which entries
    # belonged to which leg.
    if isinstance(gated.get("evidence"), dict) and (
        invariants.SOL_BURN_TOTAL in blocked or invariants.OPS_TOTAL in blocked
    ):
        evidence = gated["evidence"]
        trimmed = {k: v for k, v in evidence.items() if k in ("burn_total", "initial_supply")}
        if trimmed != evidence:
            gated["evidence"] = trimmed
            redacted.append(
                invariants.SOL_BURN_TOTAL if invariants.SOL_BURN_TOTAL in blocked else invariants.OPS_TOTAL
            )

    if redacted:
        gated["_redacted"] = sorted(set(redacted))
    return gated


def render_surface(name: str, subject) -> str:
    """Resolve a `SURFACES` entry by dotted string and return its output as
    text, for `TestSilenceRuleSweep`. Resolved lazily by `importlib` rather
    than imported at module scope: `report` and `cli` already import
    `publish`, and a module-level import the other way would be circular.
    """
    entry = SURFACES[name]
    module_name, _, attr = entry["target"].partition(":")
    module = importlib.import_module(module_name)
    target = module
    for part in attr.split("."):
        target = getattr(target, part)
    result = target(subject)
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        return json.dumps(result, sort_keys=True)
    if isinstance(result, list):
        return "\n".join(item if isinstance(item, str) else str(item) for item in result)
    return str(result)


# Every surface a figure can reach a human through, declared in one place
# (01-VERIFICATION.md's second `missing:` item: the old sweep enumerated
# surfaces by hand, which is why a third one went unswept). `target` is
# `"<module>:<callable>"`, resolved lazily by `render_surface()`. `input`
# names which kind of subject the sweep must build: `"observation"` for a
# live `Observation`, `"stored_records"` for a list of already-serialised
# dicts (what `Store.read()` returns).
SURFACES = {
    "report_text": {"target": "indexer.report:render", "input": "observation"},
    "observe_json": {"target": "indexer.publish:public_record", "input": "observation"},
    "durable_record": {"target": "indexer.publish:durable_record", "input": "observation"},
    "log_text": {"target": "indexer.cli:_log_lines", "input": "stored_records"},
    "log_json": {"target": "indexer.cli:_log_json_lines", "input": "stored_records"},
    "web_page": {"target": "indexer.site:render", "input": "observation"},
    "web_json": {"target": "indexer.site:record_json", "input": "observation"},
    # QT-01/QT-03 (02-03 quick task): registering this puts the landing page
    # inside TestSilenceRuleSweep.test_no_blocked_figures_sentinel_leaks_into_any_registered_surface's
    # automatic sweep the moment it is added -- exactly the gap
    # 01-VERIFICATION.md found once (a surface enumerated by hand instead of
    # by this registry). Deliberately absent from
    # tests/test_publication.py's FULL_DETAIL_SURFACES: those surfaces are
    # required to show every publishable figure, and the landing page is
    # required to show none at all -- a decision, not an oversight.
    "landing_page": {"target": "indexer.site:render_landing", "input": "observation"},
    # 03-01 Task 3: the coin index row. Registered so the generic sweep
    # covers it automatically the moment it exists (the same mechanism that
    # closed 01-VERIFICATION.md's gap), deliberately absent from
    # tests/test_publication.py's FULL_DETAIL_SURFACES the way the landing
    # page is -- an index row shows one figure by design, not all five.
    "index_rows": {"target": "indexer.site:index_rows", "input": "stored_records"},
}

# Every other function under `indexer/` whose body calls `print` or
# `json.dumps` -- enumerated by `TestSurfaceRegistryCoversEveryEmitter`'s AST
# walk, not by memory. Each reason is a sentence a reader can disagree with:
# why this emitter carries no figure a `SURFACES` entry needs to sweep.
NON_FIGURE_EMITTERS = {
    "indexer.cli:_observe": (
        "dispatches to report.render() and publish.public_record(), both already "
        "registered SURFACES targets, and to Store.append() (also classified below); "
        "this wrapper's own print calls emit only what those already-gated calls "
        "returned, plus a line count with no figure in it"
    ),
    "indexer.cli:_scan": (
        "prints scan progress -- signatures reached, endpoint counts, backfill "
        "state, and initial_supply (chain-derived evidence, not a member of "
        "invariants.FIGURES) -- none of it a name in invariants.FIGURES"
    ),
    "indexer.cli:_reconcile": (
        "prints reconcile.render()'s EVID-10 discrepancy admission -- "
        "attributed_burned/residual -- which is deliberately NOT the checked "
        "SUPPLY_DESTROYED figure: it is computed directly from evidence.burns_for() "
        "with no reference to invariants.apply_silence_rule() at all, is always "
        "captioned as an observation correct only as of itself (and as INCOMPLETE "
        "when the burn walk has not finished), and is committed to its own artifact "
        "(state/RECONCILIATION.md, the discrepancy table) rather than claimed as a "
        "published, checked total. Numerically the same quantity SUPPLY_DESTROYED "
        "would report once checked -- but never presented as checked, which is the "
        "distinction PUB-01 draws"
    ),
    "indexer.cli:_export": (
        "prints the file paths export_all() wrote -- filenames, not figures"
    ),
    "indexer.cli:_refresh_pages": (
        "prints the path of each coin page it re-rendered, and the mint of each it "
        "left alone -- filenames and addresses. Every figure on a page it writes went "
        "through site.render, which is itself a classified SURFACES target"
    ),
    "indexer.cli:_refresh": (
        "prints the file paths _refresh_pages and _write_index wrote -- filenames, "
        "not figures, mirroring _export's entry above"
    ),
    # The BURN leg run by hand (indexer/buyback.py, BUYBACK.md): an operator
    # keeper, PROTOCOL.md sec.5 option 3. Everything it prints is about ONE
    # transaction it built, simulated, or sent -- a plan, a simulation
    # result, a signature, and the burn instruction it read back off that
    # transaction through the indexer's own decoders. Per-transaction receipt
    # data, never a coin-wide total, and never a name in invariants.FIGURES;
    # the coin-wide figures that transaction feeds into are produced later by
    # the burn walk and gated like any other.
    "indexer.buyback:explain": (
        "returns the program's error in words a person can act on -- a message "
        "about a refused transaction, no figure in it"
    ),
    "indexer.buyback:confirm": (
        "prints confirmation polling for one signature -- status words and elapsed "
        "time, no figure"
    ),
    "indexer.buyback:run_keeper": (
        "prints one line per crank: the lot spent, the signature, and the burn read "
        "back off THAT transaction. Receipt data for a transaction the operator just "
        "sent, not a coin-wide total, and none of it a name in invariants.FIGURES"
    ),
    "indexer.cli:_print_result": (
        "prints buyback.run()'s result: the plan, the simulation's compute units, "
        "the signature if sent, and the burn instruction decoded from that one "
        "transaction. Per-transaction receipt data; no coin-wide figure and no name "
        "in invariants.FIGURES"
    ),
    "indexer.cli:_buyback": (
        "dispatches to buyback.run()/run_keeper() and prints through _print_result "
        "(classified above) plus keeper progress lines -- lots and signatures"
    ),
    "indexer.cli:_burn": (
        "the same, for a burn of tokens already held: dispatches to buyback and "
        "prints through _print_result"
    ),
    "indexer.cli:_load": (
        "prints how many rows each table of the committed export loaded -- a count "
        "of records read from a file, not a measurement of a coin. Nothing here is "
        "derived from a chain read and no figure passes through it"
    ),
    "indexer.cli:_log": (
        "dispatches to _log_lines()/_log_json_lines(), both registered SURFACES "
        "targets; prints their already-gated output verbatim"
    ),
    "indexer.cli:_derive": (
        "prints protocol PDAs derived from a program id -- addresses, not figures"
    ),
    "indexer.cli:_site": (
        "dispatches to indexer.site:render, indexer.site:record_json and "
        "indexer.site:render_landing, all already registered SURFACES targets; this "
        "wrapper's own print calls emit only the file paths site.write()/site.write_landing() "
        "wrote (mirroring _export's entry above) or the already-gated HTML string those "
        "targets already produced, never a figure read directly"
    ),
    "indexer.store:Store.append": (
        "persists whatever durable_record()/Observation.as_dict() already produced "
        "to the append-only log -- the gate is upstream of this call, not here"
    ),
    "indexer.export:export_table": (
        "writes raw evidence-store table rows to the committed deterministic "
        "export -- rows whose keys tests/test_publication.py's "
        "TestExportIncludesEveryPhaseTable asserts equal the table's own columns, "
        "not a classified or checked figure"
    ),
    "indexer.cli:_enumerate": (
        "prints coverage.sweep()'s progress and returned/decoded/truncated/refused "
        "counts -- population counts, not a member of invariants.FIGURES"
    ),
    "indexer.cli:_index": (
        "prints the file paths site.write_index() wrote -- filenames, not figures, "
        "mirroring _export's entry above"
    ),
    "indexer.cli:_intake": (
        "prints issue numbers, mints and outcome/reason names from intake.run()'s "
        "in-memory outcomes, plus the file paths _write_index() wrote -- none of it a "
        "name in invariants.FIGURES; every figure a written page carries already passed "
        "through site.write(), an already-classified SURFACES target"
    ),
    "indexer.rpc:RpcClient.call": (
        "builds the outbound JSON-RPC request body sent to the node -- a request, "
        "not a display of anything to a human"
    ),
    "indexer.evidence:Evidence._config_hash": (
        "hashes the canonical decoded config tuple via json.dumps to build a stable, "
        "order-independent cache key for the sharing_config table's primary key -- the "
        "hash is opaque and internal, never displayed and never returned to a caller "
        "as a value"
    ),
    "indexer.evidence:Evidence.record_sharing_config": (
        "serialises the raw shareholder list to JSON for storage in the sharing_config "
        "table's shareholders column -- raw chain-derived fact data (address/bps pairs), "
        "not a derived figure, and never printed or returned to a caller"
    ),
    "indexer.publish:render_surface": (
        "resolves and stringifies whichever SURFACES target it is given, for this "
        "test's own sweep -- it renders what the target already computed and emits "
        "nothing of its own"
    ),
}

"""Rendering an observation as an HTML page for a visitor.

ARCHITECTURE.md sec.4: "every number displays the check that backs it, and a
failed check is rendered *louder* than a passing one." `report.py` is the
first surface that displays a number under that rule; this module is the
second, and the first that a stranger with a browser -- not a terminal --
ever sees.

PUB-01/PUB-02: every number that is a *figure* (a name in `invariants.FIGURES`)
reaches this markup through `publish.Publisher`, never through a direct read
of an `Observation` field -- that direct read is the bypass PUB-01 forbids,
found once in `Observation.as_dict()` and closed (see `01-VERIFICATION.md`).
A generator that re-derives "is this figure OK to show" from scratch in
template logic -- comparing `check.status`, reading `observation.split`
directly, or truncating `Verdict.blocked[figure]` to its first reason -- is
that exact bypass class reopened in a new file, in a new language (HTML), and
this module refuses to reopen it.

`string.Template`/f-strings apply zero HTML escaping (unlike the autoescaping
a templating engine like Jinja2 would give for free, forbidden here by
CLAUDE.md's stdlib-only constraint), so escaping is added back by hand:
`esc()`, routed through every interpolated value that did not originate as a
hardcoded template literal.
"""

from __future__ import annotations

import html
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from . import invariants, publish

DEFAULT_OUTPUT_DIR = Path("web")


def _artifact_name(mint: str, suffix: str) -> str:
    """The one place a `web/` artifact filename is constructed (D-19): every
    reference to either file -- `write()`'s own paths, the raw-JSON href, and
    the rendered `git log -p` command -- resolves through this function, so
    the page can never name a file the generator does not produce. No dated,
    timestamped or `latest` variant exists here or anywhere else in this
    module; D-19 rejected a dated series as unbounded growth duplicating what
    git already records losslessly, and a second naming scheme is exactly how
    that rejection would quietly come back.
    """
    return f"{mint}{suffix}"


# The committed deterministic evidence export directory (D-16's rejected
# alternative was bundling raw evidence rows into the published record
# instead of linking to this) -- matches export.py's DEFAULT_EXPORT_DIR
# (`state/evidence`), kept here as a plain string rather than an import so
# this module's stdlib-only, flat-module import list (invariants, publish
# only) is untouched.
EVIDENCE_EXPORT_PATH = "state/evidence"


def esc(value) -> str:
    """`html.escape` with quotes escaped too, so a value is safe inside both
    element text and a double-quoted attribute. Route every interpolated
    value through this that did not originate as a hardcoded template
    literal -- a chain-derived address, a transaction signature, a check's
    `detail`/`expected`/`actual` text, `observation.error`.
    """
    return html.escape(str(value), quote=True)


def _format_figure(name: str, value) -> str:
    """Mirrors `report.py::_format_figure` -- same figure-name switch, HTML
    text instead of a fixed-width text column. Emits the underlying integer
    verbatim, with NO thousands separators, so `str(value)` containment
    checks (`test_every_publishable_figure_shows_its_sentinel_and_backing_checks`)
    keep matching.
    """
    if value is None:
        return "unknown"
    if name == invariants.SPLIT:
        return f"SEAL {value['seal']} bps / BURN {value['burn']} bps / OPS {value['paid']} bps"
    if name in (invariants.SEAL_TOTAL, invariants.OPS_TOTAL, invariants.BURN_TOTAL):
        return f"{value} lamports"
    if name == invariants.SUPPLY_DESTROYED:
        return f"{value} raw units"
    return str(value)


def _status_class(status) -> str:
    """Maps the three real statuses to three distinct CSS class names. Any
    other input -- an empty string, `None`, or anything unrecognised -- maps
    to the unchecked class, never the pass class. D-14 leaves `BURN_ATOMIC`
    reading `UNCHECKED` with an empty `backs` tuple for $CHARLIE today; a
    template that defaults an empty/missing status to a green-looking state
    would silently violate that reading.
    """
    if status == invariants.PASS:
        return "status-pass"
    if status == invariants.FAIL:
        return "status-fail"
    return "status-unchecked"


def _status_label(status) -> str:
    if status == invariants.PASS:
        return "PASS"
    if status == invariants.FAIL:
        return "FAIL"
    return "UNCHECKED"


def _figure_row(publisher: publish.Publisher, name: str) -> str:
    """The check-beside-figure contract: one row, four pieces -- label,
    value-or-withheld, status badge, backing/blocking check name(s) -- never
    separated. Takes a `Publisher`, not an `Observation`: this signature
    structurally cannot read a raw field.
    """
    label = esc(name)
    try:
        value, backs = publisher.figure(name)
        formatted = esc(_format_figure(name, value))
        backing = esc(", ".join(backs)) if backs else "(no check named)"
        return (
            f'<div class="figure-row" data-figure="{label}">'
            f'<span class="figure-label">{label}</span>'
            f'<span class="figure-value">{formatted}</span>'
            f'<span class="badge {_status_class(invariants.PASS)}">{_status_label(invariants.PASS)}</span>'
            f'<span class="figure-backs">backed by: {backing}</span>'
            f"</div>"
        )
    except publish.Withheld as exc:
        # UI-SPEC's Check-Beside-Figure Contract: every blocking check, never
        # exc.reasons[0] alone -- report.py's truncation is the negative
        # example this deliberately diverges from.
        worst_status = invariants.FAIL if any(s == invariants.FAIL for _n, s, _d in exc.reasons) else exc.reasons[0][1]
        blocking = "; ".join(f"{esc(n)} ({esc(s)})" for n, s, _d in exc.reasons)
        return (
            f'<div class="figure-row figure-withheld" data-figure="{label}">'
            f'<span class="figure-label">{label}</span>'
            f'<span class="figure-value">withheld</span>'
            f'<span class="badge {_status_class(worst_status)}">{_status_label(worst_status)}</span>'
            f'<span class="figure-backs">withheld by: {blocking}</span>'
            f"</div>"
        )


# -- D-18: freshness -- observed-at stamp, an age computed at generation
# time (never stored, never client-computed), and a static snapshot notice.
# No staleness judgment of any kind lives here: D-18 explicitly rejected a
# self-marking stale banner because the max-age threshold it would assert is
# a number nothing in this project derives.
def _stamp(epoch) -> str:
    """UTC form matching `cli._stamp`/`reconcile.render`'s existing format
    exactly, so a reader comparing this page against the text surfaces sees
    one stamp convention, not two.
    """
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _age(seconds) -> str:
    """Plain-English age from the two largest non-zero units among days,
    hours, minutes and seconds. Clamped at zero: a generation time at or
    before the observation reads as "0 seconds", never a negative duration --
    a minus sign here is a bug report the visitor cannot act on.
    """
    seconds = int(seconds)
    if seconds <= 0:
        return "0 seconds"
    units = (("day", 86_400), ("hour", 3_600), ("minute", 60), ("second", 1))
    parts: list[str] = []
    remaining = seconds
    for unit_name, unit_size in units:
        if len(parts) == 2:
            break
        value = remaining // unit_size
        if value <= 0:
            continue
        remaining -= value * unit_size
        parts.append(f"{value} {unit_name}" if value == 1 else f"{value} {unit_name}s")
    return ", ".join(parts) if parts else "0 seconds"


_SNAPSHOT_NOTE = (
    "This page is a snapshot taken at the observation time above -- it does "
    "not update itself, and nothing on it reads your clock."
)


def _freshness(observation, now) -> str:
    """The stamp, the age measured as `now - observation.observed_at`, and
    the snapshot sentence -- one header block, always rendered, including in
    the page-level error branch: a failed observation still has an
    observed-at, and a reader needs to know how old the failure is.
    """
    stamp = _stamp(observation.observed_at)
    age = _age(now - observation.observed_at)
    return (
        '<div class="freshness">'
        f'<p class="meta">observed at {esc(stamp)} (epoch {esc(observation.observed_at)})</p>'
        f'<p class="meta">age: {esc(age)}</p>'
        f'<p class="meta snapshot-note">{esc(_SNAPSHOT_NOTE)}</p>'
        "</div>"
    )


# -- PUB-03: the Seal Failure Banner ---------------------------------------
_SEAL_FAILURE_STATIC_SENTENCE = "No seal total is publishable for $CHARLIE — permanently, not pending."


def _seal_failure_banner(observation) -> str:
    """Unconditional, full-bleed banner when `SEAL_UNSPENDABLE` reads FAIL --
    never dismissible, never collapsible, never conditionally hidden beyond
    that one test. Renders the check's own `detail` verbatim and in full;
    never a hardcoded restatement of it.
    """
    check = next((c for c in observation.checks if c.name == "SEAL_UNSPENDABLE"), None)
    if check is None or check.status != invariants.FAIL:
        return ""
    return (
        '<section class="seal-failure-banner" data-banner="seal-failure">'
        '<h1 class="banner-headline">SEAL_UNSPENDABLE — FAIL</h1>'
        f'<p class="banner-body">{esc(check.detail)}</p>'
        f'<p class="banner-static">{esc(_SEAL_FAILURE_STATIC_SENTENCE)}</p>'
        "</section>"
    )


def _check_row(check) -> str:
    """One row per check: name, status badge, equation, and -- unlike
    `report.py`, which shows `detail` only for a non-passing check -- the
    complete `detail` for EVERY check, because this page's checks list is
    the evidence surface and the detail is the falsifying explanation
    itself. Never truncated, never ellipsised, never CSS `line-clamp`ed.
    """
    css_class = _status_class(check.status)
    label = _status_label(check.status)
    pieces = [
        f'<div class="check-row" data-check="{esc(check.name)}">',
        f'<span class="check-name">{esc(check.name)}</span>',
        f'<span class="badge {css_class}">{label}</span>',
        f'<span class="check-equation">{esc(check.equation)}</span>',
        f'<p class="check-detail">{esc(check.detail)}</p>',
    ]
    if check.expected is not None and check.actual is not None:
        pieces.append(f'<p class="check-expected-actual">expected {esc(check.expected)} | actual {esc(check.actual)}</p>')
    elif check.actual is not None:
        pieces.append(f'<p class="check-expected-actual">observed {esc(check.actual)}</p>')
    pieces.append("</div>")
    return "".join(pieces)


# -- The Burn: boost attribution, computed live from observation.burn_events --
BOOST_SOURCE = "boost_buy_and_burn"
EXPLORER_TX_PREFIX = "https://explorer.solana.com/tx/"
TX_LINK_LIMIT = 10


def _boost_summary(burn_events) -> dict:
    """Count, summed tokens, summed lamports and the window (max - min
    block_time) over only the boost-attributed rows -- every value derived
    at render time from the rows passed in, never a literal in this module.
    An empty list of boost rows returns zeroed values; the caller renders
    prose instead of numbers.
    """
    rows = [r for r in burn_events if r.get("source") == BOOST_SOURCE]
    if not rows:
        return {"count": 0, "tokens": 0, "lamports": 0, "window_seconds": 0}
    return {
        "count": len(rows),
        "tokens": sum(r["tokens_burned"] for r in rows),
        "lamports": sum(r.get("sol_spent") or 0 for r in rows),
        "window_seconds": max(r["block_time"] for r in rows) - min(r["block_time"] for r in rows),
    }


def _boost_sentence(summary: dict, decimals: int) -> str:
    """UI-SPEC's mandatory boost-attribution copy block, with its three
    numbers (tokens, transaction count, window) filled from `summary` --
    never pasted as string literals. `summary`'s own pinned-figure correction:
    an earlier UI-SPEC draft carried a stale window figure; this sentence is
    never that literal, only ever this computation's result.
    """
    if summary["count"] == 0:
        return (
            "No burns are recorded against $CHARLIE yet. The moment the evidence store "
            "records one, it is attributed here."
        )
    tokens_ui = summary["tokens"] / (10 ** decimals)
    tx_word = "transaction" if summary["count"] == 1 else "transactions"
    return (
        "Every token $CHARLIE has ever lost was destroyed by pump's boost, at migration "
        "-- not by any keeper of ours, and this protocol's own watcher could not see it "
        f"happen. Boost burned {tokens_ui:,.{decimals}f} tokens across {summary['count']} "
        f"{tx_word} in a {summary['window_seconds']}-second window. The protocol's own "
        "crank has never run for this coin -- no program is deployed yet."
    )


def _truncate_middle(value: str, head: int = 5, tail: int = 4) -> str:
    if len(value) <= head + tail + 1:
        return value
    return f"{value[:head]}…{value[-tail:]}"


def _tx_links(burn_events) -> str:
    """The first `TX_LINK_LIMIT` boost signatures as links -- full signature
    in both `href` and `title`, truncated-middle visible text -- followed by
    a count of how many more exist and a link to the raw record when more
    remain. Zero rows render prose, never an empty list. `href` is built only
    by concatenating `EXPLORER_TX_PREFIX` with an escaped signature, never
    from a stored URL, so no evidence-store value can occupy the scheme
    position.
    """
    rows = [r for r in burn_events if r.get("source") == BOOST_SOURCE]
    if not rows:
        return '<p class="no-burns">No burns are recorded for this mint yet.</p>'

    visible = rows[:TX_LINK_LIMIT]
    remainder = len(rows) - len(visible)
    items = []
    for row in visible:
        signature = str(row["signature"])
        href = EXPLORER_TX_PREFIX + esc(signature)
        display = esc(_truncate_middle(signature))
        items.append(
            f'<li><a href="{href}" title="{esc(signature)}">{display}'
            '<span class="visually-hidden"> (opens in Solana Explorer)</span></a></li>'
        )
    out = '<ul class="tx-links">' + "".join(items) + "</ul>"
    if remainder > 0:
        out += (
            f'<p class="tx-links-more">+ {remainder} more '
            '-- see the raw observation JSON</p>'
        )
    return out


# -- copy-to-clipboard control ---------------------------------------------
def _copy_button(address) -> str:
    """A `<button>` accelerator for the mint address, which stays selectable
    text in the header regardless -- the button is never the only route to
    the value. Icon is `aria-hidden="true"`/`focusable="false"`; the status
    span is `aria-live="polite"` so success/failure is announced, not only
    shown by swapping the glyph.
    """
    escaped_address = esc(address)
    return (
        f'<button type="button" class="copy-button" data-copy-address="{escaped_address}" '
        'aria-label="Copy mint address">'
        '<svg aria-hidden="true" focusable="false" viewBox="0 0 16 16" width="18" height="18">'
        '<rect x="3" y="3" width="9" height="9" fill="none" stroke="currentColor"/>'
        '</svg>'
        '</button>'
        '<span class="copy-status" aria-live="polite"></span>'
    )


_COPY_SCRIPT = """
document.querySelectorAll('.copy-button').forEach(function (btn) {
  var status = btn.nextElementSibling;
  var defaultLabel = 'Copy mint address';
  var successLabel = 'Copied';
  var failureLabel = 'Copy failed -- select the address to copy it manually';
  btn.addEventListener('click', function () {
    var address = btn.getAttribute('data-copy-address');
    function succeed() {
      btn.setAttribute('aria-label', successLabel);
      if (status) { status.textContent = successLabel; }
      setTimeout(function () {
        btn.setAttribute('aria-label', defaultLabel);
        if (status) { status.textContent = ''; }
      }, 2000);
    }
    function fail() {
      btn.setAttribute('aria-label', failureLabel);
      if (status) { status.textContent = failureLabel; }
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(address).then(succeed, fail);
    } else {
      fail();
    }
  });
});
"""


# -- D-20: the generator itself is unverified ------------------------------
_GENERATOR_UNVERIFIED = (
    "The publication sweep proves no ungated figure can leak onto this page. It does "
    "not prove the numbers shown are the right ones, because nothing independently "
    "checks that this renderer faithfully reflects the record it was generated from."
)
RISK_GENERATOR_ANCHOR = "risk-generator-unverified"

_RISKS = (
    "No program is deployed.",
    "There is no funding, and Phase 5 is gated on SOL that does not exist yet.",
    "Revoking upgrade authority is a one-way door.",
    "SEAL_UNSPENDABLE fails permanently for this coin, not pending.",
    "The opening-balance mechanism is dormant on live data (D-07).",
    "The mint-wide burn walk is incomplete, so the residual is not a settled figure -- "
    "see the committed reconciliation artifact (state/RECONCILIATION.md), never a number "
    "on this page.",
)


def _cannot_enroll() -> str:
    """UI-SPEC's mandatory copy block (domain context #2), rendered as plain
    prose with no card/border treatment -- it is context, not a figure.
    """
    return (
        '<section id="cannot-enroll">'
        "<p>$CHARLIE is the reference implementation -- the coin this protocol helps "
        "least. Its sharing config is <code>admin_revoked</code>: permanent, single "
        "shareholder, and only pump could ever reset it. $CHARLIE cannot enroll in its "
        "own protocol.</p>"
        "</section>"
    )


def _how_it_works() -> str:
    """Static prose: the three legs, adapted from PROTOCOL.md sec.1, with the
    permitted/forbidden claims table reproduced so a visitor does not have to
    leave the page to learn the vocabulary.
    """
    return (
        '<section id="how-it-works">'
        "<h2>How It Works</h2>"
        "<p>Three destinations for creator fees. They are not the same kind of object, "
        "and the protocol never calls them by the same word.</p>"
        '<table class="legs">'
        "<tr><th>Leg</th><th>Action</th><th>Permitted claim</th><th>Forbidden claim</th></tr>"
        "<tr><td>SEAL</td><td>SOL to an unspendable vault</td>"
        '<td>"removed from circulation"</td><td>"burned"</td></tr>'
        "<tr><td>BURN</td><td>SOL buys the token, then an SPL burn</td>"
        '<td>"permanently destroyed"</td><td>—</td></tr>'
        "<tr><td>OPS</td><td>SOL to a spendable wallet</td>"
        '<td>"funds operations"</td><td>"burned", "sealed"</td></tr>'
        "</table>"
        "</section>"
    )


def _the_burn(observation) -> str:
    """The boost-attribution mandatory copy block (figures computed live),
    the linked transaction list, and BURN_ATOMIC's own live not-applicable
    detail, rendered verbatim as prose -- it backs no figure today (D-14).
    """
    decimals = observation.mint_state.decimals if observation.mint_state is not None else 6
    summary = _boost_summary(observation.burn_events)
    sentence = _boost_sentence(summary, decimals)
    tx_links = _tx_links(observation.burn_events)
    atomic_check = next((c for c in observation.checks if c.name == "BURN_ATOMIC"), None)
    atomic_paragraph = (
        f'<p class="burn-atomic-detail">{esc(atomic_check.detail)}</p>' if atomic_check is not None else ""
    )
    return (
        '<section id="the-burn">'
        "<h2>The Burn</h2>"
        f"<p>{esc(sentence)}</p>"
        f"{tx_links}"
        f"{atomic_paragraph}"
        "</section>"
    )


def _quiet(observation) -> str:
    """Honest about what does not apply -- the window figure is computed
    from the same `_boost_summary`, never a second, independently hardcoded
    copy of the number `_the_burn` already computed.
    """
    summary = _boost_summary(observation.burn_events)
    return (
        '<section id="quiet">'
        "<h2>Quiet</h2>"
        "<p>$CHARLIE has no BURN leg -- its split is 100% SEAL. There is no crank to "
        "pause or resume, because none has ever run. The one burn event in this coin's "
        f"history was pump's boost, a single {esc(summary['window_seconds'])}-second "
        "window at migration, not a recurring mechanism.</p>"
        "</section>"
    )


def _log() -> str:
    """Today's expected empty state -- no protocol crank has ever run,
    against any coin, until phase 5 deploys a program.
    """
    return (
        '<section id="log">'
        "<h2>Log</h2>"
        "<h3>No cranks yet</h3>"
        "<p>The protocol's crank has never run for $CHARLIE -- no program is deployed "
        '(see <a href="#risks">Risks</a>). Every burn recorded against this mint so far '
        "was pump's boost, not any keeper of ours -- see "
        '<a href="#the-burn">The Burn</a>.</p>'
        "</section>"
    )


REPO_URL = "https://github.com/needsmorergb/charlie-protocol-v1"


def _footer() -> str:
    """Links to the repository -- no social or marketing content.

    `git remote -v` is empty in this working tree, and that is NOT evidence
    that the project is unpublished: the public repo is a *filtered* publish
    pushed from a throwaway clone (see PUBLISHING.md), so this tree has no
    remote by design. `charlie-protocol-v1` is public and carries the spec,
    the architecture, the build log and this code, so the link resolves.

    There is deliberately no `charlie-mode` link: that repository was
    published and then deleted, and the spec it held now lives here.
    """
    return (
        "<footer>"
        f'<p>Spec, code and committed evidence: <a href="{esc(REPO_URL)}">'
        f"{esc(REPO_URL)}</a></p>"
        "<p>Every figure above is recomputable from the record published "
        "beside this page.</p>"
        "</footer>"
    )


# -- WEB-06/D-16: the raw record, linked and honestly described -----------
def _raw_record_link(observation) -> str:
    """UI-SPEC's Copywriting Contract Primary CTA, verbatim: a plain link
    (nothing is submitted or triggered -- the JSON already exists), href
    derived from the same `_artifact_name` helper `write()` uses, so the link
    and the sibling file it points at can never disagree. Visually-hidden
    suffix follows the accessible-labelling clip pattern (`.visually-hidden`),
    not `display:none`.
    """
    href = esc(_artifact_name(observation.mint, ".json"))
    return (
        f'<a href="{href}">View the raw observation JSON'
        '<span class="visually-hidden"> (opens the raw observation record)</span></a>'
    )


_HISTORY_COMMAND = "git log -p web/{name}"


def _history_note(observation) -> str:
    """D-19: two plain sentences -- this page and its record are overwritten
    on every build, and every previous version lives in the repository's git
    history -- followed by the exact command that shows it, rendered against
    this page's own filename via `_artifact_name` so the command can never
    name a file the generator does not produce. Says what the command shows
    (the diff at which a figure moved between published and withheld), not
    just that it exists.
    """
    name = _artifact_name(observation.mint, ".html")
    command = _HISTORY_COMMAND.format(name=name)
    return (
        '<div class="history-note">'
        "<p>Every build overwrites this page and its record in place -- there "
        'is no dated series and no "latest" pointer.</p>'
        "<p>Every version this page has ever had is in this repository's git "
        "history. The command below shows the diff at which a figure moved "
        "between published and withheld:</p>"
        f"<pre><code>{esc(command)}</code></pre>"
        "</div>"
    )


_RECORD_LIMIT_STATEMENT = (
    "This record proves that the page above faithfully renders it. It does "
    "not prove that the record itself matches the chain -- that is a "
    "different check, and it is the indexer's job, not the page's."
)


def _raw_record_section(observation) -> str:
    """Page Structure item 10 (WEB-06/D-16): the primary-CTA link repeated,
    the record's own framing, the limit D-16 says to state rather than hide
    (with an inline link to the Risks entry naming the generator itself as
    unverified -- D-20 -- since this section is where the page's
    verification claim is strongest and therefore where that limit is most
    likely to be over-read), D-19's history pointer immediately after the
    limit statement, and the route to verify the underlying transaction list
    without bundling it into the published record (D-16's rejected
    alternative).
    """
    link = _raw_record_link(observation)
    evidence_href = esc(f"../{EVIDENCE_EXPORT_PATH}/")
    return (
        '<section id="raw-record">'
        "<h2>Raw Observation JSON</h2>"
        f"<p>{link} -- the exact record this page was generated from. "
        "Recompute it yourself rather than trust it.</p>"
        f'<p class="record-limit">{esc(_RECORD_LIMIT_STATEMENT)} See '
        f'<a href="#{RISK_GENERATOR_ANCHOR}">the generator-unverified risk</a> '
        "for the reason this page cannot check itself.</p>"
        f"{_history_note(observation)}"
        "<p>Verify the boost transaction list yourself in the committed "
        f'evidence export: <a href="{evidence_href}">{evidence_href}</a></p>'
        "</section>"
    )


def _sections(observation) -> str:
    """How It Works, The Burn, Quiet, Log and Risks, in UI-SPEC's Page
    Structure order (items 5-9) -- everything after the checks list and
    before the footer. The cannot-enroll statement (item 2) and the Seal
    Failure Banner (item 3) are rendered earlier in `render()`, ahead of the
    Figures section (item 4), matching UI-SPEC's approved structural order
    exactly.

    Risks is built inline here, not in a helper, so D-20's seventh entry --
    the sweep's claim and its limit, carried in the module's one generator-
    unverified constant -- is interpolated exactly once in this function's
    own source, never re-typed apart from it.
    """
    risk_items = [f"<li>{esc(text)}</li>" for text in _RISKS]
    risk_items.append(f'<li id="{RISK_GENERATOR_ANCHOR}">{esc(_GENERATOR_UNVERIFIED)}</li>')
    risks = '<section id="risks"><h2>Risks</h2><ol class="risks">' + "".join(risk_items) + "</ol></section>"

    return "".join(
        (
            _how_it_works(),
            _the_burn(observation),
            _quiet(observation),
            _log(),
            risks,
        )
    )


_STYLE = """
:root {
  --paper: #FAF7F0;
  --panel: #EFEAE0;
  --accent: #1D4E89;
  --destructive: #A3271F;
  --unchecked: #7A5A12;
  --ink: #1A1A1A;
  --pass-glyph: #6B6558;
  --sp-xs: 4px;
  --sp-sm: 8px;
  --sp-md: 16px;
  --sp-lg: 24px;
  --sp-xl: 32px;
  --sp-2xl: 48px;
  --sp-3xl: 64px;
}
* { box-sizing: border-box; }
body {
  background: var(--paper);
  color: var(--ink);
  font-family: ui-monospace, "SFMono-Regular", Menlo, Consolas, "Liberation Mono", monospace;
  font-size: 16px;
  font-weight: 400;
  line-height: 1.5;
  margin: 0;
  padding: var(--sp-3xl) var(--sp-lg);
}
h1 { font-size: 32px; font-weight: 700; line-height: 1.2; margin: 0 0 var(--sp-md) 0; }
h2 { font-size: 20px; font-weight: 700; line-height: 1.2; margin: var(--sp-xl) 0 var(--sp-md) 0; }
.meta { font-size: 14px; font-weight: 400; line-height: 1.5; }
section { margin-bottom: var(--sp-xl); }
.figure-row {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--sp-sm);
  background: var(--panel);
  padding: var(--sp-md);
  margin-bottom: var(--sp-lg);
  border-left: 1px dashed var(--unchecked);
}
.figure-row.figure-withheld { border-left: 1px dashed var(--unchecked); }
.figure-label { font-size: 14px; font-weight: 400; }
.figure-value { font-size: 16px; font-weight: 400; }
.figure-backs { font-size: 14px; font-weight: 400; }
.badge {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-xs);
  padding: 2px 8px;
}
.status-pass {
  color: var(--pass-glyph);
  background: none;
  font-size: 14px;
  font-weight: 400;
}
.status-fail {
  background: var(--destructive);
  color: #fff;
  font-size: 20px;
  font-weight: 700;
}
.status-unchecked {
  border: 1px dashed var(--unchecked);
  color: var(--unchecked);
  font-size: 14px;
  font-weight: 400;
}
.figure-row.status-fail-row {
  border-left: 4px solid var(--destructive);
  background: rgba(163, 39, 31, 0.08);
}
a { color: var(--accent); }
.freshness { margin-bottom: var(--sp-md); }
.freshness .meta { margin: 0 0 var(--sp-xs) 0; }
.seal-failure-banner {
  background: var(--destructive);
  color: #fff;
  padding: var(--sp-lg);
  margin: var(--sp-2xl) 0;
}
.banner-headline { font-size: 32px; font-weight: 700; line-height: 1.2; margin: 0 0 var(--sp-md) 0; color: #fff; }
.banner-body { font-size: 16px; font-weight: 400; margin: 0 0 var(--sp-md) 0; }
.banner-static { font-size: 16px; font-weight: 700; margin: 0; }
.check-row {
  padding: var(--sp-md);
  margin-bottom: var(--sp-md);
}
.check-name { font-size: 14px; font-weight: 400; }
.check-equation { font-size: 14px; font-weight: 400; }
.check-detail { font-size: 16px; font-weight: 400; margin: var(--sp-xs) 0 0 0; }
.check-expected-actual { font-size: 14px; font-weight: 400; margin: var(--sp-xs) 0 0 0; }
.error-state p { font-size: 16px; font-weight: 400; }
.copy-button {
  min-width: 44px;
  min-height: 44px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: none;
  border: 1px dashed var(--unchecked);
  cursor: pointer;
  color: var(--ink);
}
.copy-status { margin-left: var(--sp-sm); font-size: 14px; }
.visually-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}
.legs { border-collapse: collapse; width: 100%; }
.legs th, .legs td { text-align: left; padding: var(--sp-sm); border-bottom: 1px solid var(--panel); }
.tx-links { list-style: none; margin: var(--sp-md) 0; padding: 0; }
.tx-links li { margin-bottom: var(--sp-xs); }
.risks { padding-left: var(--sp-lg); }
.raw-record-cta { margin: var(--sp-sm) 0 0 0; }
.record-limit { font-weight: 700; }
.history-note pre {
  background: var(--panel);
  padding: var(--sp-md);
  margin: var(--sp-xs) 0 var(--sp-md) 0;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-all;
}
.history-note code { -webkit-user-select: all; user-select: all; }
"""


def _document(mint: str, body: str) -> str:
    """Wraps `body` (already-built HTML) in the page shell -- the one place
    `<!doctype html>`/`<head>`/`<style>` are assembled, shared by both the
    normal render path and the page-level error branch.
    """
    return (
        "<!doctype html>"
        '<html lang="en">'
        "<head>"
        '<meta charset="utf-8">'
        f"<title>{mint} -- Charlie Protocol</title>"
        f"<style>{_STYLE}</style>"
        "</head>"
        f"<body>{body}</body>"
        "</html>"
    )


def render(observation, *, now=None) -> str:
    """A complete HTML5 document for one observation. Single required
    positional parameter -- `publish.render_surface()` calls `target(subject)`
    with exactly one positional argument for an `"observation"`-input
    `SURFACES` entry; `now` is keyword-only with a default so that call site
    is unaffected. `now` defaults to `time.time()` -- the same injectable-clock
    shape `observe()` already uses -- so the D-18 freshness block's age is
    computed here at generation time and is assertable without freezing time.
    """
    now = now if now is not None else time.time()
    mint = esc(observation.mint)
    freshness = _freshness(observation, now)
    header = (
        "<header>"
        f"<h1>{mint}</h1>"
        f"{_copy_button(observation.mint)}"
        f"{freshness}"
        f'<p class="raw-record-cta">{_raw_record_link(observation)}</p>'
        "</header>"
    )

    if observation.error and observation.config is None:
        # Mirrors report.py's established voice (report.py:52-58), translated
        # to markup per UI-SPEC's Copywriting Contract error-state entry. A
        # tick that could not read the chain is part of the record, not an
        # absence from it -- no figure rows, no sections, below the freshness
        # block a failed observation still carries.
        body = (
            header
            + '<section class="error-state">'
            + f"<p>No observation: {esc(observation.error)}. Recorded as a failed "
            "observation -- a tick that could not read the chain is part of the "
            "record, not an absence from it.</p>"
            + "<p>See the observation history: <code>python -m indexer log</code>.</p>"
            + "</section>"
        )
        return _document(mint, body + f"<script>{_COPY_SCRIPT}</script>")

    publisher = publish.Publisher(observation)
    banner = _seal_failure_banner(observation)
    figure_rows = "".join(_figure_row(publisher, name) for name in invariants.FIGURES)
    checks_rows = "".join(_check_row(check) for check in observation.checks)

    # Structural order follows UI-SPEC's Page Structure & Component Inventory
    # exactly: header (1) -> cannot-enroll (2) -> Seal Failure Banner (3) ->
    # Figures (4) -> [checks list, not separately numbered there] -> How It
    # Works/The Burn/Quiet/Log/Risks (5-9, `_sections()`) -> Raw Observation
    # JSON (10, `_raw_record_section()`) -> footer (11).
    body = (
        header
        + _cannot_enroll()
        + banner
        + '<section id="figures">'
        + "<h2>Figures</h2>"
        + figure_rows
        + "</section>"
        + '<section id="checks">'
        + "<h2>Checks</h2>"
        + checks_rows
        + "</section>"
        + _sections(observation)
        + _raw_record_section(observation)
        + _footer()
        + f"<script>{_COPY_SCRIPT}</script>"
    )

    return _document(mint, body)


def record_json(observation) -> str:
    """WEB-06's payload (D-16): the gated durable record, serialised with
    sorted keys so two runs over an unchanged observation are byte-identical
    (`export.py`'s existing determinism discipline). Every `json.dumps` call
    in this module lives inside this one function.
    """
    return json.dumps(publish.durable_record(observation), sort_keys=True, indent=2)


def write(observation, out_dir=DEFAULT_OUTPUT_DIR) -> tuple[Path, Path]:
    """Writes `render(observation)` to `<mint>.html` and `record_json(observation)`
    (plus a trailing newline) to `<mint>.json`, sibling paths differing only
    by extension -- WEB-06's "published beside the page" (02-03's in-page
    raw-JSON link depends on this sibling relationship). Calls neither
    `print` nor `json.dumps` directly; delegates to `render`/`record_json`,
    which are already-classified `SURFACES` targets.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / _artifact_name(observation.mint, ".html")
    json_path = out_dir / _artifact_name(observation.mint, ".json")
    html_path.write_text(render(observation), encoding="utf-8", newline="\n")
    json_path.write_text(record_json(observation) + "\n", encoding="utf-8", newline="\n")
    return html_path, json_path

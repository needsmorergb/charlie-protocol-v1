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
    header = f"<header><h1>{mint}</h1>{freshness}</header>"

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
        return _document(mint, body)

    publisher = publish.Publisher(observation)
    banner = _seal_failure_banner(observation)
    figure_rows = "".join(_figure_row(publisher, name) for name in invariants.FIGURES)
    checks_rows = "".join(_check_row(check) for check in observation.checks)

    body = (
        header
        + banner
        + '<section id="figures">'
        + "<h2>Figures</h2>"
        + figure_rows
        + "</section>"
        + '<section id="checks">'
        + "<h2>Checks</h2>"
        + checks_rows
        + "</section>"
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
    html_path = out_dir / f"{observation.mint}.html"
    json_path = out_dir / f"{observation.mint}.json"
    html_path.write_text(render(observation), encoding="utf-8", newline="\n")
    json_path.write_text(record_json(observation) + "\n", encoding="utf-8", newline="\n")
    return html_path, json_path

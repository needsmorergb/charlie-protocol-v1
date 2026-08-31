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

from . import invariants, publish


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
"""


def render(observation) -> str:
    """A complete HTML5 document for one observation. Single positional
    parameter -- `publish.render_surface()` calls `target(subject)` with
    exactly one positional argument for an `"observation"`-input `SURFACES`
    entry.
    """
    publisher = publish.Publisher(observation)
    mint = esc(observation.mint)

    figure_rows = "".join(_figure_row(publisher, name) for name in invariants.FIGURES)

    body = (
        f"<header>"
        f"<h1>{mint}</h1>"
        f'<p class="meta">observed at {esc(observation.observed_at)}</p>'
        f"</header>"
        f"<section>"
        f"<h2>Figures</h2>"
        f"{figure_rows}"
        f"</section>"
    )

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


def record_json(observation) -> str:
    """WEB-06's payload (D-16): the gated durable record, serialised with
    sorted keys so two runs over an unchanged observation are byte-identical
    (`export.py`'s existing determinism discipline). Every `json.dumps` call
    in this module lives inside this one function.
    """
    return json.dumps(publish.durable_record(observation), sort_keys=True, indent=2)

"""`/campaigns/<mint>` -- a coin's declared goals, beside what the chain shows.

THE CLAIM THIS PAGE MAKES, and the one it refuses to make. A campaign is the
only thing on this site that somebody *said* rather than something the chain
did. So every row here is two different kinds of statement side by side, and
the page's whole job is to keep them apart:

* **the goal** -- a target, a trigger, a name. Declared. Not measured, not
  checked, and never presented as either.
* **the progress** -- recorded burns, obtained through `publish.Publisher`
  exactly as every other figure on this site is. When the check behind it has
  not passed, the bar does not fill and the number does not appear: the page
  prints the name of the check that stopped it, in the same words the coin
  page uses.

WHY A WITHHELD CAMPAIGN LOOKS EMPTY RATHER THAN ZERO. "We cannot say" and
"nothing happened" are different statements, and a progress bar cannot say
both. A campaign whose figure is withheld renders with no bar at all and the
blocking check named -- never a 0% bar, which a reader would take as a
measurement of inactivity. `campaign.Progress` carries `value is None` for
exactly this case and `format_amount` renders it "not published".

WHAT THIS PAGE DOES NOT DO. No projection, no countdown implying a target
will be met, no price and no dollar figure -- the rule `/dilution` and
`/flywheel` already run under (PROTOCOL.md sec.2: no mode creates a price
floor, and a page that implied one would be making the claim the spec
forbids). A campaign short of its goal is rendered as what it is, with no
language suggesting the shortfall is temporary.

Its own module for the same reason `flywheel_page` and `splitter_page` are:
`site.py` imports only `invariants` and `publish` by design, and a test holds
that.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import campaign, site

CAMPAIGNS_DIRNAME = "campaigns"


def page_filename(mint: str) -> str:
    """One page per coin, under `campaigns/`. The mint is the filename, as it
    is for a coin page -- `site._artifact_name`'s convention, kept so a
    reader who knows one URL can guess the other.
    """
    return f"{CAMPAIGNS_DIRNAME}/{mint}.html"


def _status_word(status: str) -> str:
    """The status in a reader's words, not the store's."""
    return {
        campaign.STATUS_ACTIVE: "open",
        campaign.STATUS_REACHED: "target reached",
        campaign.STATUS_CLOSED: "closed",
        campaign.STATUS_CANCELLED: "cancelled",
    }.get(status, status)


def _trigger_sentence(row: dict) -> str:
    """What starts this campaign counting, said plainly."""
    trigger = row["trigger_type"]
    if trigger == campaign.TRIGGER_MANUAL:
        return "counts every recorded burn since it was declared"
    if trigger == campaign.TRIGGER_SCHEDULE:
        return "counts burns inside its declared window"
    if trigger == campaign.TRIGGER_SOL_AMOUNT:
        return (
            "closes once recorded burns reach "
            f"{campaign.format_amount(row['trigger_value'], campaign.ASSET_SOL)}"
        )
    if trigger == campaign.TRIGGER_TOKEN_AMOUNT:
        return f"closes once recorded burns reach {row['trigger_value']:,} raw token units"
    return trigger


def _bar(progress) -> str:
    """The progress bar -- ABSENT when the figure is withheld.

    Not a 0% bar and not a greyed-out one: a bar is a measurement, and a
    withheld figure has none to draw. The caller renders the blocking check
    instead.
    """
    percent = progress.percent
    if percent is None:
        return ""
    width = f"{percent:.1f}"
    return (
        '<div class="bar" role="img" '
        f'aria-label="{width}% of the target, from recorded burns">'
        f'<span style="width:{width}%"></span>'
        "</div>"
    )


def _withheld(progress) -> str:
    """Why there is no figure, in the coin page's own vocabulary."""
    # The plain sentence first, the check's own name after it. Leading with a
    # bare identifier like SOL_BURN_BALANCE reads as an error code to anyone
    # who has not seen a coin page -- "something is broken" rather than "not
    # checkable yet", which is the exact misreading this section exists to
    # prevent. The name stays, because a figure's backing check is what makes
    # the withholding verifiable rather than an excuse.
    items = "".join(
        f"<li>{site.esc(detail)} "
        f'<span class="check">(check: <code>{site.esc(name)}</code>, '
        f"{site.esc(status)})</span></li>"
        for name, status, detail in progress.withheld_by
    )
    return (
        '<div class="withheld">'
        "<p><strong>No progress figure is published for this campaign.</strong> "
        "The burns behind it are not backed by a passing check, and this "
        "protocol does not publish a number a check has not cleared &mdash; "
        "an absence of evidence is not a total.</p>"
        f"<ul>{items}</ul>"
        "</div>"
    )


def _campaign_section(row: dict, progress) -> str:
    """One campaign: the goal, then what the chain records toward it."""
    target = campaign.format_amount(row["target_value"], row["asset"])
    value = campaign.format_amount(progress.value, row["asset"])
    description = (
        f'<p class="description">{site.esc(row["description"])}</p>'
        if row.get("description")
        else ""
    )

    if progress.withheld_by:
        measured = _withheld(progress)
    else:
        backing = ", ".join(progress.backed_by)
        measured = (
            f"{_bar(progress)}"
            f'<p class="measured"><strong>{site.esc(value)}</strong> of the '
            f"{site.esc(target)} goal, recorded</p>"
            + (
                f'<p class="backing">backed by <code>{site.esc(backing)}</code></p>'
                if backing
                else ""
            )
        )

    # The declared goal is deliberately NOT bolded, and the recorded figure
    # is. They were once typographically identical -- same function, same
    # units, same weight -- so a reader skimming only the numbers saw two
    # that looked equally authoritative, and the sentence telling them apart
    # was the only thing doing that work. One of these is a claim somebody
    # typed; the other survived a check. They should not look alike.
    return (
        '<section class="campaign">'
        f'<h2>{site.esc(row["name"])} '
        f'<span class="status status-{site.esc(row["status"])}">'
        f'{site.esc(_status_word(row["status"]))}</span></h2>'
        f"{description}"
        '<p class="goal"><strong>The goal, as declared:</strong> '
        f'<em>{site.esc(target)}</em> &mdash; {site.esc(_trigger_sentence(row))}. '
        "This is a statement the coin made, not a measurement.</p>"
        f"{measured}"
        "</section>"
    )


_STYLE = site._TOKENS + """
body {
  margin: 0 auto; padding: var(--sp-lg); max-width: 52rem;
  background: var(--paper); color: var(--ink);
  font-family: ui-monospace, "SFMono-Regular", Menlo, Consolas, "Liberation Mono", monospace;
  line-height: 1.6;
}
h1 { font-size: 1.5rem; line-height: 1.25; margin: 0 0 var(--sp-sm); }
h2 { font-size: 1.05rem; margin: 0 0 var(--sp-sm); }
p { margin: 0 0 var(--sp-md); }
a { color: var(--accent); }
code { background: var(--panel); padding: 1px 4px; }
.lede { font-size: 1.02rem; }
.campaign {
  border: 1px solid var(--panel); padding: var(--sp-md); margin: var(--sp-lg) 0;
}
.status {
  font-size: 0.72rem; font-weight: 400; text-transform: uppercase;
  letter-spacing: 0.04em; padding: 1px 6px; border: 1px solid currentColor;
}
.status-reached { color: var(--accent); }
.status-closed, .status-cancelled { color: var(--pass-glyph); }
.goal, .description { font-size: 0.9rem; }
.goal em { font-style: normal; color: var(--pass-glyph); }
.check { color: var(--pass-glyph); font-size: 0.78rem; }
.bar {
  height: 10px; background: var(--panel); margin: var(--sp-md) 0 var(--sp-sm);
}
.bar span { display: block; height: 100%; background: var(--accent); }
.measured { margin-bottom: var(--sp-xs); }
.backing { font-size: 0.8rem; color: var(--pass-glyph); margin-bottom: 0; }
.withheld {
  border-left: 2px solid var(--unchecked); padding-left: var(--sp-md);
  margin-top: var(--sp-md);
}
.withheld ul { font-size: 0.82rem; padding-left: var(--sp-lg); }
.withheld p { font-size: 0.88rem; }
.none { color: var(--pass-glyph); }
footer {
  margin-top: var(--sp-3xl); border-top: 1px solid var(--panel);
  padding-top: var(--sp-md); font-size: 0.82rem; color: var(--pass-glyph);
}
"""


def render(mint: str, results, *, now=None) -> str:
    """`results` is `campaign.evaluate()`'s output: `[(row, Progress, status)]`.

    Takes the already-evaluated list rather than an observation and a store,
    so this module cannot compute a figure of its own -- it renders what the
    gate already decided, which is the same division `flywheel_page` keeps
    between rendering a run and producing one.
    """
    now = time.gmtime(now if now is not None else time.time())
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", now)

    if results:
        sections = "".join(_campaign_section(row, progress) for row, progress, _s in results)
    else:
        sections = (
            '<p class="none">This coin has declared no burn campaigns. '
            "Nothing is claimed here, and nothing is measured.</p>"
            f'<p><a href="/{site.esc(mint)}.html">What is measured for this coin</a> '
            'is on its own page, campaigns or not.</p>'
        )

    body = [
        "<h1>Burn campaigns</h1>",
        f'<p class="lede">What <code>{site.esc(mint)}</code> said it would burn, '
        "beside what the chain has recorded toward it. The two are different "
        "kinds of statement and this page keeps them apart: a goal is declared, "
        "a total is recorded.</p>",
        sections,
        "<h2>How to read this</h2>",
        "<p>A <strong>goal</strong> is a claim the coin made. Nothing verifies "
        "it, because there is nothing yet to verify &mdash; it is an intention.</p>",
        "<p>A <strong>recorded</strong> figure is a burn total, and it appears "
        "only when a check backs it. Where a check has not passed, this page "
        "prints the check's name instead of a number and draws no bar. That is "
        "deliberate: an empty bar would read as \"nothing burned\", and the "
        "honest statement is \"this protocol cannot say yet\".</p>",
        "<p>A campaign short of its target is simply short of its target. "
        "Nothing here predicts that it will be met, and no figure on this page "
        "is a price, a projection, or a promise.</p>",
        "<footer>"
        f"<p>generated at {site.esc(stamp)}. Every figure is recomputed from "
        "recorded evidence at render time; none is stored on the campaign.</p>"
        f'<p><a href="/">Charlie Protocol</a> &middot; '
        f'<a href="/{site.esc(mint)}.html">this coin\'s page</a> &middot; '
        '<a href="/verify">/verify</a></p>'
        "</footer>",
    ]

    return site._document(
        f"Burn campaigns -- {mint} -- Charlie Protocol",
        "".join(body),
        style=_STYLE,
        description=(
            "What this coin said it would burn, beside what the chain has "
            "recorded toward it. Goals are declared; totals are checked, and "
            "a figure no check backs is not published."
        ),
    )


def write(mint: str, results, out_dir=site.DEFAULT_OUTPUT_DIR, *, now=None) -> Path:
    path = Path(out_dir) / page_filename(mint)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(mint, results, now=now), encoding="utf-8")
    return path

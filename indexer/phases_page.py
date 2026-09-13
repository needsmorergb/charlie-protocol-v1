"""`/phases` -- the five phases, and which of them the checks say are done.

The project has stated its own completion criterion since the README's first
version, and this page is that sentence made mechanical:

    "done" for each one means a check that currently reads UNCHECKED returns
    PASS or FAIL -- not that code exists.

So no phase on this page is marked complete by hand. A phase whose criterion
is a set of named checks gets its state computed from those checks on the
observation this page is rendered against, and a phase whose criterion is not
a check says so in those words rather than borrowing a green badge it has not
earned.

DATES, AND WHY THIS PAGE CARRIES THEM WHEN `buildlog_page` REFUSES TO.
That module bans future dates for two reasons, and only one of them is
general. The general one is that it is a LOG: a record of what happened has no
business carrying what has not. The specific one was that phase 5 was gated on
funding that did not exist, so any date beside it was a promise about someone
else's money.

This page is a plan, not a log, and a plan with no dates is not a plan. So it
carries two kinds of date and never confuses them:

    landed   a date in the past. A fact. What `buildlog_page` records.
    target   a date this project has committed to in public, with the thing
             it depends on named beside it.

The discipline that makes a target publishable here is the same one that makes
a figure publishable anywhere else on this site: it has to be able to fail in
public. When a target passes and the phase's criterion has not been met, this
page says SLIPPED and prints how many days late it is. It does not move the
date, and no script may move it -- editing a target is an edit to this file,
reviewed like the claim it is.

A missed target a reader can see is worth more than no target at all. Every
roadmap in this category quietly slides; the differentiator is not hitting
dates, it is being the only one that shows you did not.

WHAT MAY BE CLAIMED HERE. The program is deployed to DEVNET. The word
"deployed" never appears on this page without a cluster beside it. No figure
a check withholds appears here at all -- this page reports the STATE of
checks, never the figures behind them, which is why it is a non-figure
emitter (see `publish.NON_FIGURE_EMITTERS`).

Its own module for the same reason `dilution_page`, `buildlog_page` and
`enroll_page` are: `site.py` imports only `invariants` and `publish` by
design and a test holds that.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timezone
from pathlib import Path

from . import invariants, site

PHASES_FILENAME = "phases.html"

# ---------------------------------------------------------------------------
# On the numbering, because it changed and a reader may have seen the old one.
#
# `buildlog_page._ENTRIES` numbers phase 1 as the evidence walk and phase 2 as
# the public surface, and names no phase 3 at all. That numbering started
# counting at the repository, and the project did not start at the repository:
# it started at a coin with a permanent split and a watcher that refused to
# post when its own arithmetic broke, three weeks before the spec existed.
#
# Counting from there closes the gap that had no phase 3 in it, and it is the
# honest arc: prove it on one coin, generalise it to any coin, open it to other
# people, build the thing that enforces it, make that thing immutable.
#
# `buildlog_page._ENTRIES` must be updated to match, or the two pages disagree
# about what phase 1 was. See the checklist in the handoff.
#
# The placeholder machinery below stays. There will be a phase 6, and the next
# person to add one should hit the same wall rather than shipping a blank.
_UNDEFINED = "<<UNDEFINED>>"


# ---------------------------------------------------------------------------
# The phases.
#
# `gates` is the set of check names whose status decides this phase.
#
# A phase with gates carries NO hand-written status at all. There is nothing
# for a check to be overridden by, which is the point: a check-gated phase
# renders SHIPPED only when its checks actually return a verdict on the
# observation this page was rendered against, and OPEN whenever they have not
# -- including when this render had no observation to read. A page that filled
# that gap with an author's assertion would be marking a phase complete by
# hand, which is the single thing this page exists to refuse.
#
# `stated` therefore appears only where no check was ever going to decide the
# phase, and it is a fact about the world rather than a completion claim:
#
# stated: "DEVNET"  -- on devnet only, id given, mainnet is not this
#         "GATED"   -- blocked on funding that does not exist. Phase 5 has
#                      gates AND this, because "not yet paid for" is not the
#                      same as "not yet measured" and OPEN would imply the
#                      second. Its checks still render their own status
#                      beneath it, so nothing is hidden by saying so.
_PHASES = (
    {
        "n": 1,
        "title": "$CHARLIE, and the watcher that proved the thesis on it",
        "landed": "2026-08-23",
        "what": "Before there was a protocol there was one coin and one "
        "question: can a fee stream be routed somewhere no key can spend, and "
        "can a stranger confirm it. $CHARLIE answered it by construction. Its "
        "sharing config is admin_revoked with a single shareholder at 10000 "
        "bps, so its split is permanent and its creator cannot move it. Mint "
        "and freeze authority are both revoked, so nothing it destroys can be "
        "reissued. The burn watcher has run against it since this date, "
        "reconciling opening plus recorded inflows against the balance on "
        "every pass and refusing to post when that identity breaks. The "
        "silence rule was running in production before it was written down.",
        "criterion": "The two checks that can be answered from the coin alone, "
        "with no evidence walk and no protocol, return a verdict on $CHARLIE: "
        "that its burn destination cannot be spent, and that what it destroys "
        "cannot be reissued.",
        "gates": ("SOL_BURN_UNSPENDABLE", "BURN_IRREVERSIBLE"),
        "blocked_by": None,
        "code": "8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump",
        "code_label": "$CHARLIE mint",
    },
    {
        "n": 2,
        "title": "The spec, and an indexer that reads any coin",
        "landed": "2026-08-29",
        "what": "The specification was published on the day the code that "
        "implements it did not exist, which is the ratio the project wanted: "
        "the invariants can be argued with before anything depends on them. "
        "The indexer landed the same day, reading a pump sharing config, "
        "attributing every shareholder to a leg, and appending the result to "
        "an append-only log. It consults no allowlist, no enrollment flag and "
        "no consent table, so it reads any pump coin whether or not that coin "
        "has ever heard of this protocol. The first check it ran failed, on "
        "$CHARLIE, and that entry is still in the build log with its "
        "retraction attached.",
        "criterion": "The two checks that need only a config, not an evidence "
        "walk, return a verdict on any coin submitted: that the config read "
        "belongs to the coin asked about, and that its split accounts for the "
        "whole fee stream.",
        "gates": ("CONFIG_MINT", "SPLIT_SUM"),
        "blocked_by": None,
    },
    {
        "n": 3,
        "title": "The evidence walk, the public surface, and the crank",
        "landed": "2026-09-05",
        "what": "Fee inflows reconciled per destination, SPL burn and boost "
        "decoders, and initial supply derived from the coin's own CreateEvent. "
        "Then the surface that publishes it: the coin page, its raw JSON "
        "record, /verify taking a pasted address with no wallet and no signup, "
        "and the index. Then the parts that make it run for someone else: "
        "enrollment front to back in one signature, the BURN leg on both "
        "venues, and a crank paying every enrolled coin hourly through pump's "
        "own permissionless instruction. Built deliberately ahead of the "
        "deploy gate rather than behind it.",
        "criterion": "Every check that needs recorded evidence stops reading "
        "UNCHECKED and returns a verdict, and whether a coin is enrolled is "
        "read off the chain rather than asserted by this project about its "
        "own membership list.",
        "gates": (
            "SOL_BURN_BALANCE",
            "BURN_SUPPLY",
            "OPS_ROUTED",
            "PROTOCOL_SHARE",
        ),
        "blocked_by": None,
    },
    {
        "n": 4,
        "title": "The program",
        "landed": "2026-09-12",
        "what": "Four instructions: init_charlie_pool, init_route, set_route, "
        "distribute. The guarantee is the absence of code. Nothing debits a "
        "coin's collector except distribute, and no instruction anywhere sends "
        "to an address its caller supplied. Deployed to DEVNET at the id "
        "below; mainnet gets its own keypair and is not this.",
        "criterion": "No check gates this one, and it is the phase where "
        "saying so matters most: a program existing is not a check passing. "
        "The checks it will eventually turn on are phase 5's, because they "
        "need a mainnet program id and this is not one.",
        "gates": (),
        "stated": "DEVNET",
        "blocked_by": None,
        "code": "GFA3nG9geMhpPXaExVLGYBtj6aJX7S125dLzv4EcXGiG",
        "code_label": "devnet program id",
    },
    {
        "n": 5,
        "title": "Mainnet deploy, and revoking upgrade authority",
        "landed": None,
        "what": "The absence-of-code guarantee only means anything once the "
        "program is immutable, and revoking upgrade authority is a one-way "
        "door that freezes every bug permanently. So the order is: deploy "
        "upgradeable, run the whole pipeline in production against one live "
        "coin including a graduated one, then revoke and publish a "
        "reproducible build.",
        "criterion": "The two checks that have no mainnet program to measure "
        "stop reading UNCHECKED and return a verdict. Until then the indexer "
        "holds PROGRAM_ID = None rather than guessing, so no mainnet address "
        "derives as a SOL-burn or token-burn vault.",
        "gates": ("BURN_SPEND", "BURN_ATOMIC"),
        "stated": "GATED",
        # TARGETS ARE COMMITMENTS. Set these yourself; nothing generates them.
        # `target` is when the criterion above is expected to be met, which is
        # NOT the deploy date -- deploying is not the criterion, the two checks
        # returning a verdict is, and they cannot settle until a coin has been
        # through the pipeline on mainnet. `depends_on` is what a reader should
        # watch to judge whether the target is realistic.
        "target": "2026-09-25",
        "target_set": "2026-09-13",
        "depends_on": "the mainnet deploy, then one live coin through the "
        "whole pipeline. The deploy is targeted for 2026-09-18 and is tracked "
        "in the build log, not here, because a deploy is an event and this "
        "page grades criteria.",
        "blocked_by": None,
    },
)


_STATUS_CLASS = {
    "SHIPPED": "status-pass",
    "DEVNET": "status-unchecked",
    "GATED": "status-unchecked",
    "OPEN": "status-unchecked",
    # A target that has passed with its criterion unmet is the one state on
    # this page that is a failure rather than a wait, and it is coloured like
    # one. Nothing here downgrades it to "in progress" as it ages.
    "SLIPPED": "status-fail",
}

_STYLE = """
/* -- the phases -------------------------------------------------------
   Deliberately not a timeline and deliberately not a progress bar. Both
   imply a finish line on a schedule, and the last phase is gated on money
   that does not exist. This is a list of conditions, ordered, with the
   state of each condition read off the checks rather than asserted. */
.phases { list-style: none; padding: 0; margin: var(--sp-xl) 0 0 0; }
.phase {
  border-top: 1px solid var(--unchecked);
  padding: var(--sp-lg) 0;
}
.phase:last-child { border-bottom: 1px solid var(--unchecked); }
.phase-head {
  display: flex; flex-wrap: wrap; gap: var(--sp-sm);
  align-items: baseline; margin-bottom: var(--sp-sm);
}
.phase-n {
  font-variant-numeric: tabular-nums; font-size: 14px;
  color: var(--pass-glyph);
}
.phase-date { font-size: 14px; color: var(--pass-glyph); }
.phase-date.open { font-style: italic; }
.phase-date.target { font-style: italic; }
/* Same token the FAIL badge uses, so a slipped target reads as the same
   class of thing a failed check does, which is what it is. */
.phase-date.late, .late { color: var(--destructive); }
.target-line { font-size: clamp(14px, 1.7vw, 16px); }
.phase h3 {
  flex: 1 1 100%; margin: 0; font-size: clamp(17px, 2.2vw, 20px);
  line-height: 1.3;
}
.phase p { margin: 0 0 var(--sp-md) 0; line-height: 1.6;
  font-size: clamp(15px, 1.8vw, 17px); }
.phase p:last-child { margin-bottom: 0; }
.criterion {
  padding: var(--sp-md); background: var(--panel);
  border-left: 3px solid var(--unchecked);
}
.criterion-label {
  display: block; font-size: 13px; letter-spacing: 0.04em;
  text-transform: uppercase; color: var(--pass-glyph);
  margin-bottom: var(--sp-xs);
}
.gates { list-style: none; padding: 0; margin: var(--sp-sm) 0 0 0; }
.gates li {
  display: flex; gap: var(--sp-sm); align-items: baseline;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 13px; padding: 2px 0;
}
.gates .gate-name { flex: 1 1 auto; word-break: break-all; }
.phase-code {
  display: block; margin-top: var(--sp-md); padding: var(--sp-sm);
  background: var(--panel); border: 1px solid var(--unchecked);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 13px; word-break: break-all;
}
.phase-code-label {
  display: block; margin-top: var(--sp-xs);
  font-size: 13px; color: var(--pass-glyph);
}
.blocked { color: var(--pass-glyph); }
.gate-note {
  margin: var(--sp-xl) 0 0 0; padding: var(--sp-md);
  background: var(--panel); border-left: 3px solid var(--unchecked);
}
"""


class UndefinedPhase(Exception):
    """Raised when a phase in `_PHASES` still carries the placeholder.

    Deliberately fatal rather than skipped. A five-phase plan rendered with
    four phases in it, or with a phase whose body reads "TBD", is a worse
    page than no page, and this project does not ship the softer failure.
    """


def _assert_defined() -> None:
    for phase in _PHASES:
        for field in ("title", "what", "criterion", "blocked_by"):
            if phase.get(field) == _UNDEFINED:
                raise UndefinedPhase(
                    f"phase {phase['n']} has no {field}. Write it in "
                    f"indexer/phases_page.py, or delete the phase and "
                    f"renumber. Do not guess it onto a public page."
                )


def _by_name(checks) -> dict:
    """Index an observation's checks by name. Empty when none were supplied."""
    return {c.name: c for c in (checks or ())}


def _today(now) -> date:
    """The UTC date this render happened on."""
    epoch = time.time() if now is None else now
    return datetime.fromtimestamp(epoch, tz=timezone.utc).date()


def _target_slip(phase, today) -> int | None:
    """Days past a phase's target, or None if there is no target or it is ahead.

    Positive means late. Zero on the target date itself is not late.
    """
    target = phase.get("target")
    if not target:
        return None
    days = (today - date.fromisoformat(target)).days
    return days if days > 0 else None


def _phase_state(phase, by_name, today=None) -> tuple:
    """The phase's state and the per-gate rows behind it.

    Returns `(state, rows, graded)`. `graded` is False when no check decides
    this phase, or when this render had no observation to read.

    A gated phase has no stated value to fall back on. With no observation its
    checks read UNCHECKED, so it renders OPEN -- never SHIPPED, which only a
    check returning a verdict can produce. The one exception is a phase whose
    `stated` says it is blocked on something no check measures (phase 5's
    funding): that is a fact about the world, not a claim of completion, and
    OPEN would misread it as merely unmeasured.
    """
    gates = phase["gates"]
    if not gates:
        return phase["stated"], (), False

    rows = []
    for name in gates:
        check = by_name.get(name)
        rows.append((name, check.status if check else invariants.UNCHECKED))

    if not by_name:
        return phase.get("stated", "OPEN"), tuple(rows), False

    # The criterion, exactly as the README states it: a phase is done when
    # every check gating it returns a verdict. FAIL is a verdict. A check
    # that failed has measured something and reported it, which is the thing
    # this project means by done -- it does not mean the coin is healthy, and
    # the coin page is where that is read.
    settled = all(status != invariants.UNCHECKED for _, status in rows)
    if settled:
        return "SHIPPED", tuple(rows), True

    # Blocked on something no check measures (phase 5's funding). Said plainly
    # rather than rendered OPEN, which would read as "in progress".
    if phase.get("stated") == "GATED":
        return "GATED", tuple(rows), True

    # Unmet. Whether that is a wait or a failure is decided by the target,
    # and the page is not allowed to soften the second into the first.
    late = _target_slip(phase, today) if today else None
    return ("SLIPPED" if late else "OPEN"), tuple(rows), True


def render(checks=None, *, now=None) -> str:
    """The phases page, as a string.

    `checks` is an iterable of `invariants.Check` from an observation. Pass
    one and the check-gated phases are graded against it. Pass nothing and
    every check-gated phase renders OPEN with its checks reading UNCHECKED,
    and the page says that nothing was measured for this render -- which is
    the honest rendering of a page built with no observation in hand, and
    the reason no phase here can be marked complete by hand.
    """
    _assert_defined()

    esc = site.esc
    stamp = site._stamp(time.time() if now is None else now)
    by_name = _by_name(checks)
    today = _today(now)

    parts = [
        "<header>",
        "<h1>Phases</h1>",
        "<p>The plan runs to five phases. A phase is done when a check that "
        "reads UNCHECKED starts returning a verdict, not when code for it "
        "exists. Nothing here is marked complete by hand, and a target "
        "that passes unmet is shown as missed rather than moved.</p>",
        "</header>",
        "<main>",
    ]

    if not by_name:
        parts.append(
            '<aside class="gate-note"><p>This render had no observation to '
            "read, so the check-gated phases below show the status this "
            "repository states for them and the checks behind each one read "
            "UNCHECKED here for that reason rather than because they failed. "
            "Run an observation to grade the page.</p></aside>"
        )

    parts.append('<ul class="phases">')

    for phase in _PHASES:
        state, rows, graded = _phase_state(phase, by_name, today)
        cls = _STATUS_CLASS[state]

        if phase["landed"]:
            date_html = f'<span class="phase-date">{esc(phase["landed"])}</span>'
        elif phase.get("target"):
            late = _target_slip(phase, today)
            if late and state != "SHIPPED":
                date_html = (
                    f'<span class="phase-date late">target '
                    f'{esc(phase["target"])}, {late} '
                    f'{"day" if late == 1 else "days"} ago</span>'
                )
            else:
                date_html = (
                    f'<span class="phase-date target">target '
                    f'{esc(phase["target"])}</span>'
                )
        else:
            date_html = '<span class="phase-date open">no date</span>'

        parts.append('<li class="phase">')
        parts.append('<div class="phase-head">')
        parts.append(f'<span class="phase-n">Phase {phase["n"]}</span>')
        parts.append(date_html)
        parts.append(f'<span class="badge {cls}">{esc(state)}</span>')
        parts.append(f'<h3>{esc(phase["title"])}</h3>')
        parts.append("</div>")
        parts.append(f'<p>{esc(phase["what"])}</p>')

        parts.append('<div class="criterion">')
        parts.append('<span class="criterion-label">Done means</span>')
        parts.append(f'<p>{esc(phase["criterion"])}</p>')
        if rows:
            parts.append('<ul class="gates">')
            for name, status in rows:
                if status == invariants.PASS:
                    badge = "status-pass"
                elif status == invariants.FAIL:
                    badge = "status-fail"
                else:
                    badge = "status-unchecked"
                parts.append(
                    f'<li><span class="gate-name">{esc(name)}</span>'
                    f'<span class="badge {badge}">{esc(status)}</span></li>'
                )
            parts.append("</ul>")
            if not graded:
                parts.append(
                    '<p class="meta">Not graded on this render. No '
                    "observation was supplied.</p>"
                )
        parts.append("</div>")

        if phase.get("code"):
            parts.append(f'<code class="phase-code">{esc(phase["code"])}</code>')
            parts.append(
                f'<span class="phase-code-label">{esc(phase["code_label"])}</span>'
            )

        if phase.get("target"):
            late = _target_slip(phase, today)
            parts.append('<p class="target-line">')
            parts.append(
                f'Targeted <strong>{esc(phase["target"])}</strong>, '
                f'committed {esc(phase["target_set"])}.'
            )
            if late and state != "SHIPPED":
                parts.append(
                    f' <strong class="late">That date has passed and the '
                    f'criterion above is not met. {late} '
                    f'{"day" if late == 1 else "days"} late.</strong> The '
                    f'date is not moved when it is missed.'
                )
            parts.append("</p>")
            if phase.get("depends_on"):
                parts.append(
                    f'<p class="blocked">Depends on {esc(phase["depends_on"])}</p>'
                )

        if phase.get("blocked_by"):
            parts.append(
                f'<p class="blocked">Blocked on {esc(phase["blocked_by"])}</p>'
            )

        parts.append("</li>")

    parts.append("</ul>")

    parts.append(
        '<aside class="gate-note">'
        "<p>No mainnet program is deployed. The program is written and "
        "deployed to devnet, and a devnet id derives nothing on mainnet. "
        "Phase 5 is funding-gated and the gate is closed. There are no dates "
        "on this page for anything that has not happened, because a date on "
        "an ungated deliverable is a schedule this project cannot keep.</p>"
        "</aside>"
    )

    parts.append(
        f'<p class="meta">Page generated {esc(stamp)}. It is a snapshot and '
        "does not update itself.</p>"
    )
    parts.append(
        '<p><a href="buildlog.html">The build log</a> dates what landed. '
        '<a href="https://github.com/needsmorergb/charlie-protocol-v1">'
        "The repository</a> is where every criterion above is enforced.</p>"
    )
    parts.append("</main>")

    return site._document(
        "Phases -- Charlie Protocol",
        "".join(parts),
        style=site._INDEX_STYLE + _STYLE,
        description=(
            "The five phases of Charlie Protocol, each graded by the checks "
            "that decide it rather than marked complete by hand."
        ),
    )


def write(out_dir=site.DEFAULT_OUTPUT_DIR, *, checks=None, now=None):
    """Writes the phases page into `out_dir`.

    Same ownership rule the other entry pages follow (`site._is_authored`):
    a hand-authored file at this path is left alone rather than overwritten.
    """
    path = Path(out_dir) / PHASES_FILENAME
    if path.exists() and site._is_authored(path):
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(checks=checks, now=now), encoding="utf-8")
    return path

"""`/buildlog` -- dated milestones, and the gate that is still closed.

A build log for a protocol that asks strangers to check its figures has one
obligation the genre usually skips: it has to be as willing to date what is
NOT done as what is. A log that lists only shipped things reads as progress
and hides the gate, and the gate is the single most important fact about this
project's status -- phase 5 is funding-gated and closed, so the mainnet
program does not exist.

So every entry here carries a status, and the statuses that matter most are
the open ones. `GATED` is not a milestone waiting to be crossed off; it is a
statement that nothing behind it has happened.

Its own module for the same reason `dilution_page` and `enroll_page` are:
`site.py` imports only `invariants` and `publish` by design and a test holds
that.

WHAT MAY BE CLAIMED HERE. The same rule the rest of the site runs under: no
frame implying the protocol program is deployed to mainnet. The devnet
deployment is real, has an id, and anyone can check it -- and saying
"deployed" without "devnet" beside it would be the exact claim this project
has refused to make since 2026-09-03. Every entry that mentions the program
says which cluster it means.

NO DATES FROM THE FUTURE. Nothing here is a schedule. A build log that lists
a date for an ungated deliverable is a roadmap with a timestamp, and phase 5
depends on funding that does not exist. Entries are things that happened,
plus open items with no date attached.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import site

BUILDLOG_FILENAME = "buildlog.html"

# ---------------------------------------------------------------------------
# The log. Hand-curated rather than generated from `git log`, on purpose:
# 123 commits are not 123 milestones, and a reader wants the shape of the
# build, not its changelog. Every date here is the date of the commit that
# landed the thing, and no script rewrites this list -- adding an entry is an
# edit to this file, which is the point. It is a claim, so it is reviewed
# like one.
#
# status: "SHIPPED" -- built, committed, and checkable by a reader today
#         "DEVNET"  -- deployed to devnet only, id given, mainnet is not this
#         "GATED"   -- not done, and blocked on something named
_ENTRIES = [
    {
        "date": "2026-08-29",
        "status": "SHIPPED",
        "title": "The implementation repository, and the plan for all three pieces",
        "body": "Indexer, site and program, scoped as five phases. Nothing in "
        "the build waits on pump.",
    },
    {
        "date": "2026-08-30",
        "status": "SHIPPED",
        "title": "Phase 1 -- the evidence walk",
        "body": "Fee inflows reconciled per destination, SPL burn and boost "
        "decoders, initial supply derived from the coin's own CreateEvent, and "
        "the atomicity check that holds a swap and its burn to one "
        "transaction. Figures are published through one accessor, so a number "
        "with no passing check behind it cannot reach a page.",
    },
    {
        "date": "2026-08-31",
        "status": "SHIPPED",
        "title": "Phase 2 -- the public surface",
        "body": "The coin page, its raw JSON record, and the checks list that "
        "names every check that passed, failed, or was never run. Built "
        "deliberately ahead of the deploy gate rather than behind it.",
    },
    {
        "date": "2026-09-02",
        "status": "SHIPPED",
        "title": "/verify takes a pasted contract address, with no JavaScript",
        "body": "Any pump.fun coin, no wallet and no signup. The chain is read "
        "while the reader waits.",
    },
    {
        "date": "2026-09-05",
        "status": "SHIPPED",
        "title": "The coin index, and enrollment read from the chain",
        "body": "Whether a coin is enrolled is read from its pump fee-sharing "
        "config rather than asserted, on every coin page and in the index.",
    },
    {
        "date": "2026-09-11",
        "status": "SHIPPED",
        "title": "/dilution -- what issuance costs a reader's own bag",
        "body": "The one page here that argues rather than reports. Two "
        "measured figures and arithmetic a reader can redo. No price anywhere "
        "in it, deliberately.",
    },
    {
        "date": "2026-09-12",
        "status": "DEVNET",
        "title": "Phase 4 -- the program is written, and on devnet",
        "body": "Four instructions: init_charlie_pool, init_route, set_route, "
        "distribute. The guarantee is the absence of code -- nothing debits a "
        "coin's collector except distribute, and no instruction anywhere sends "
        "to an address its caller supplied. Every destination is re-derived "
        "from the mint or read from the coin's own route account. 11 tests "
        "pass. Deployed to DEVNET at the id below; mainnet gets its own "
        "keypair and is not this.",
        "code": "9yLFoJzo6gVvPUNEKsYhvhERfYnaW25GupjFQvQGbdVY",
        "code_label": "devnet program id",
    },
    {
        "date": None,
        "status": "GATED",
        "title": "Phase 5 -- mainnet deploy, and revoking upgrade authority",
        "body": "Funding-gated, and the gate is closed. The absence-of-code "
        "guarantee only means anything once the program is immutable, and "
        "revoking upgrade authority is a one-way door that freezes every bug "
        "permanently. Until this happens there is no mainnet program id, so no "
        "mainnet address derives as a SOL-burn or token-burn vault, and the "
        "indexer holds PROGRAM_ID = None rather than guessing.",
    },
    {
        "date": None,
        "status": "GATED",
        "title": "/enroll, as a live path",
        "body": "The page exists and describes the mechanism. Enrolling a coin "
        "through the program is behind the same phase 5 gate.",
    },
]

# The open items are the ones a reader is most likely to be misled about, so
# they are not styled as achievements. `status-unchecked` is the same class
# the rest of the site uses for "no passing check backs this", which is
# exactly what an ungated deliverable is.
_STATUS_CLASS = {
    "SHIPPED": "status-pass",
    "DEVNET": "status-unchecked",
    "GATED": "status-unchecked",
}

_STYLE = """
/* -- the log ----------------------------------------------------------
   A single column of dated entries. No timeline rail: a rail implies even
   spacing between milestones and these are not evenly spaced, and it reads
   as a schedule running to a finish line that funding has not bought. */
.log { list-style: none; padding: 0; margin: var(--sp-xl) 0 0 0; }
.log-entry {
  border-top: 1px solid var(--unchecked);
  padding: var(--sp-lg) 0;
}
.log-entry:last-child { border-bottom: 1px solid var(--unchecked); }
.log-head {
  display: flex; flex-wrap: wrap; gap: var(--sp-sm);
  align-items: baseline; margin-bottom: var(--sp-sm);
}
.log-date {
  font-variant-numeric: tabular-nums; font-size: 14px;
  color: var(--pass-glyph);
}
.log-date.open { font-style: italic; }
.log-entry h3 {
  flex: 1 1 100%; margin: 0; font-size: clamp(17px, 2.2vw, 20px);
  line-height: 1.3;
}
.log-entry p { margin: 0; line-height: 1.6; font-size: clamp(15px, 1.8vw, 17px); }
.log-code {
  display: block; margin-top: var(--sp-md); padding: var(--sp-sm);
  background: var(--panel); border: 1px solid var(--unchecked);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 13px; word-break: break-all;
}
.log-code-label {
  display: block; margin-top: var(--sp-xs);
  font-size: 13px; color: var(--pass-glyph);
}
.gate-note {
  margin: var(--sp-xl) 0 0 0; padding: var(--sp-md);
  background: var(--panel); border-left: 3px solid var(--unchecked);
}
"""


def render(*, now=None) -> str:
    """The build log page, as a string."""
    esc = site.esc
    stamp = site._stamp(time.time() if now is None else now)

    parts = [
        "<header>",
        "<h1>Build log</h1>",
        "<p>What has shipped, what is on devnet only, and what is gated. "
        "Every entry names which of those it is.</p>",
        "</header>",
        "<main>",
        '<ul class="log">',
    ]

    for entry in _ENTRIES:
        cls = _STATUS_CLASS[entry["status"]]
        if entry["date"] is None:
            date_html = '<span class="log-date open">no date</span>'
        else:
            date_html = f'<span class="log-date">{esc(entry["date"])}</span>'
        parts.append('<li class="log-entry">')
        parts.append('<div class="log-head">')
        parts.append(date_html)
        parts.append(f'<span class="badge {cls}">{esc(entry["status"])}</span>')
        parts.append(f'<h3>{esc(entry["title"])}</h3>')
        parts.append("</div>")
        parts.append(f'<p>{esc(entry["body"])}</p>')
        if entry.get("code"):
            parts.append(f'<code class="log-code">{esc(entry["code"])}</code>')
            parts.append(
                f'<span class="log-code-label">{esc(entry["code_label"])}</span>'
            )
        parts.append("</li>")

    parts.append("</ul>")

    # The one thing a reader should leave this page knowing, stated once more
    # where it cannot be missed by someone who skimmed the entries.
    parts.append(
        '<aside class="gate-note">'
        "<p>No mainnet program is deployed. The devnet id above is a devnet id "
        "and derives nothing on mainnet. Phase 5 is funding-gated and the gate "
        "is closed, so there is nothing to sign up for and nothing to buy in "
        "order to be ready for it.</p>"
        "</aside>"
    )

    parts.append(
        f'<p class="meta">Page generated {esc(stamp)}. It is a snapshot and '
        "does not update itself.</p>"
    )
    parts.append(
        '<p><a href="https://github.com/needsmorergb/charlie-protocol-v1">'
        "Every entry above is a commit in the repository</a>.</p>"
    )
    parts.append("</main>")

    return site._document(
        "Build log -- Charlie Protocol",
        "".join(parts),
        style=site._INDEX_STYLE + _STYLE,
        description=(
            "Dated milestones for Charlie Protocol: what has shipped, what is "
            "on devnet only, and what is still gated."
        ),
    )


def write(out_dir=site.DEFAULT_OUTPUT_DIR, *, now=None):
    """Writes the build log into `out_dir`.

    Same ownership rule the other entry pages follow (`site._is_authored`):
    a hand-authored file at this path is left alone rather than overwritten.
    """
    path = Path(out_dir) / BUILDLOG_FILENAME
    if path.exists() and site._is_authored(path):
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(now=now), encoding="utf-8")
    return path

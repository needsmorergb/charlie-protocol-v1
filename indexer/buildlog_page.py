"""`/buildlog` -- dated milestones, and the work that is not done yet.

A build log for a protocol that asks strangers to check its figures has one
obligation the genre usually skips: it has to be as willing to date what is
NOT done as what is. A log that lists only shipped things reads as progress
and hides what is open, and what is open is the single most important fact
about this project's status -- phase 5 has not happened, so no mainnet
program exists.

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
a date for something not yet done is a roadmap with a timestamp, and this is
a log. Entries are things that happened, plus open items with no date
attached. `/phases` is where anything forward-looking lives.
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
#         "GATED"   -- not done, and the entry names what is holding it
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
        "title": "Phase 3 -- the evidence walk, and what it reconciles",
        "body": "Fee inflows reconciled per destination, SPL burn and boost "
        "decoders, initial supply derived from the coin's own CreateEvent, and "
        "the atomicity check that holds a swap and its burn to one "
        "transaction. Figures are published through one accessor, so a number "
        "with no passing check behind it cannot reach a page.",
    },
    {
        "date": "2026-08-31",
        "status": "SHIPPED",
        "title": "Phase 3, continued -- the public surface",
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
        "from the mint or read from the coin's own route account. 13 tests "
        "pass. Deployed to DEVNET at the id below; mainnet gets its own "
        "keypair and is not this.",
        "code": "GFA3nG9geMhpPXaExVLGYBtj6aJX7S125dLzv4EcXGiG",
        "code_label": "devnet program id",
    },
    {
        "date": "2026-09-12",
        "status": "DEVNET",
        "title": "The first devnet run of distribute failed, and why",
        "body": "A system-program transfer CPI cannot debit the collector, "
        "because the collector is owned by this program and the runtime "
        "refuses to let the system program move lamports out of an account it "
        "does not own. The first devnet run returned "
        "ExternalAccountLamportSpend. A program moves lamports out of an "
        "account it owns by mutating the balances directly, with the runtime "
        "checking the sum is conserved when the instruction returns -- which "
        "also removes the CPI from the guarantee, so there is no inner "
        "instruction and no signer seeds handed to another program for a "
        "reader to follow. 13 tests pass, including a cross-check that the "
        "PDAs this program derives match the ones the Python driver derives.",
    },
    {
        "date": "2026-09-12",
        "status": "DEVNET",
        "title": "The $CHARLIE flywheel, run on a chain",
        "body": "A separate splitter program, deployed to devnet, answering a "
        "hypothetical: $CHARLIE's own sharing config is admin_revoked and its "
        "one irreversible update is spent, so no key its deployer or Charlie "
        "holds can point its fees anywhere (pump's own admin_cto still could). If that destination COULD be changed "
        "to a splitter, would the mechanism hold? Three rounds ran. 2,400,000 "
        "lamports of simulated fee split 1,349,414 to the incinerator, 674,707 "
        "to the buyback vault, 674,707 to ops, and 3 lamports left behind "
        "rather than swept to a leg. The fee ARRIVING is simulated, because "
        "pump does not exist on devnet; the split of it is a landed "
        "transaction every time. One round was cranked by a wallet that is not "
        "ops, which is what permissionless means here. The buyback leg is "
        "funded and not spent -- burning needs a live pool devnet does not "
        "have -- and /charlie-flywheel says so rather than implying tokens "
        "were burned.",
        "code": "2CfShCuyuLw1935ihB5BjzaymdZsmPLNtUFAAqUeMHBS",
        "code_label": "devnet splitter program id",
    },
    {
        "date": "2026-09-12",
        "status": "DEVNET",
        "title": "A keeper that runs unattended",
        "body": "The split is not much of a mechanism if a person has to run "
        "it. The keeper calls split on an hourly schedule so fees do not sit, "
        "and writes every invocation to state/flywheel/keeper.jsonl with its "
        "signature and outcome -- including the idle ones, where there was "
        "nothing above the rent reserve and it did nothing. A log that only "
        "recorded the interesting runs would not be evidence of a schedule.",
    },
    {
        "date": "2026-09-18",
        "status": "SHIPPED",
        "title": "Mainnet operations: the launch and enrolment consoles",
        "body": "/launch and /enroll went live and enrolment opened. A coin "
        "enrols by spending pump's one sharing-config change on the protocol's "
        "destinations, so the SOL burn leg pays Solana's incinerator directly "
        "and the protocol's share reaches its collection wallet -- both without "
        "a program of ours, which is why this entry is not phase 5. The BURN "
        "leg runs from that wallet: one transaction that buys $CHARLIE and "
        "burns it, with the swap and the burn in the same transaction, "
        "recorded in protocol-burns.json with the signature behind every unit "
        "and a statement of whether the total is exact or a floor.",
    },
    {
        "date": None,
        "status": "GATED",
        "title": "Phase 5 -- mainnet deploy, and revoking upgrade authority",
        "body": "Held, not blocked. The cost is measured and published in the "
        "2026-09-13 entry above; what remains is the deploy order, which is "
        "deliberate. The absence-of-code guarantee only means anything once "
        "the program is immutable, and "
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
   as a schedule running to a finish line nobody has committed to. */
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
        "and derives nothing on mainnet. Phase 5 is held rather than blocked: "
        "its cost is measured and published, and what remains is the deploy "
        "order. There is nothing to sign up for and nothing to buy in order to "
        "be ready for it.</p>"
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

"""`/flywheel` -- the fee split, executed on a real chain rather than described.

Every other page on this site reports mainnet. This one reports **devnet**, and
that difference is the whole reason it needs saying carefully.

WHAT IS REAL HERE. The program is deployed and its id is on the page. The
transactions landed and their signatures are on the page. `distribute` split a
real collector balance four ways, the runtime destroyed the SOL burn leg's
lamports at the incinerator, and three attacks on the instruction were refused
by the program in its own words. A reader can fetch any signature here from
devnet and get the same numbers.

WHAT IS MOCKED, in one sentence, because a page that blurs this is worse than
no page: **pump does not exist on devnet, so the creator fee ARRIVING is an
ordinary transfer into the collector.** Everything downstream of that arrival
is the protocol doing its actual job. What is proven is the mechanism -- the
split, the destinations, the refusals; what is NOT proven is any figure about
volume, price, or what a coin would earn, and none appears.

WHAT THIS PAGE DOES NOT DO. It does not model. There is no slider, no
projection, no "if $CHARLIE did X" -- `/dilution` is the page that argues from
arithmetic, and it is careful to carry no price. A flywheel page that invented
a price path would be inventing exactly the figure a reader would take away,
so the only numbers here are lamports that moved.

Its own module for the same reason `dilution_page` and `enroll_page` are:
`site.py` imports only `invariants` and `publish` by design, and a test holds
that.

The input is `state/flywheel/devnet.json`, written by `tools/flywheel_devnet.py`
from a run against the chain. If that file is absent the page is not rendered
at all -- there is no placeholder and no "coming soon", because an unrun proof
has nothing to show.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import legs, site

FLYWHEEL_FILENAME = "flywheel.html"

# Written by `tools/flywheel_devnet.py`. Not generated here: this module
# renders a run, and rendering must never be able to invent one.
RECORD = Path(__file__).resolve().parents[1] / "state" / "flywheel" / "devnet.json"

LAMPORTS_PER_SOL = 1_000_000_000

EXPLORER = "https://explorer.solana.com"

# The four legs, in the order the program pays them, with the name a reader
# recognises and the sentence that says what the destination actually is.
LEG_ORDER = (
    (
        "toll",
        "$CHARLIE buy &amp; burn",
        "charlie_pool",
        "The protocol's share. A PDA of the program; no key exists for it, and "
        "no instruction moves lamports out of it except the crank that buys "
        "$CHARLIE and burns it.",
    ),
    (
        "sol_burn",
        "SOL burn",
        "incinerator",
        "Solana's incinerator. The runtime removes these lamports from the "
        "total supply at the end of the block -- not merely unspendable, "
        "destroyed. That is why its balance below is zero.",
    ),
    (
        "own_burn",
        "the coin's own buy &amp; burn",
        "burn_pool",
        "The dev's own buy-and-burn leg, a PDA of the program derived from "
        "their mint.",
    ),
    (
        "ops",
        "ops",
        "route.ops_address",
        "An ordinary wallet the dev names. Spendable, and the page says so: "
        "what it is spent on is not something a chain shows.",
    ),
)


def load(path: Path | None = None) -> dict | None:
    """The recorded run, or None when the proof has not been run."""
    path = path or RECORD
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def sol(lamports: int, digits: int = 6) -> str:
    return f"{lamports / LAMPORTS_PER_SOL:.{digits}f}"


def share_percent(part: int, whole: int) -> str:
    """A leg as a percentage of the fee it came out of. Two decimals,
    because the whole point is that it lands on exactly 25.00."""
    if not whole:
        return "0.00"
    return f"{part / whole * 100:.2f}"


def _tx_link(signature: str, label: str | None = None) -> str:
    short = f"{signature[:8]}...{signature[-6:]}"
    return (
        f'<a href="{EXPLORER}/tx/{site.esc(signature)}?cluster=devnet" '
        f'rel="noopener">{site.esc(label or short)}</a>'
    )


def _account_link(address: str) -> str:
    short = f"{address[:6]}...{address[-4:]}"
    return (
        f'<a href="{EXPLORER}/address/{site.esc(address)}?cluster=devnet" '
        f'rel="noopener"><code>{site.esc(short)}</code></a>'
    )


def _devnet_banner() -> str:
    """The disclosure, first thing on the page and not in a footnote.

    A page showing real signatures for a real split invites exactly one wrong
    conclusion -- that this is the protocol running in production. It is not,
    and the sentence that says so is the first thing a reader meets.
    """
    return (
        '<div class="banner">'
        "<p><strong>This is devnet, and one step of it is mocked.</strong> "
        "The program below is deployed and the transactions below landed: the "
        "split, the destinations and the refusals are real, and every signature "
        "is checkable. But pump does not exist on devnet, so the creator fee "
        "<em>arriving</em> at the collector is an ordinary transfer rather than "
        "pump paying an enrolled coin.</p>"
        "<p>What this proves is the <strong>mechanism</strong>. It proves nothing "
        "about volume, price, or what any coin would earn, and no such figure "
        "appears on this page.</p>"
        "</div>"
    )


def _split_table(record: dict) -> str:
    """The split, per leg, as it actually landed."""
    totals = record["totals"]
    fee = totals["fee"]
    rows = []
    for key, label, destination, why in LEG_ORDER:
        moved = totals[key]
        # Two of the four destinations are PDAs this record names, so they get
        # a link a reader can open. The incinerator and the dev's ops wallet
        # do not: one is a constant every Solana reader already knows, and the
        # other is an ordinary wallet whose address is the dev's business.
        address = _address_key(key)
        link = (
            f"<br>{_account_link(record['addresses'][address])}"
            if address and record["addresses"].get(address)
            else ""
        )
        rows.append(
            "<tr>"
            f"<td><strong>{label}</strong><br>"
            f'<span class="why">{why}</span></td>'
            f'<td class="num">{share_percent(moved, fee)}%</td>'
            f'<td class="num">{moved:,}</td>'
            f"<td>{site.esc(destination)}{link}</td>"
            "</tr>"
        )

    remainder = totals["remainder"]
    return (
        '<table class="legs">'
        "<thead><tr>"
        "<th>leg</th><th>share of the fee</th><th>lamports</th><th>destination</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "<tfoot>"
        f'<tr><td>the fee that arrived</td><td class="num">100%</td>'
        f'<td class="num">{fee:,}</td><td>collector(mint)<br>'
        f"{_account_link(record['addresses']['collector'])}</td></tr>"
        f'<tr><td>left in the collector<br>'
        '<span class="why">the integer-division remainder. It is not swept to '
        "a leg -- which leg got it would depend on the order of the code -- so "
        "it waits and is counted in the next distribution.</span></td>"
        f'<td class="num">{share_percent(remainder, fee)}%</td>'
        f'<td class="num">{remainder:,}</td><td></td></tr>'
        "</tfoot>"
        "</table>"
    )


def _address_key(leg: str) -> str | None:
    return {
        "toll": "charlie_pool",
        "own_burn": "burn_pool",
        "sol_burn": None,
        "ops": None,
    }.get(leg)


def _rounds_table(record: dict) -> str:
    """Every distribution, with its signature, so the table is checkable."""
    rows = []
    for entry in record["rounds"]:
        event = entry.get("event") or {}
        rows.append(
            "<tr>"
            f"<td class=\"num\">{entry['round']}</td>"
            f"<td class=\"num\">{entry['fee_lamports']:,}</td>"
            f"<td class=\"num\">{event.get('toll', 0):,}</td>"
            f"<td class=\"num\">{event.get('sol_burn', 0):,}</td>"
            f"<td class=\"num\">{event.get('own_burn', 0):,}</td>"
            f"<td class=\"num\">{event.get('ops', 0):,}</td>"
            f"<td>{_tx_link(entry['distribute_signature'])}</td>"
            "</tr>"
        )
    return (
        '<table class="rounds">'
        "<thead><tr>"
        "<th>#</th><th>fee in</th><th>toll</th><th>SOL burn</th>"
        "<th>own burn</th><th>ops</th><th>distribute</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


def _permissionless(record: dict) -> list:
    """One distribution called by a wallet with no stake in it.

    `distribute` takes no signer, which is the claim. A run whose only
    caller is also the ops wallet does not demonstrate it -- that caller had
    a reason to call. So the proof is a stranger calling it: the toll still
    moves, and the caller ends DOWN by the transaction fee.

    Returns [] when the run did not include it, rather than describing an
    experiment that was not performed.
    """
    entry = record.get("permissionless")
    if not entry:
        return []
    delta = entry["cranker_lamports_delta"]
    return [
        "<h2>Anyone can turn it</h2>",
        "<p><code>distribute</code> takes no signer. The round below was "
        "called by a wallet that is neither the coin's admin nor its ops "
        "address &mdash; it has no claim on any leg &mdash; and the toll moved "
        "anyway:</p>",
        '<ul class="facts">'
        f"<li>caller: {_account_link(entry['cranker'])}, "
        f"admin: {'yes' if entry['is_admin'] else 'no'}, "
        f"ops: {'yes' if entry['is_ops'] else 'no'}</li>"
        f"<li>the toll it moved: <strong>{entry['event']['toll']:,}</strong> "
        f"lamports, and <code>charlie_pool</code> gained exactly that "
        f"({entry['charlie_pool_gained']:,})</li>"
        f"<li>what the caller was paid: <strong>nothing</strong>. Its balance "
        f"changed by {delta:,} lamports &mdash; it paid the transaction fee and "
        "received no share of any leg</li>"
        f"<li>{_tx_link(entry['distribute_signature'], 'the transaction')}</li>"
        "</ul>",
        "<p>That last line is deliberate, not an oversight. A fixed tip for "
        "calling the crank would be an extractable bounty: whoever called it "
        "would be paid out of the toll and both burn legs, so the legs would "
        "fund their own collection. The crank's incentive is the ops share and "
        "the protocol's own operation.</p>",
    ]


def _refusals(record: dict) -> str:
    """What a caller cannot do, shown rather than asserted."""
    items = []
    for case in record.get("refusals", []):
        expected_accept = case.get("must_be_accepted")
        refused = case["refused"]
        ok = (not refused) if expected_accept else refused
        verdict = "accepted" if not refused else "refused"
        mark = "&#10003;" if ok else "&#10007;"
        log = case.get("log") or ""
        log = log.replace("Program log: ", "")
        items.append(
            "<li>"
            f'<span class="mark {"ok" if ok else "bad"}">{mark}</span> '
            f"<strong>{site.esc(case['case'])}</strong> &mdash; {verdict}"
            + (f"<br><code>{site.esc(log)}</code>" if log else "")
            + f'<br><span class="why">{site.esc(case["why"])}</span>'
            "</li>"
        )
    return f'<ul class="refusals">{"".join(items)}</ul>'


_STYLE = site._TOKENS + """
body {
  margin: 0 auto; padding: var(--sp-lg); max-width: 62rem;
  background: var(--paper); color: var(--ink);
  font-family: ui-monospace, "SFMono-Regular", Menlo, Consolas, "Liberation Mono", monospace;
  line-height: 1.6;
}
h1 { font-size: 1.6rem; line-height: 1.25; margin: 0 0 var(--sp-sm); }
h2 { font-size: 1.1rem; margin: var(--sp-2xl) 0 var(--sp-md); }
p { margin: 0 0 var(--sp-md); }
a { color: var(--accent); }
code { background: var(--panel); padding: 1px 4px; }
.lede { font-size: 1.05rem; }
.banner {
  border: 1px solid var(--unchecked); background: rgba(122, 90, 18, 0.07);
  padding: var(--sp-md); margin: var(--sp-lg) 0;
}
.banner p:last-child { margin-bottom: 0; }
table { border-collapse: collapse; width: 100%; font-size: 0.88rem; }
.scroll { overflow-x: auto; margin: var(--sp-md) 0; }
th, td {
  border-bottom: 1px solid var(--panel); padding: var(--sp-sm);
  text-align: left; vertical-align: top;
}
th { font-weight: 700; border-bottom: 1px solid var(--ink); }
.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
tfoot td { border-top: 1px solid var(--ink); font-weight: 700; }
tfoot .why, .why { font-weight: 400; color: var(--pass-glyph); font-size: 0.82rem; }
.why { display: block; margin-top: var(--sp-xs); }
.refusals { list-style: none; padding: 0; }
.refusals li { margin-bottom: var(--sp-md); }
.mark { font-weight: 700; }
.mark.ok { color: var(--accent); }
.mark.bad { color: var(--destructive); }
.facts { list-style: none; padding: 0; font-size: 0.9rem; }
.facts li { margin-bottom: var(--sp-sm); }
.meta { color: var(--pass-glyph); font-size: 0.82rem; }
footer { margin-top: var(--sp-3xl); border-top: 1px solid var(--panel);
  padding-top: var(--sp-md); font-size: 0.82rem; color: var(--pass-glyph); }
"""


def render(record: dict | None = None, *, now=None) -> str:
    record = record if record is not None else load()
    if record is None:
        raise FileNotFoundError(
            f"{RECORD} does not exist -- run `python -m tools.flywheel_devnet` first. "
            "This page renders a run and must not be able to invent one."
        )

    now = time.gmtime(now if now is not None else time.time())
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", now)
    totals = record["totals"]
    route = record["route_on_chain"]
    toll_pct = share_percent(totals["toll"], totals["fee"])

    body = [
        "<h1>The flywheel, executed</h1>",
        '<p class="lede">A fee arrives. The program splits it four ways: a '
        "quarter to buying and burning $CHARLIE, and the rest to the three "
        "legs the coin's own dev chose. Nobody can redirect any of it. "
        "Below is that happening on a chain, with signatures.</p>",
        _devnet_banner(),
        "<h2>What ran</h2>",
        '<ul class="facts">'
        f"<li>program <code>{site.esc(record['program_id'])}</code> "
        f"{_account_link(record['program_id'])}, on devnet</li>"
        f"<li>the toll stored in <code>charlie_pool</code>: "
        f"<strong>{record['toll_bps_on_chain']} bps</strong> of the creator fee "
        f"({int(record['toll_bps_on_chain']) / 100:.0f}%), asserted against the "
        "program's own compiled constant on every distribute</li>"
        f"<li>the dev's split, read from <code>route(mint)</code>: "
        f"SOL burn {route['sol_burn_bps']}, own burn {route['own_burn_bps']}, "
        f"ops {route['ops_bps']} &mdash; summing to "
        f"{route['sol_burn_bps'] + route['own_burn_bps'] + route['ops_bps']}, "
        "which is what the toll leaves</li>"
        f"<li>{len(record['rounds'])} distributions, "
        f"{totals['fee']:,} lamports of creator fee in total "
        f"({sol(totals['fee'])} SOL)</li>"
        "</ul>",
        "<h2>Where it went</h2>",
        f'<div class="scroll">{_split_table(record)}</div>',
        f"<p>The toll came to <strong>{toll_pct}%</strong> of the fee, which is "
        f"what <code>TOLL_BPS = {legs.TOLL_BPS}</code> means. It is a constant in "
        "the program's code and is not a field of any account a dev can write &mdash; "
        "the three shares they do choose are required to sum to exactly what the "
        "toll leaves.</p>",
        "<h2>Each distribution</h2>",
        f'<div class="scroll">{_rounds_table(record)}</div>',
        "<p>The figures in that table are the program's own, emitted by "
        "<code>distribute</code> and read back out of each transaction's logs. "
        "The balances of the two pools were also read directly and agree with "
        "them; the SOL burn leg has no balance to read, because the runtime "
        "destroys it.</p>",
        *_permissionless(record),
        "<h2>What a caller cannot do</h2>",
        "<p>The guarantee is an absence, and an absence is only worth stating if "
        "you try the thing and print the refusal. Each of these was submitted to "
        "the deployed program:</p>",
        _refusals(record),
        "<h2>What this is not</h2>",
        "<p>It is not mainnet, it is not enrollment, and it is not a claim that "
        "any coin earns anything. The creator fee arriving was a transfer, not "
        "pump. The program is <strong>upgradeable</strong>, which means the "
        "absences above are today's absences and not yet permanent &mdash; "
        "revoking upgrade authority is the one-way door that makes them "
        "guarantees, and it comes after the pipeline has run in production, not "
        "before.</p>",
        "<footer>"
        f"<p>generated at {site.esc(stamp)} from a devnet run recorded in "
        "<code>state/flywheel/devnet.json</code>. Every signature above is "
        "checkable on devnet; nothing on this page is a projection.</p>"
        '<p><a href="/">Charlie Protocol</a> &middot; '
        '<a href="/verify">/verify</a> &middot; '
        '<a href="/dilution">/dilution</a></p>'
        "</footer>",
    ]

    return site._document(
        "The flywheel, executed on devnet -- Charlie Protocol",
        "".join(body),
        style=_STYLE,
        description=(
            "The Charlie Protocol fee split running on a real chain: a creator "
            "fee arrives, the program sends a quarter to buying and burning "
            "$CHARLIE and the rest to the legs the dev chose, and three attempts "
            "to redirect it are refused. Devnet, with signatures."
        ),
    )


def write(out_dir=site.DEFAULT_OUTPUT_DIR, *, now=None) -> Path:
    path = Path(out_dir) / FLYWHEEL_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(now=now), encoding="utf-8")
    return path

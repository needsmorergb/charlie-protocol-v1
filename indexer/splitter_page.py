"""`/charlie-flywheel` -- what $CHARLIE's fee split would do, run on a chain.

A DIFFERENT CLAIM FROM `/flywheel`, and the two must not be confused.
`/flywheel` reports the protocol program: a 25% toll no dev can redirect,
plus three legs the dev chooses. This page reports a **splitter for
$CHARLIE specifically**, with no toll and a different shape:

    50%  SOL burn               -> the incinerator, destroyed
    25%  $CHARLIE buy and burn  -> a vault only a crank can spend
    25%  ops                    -> an ordinary wallet

WHY IT IS HYPOTHETICAL, said here and on the page. $CHARLIE's pump sharing
config is `admin_revoked` and pays `burn111...111` 100%: pump allows one
irreversible update and it is spent, so nobody -- its deployer included --
can point its fees anywhere else. This page does not claim otherwise. It
answers the question that is still open: **if that destination could be
changed to a splitter, would the mechanism hold?**

WHAT IS REAL. The program is deployed. The transactions landed. The vault is
off the ed25519 curve, so no private key can exist for it and the split is
the only exit -- not the preferred one, the only one. Every PDA is derived
from $CHARLIE's real mainnet mint.

WHAT IS SIMULATED. The trading. `tools/market_sim.py` starts from
$CHARLIE's live pool reserves and walks a price path, taking pump's real
creator fee per trade at the right tier; the fee then arrives at the vault
as a transfer, because pump does not exist on devnet. So the arithmetic
between "a trade happened" and "a fee arrived" is pump's, and the step
where pump pays is the mock.

WHAT IS NOT PROVEN. The buyback leg accrues and is never spent. Turning
that SOL into destroyed $CHARLIE needs a swap against a live pool, which
devnet does not have, so the page says the leg is funded and stops there.
It does not imply a single token was burned.

No price figure, no dollar figure, no projection -- the rule `/dilution`
runs under, for the same reason.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import site

SPLITTER_FILENAME = "charlie-flywheel.html"

STATE = Path(__file__).resolve().parents[1] / "state" / "flywheel"
SPLITTER_RECORD = STATE / "splitter.json"

# The market runs, in the order a reader should meet them: the rally first
# because it is the headline, then the two that show the mechanism does not
# depend on the price going up.
MARKET_RECORDS = (
    ("rally", STATE / "market.json"),
    ("chop", STATE / "market-chop.json"),
    ("dump", STATE / "market-dump.json"),
)

LAMPORTS_PER_SOL = 1_000_000_000
EXPLORER = "https://explorer.solana.com"

LEGS = (
    ("sol_burn", "SOL burn", "50%",
     "Solana's incinerator. The runtime removes these lamports from the total "
     "supply at the end of the block -- not merely unspendable, destroyed. "
     "That is why its balance is always zero: the burn is proven by the "
     "transfer, never by a balance."),
    ("buyback", "$CHARLIE buy &amp; burn", "25%",
     "A vault only a crank can spend. <strong>This leg is funded here and not "
     "yet spent</strong> -- buying and burning needs a live pool, which devnet "
     "does not have."),
    ("ops", "ops", "25%",
     "An ordinary wallet. Spendable, and the page says so: what it is spent "
     "on is not something a chain shows."),
)


def load(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_all() -> dict | None:
    """The splitter run plus whichever market runs exist, or None when the
    splitter has not been run -- there is no page without it."""
    splitter = load(SPLITTER_RECORD)
    if splitter is None:
        return None
    markets = []
    for name, path in MARKET_RECORDS:
        record = load(path)
        if record is not None:
            markets.append((name, record))
    return {"splitter": splitter, "markets": markets}


def sol(lamports: int, digits: int = 6) -> str:
    return f"{lamports / LAMPORTS_PER_SOL:.{digits}f}"


def share_percent(part: int, whole: int) -> str:
    if not whole:
        return "0.00"
    return f"{part / whole * 100:.2f}"


def _tx_link(signature: str, label: str | None = None) -> str:
    short = f"{signature[:8]}...{signature[-6:]}"
    return (
        f'<a href="{EXPLORER}/tx/{site.esc(signature)}?cluster=devnet" '
        f'rel="noopener">{site.esc(label or short)}</a>'
    )


def _account_link(address: str, label: str | None = None) -> str:
    short = label or f"{address[:6]}...{address[-4:]}"
    return (
        f'<a href="{EXPLORER}/address/{site.esc(address)}?cluster=devnet" '
        f'rel="noopener"><code>{site.esc(short)}</code></a>'
    )


def _banner(splitter: dict) -> str:
    """Three sentences, before any figure, because a reader who stops early
    must still have been told all three."""
    return (
        '<div class="banner">'
        "<p><strong>$CHARLIE cannot actually do this, and that is the point of "
        "the exercise.</strong> Its pump sharing config is <code>admin_revoked</code> "
        "and pays the SOL-burn address 100%: pump allows one irreversible update "
        "and it is spent, so nobody &mdash; its deployer included &mdash; can "
        "point its fees anywhere else.</p>"
        "<p>So this is the answer to a hypothetical: <em>if that destination "
        "could be changed to a splitter, would the mechanism hold?</em> "
        "The program below is deployed on <strong>devnet</strong> and the "
        "transactions below landed. The trading that produced the fees is "
        "<strong>simulated</strong>; the split of those fees is real.</p>"
        "<p>Nothing here is a claim about what $CHARLIE will do, or about "
        "volume, or about price. No such figure appears on this page.</p>"
        "</div>"
    )


def _what_is_real(splitter: dict) -> str:
    vault = splitter["addresses"]["vault"]
    return (
        "<h2>What is real and what is not</h2>"
        '<table class="realness">'
        "<thead><tr><th>the step</th><th>which</th></tr></thead>"
        "<tbody>"
        "<tr><td>the price path, trade sizes, volume</td>"
        '<td class="sim">simulated</td></tr>'
        "<tr><td>pump's creator fee, per trade, at the tier the cap lands on</td>"
        '<td class="real">real &mdash; pump\'s own schedule</td></tr>'
        "<tr><td>the pool arithmetic the price moves on</td>"
        '<td class="real">real &mdash; <code>x&middot;y=k</code>, as PumpSwap does it</td></tr>'
        "<tr><td>the fee <em>arriving</em> at the vault</td>"
        '<td class="sim">simulated &mdash; a transfer, because pump is not on devnet</td></tr>'
        "<tr><td>the three-way split of what arrived</td>"
        '<td class="real">real &mdash; a landed transaction, every time</td></tr>'
        "<tr><td>the SOL burn leg leaving the supply</td>"
        '<td class="real">real &mdash; the runtime destroys it</td></tr>'
        "<tr><td>the buyback leg becoming burned tokens</td>"
        '<td class="sim">not proven &mdash; needs a live pool; the leg is funded and stops there</td></tr>'
        "</tbody></table>"
        f"<p>The starting pool was read from $CHARLIE's live mainnet pool, so the "
        f"fee tier the simulation trades at is the tier the coin is really on. "
        f"Every address here derives from $CHARLIE's real mint.</p>"
        f"<p><strong>The fee destination would be "
        f"{_account_link(vault)}</strong> &mdash; and that address is "
        "<strong>off the ed25519 curve</strong>, so no private key can exist for "
        "it. The split is not the intended exit. It is the only one.</p>"
    )


def _split_table(splitter: dict) -> str:
    totals = splitter["totals"]
    split = totals["sol_burn"] + totals["buyback"] + totals["ops"] + totals["remainder"]
    rows = []
    for key, label, target, why in LEGS:
        moved = totals[key]
        rows.append(
            "<tr>"
            f"<td><strong>{label}</strong><br><span class=\"why\">{why}</span></td>"
            f'<td class="num">{target}</td>'
            f'<td class="num">{share_percent(moved, split)}%</td>'
            f'<td class="num">{moved:,}</td>'
            "</tr>"
        )
    return (
        '<table class="legs">'
        "<thead><tr><th>leg</th><th>target</th><th>measured</th>"
        "<th>lamports</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "<tfoot>"
        f'<tr><td>left in the vault<br><span class="why">the integer-division '
        "remainder. Not swept to a leg &mdash; which leg got it would depend on "
        "the order of the code &mdash; so it waits for the next split.</span></td>"
        f'<td class="num">&mdash;</td>'
        f'<td class="num">{share_percent(totals["remainder"], split)}%</td>'
        f'<td class="num">{totals["remainder"]:,}</td></tr>'
        "</tfoot>"
        "</table>"
    )


def _markets(markets: list) -> list:
    """Each simulated run, with what it actually split.

    Three scenarios rather than one, because a mechanism shown only on a
    rally is a mechanism nobody should trust: pump charges the creator fee
    on sells too, and the falling market is the case that proves it.
    """
    if not markets:
        return []
    out = ["<h2>Simulated trading, real splits</h2>",
           "<p>Each run walks a price path from $CHARLIE's live pool reserves, "
           "takes pump's creator fee on every trade at the tier the market cap "
           "lands on, and sends the accrued fee to the splitter. The rows below "
           "are what the program did with it.</p>"]
    rows = []
    for name, record in markets:
        sim = record["simulated"]
        legs = record["legs_real"]
        total = record["split_total_lamports"]
        start_price = record["start"]["price"]
        end = record["end"]
        change = (end["price"] / start_price - 1) * 100 if start_price else 0
        rows.append(
            "<tr>"
            f"<td><strong>{site.esc(name)}</strong><br>"
            f'<span class="why">{site.esc(record["scenario_why"])}</span></td>'
            f'<td class="num">{sim["trades"]}</td>'
            f'<td class="num">{sol(sim["volume_lamports"], 2)}</td>'
            f'<td class="num">{change:+.1f}%</td>'
            f'<td class="num">{end["creator_fee_bps"]}</td>'
            f'<td class="num">{sol(sim["creator_fee_lamports"])}</td>'
            f'<td class="num">{share_percent(legs["sol_burn"], total)} / '
            f'{share_percent(legs["buyback"], total)} / '
            f'{share_percent(legs["ops"], total)}</td>'
            "</tr>"
        )
    out.append(
        '<div class="scroll"><table class="markets">'
        "<thead><tr><th>scenario</th><th>trades</th><th>volume (SOL)</th>"
        "<th>price</th><th>fee bps</th><th>creator fee (SOL)</th>"
        "<th>split measured</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table></div>"
    )
    out.append(
        "<p><strong>The fee falls as the market cap rises</strong>, which is "
        "pump's design and not this protocol's: a rally pays a <em>smaller</em> "
        "share of each trade. That is the opposite of the intuition, and it is "
        "the reason the simulation uses pump's published schedule rather than a "
        "curve of its own &mdash; a friendlier one would produce larger legs and "
        "prove nothing.</p>"
    )
    out.append(
        "<p>The <strong>dump</strong> row is the one worth reading twice. The "
        "price falls and all three legs still fund, because pump charges the "
        "creator fee on both sides of a trade. A mechanism that only worked on "
        "the way up would be a different mechanism.</p>"
    )
    return out


def _settlements(markets: list) -> list:
    """The individual landed splits, so the table above is checkable."""
    rows = []
    for name, record in markets:
        for entry in record.get("settlements", []):
            event = entry["event"]
            rows.append(
                "<tr>"
                f"<td>{site.esc(name)}</td>"
                f'<td class="num">{entry["after_trade"]}</td>'
                f'<td class="num">{entry["simulated_fee_lamports"]:,}</td>'
                f'<td class="num">{entry["sent_lamports"]:,}</td>'
                f'<td class="num">{event["sol_burn"]:,}</td>'
                f'<td class="num">{event["buyback"]:,}</td>'
                f'<td class="num">{event["ops"]:,}</td>'
                f"<td>{_tx_link(entry['split_signature'])}</td>"
                "</tr>"
            )
    if not rows:
        return []
    return [
        "<h2>Every split that landed</h2>",
        "<p>The simulated fee is what the modelled market produced; the sent "
        "column is what was actually moved on devnet. They differ by a fixed "
        "scale factor, because a devnet wallet does not hold mainnet-sized fees "
        "and the split is proportional &mdash; dividing a fraction proves the "
        "same ratios as dividing all of it. Both numbers are printed so neither "
        "reads as the other.</p>",
        f'<div class="scroll"><table class="settlements">'
        "<thead><tr><th>run</th><th>after trade</th><th>simulated fee</th>"
        "<th>sent</th><th>SOL burn</th><th>buyback</th><th>ops</th>"
        "<th>split tx</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>",
    ]


def _permissionless(splitter: dict) -> list:
    entry = splitter.get("permissionless")
    if not entry:
        return []
    delta = entry["cranker_lamports_delta"]
    return [
        "<h2>Anyone can turn it</h2>",
        "<p><code>split</code> takes no signer. The round below was called by a "
        "wallet that is not the ops address and has no claim on any leg:</p>",
        '<ul class="facts">'
        f"<li>caller: {_account_link(entry['cranker'])}, "
        f"ops: {'yes' if entry['is_ops'] else 'no'}</li>"
        f"<li>the SOL burn leg it moved: <strong>"
        f"{entry['event']['sol_burn']:,}</strong> lamports</li>"
        f"<li>what the caller was paid: <strong>nothing</strong>. Its balance "
        f"changed by {delta:,} lamports &mdash; it paid the fee and received no "
        "share of any leg</li>"
        f"<li>{_tx_link(entry['split_signature'], 'the transaction')}</li>"
        "</ul>",
        "<p>A tip for calling it would be an extractable bounty: the legs would "
        "fund their own collection. There is none.</p>",
    ]


def _refusals(splitter: dict) -> str:
    items = []
    for case in splitter.get("refusals", []):
        refused = case["refused"]
        mark = "&#10003;" if refused else "&#10007;"
        log = (case.get("log") or "").replace("Program log: ", "")
        items.append(
            "<li>"
            f'<span class="mark {"ok" if refused else "bad"}">{mark}</span> '
            f"<strong>{site.esc(case['case'])}</strong> &mdash; refused"
            + (f"<br><code>{site.esc(log)}</code>" if log else "")
            + f'<br><span class="why">{site.esc(case["why"])}</span>'
            "</li>"
        )
    return f'<ul class="refusals">{"".join(items)}</ul>'


_STYLE = site._TOKENS + """
body {
  margin: 0 auto; padding: var(--sp-lg); max-width: 64rem;
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
.realness td:last-child { white-space: normal; }
.real { color: var(--accent); }
.sim { color: var(--unchecked); }
.refusals { list-style: none; padding: 0; }
.refusals li { margin-bottom: var(--sp-md); }
.mark { font-weight: 700; }
.mark.ok { color: var(--accent); }
.mark.bad { color: var(--destructive); }
.facts { list-style: none; padding: 0; font-size: 0.9rem; }
.facts li { margin-bottom: var(--sp-sm); }
footer { margin-top: var(--sp-3xl); border-top: 1px solid var(--panel);
  padding-top: var(--sp-md); font-size: 0.82rem; color: var(--pass-glyph); }
"""


def render(data: dict | None = None, *, now=None) -> str:
    data = data if data is not None else load_all()
    if data is None:
        raise FileNotFoundError(
            f"{SPLITTER_RECORD} does not exist -- run "
            "`python -m tools.splitter_devnet` first. This page renders a run "
            "and must not be able to invent one."
        )
    splitter, markets = data["splitter"], data["markets"]
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now if now is not None else time.time()))
    config = splitter["config_on_chain"]

    body = [
        "<h1>The $CHARLIE flywheel, run on a chain</h1>",
        '<p class="lede">A trade pays a creator fee. Half of it is destroyed, a '
        "quarter buys and burns $CHARLIE, a quarter funds ops. No key can take "
        "any of it anywhere else. Below is that mechanism running, with "
        "signatures &mdash; and a clear line around the parts that are "
        "simulated.</p>",
        _banner(splitter),
        _what_is_real(splitter),
        "<h2>The split, as it landed</h2>",
        f'<div class="scroll">{_split_table(splitter)}</div>',
        '<ul class="facts">'
        f"<li>program {_account_link(splitter['program_id'])} on devnet</li>"
        f"<li>fee destination (the vault) {_account_link(splitter['addresses']['vault'])} "
        "&mdash; off-curve, no key can exist</li>"
        f"<li>buyback vault {_account_link(splitter['addresses']['burn_vault'])} "
        f"&mdash; holding {sol(splitter['burn_vault_balance'])} SOL, unspent</li>"
        f"<li>ops wallet {_account_link(splitter['ops_address'])}</li>"
        f"<li>the mint every address derives from: "
        f"<code>{site.esc(config['mint'])}</code>, $CHARLIE's real mint</li>"
        "</ul>",
        *_markets(markets),
        *_settlements(markets),
        *_permissionless(splitter),
        "<h2>What a caller cannot do</h2>",
        "<p>The guarantee is an absence, and an absence is only worth stating if "
        "you try the thing and print the refusal. Each of these was submitted to "
        "the deployed program:</p>",
        _refusals(splitter),
        "<h2>What would still have to be true</h2>",
        "<p>Three things, none of them code:</p>"
        '<ul class="facts">'
        "<li><strong>A coin whose one update is unspent.</strong> $CHARLIE's is "
        "spent, so this shape can only be adopted by a coin that has not yet set "
        "its split &mdash; or by a new one.</li>"
        "<li><strong>The buyback leg actually spending.</strong> It is funded "
        "here and never spent: burning needs a swap against a live pool. That "
        "step exists and simulates against mainnet, but it has not run.</li>"
        "<li><strong>Upgrade authority revoked.</strong> The program is "
        "upgradeable, so the absences above are today's absences and not yet "
        "guarantees.</li>"
        "</ul>",
        "<footer>"
        f"<p>generated at {site.esc(stamp)} from runs recorded under "
        "<code>state/flywheel/</code>. Every signature is checkable on devnet. "
        "The trading is a model; the splits are transactions.</p>"
        '<p><a href="/">Charlie Protocol</a> &middot; '
        '<a href="/flywheel">the protocol\'s own flywheel</a> &middot; '
        '<a href="/verify">/verify</a></p>'
        "</footer>",
    ]

    return site._document(
        "The $CHARLIE flywheel, run on a chain -- Charlie Protocol",
        "".join(body),
        style=_STYLE,
        description=(
            "If $CHARLIE's creator fee went to a splitter instead of one address: "
            "half destroyed, a quarter to buying and burning $CHARLIE, a quarter "
            "to ops. Simulated trading, real splits, on devnet, with signatures."
        ),
    )


def write(out_dir=site.DEFAULT_OUTPUT_DIR, *, now=None) -> Path:
    path = Path(out_dir) / SPLITTER_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(now=now), encoding="utf-8")
    return path

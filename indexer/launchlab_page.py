"""`/launchlab` -- the LaunchLab rail's cranks, as they ran on devnet.

The rail (`launchlab/`, LAUNCHLAB-RAIL.md) makes Charlie a Raydium LaunchLab
platform: each coin's creator is a program address, so its creator fees can
only be routed by the program. This page shows the four cranks doing that on
devnet, one landed transaction each, and nothing else.

WHAT IS REAL HERE. Unlike `/flywheel`, nothing upstream is mocked: LaunchLab,
CPMM and Raydium's LP lock are Raydium's own devnet programs, the fees came
from real swaps, and graduation was Raydium's migration bot. Every figure is
read off the transaction by `tools/launchlab_devnet.py` into
`state/launchlab/devnet.json`, which is this page's only input.

WHAT IT DOES NOT CLAIM. Devnet, so no figure here says anything about volume,
price, or what a coin earns. The program is upgradeable and its cranks are
admin-only for now (the own-burn buy has no on-chain price bound yet); the
page says both. It carries no generation timestamp, so it changes only when
the record does.

Its own module, like the other pages: `site.py` imports only `invariants` and
`publish` by design.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import flywheel_page, site

LAUNCHLAB_FILENAME = "launchlab.html"

RECORD = Path(__file__).resolve().parents[1] / "state" / "launchlab" / "devnet.json"

# What each crank is, in words a reader can check against its row.
CRANKS = {
    "crank_curve": (
        "Curve creator fee",
        "The coin is still on the LaunchLab curve. Its 0.50% creator fee is claimed "
        "into the coin's collect address and split: 40% buys the coin and burns it, "
        "40% goes to the incinerator, 20% to the dev's ops address.",
    ),
    "crank_amm_creator": (
        "Pool creator fee",
        "The coin graduated to a Raydium CPMM pool whose creator is the coin's "
        "collect address. The pool's creator fee, in both SOL and coin, is collected "
        "and split the same way; the coin side is burned.",
    ),
    "crank_lp": (
        "LP fees",
        "At graduation the pool's LP fee NFT went to the program's signer. Its LP "
        "fees are claimed: 25% of the SOL side to Charlie's pool, the rest split the "
        "same way as above, and the coin side burned.",
    ),
    "crank_platform": (
        "Platform fee",
        "Charlie's 0.25% platform fee on every curve trade, claimed from LaunchLab "
        "and paid in full to Charlie's pool.",
    ),
}


def load(path: Path | None = None) -> dict | None:
    path = path or RECORD
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _coins(record: dict) -> str:
    rows = "".join(
        f"<tr><td>{flywheel_page._account_link(mint)}</td><td>{site.esc(stage)}</td></tr>"
        for mint, stage in record["coins"].items()
    )
    return f"<table><thead><tr><th>coin</th><th>stage</th></tr></thead><tbody>{rows}</tbody></table>"


def _runs(record: dict) -> str:
    rows = []
    for run in record["runs"]:
        name, _ = CRANKS[run["crank"]]
        rows.append(
            "<tr>"
            f"<td>{site.esc(name)}<br>{flywheel_page._tx_link(run['signature'])}</td>"
            f"<td>{run['incinerator']:,}</td>"
            f"<td>{run['charlie_pool']:,}</td>"
            f"<td>{run['ops']:,}</td>"
            f"<td>{run['coins_burned']:,}</td>"
            f"<td>{run['compute_units']:,}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>crank</th><th>to incinerator (lamports)</th>"
        "<th>to Charlie's pool (lamports)</th><th>to ops (lamports)</th>"
        "<th>coin burned (base units)</th><th>compute units</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def render(record: dict | None = None) -> str:
    record = record if record is not None else load()
    if record is None:
        raise FileNotFoundError(
            f"{RECORD} does not exist -- run `python -m tools.launchlab_devnet` first. "
            "This page renders recorded transactions and must not be able to invent one."
        )
    explained = "".join(
        f"<li><strong>{site.esc(CRANKS[c][0])}.</strong> {site.esc(CRANKS[c][1])}</li>"
        for c in CRANKS
    )
    body = [
        "<h1>The LaunchLab rail, run on devnet</h1>",
        '<p class="lede">A coin launched on Raydium LaunchLab through Charlie has a '
        "program address as its creator, so its creator fees cannot be sent anywhere "
        "the program does not send them. Below are the four cranks that route them, "
        "each a landed transaction.</p>",
        '<div class="banner">'
        "<p><strong>This is devnet.</strong> Nothing upstream is mocked: the curve, "
        "the pool and the LP lock are Raydium's own devnet programs, the fees came "
        "from real trades, and Raydium's bot did the graduation. It proves the "
        "mechanism, and says nothing about volume, price, or what any coin would "
        "earn.</p>"
        "<p>The program is <strong>upgradeable</strong>, and for now only its admin "
        "can run the cranks, because the buy that funds the own-token burn has no "
        "on-chain price bound yet.</p>"
        "</div>",
        "<h2>What ran</h2>",
        '<ul class="facts">'
        f"<li>program <code>{site.esc(record['program_id'])}</code> "
        f"{flywheel_page._account_link(record['program_id'])}, on devnet</li>"
        "</ul>",
        '<div class="scroll">' + _coins(record) + "</div>",
        "<h2>The cranks</h2>",
        f'<ul class="facts">{explained}</ul>',
        '<div class="scroll">' + _runs(record) + "</div>",
        "<p>Every figure is a balance change or a burn read off that transaction. The "
        "incinerator's balance is destroyed by the runtime at the end of the block, "
        "so those lamports leave circulation. The ops address on devnet is the "
        "keeper's own wallet; its transaction fee is added back so the column reads "
        "as what the program paid.</p>",
        "<h2>What can still change</h2>",
        "<p>No key the coin's dev holds can redirect these fees. Charlie's admin key "
        "can upgrade the program today, and Raydium controls its own programs, "
        "including a creator-fee share setting on its pools that is zero on every "
        "mainnet config as of 18 September 2026.</p>",
        "<footer>"
        "<p>Rendered from <code>state/launchlab/devnet.json</code>, written by "
        "<code>tools/launchlab_devnet.py</code> from the transactions themselves. "
        "Every signature above is checkable on devnet.</p>"
        '<p><a href="/">Charlie Protocol</a> &middot; '
        '<a href="/flywheel">/flywheel</a> &middot; '
        '<a href="/buildlog">/buildlog</a></p>'
        "</footer>",
    ]
    return site._document(
        "The LaunchLab rail, run on devnet -- Charlie Protocol",
        "".join(body),
        style=flywheel_page._STYLE,
        description=(
            "Charlie Protocol's Raydium LaunchLab rail on devnet: the curve, pool, LP "
            "and platform fee cranks, each a landed transaction with its figures."
        ),
    )


def write(out_dir=site.DEFAULT_OUTPUT_DIR, *, now=None) -> Path:
    path = Path(out_dir) / LAUNCHLAB_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(), encoding="utf-8")
    return path

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
import re
import time
import urllib.parse
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


# -- QT-02: clean-URL composition for the landing page's outbound links ---
# The phrase in an observation's error that means "the chain answered and this
# coin has no fee split", as opposed to "the read failed". Duplicated from
# `pump.NO_FEE_SPLIT_MARKER` rather than imported: this module imports only
# `invariants` and `publish` by design, and a renderer that could import the
# chain decoders would be free to decode rather than render.
# `TestNoFeeSplitPage.test_marker_matches_the_message_pump_actually_raises`
# fails the moment the two drift.
NO_FEE_SPLIT_MARKER = "is not a fee-sharing config"
# `observe` sets this `error_kind` when the bonding curve's creator is not a
# fee-sharing config. A structured field beats matching an error message, and
# the string marker above is kept only so a record written before this field
# existed still renders as the finding it is.
NO_SHARING_CONFIG = "no_sharing_config"

SITE_ORIGIN = "https://charlieprotocol.fun"
META_IMAGE_SRC = "/assets/meta-image.png"
SITE_DESCRIPTION = (
    "Paste a pump.fun contract address and see where its creator fees go, "
    "read from the chain, with every check that passed, failed, or was never run."
)

LANDING_FILENAME = "index.html"
LAMPORTS_PER_SOL = 1_000_000_000
COIN_ROUTE_PREFIX = "/coin/"

# -- artwork ----------------------------------------------------------
# Charlie is NOT redrawn here. `web/assets/charlie.png` is the project's own
# artwork, lifted from the $CHARLIE brand image and keyed to transparency by
# flood-filling only the background-connected black -- his outlines are the
# same #000 as the ground behind him, so a naive colour key would have eaten
# them. Transparency is what lets one file serve both grounds: the near-black
# hero and the paper counters section below it.
#
# The flame is likewise the real `pixel-fire.svg`, inlined rather than linked
# so the guttering animation can be CSS on an element this module emits and
# the page keeps its zero-script property.
CHARLIE_SRC = "/assets/charlie.png"
CHARLIE_INTRINSIC = (383, 484)

# The counters band uses the site's own animated Charlie rather than the
# still. It is the only GIF on charlie-incinerator.com that can sit on the
# paper ground: its background is a real transparent index, where the
# monochrome one's is opaque near-black and would render as a box.
CHARLIE_GIF_SRC = "/assets/charlie-found.gif"

# Charlie watching the candles, from the project's own art. Its background is
# opaque near-black, so it is presented inside a dark panel rather than keyed
# to transparency -- a monitor Charlie is watching, which is what the image
# actually depicts and what the verify page is doing while you wait.
SCANNING_GIF_SRC = "/assets/charlie-scanning.gif"
SCANNING_GIF_INTRINSIC = (320, 320)
CHARLIE_GIF_INTRINSIC = (256, 256)

# The Incinerator logo is Sol Incinerator's mark, not this project's -- the
# same asset charlie-incinerator.com already shows in its history section.
# Charlie's lore starts there, which is the only reason it appears here.
# Split into two layers at the one empty pixel row band in the source
# (rows 130-135), so the smoke can drift without the building drifting with
# it. The percentages below are the layers' own share of the original 333px
# height, which is what keeps the two halves registered as the mark scales.
INCINERATOR_SMOKE_SRC = "/assets/incinerator-smoke.png"
INCINERATOR_STACK_SRC = "/assets/incinerator-stack.png"
INCINERATOR_INTRINSIC = (241, 333)

_INK = {
    "g": "#8FE13F",   # brand green, chart bars
    "d": "#3E6B1C",   # chart grid
}

_FLAME_RECTS = (
    '<rect x="7" y="1" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="6" y="2" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="7" y="2" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="6" y="3" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="7" y="3" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="5" y="4" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="6" y="4" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="7" y="4" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="4" y="5" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="5" y="5" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="6" y="5" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="7" y="5" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="3" y="6" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="4" y="6" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="5" y="6" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="6" y="6" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="7" y="6" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="9" y="6" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="3" y="7" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="4" y="7" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="5" y="7" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="6" y="7" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="7" y="7" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="8" y="7" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="9" y="7" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="2" y="8" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="3" y="8" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="4" y="8" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="5" y="8" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="6" y="8" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="7" y="8" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="8" y="8" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="9" y="8" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="2" y="9" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="3" y="9" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="4" y="9" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="5" y="9" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="6" y="9" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="7" y="9" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="8" y="9" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="9" y="9" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="1" y="10" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="2" y="10" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="3" y="10" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="4" y="10" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="5" y="10" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="6" y="10" width="1" height="1" fill="#ffd84a"/>'
    '<rect x="7" y="10" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="8" y="10" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="9" y="10" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="10" y="10" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="1" y="11" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="2" y="11" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="3" y="11" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="4" y="11" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="5" y="11" width="1" height="1" fill="#ffd84a"/>'
    '<rect x="6" y="11" width="1" height="1" fill="#ffd84a"/>'
    '<rect x="7" y="11" width="1" height="1" fill="#ffd84a"/>'
    '<rect x="8" y="11" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="9" y="11" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="10" y="11" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="11" y="11" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="1" y="12" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="2" y="12" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="3" y="12" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="4" y="12" width="1" height="1" fill="#ffd84a"/>'
    '<rect x="5" y="12" width="1" height="1" fill="#ffd84a"/>'
    '<rect x="6" y="12" width="1" height="1" fill="#ffd84a"/>'
    '<rect x="7" y="12" width="1" height="1" fill="#ffd84a"/>'
    '<rect x="8" y="12" width="1" height="1" fill="#ffd84a"/>'
    '<rect x="9" y="12" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="10" y="12" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="11" y="12" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="12" y="12" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="1" y="13" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="2" y="13" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="3" y="13" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="4" y="13" width="1" height="1" fill="#ffd84a"/>'
    '<rect x="5" y="13" width="1" height="1" fill="#ffd84a"/>'
    '<rect x="6" y="13" width="1" height="1" fill="#ffd84a"/>'
    '<rect x="7" y="13" width="1" height="1" fill="#ffd84a"/>'
    '<rect x="8" y="13" width="1" height="1" fill="#ffd84a"/>'
    '<rect x="9" y="13" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="10" y="13" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="11" y="13" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="12" y="13" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="1" y="14" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="2" y="14" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="3" y="14" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="4" y="14" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="5" y="14" width="1" height="1" fill="#ffd84a"/>'
    '<rect x="6" y="14" width="1" height="1" fill="#ffd84a"/>'
    '<rect x="7" y="14" width="1" height="1" fill="#ffd84a"/>'
    '<rect x="8" y="14" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="9" y="14" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="10" y="14" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="11" y="14" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="2" y="15" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="3" y="15" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="4" y="15" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="5" y="15" width="1" height="1" fill="#ffd84a"/>'
    '<rect x="6" y="15" width="1" height="1" fill="#ffd84a"/>'
    '<rect x="7" y="15" width="1" height="1" fill="#ffd84a"/>'
    '<rect x="8" y="15" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="9" y="15" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="10" y="15" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="11" y="15" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="2" y="16" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="3" y="16" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="4" y="16" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="5" y="16" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="6" y="16" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="7" y="16" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="8" y="16" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="9" y="16" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="10" y="16" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="11" y="16" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="3" y="17" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="4" y="17" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="5" y="17" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="6" y="17" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="7" y="17" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="8" y="17" width="1" height="1" fill="#ff8a1f"/>'
    '<rect x="9" y="17" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="10" y="17" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="4" y="18" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="5" y="18" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="6" y="18" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="7" y="18" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="8" y="18" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="9" y="18" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="5" y="19" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="6" y="19" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="7" y="19" width="1" height="1" fill="#ff4d2e"/>'
    '<rect x="8" y="19" width="1" height="1" fill="#ff4d2e"/>'
)


def _charlie_img(cls: str = "charlie", label: str = "Charlie, the incinerator slug") -> str:
    """The real artwork, as an <img>.

    Width/height are the file's intrinsic pixels so the browser reserves the
    box before the image lands and nothing below it shifts; the stylesheet
    overrides both. `image-rendering: pixelated` there keeps the grid hard at
    display sizes larger than 1:1.
    """
    w, h = CHARLIE_INTRINSIC
    return (
        f'<img class="{cls}" src="{CHARLIE_SRC}" width="{w}" height="{h}"'
        f' alt="{esc(label)}" decoding="async">'
    )


def _charlie_gif(cls: str = "charlie-gif", label: str = "") -> str:
    w, h = CHARLIE_GIF_INTRINSIC
    return (
        f'<img class="{cls}" src="{CHARLIE_GIF_SRC}" width="{w}" height="{h}"'
        f' alt="{esc(label)}" decoding="async" loading="lazy">'
    )


def _flame_svg(cls: str = "flame-svg") -> str:
    return (
        f'<svg class="{cls}" viewBox="0 0 16 20" width="16" height="20"'
        ' shape-rendering="crispEdges" xmlns="http://www.w3.org/2000/svg"'
        ' aria-hidden="true">' + "".join(_FLAME_RECTS) + "</svg>"
    )


def _scene() -> str:
    """Charlie crawling toward the incinerator, which is smoking.

    Three nested elements around Charlie because each carries its own
    `transform` and a second animation on the same element would simply
    overwrite the first: `.walker` travels, `.facing` turns him to face the
    way he is going, the `<img>` does the squash of a slug pulling itself
    along. The smoke gets two elements for the same reason -- one sways, one
    billows -- on deliberately non-harmonic periods so the drift does not
    read as a short loop.
    """
    return (
        '<div class="scene" aria-hidden="true">'
        '<div class="scene-track">'
        '<div class="walker"><div class="facing">'
        + _charlie_img(cls="charlie walk", label="")
        + "</div></div>"
        "</div>"
        '<div class="furnace">'
        '<span class="smoke-drift">'
        f'<img class="smoke" src="{INCINERATOR_SMOKE_SRC}" alt="" decoding="async">'
        "</span>"
        f'<img class="stack" src="{INCINERATOR_STACK_SRC}" alt="" decoding="async">'
        "</div>"
        "</div>"
    )


def _sol_coin_svg(cls: str = "payload") -> str:
    """The thing Charlie is carrying. Drawn rather than an asset so it takes
    the page's own colours and stays crisp at the size the orbit needs.
    """
    return (
        f'<svg class="{cls}" viewBox="0 0 16 16" width="16" height="16" '
        'aria-hidden="true" focusable="false">'
        '<rect x="2" y="2" width="12" height="12" class="coin-body"></rect>'
        '<rect x="4" y="5" width="8" height="2" class="coin-mark"></rect>'
        '<rect x="4" y="9" width="8" height="2" class="coin-mark"></rect>'
        "</svg>"
    )


def _flywheel() -> str:
    """The loop, running.

    A ring with the incinerator at its hub and Charlie carrying a payload
    round it. The motion is the point: this is a cycle that feeds itself, and
    a static diagram of a cycle is a picture of something standing still.

    Two nested elements around Charlie, for the reason `_scene` needs three:
    one element can carry one `transform` animation. `.fly-orbit` sweeps the
    full circle, `.fly-rider` counter-sweeps at the same period so he stays
    upright the whole way round rather than cartwheeling.

    The ordered list below is not a caption for the graphic, it IS the
    mechanism -- readable with the animation stopped, with images off, and on
    a phone where the ring is too small for labels. The ring is `aria-hidden`
    for that reason: a screen reader gets the list, not a description of a
    circle.
    """
    steps = (
        ("Trading pays a creator fee",
         "Every buy and every sell on the coin generates one. It accrues "
         "whether anyone is watching or not."),
        ("The fee buys the token, and the token is burned",
         "Supply falls, and it cannot be reissued: the mint authority is "
         "revoked."),
        ("Buying is volume, and volume pays more fees",
         "The burn is funded by the activity it creates. That is the part "
         "that turns a mechanism into a loop."),
        ("A share reaches Solana&#x27;s incinerator",
         "The runtime removes those lamports from the total supply at the "
         "end of the block. Two supplies fall at once: the coin&#x27;s, and "
         "Solana&#x27;s."),
    )
    items = "".join(
        f"<li><h3>{title}</h3><p>{body}</p></li>" for title, body in steps
    )
    return (
        '<section id="flywheel">'
        "<h2>The loop</h2>"
        "<p>Fees are not collected here. They are spent destroying supply, and "
        "the destroying is what produces the next fee.</p>"
        '<figure class="fly">'
        '<div class="fly-stage" aria-hidden="true">'
        '<svg class="fly-ring" viewBox="0 0 240 240" focusable="false">'
        '<circle class="fly-path" cx="120" cy="120" r="92"></circle>'
        '<g class="fly-ticks">'
        '<circle cx="120" cy="28" r="5"></circle>'
        '<circle cx="212" cy="120" r="5"></circle>'
        '<circle cx="120" cy="212" r="5"></circle>'
        '<circle cx="28" cy="120" r="5"></circle>'
        "</g>"
        "</svg>"
        '<div class="fly-hub">'
        '<span class="smoke-drift">'
        f'<img class="smoke" src="{INCINERATOR_SMOKE_SRC}" alt="" decoding="async">'
        "</span>"
        f'<img class="stack" src="{INCINERATOR_STACK_SRC}" alt="" decoding="async">'
        "</div>"
        '<div class="fly-orbit"><div class="fly-rider">'
        + _sol_coin_svg()
        + f'<img class="charlie" src="{CHARLIE_SRC}" '
        f'width="{CHARLIE_INTRINSIC[0]}" height="{CHARLIE_INTRINSIC[1]}" '
        'alt="" decoding="async">'
        "</div></div>"
        "</div>"
        "<figcaption>Charlie carrying it round, and into the fire.</figcaption>"
        "</figure>"
        f'<ol class="fly-steps">{items}</ol>'
        "<p class=\"meta\">Every number this produces is checked before it is "
        "shown, and withheld when it is not. That is the rest of this site.</p>"
        "</section>"
    )


def _chart_svg(cls: str = "chart") -> str:
    """A candlestick the slug is watching. Each bar is its own element so the
    CSS can scale them on staggered delays -- the chart moves, which is the
    only thing a chart has ever done.
    """
    bars = ((0, 14, 10), (1, 9, 16), (2, 18, 6), (3, 5, 20), (4, 12, 13), (5, 2, 23))
    rects = []
    for i, y, h in bars:
        rects.append(
            f'<rect class="bar b{i}" x="{4 + i * 8}" y="{y}" width="5" height="{h}"'
            f' fill="{_INK["g"]}"/>'
        )
    grid = "".join(
        f'<rect x="0" y="{y}" width="52" height="1" fill="{_INK["d"]}"/>'
        for y in (8, 16, 24)
    )
    return (
        f'<svg class="{cls}" viewBox="0 0 52 28" width="52" height="28"'
        ' shape-rendering="crispEdges" xmlns="http://www.w3.org/2000/svg"'
        ' role="img" aria-label="A chart, moving">'
        + grid + "".join(rects) + "</svg>"
    )


def _coin_url(mint: str, suffix: str) -> str:
    """A `/coin/` clean URL for a coin page or its record (D-A: routing by
    rewrite, never by rename). Always composes `_artifact_name` rather than
    building the filename a second way, so a clean-URL link and the flat
    file `write()` produces can never disagree.
    """
    return COIN_ROUTE_PREFIX + _artifact_name(mint, suffix)


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
        return f"SOL burn {value['sol_burn']} bps / BURN {value['burn']} bps / OPS {value['paid']} bps"
    if name in (invariants.SOL_BURN_TOTAL, invariants.OPS_TOTAL, invariants.BURN_TOTAL):
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


# -- PUB-03: the SOL burn Failure Banner ---------------------------------------
def _sol_burn_failure_sentence(config) -> str:
    """The permanence claim, computed from THIS coin's own `admin_revoked` --
    replaces a static sentence that asserted a permanence established only
    for one coin's revoked config. When `admin_revoked` is true the config
    cannot be changed by anyone but pump, which is what makes "not pending"
    a claim this coin's own state actually supports; when it is not revoked,
    the config can still be changed by its admin, and nothing here may claim
    otherwise.
    """
    if config is not None and config.admin_revoked:
        return "No SOL burn total is publishable for this coin -- permanently, not pending: its configuration is admin_revoked, and cannot be changed by anyone but pump."
    return "No SOL burn total is publishable for this coin. Its configuration is not admin_revoked, so it can still be changed by its own admin."


def _sol_burn_failure_banner(observation) -> str:
    """Unconditional, full-bleed banner when `SOL_BURN_UNSPENDABLE` reads FAIL --
    never dismissible, never collapsible, never conditionally hidden beyond
    that one test. Renders the check's own `detail` verbatim and in full;
    never a hardcoded restatement of it.
    """
    check = next((c for c in observation.checks if c.name == "SOL_BURN_UNSPENDABLE"), None)
    if check is None or check.status != invariants.FAIL:
        return ""
    return (
        '<section class="sol-burn-failure-banner" data-banner="sol-burn-failure">'
        '<h1 class="banner-headline">SOL_BURN_UNSPENDABLE: FAIL</h1>'
        f'<p class="banner-body">{esc(check.detail)}</p>'
        f'<p class="banner-static">{esc(_sol_burn_failure_sentence(observation.config))}</p>'
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
            "No burns are recorded against this coin yet. The moment the evidence "
            "store records one, it is attributed here."
        )
    tokens_ui = summary["tokens"] / (10 ** decimals)
    tx_word = "transaction" if summary["count"] == 1 else "transactions"
    return (
        f"Boost burned {tokens_ui:,.{decimals}f} tokens across {summary['count']} "
        f"{tx_word} in a {summary['window_seconds']}-second window, at migration -- not "
        "by any keeper of ours, and this protocol's own watcher could not see it happen. "
        "The protocol's own crank has never run for this coin -- no program is deployed "
        "yet."
    )


def _non_boost_sentence(burn_events, decimals: int) -> str:
    """Recorded burns that are NOT boost, stated separately and computed.

    This sentence exists because the boost block used to open "every token
    $CHARLIE has ever lost was destroyed by pump's boost", which was true when
    the only recorded burns were boost's and became FALSE the first time the
    walk recorded an `spl_burn`. A superlative about all burns cannot be a
    template constant on a page whose evidence store keeps growing; it has to
    be recomputed or not said. This says the narrower, checkable thing.
    """
    rows = [r for r in (burn_events or []) if r.get("source") != BOOST_SOURCE]
    if not rows:
        return (
            "No burn from any other source is recorded against this coin. If one is "
            "ever recorded, it is attributed here rather than folded into the total "
            "above."
        )
    tokens_ui = sum(int(r.get("tokens_burned", 0)) for r in rows) / (10 ** decimals)
    word = "burn" if len(rows) == 1 else "burns"
    return (
        f"Separately, {len(rows)} recorded {word} came from somewhere else: "
        f"{tokens_ui:,.{decimals}f} tokens destroyed directly, by holders burning their "
        "own, with no mechanism running and nobody asking. Boost did not do these and "
        "neither did we. They are counted anyway, and that is deliberate (D-09): the "
        "figure they back asks how much of this token is gone, not how much of it we "
        "destroyed. A protocol that counted only its own burns would be flattering "
        "itself -- it would report the burns it caused and quietly drop everyone "
        "else's. So the walk records every burn against this mint, by anyone, whether "
        "or not it ever touched our crank."
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
    "The opening-balance mechanism is dormant on live data (D-07).",
)


def _sol_burn_risk(observation) -> str:
    """The SOL burn leg's standing, read from this coin's own check.

    This was a template constant: "SOL_BURN_UNSPENDABLE fails permanently
    for this coin, not pending." It was written for $CHARLIE, rendered on
    every coin's page, and then stopped being true for $CHARLIE as well when
    the check stopped grading an unenrolled coin against the enrolled-coin
    vault standard. Four branches, and which one applies is the
    observation's to say, not this module's.
    """
    checks = {c.name: c for c in getattr(observation, "checks", None) or ()}
    check = checks.get("SOL_BURN_UNSPENDABLE")
    if check is None or check.status == invariants.UNCHECKED:
        return (
            "This split names no SOL burn destination, so no SOL burn claim is "
            "available to this coin at all."
        )
    if check.status == invariants.FAIL:
        return (
            "SOL_BURN_UNSPENDABLE fails for this coin: a SOL burn destination is an "
            "ordinary address someone can spend from, and no SOL burn total will be "
            "published while that is so."
        )
    # Read from the attribution the observation already carries, not from a
    # registry consulted here: the page states the standing the coin was
    # observed under, and a default registry read at render time is not that.
    split = getattr(observation, "split", None)
    attributions = [a for a in getattr(split, "attributions", ()) if a.leg == "sol_burn"]
    if any("grandfathered" in a.reason for a in attributions):
        return (
            "This coin's SOL burn destination is the shared grandfathered address. "
            "Attribution across the coins sharing it is not possible, so it carries "
            "the weaker <= invariant and no SOL burn total is published for it."
        )
    return (
        "SOL_BURN_UNSPENDABLE passes. A SOL burn total is still gated on "
        "SOL_BURN_BALANCE, which reconciles recorded inflows against the live "
        "balance and stays UNCHECKED until a walk of the destination completes."
    )


def _walk_risk(observation) -> str:
    """The burn-walk risk, stated from the recorded cursor rather than pinned.

    This was a template constant reading "the mint-wide burn walk is
    incomplete". It stayed on the page after the walk completed, which made it
    a claim about the evidence that the evidence contradicted -- the precise
    failure this page exists to refuse, committed by the page itself. Both
    branches are worth saying, and which one is true is not this module's to
    decide.
    """
    if getattr(observation, "burn_walk_complete", False):
        return (
            "The mint-wide burn walk is complete and the residual survives it: tokens "
            "are missing from the supply that a full walk of the burn history does not "
            "account for. The residual is a recorded open discrepancy, not a settled "
            "figure, and never a number on this page -- see the committed reconciliation "
            "artifact (state/RECONCILIATION.md)."
        )
    return (
        "The mint-wide burn walk is incomplete, so the residual is not a settled figure "
        "-- see the committed reconciliation artifact (state/RECONCILIATION.md), never a "
        "number on this page."
    )


def _cannot_enroll(observation) -> str:
    """UI-SPEC's mandatory copy block (domain context #2), rendered as plain
    prose with no card/border treatment -- it is context, not a figure.

    D-27: the index and every page in it are the top of the enrollment
    funnel, not a leaderboard. Computed from THIS coin's own
    `config.admin_revoked` -- a revoked config gets the explanation a
    revoked config actually supports; a config that is not revoked gets the
    other half, phrased as an open door rather than a verdict, and promising
    no date, mechanism or outcome (phase 5 owns enrollment).
    """
    config = observation.config
    if config is not None and config.admin_revoked:
        body = (
            "This coin's sharing config is <code>admin_revoked</code>: permanent, "
            "and only pump could ever reset it. It cannot enroll in its own "
            "protocol."
        )
    else:
        body = (
            "This coin's sharing config is not <code>admin_revoked</code> -- its "
            "split can still be changed by its own admin. That makes it a coin "
            "that could enroll once the protocol program exists; nothing here "
            "promises when or how."
        )
    return f'<section id="cannot-enroll"><p>{body}</p></section>'


def _launch_mode(observation) -> str:
    """Which pump launch mode this coin is, from the chain.

    Three observed facts, not gated figures -- each says what the coin IS,
    never how much moved, so none is a member of `invariants.FIGURES`.
    """
    rows = []
    cashback = getattr(observation, "cashback", None)
    if cashback is None:
        rows.append(
            "<li><strong>Trader Cashback:</strong> unknown. This coin's bonding "
            "curve predates the field, so the flag is absent rather than clear "
            "-- absent is not the same as off, and this page will not read it "
            "as one.</li>"
        )
    elif cashback:
        rows.append(
            "<li><strong>Trader Cashback: on.</strong> Chosen at launch and "
            "locked on chain, it routes the whole creator fee to traders "
            "instead of the deployer. Read from the bonding curve, not from "
            "pump's API.</li>"
        )
    else:
        rows.append(
            "<li><strong>Trader Cashback: off.</strong> The creator fee goes "
            "where the sharing config sends it.</li>"
        )

    charity = getattr(observation, "charity_recipients", ()) or ()
    if charity:
        bps = getattr(observation, "donate_gg_fee_bps", None)
        cut = f"{bps / 100:g}%" if bps else "an unread share"
        listed = ", ".join(f"<code>{esc(a)}</code>" for a in charity)
        rows.append(
            "<li><strong>Charity coin.</strong> The config pays donate.gg's fee "
            f"wallet {cut} and sends the rest to {listed}. "
            "<em>That is where the config points, and nothing more.</em> "
            "pump takes no custody and donate.gg alone converts and forwards, "
            "so the chain stops being evidence at that wallet: this page "
            "cannot tell you a charity received anything, and does not "
            "pretend to.</li>"
        )

    if not rows:
        return ""
    return (
        '<section id="launch-mode">'
        "<h2>Launch Mode</h2>"
        "<p>What pump was configured to do with this coin's creator fees, read "
        "from the chain.</p>"
        f'<ul class="modes">{"".join(rows)}</ul>'
        "</section>"
    )


def _results_chart(observation) -> str:
    """A bar per check status, drawn from the checks themselves.

    Inline SVG because the page ships no script and never will. Every bar is
    labelled with its own count in text as well as length, so the figure is
    readable without seeing the graphic at all -- a chart nobody can read is
    decoration, and a chart that is the ONLY place a number appears is worse.
    """
    checks = getattr(observation, "checks", ()) or ()
    if not checks:
        return ""
    order = [("PASS", invariants.PASS), ("FAIL", invariants.FAIL),
             ("UNCHECKED", invariants.UNCHECKED)]
    counts = [(label, sum(1 for c in checks if c.status == status)) for label, status in order]
    total = len(checks)
    widest = max((n for _l, n in counts), default=0) or 1

    # See `_no_split_breakdown`: narrow enough to render unscaled on a phone.
    row_h, gap, label_w, bar_w = 26, 8, 96, 170
    height = len(counts) * (row_h + gap)
    bars = []
    for i, (label, n) in enumerate(counts):
        y = i * (row_h + gap)
        w = int(bar_w * n / widest) if n else 0
        bars.append(
            f'<text x="0" y="{y + 17}" class="chart-label">{esc(label)}</text>'
            f'<rect x="{label_w}" y="{y}" width="{w}" height="{row_h}" '
            f'class="bar-{label.lower()}"></rect>'
            f'<text x="{label_w + w + 8}" y="{y + 17}" class="chart-value">{n}</text>'
        )
    return (
        '<section id="results">'
        "<h2>Results</h2>"
        f"<p>{total} checks ran: "
        + ", ".join(f"{n} {label.lower()}" for label, n in counts)
        + ". A failed check is not a footnote here -- it is why a figure above "
        "is missing.</p>"
        f'<svg class="chart" viewBox="0 0 {label_w + bar_w + 48} {height}" '
        f'width="{label_w + bar_w + 48}" height="{height}" role="img" '
        f'aria-label="{total} checks: '
        + "; ".join(f"{n} {label.lower()}" for label, n in counts)
        + '">'
        + "".join(bars)
        + "</svg>"
        "</section>"
    )


def _deflation(observation) -> str:
    """The SOL that passed through this coin's burns, and the deflation it
    would have produced had it been burned instead of spent.

    ONE WORD FOR IT. A SOL burn IS deflation -- SOL sent where no key can
    spend it never returns to circulation, which is the only sense in which
    SOL can be burned at all. "Removed from circulation", "locked" and
    "sealed" are the same event under softer names, and several names for one
    thing leave a reader unsure whether they are several things.

    Summed from `burn_events.sol_spent` -- the same rows behind the "SOL spent
    buying them" counter, so this figure and that one can never disagree. It
    is an observed fact, not a member of `invariants.FIGURES`: it states what
    the recorded burns cost, and claims nothing about what was routed.

    ONE SENTENCE, AND IT SAYS ONLY "DEFLATION". The figure is a counterfactual,
    so `This did not happen` states that outright -- and then it stops. Every
    further clause is an opportunity to imply something about the protocol's
    modes that is not true.

    In particular it must never read as a trade-off between burning the coin's
    supply and burning SOL. Those are not alternatives: PROTOCOL.md sec.2
    mode 2 is `{SOL_BURN: n, BURN: 10000 - n}` -- both, in one split, already
    specified. Copy pitting one against the other argues against a shipped
    mode.

    Stated only when the burn walk is COMPLETE. A partial walk yields a
    smaller total that looks exactly like a finished one, and `ankr` and
    `shyft` were observed answering signature queries with an empty array
    rather than an error, so "we scanned and found little" and "we could not
    scan" are indistinguishable downstream unless something refuses to guess.
    """
    rows = getattr(observation, "burn_events", None) or []
    complete = bool(getattr(observation, "burn_walk_complete", False))
    lamports = sum(int(r.get("sol_spent") or 0) for r in rows)

    if not complete:
        return (
            '<section id="deflation">'
            "<h2>SOL That Could Have Been Burned</h2>"
            "<p>Not shown. The burn walk for this mint has not finished, and a "
            "partial walk gives a smaller total that looks exactly like a "
            "finished one.</p>"
            "</section>"
        )
    if not rows:
        return (
            '<section id="deflation">'
            "<h2>SOL That Could Have Been Burned</h2>"
            "<p>No burn is recorded against this mint, so no SOL has passed "
            "through one. The walk finished and found none.</p>"
            "</section>"
        )

    sol = lamports / LAMPORTS_PER_SOL
    return (
        '<section id="deflation">'
        "<h2>SOL That Could Have Been Burned</h2>"
        f'<p class="deflation-value">{sol:,.9f} SOL</p>'
        "<p><strong>This did not happen.</strong> Burned SOL never comes back, "
        "so routing this much to a SOL burn vault is that much permanent "
        "deflation.</p>"
        f'<p class="meta">Summed from the {len(rows)} recorded burn '
        f"{'transaction' if len(rows) == 1 else 'transactions'} behind the "
        "figures above, over a completed walk of this mint.</p>"
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
        "<p>Three destinations for creator fees. Two of them burn; what they "
        "destroy is different, and the third destroys nothing.</p>"
        '<div class="table-scroll"><table class="legs">'
        "<tr><th>Leg</th><th>Action</th><th>Permitted claim</th><th>Forbidden claim</th></tr>"
        "<tr><td>SOL burn</td><td>SOL to a vault no key can spend</td>"
        '<td>"burned", "deflationary", only where '
        "SOL_BURN_UNSPENDABLE passes</td>"
        '<td>"burned" when the destination is spendable</td></tr>'
        "<tr><td>BURN</td><td>SOL buys the token, then an SPL burn</td>"
        '<td>"burned", "permanently destroyed"</td><td>none</td></tr>'
        "<tr><td>OPS</td><td>SOL to a spendable wallet</td>"
        '<td>"funds operations"</td><td>"burned"</td></tr>'
        "</table></div>"
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
    non_boost = _non_boost_sentence(observation.burn_events, decimals)
    tx_links = _tx_links(observation.burn_events)
    atomic_check = next((c for c in observation.checks if c.name == "BURN_ATOMIC"), None)
    atomic_paragraph = (
        f'<p class="burn-atomic-detail">{esc(atomic_check.detail)}</p>' if atomic_check is not None else ""
    )
    return (
        '<section id="the-burn">'
        "<h2>The Burn</h2>"
        f"<p>{esc(sentence)}</p>"
        f'<p class="non-boost">{esc(non_boost)}</p>'
        f"{tx_links}"
        f"{atomic_paragraph}"
        "</section>"
    )


def _quiet(observation) -> str:
    """Honest about what does not apply -- true of every coin, and asserts
    no coin's split.

    This used to assert a specific split as a template constant ("its split
    is 100% SOL burn"), the exact failure this project has shipped twice. The
    claim that is actually true of every coin today is checkable in the
    code, not in any one coin's bps: `legs.PROGRAM_ID` is `None`, so no
    address can be derived as a burn pool, so no coin has a recognised BURN
    destination and nothing cranks for anyone. The split itself is already
    rendered, with its own backing check, in the figures block above --
    saying it again here in prose would be a second place it could drift.
    The window figure is computed from the same `_boost_summary`, never a
    second, independently hardcoded copy of the number `_the_burn` already
    computed; it depends on burn history, not on the split, so this section's
    text is identical for two coins that differ only in their bps.
    """
    summary = _boost_summary(observation.burn_events)
    if summary["count"]:
        boost_note = (
            "The one burn event in this coin's history was pump's boost, a "
            f"single {esc(summary['window_seconds'])}-second window at migration, "
            "not a recurring mechanism."
        )
    else:
        boost_note = "No burn is recorded against this coin's history."
    return (
        '<section id="quiet">'
        "<h2>Quiet</h2>"
        "<p>No protocol program is deployed, so no address can be derived as a burn "
        "pool -- no coin has a recognised BURN destination today, and nothing cranks "
        "for anyone. There is no crank to pause or resume for this coin, because none "
        f"has ever run. {boost_note}</p>"
        "</section>"
    )


def _log(observation) -> str:
    """Today's expected empty state -- no protocol crank has ever run, for
    any coin, until phase 5 deploys a program. That fact is universal and
    named as such; the burn-composition sentence beside it is computed from
    THIS coin's own `burn_events`, never assumed to match the reference
    coin's shape (all-boost).
    """
    burn_events = observation.burn_events or []
    non_boost = [r for r in burn_events if r.get("source") != BOOST_SOURCE]
    if not burn_events:
        burn_note = "No burn is recorded against this mint yet."
    elif not non_boost:
        burn_note = (
            "Every burn recorded against this mint so far was pump's boost, not "
            'any keeper of ours -- see <a href="#the-burn">The Burn</a>.'
        )
    else:
        burn_note = (
            "Not every burn recorded against this mint was pump's boost -- see "
            '<a href="#the-burn">The Burn</a> for the full breakdown.'
        )
    return (
        '<section id="log">'
        "<h2>Log</h2>"
        "<h3>No cranks yet</h3>"
        "<p>The protocol's crank has never run, for any coin, because no program is "
        f'deployed (see <a href="#risks">Risks</a>). {burn_note}</p>'
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
    # Absolute, into the repository, NOT relative. `../state/evidence/`
    # resolves correctly while the page is read inside the repo and 404s the
    # moment it is served, because vercel.json's outputDirectory is `web/`
    # and the export lives outside it. This is the "verify it yourself" link;
    # a 404 here costs more than a 404 anywhere else on the page.
    evidence_href = esc(f"{REPO_URL}/tree/main/{EVIDENCE_EXPORT_PATH}")
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
    before the footer. The cannot-enroll statement (item 2) and the SOL burn
    Failure Banner (item 3) are rendered earlier in `render()`, ahead of the
    Figures section (item 4), matching UI-SPEC's approved structural order
    exactly.

    Risks is built inline here, not in a helper, so D-20's seventh entry --
    the sweep's claim and its limit, carried in the module's one generator-
    unverified constant -- is interpolated exactly once in this function's
    own source, never re-typed apart from it.
    """
    risk_items = [f"<li>{esc(text)}</li>" for text in _RISKS]
    risk_items.append(f"<li>{esc(_sol_burn_risk(observation))}</li>")
    risk_items.append(f"<li>{esc(_walk_risk(observation))}</li>")
    risk_items.append(f'<li id="{RISK_GENERATOR_ANCHOR}">{esc(_GENERATOR_UNVERIFIED)}</li>')
    risks = '<section id="risks"><h2>Risks</h2><ol class="risks">' + "".join(risk_items) + "</ol></section>"

    return "".join(
        (
            _launch_mode(observation),
            _results_chart(observation),
            # `_deflation` is deliberately NOT rendered here.
            #
            # It stated a counterfactual: what a coin's recorded burns would
            # have destroyed had that SOL gone to a burn instead of buying
            # tokens. No check backs a counterfactual, because nothing
            # happened for a check to read. Every other figure on this page is
            # gated on a passing check; that one was gated on nothing, which
            # is the rule this page exists to enforce.
            #
            # It also read as an accusation rather than a measurement. On a
            # coin whose 17.58 SOL bought and burned 43.58M tokens, the
            # largest number on the page was a hypothetical printed under the
            # words "This did not happen".

            _how_it_works(),
            _the_burn(observation),
            _quiet(observation),
            _log(observation),
            risks,
        )
    )


# 02-03 Task 2: the `:root` custom-property block, extracted so the coin
# page and the landing page share one palette and one spacing scale rather
# than two that could drift -- `_STYLE` and `_LANDING_STYLE` both begin with
# this same string, asserted (not asserted-by-eye) in tests/test_site.py.
_TOKENS = """
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
}"""

# The paste box appears on four surfaces now -- /verify, the landing hero, the
# no-split coin page and 404 -- across three stylesheets. Defined once so it
# cannot be styled on one and unstyled on another, which is what shipped when
# the box was added to the landing page whose sheet had never carried it.
_VERIFY_FORM_CSS = """
.verify-form { display: flex; flex-wrap: wrap; gap: var(--sp-sm);
  align-items: center; margin: var(--sp-lg) 0; }
.verify-form label { flex: 1 1 100%; font-size: 14px; }
.verify-form input {
  flex: 1 1 22em; min-width: 0; padding: var(--sp-sm);
  font-family: inherit; font-size: 16px;
  border: 1px solid var(--unchecked); background: #fff; color: var(--ink);
}
.verify-form button {
  padding: var(--sp-sm) var(--sp-lg); font-family: inherit; font-size: 16px;
  border: 1px solid var(--ink); background: var(--ink); color: var(--paper);
  cursor: pointer; min-height: 44px;
}
.verify-form button:hover, .verify-form button:focus-visible {
  background: var(--accent); border-color: var(--accent);
}
.hero-verify { margin: var(--sp-xl) 0 0 0; }
.hero-verify .meta { margin: 0; }
"""

_STYLE = _TOKENS + _VERIFY_FORM_CSS + """
.modes { padding-left: var(--sp-lg); }
.modes li { margin-bottom: var(--sp-md); line-height: 1.6; }
.chart { max-width: 100%; height: auto; margin: var(--sp-md) 0; }
.chart-label { font-size: 13px; fill: var(--ink); font-family: inherit; }
.chart-value { font-size: 13px; fill: var(--ink); font-family: inherit; font-weight: 700; }
.bar-pass { fill: var(--pass-glyph); }
.bar-fail { fill: var(--destructive); }
.bar-unchecked { fill: none; stroke: var(--unchecked); stroke-width: 1; stroke-dasharray: 3 3; }
.bar-neutral { fill: var(--pass-glyph); }
.deflation-value {
  font-size: clamp(22px, 4vw, 34px); font-weight: 700; margin: 0 0 var(--sp-md) 0;
  overflow-wrap: anywhere;
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
  /* The page is full of long, unbroken chain-derived strings (addresses,
     signatures) with no internal spaces to wrap at -- inherited so every
     descendant wraps a long token rather than forcing the whole document
     into horizontal scroll at a narrow viewport (the mint-address/config-
     admin-address backstop). */
  overflow-wrap: anywhere;
}
h1 { font-size: 32px; font-weight: 700; line-height: 1.2; margin: 0 0 var(--sp-md) 0; overflow-wrap: anywhere; }
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
.sol-burn-failure-banner {
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
.table-scroll { overflow-x: auto; }
.legs { border-collapse: collapse; width: 100%; table-layout: fixed; }
.legs th, .legs td {
  text-align: left;
  padding: var(--sp-sm);
  border-bottom: 1px solid var(--panel);
  word-break: break-word;
  overflow-wrap: anywhere;
}
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
footer a { word-break: break-all; overflow-wrap: anywhere; }
"""


# 02-03 Task 2: the landing page's own stylesheet -- shares `_TOKENS` (one
# palette, one spacing scale) and diverges only in density: fewer, larger
# blocks, the hero counters at display size, its own room to breathe. No
# `_STYLE` rule is imported wholesale -- the landing page is its own HTML
# document via `_document(..., style=_LANDING_STYLE)`, so every rule it
# needs (including the ones the reused `_check_row()` markup depends on --
# `.check-row`/`.badge`/the three status classes) is declared here too.
_LANDING_STYLE = _TOKENS + _VERIFY_FORM_CSS + """
/* -- the flywheel ------------------------------------------------------
   The ring turns because the loop turns. A still picture of a cycle is a
   picture of something that has stopped.

   `.fly-orbit` sweeps the full circle and `.fly-rider` counter-sweeps on the
   same period, so Charlie stays upright the whole way round instead of
   cartwheeling. One element carries one transform animation; a second on the
   same element overwrites the first, which is why there are two. */
.fly { margin: var(--sp-xl) 0; }
.fly-stage {
  position: relative;
  width: min(240px, 74vw);
  aspect-ratio: 1;
  margin: 0 auto;
}
.fly-ring { position: absolute; inset: 0; width: 100%; height: 100%; }
.fly-path {
  fill: none;
  stroke: var(--unchecked);
  stroke-width: 1;
  stroke-dasharray: 4 6;
  opacity: 0.75;
  animation: fly-crawl 5.5s linear infinite;
}
@keyframes fly-crawl { to { stroke-dashoffset: -40; } }
.fly-ticks circle { fill: var(--pass-glyph); opacity: 0.5; }
.fly-hub {
  position: absolute;
  left: 50%; top: 50%;
  transform: translate(-50%, -50%);
  width: 30%;
  display: flex; flex-direction: column; align-items: center;
  line-height: 0;
}
.fly-hub .stack { width: 100%; height: auto; image-rendering: pixelated; }
.fly-hub .smoke { width: 62%; height: auto; image-rendering: pixelated; }
.fly-orbit {
  position: absolute; inset: 0;
  animation: fly-orbit 18s linear infinite;
}
/* The ring is r=92 in a 240 box, so its top sits 28/240 = 11.67% down. The
   rider is centred ON that point rather than hung off the top edge of the
   stage -- at the edge he orbited a larger circle than the one drawn and
   leaned over the hub. Centring means the translate has to be carried into
   the counter-rotation keyframe too, or the first frame snaps him sideways. */
.fly-rider {
  position: absolute;
  left: 50%; top: 11.67%;
  width: 15%;
  transform: translate(-50%, -50%);
  animation: fly-rider 18s linear infinite;
  display: flex; align-items: flex-end; justify-content: center; gap: 3px;
}
.fly-rider .charlie { width: 100%; height: auto; image-rendering: pixelated; }
/* Beside him, not above: carried, not hovering. */
.fly-rider .payload { width: 38%; height: auto; flex: none; }
.coin-body { fill: var(--accent); }
.coin-mark { fill: var(--paper); }
@keyframes fly-orbit { to { transform: rotate(360deg); } }
@keyframes fly-rider {
  to { transform: translate(-50%, -50%) rotate(-360deg); }
}
.fly figcaption {
  text-align: center; font-size: 14px; margin-top: var(--sp-md);
  color: var(--pass-glyph);
}
.fly-steps {
  list-style: none; counter-reset: fly; padding: 0;
  margin: var(--sp-xl) 0 0 0;
  display: grid; gap: var(--sp-lg);
  grid-template-columns: repeat(auto-fit, minmax(15em, 1fr));
}
.fly-steps li {
  counter-increment: fly;
  border-left: 1px dashed var(--unchecked);
  padding-left: var(--sp-md);
}
.fly-steps li h3 {
  font-size: 16px; margin: 0 0 var(--sp-xs) 0; line-height: 1.3;
}
.fly-steps li h3::before {
  content: counter(fly) " ";
  color: var(--pass-glyph); font-weight: 400;
}
.fly-steps li p { margin: 0; font-size: 15px; }

/* Motion is decoration here: the list carries the mechanism, so a reader who
   asks for less of it loses nothing. */
@media (prefers-reduced-motion: reduce) {
  .fly-path, .fly-orbit, .fly-rider { animation: none; }
}

:root {
  /* Palette taken from the live $CHARLIE site (charlie-incinerator.com):
     near-black ground with the brand green as the single accent. Matching it
     is the point -- two surfaces for one project should not disagree about
     what colour the project is. */
  --ash: #0D0F0C;
  --ash-raised: #161A14;
  --ash-ink: #EAF3E3;
  --ash-muted: #8B9585;
  --ember: #8FE13F;
  --hair: #D3D8CD;
  --measure: 62ch;
  --shell: 1120px;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  background: var(--paper);
  color: var(--ink);
  font-family: ui-monospace, "SFMono-Regular", Menlo, Consolas, "Liberation Mono", monospace;
  font-size: 16px;
  font-weight: 400;
  line-height: 1.55;
  margin: 0;
  padding: 0;
  overflow-wrap: anywhere;
}

/* -- entrance motion -------------------------------------------------
   Entrance only. Nothing here animates a VALUE: a count-up would render
   figures that are not the observed ones for the length of the tween, and
   a page whose whole claim is that every number is backed by a check must
   not display numbers no check backs, not even for a second. Motion is
   restricted to opacity and position, which carry no data. The landing
   page also ships zero scripts, so this is CSS only and degrades to the
   final state when animation is unavailable. */
@keyframes rise { from { opacity: 0; transform: translateY(14px); } to { opacity: 1; transform: none; } }
@keyframes draw { from { transform: scaleX(0); } to { transform: scaleX(1); } }
.rise { animation: rise 620ms cubic-bezier(.16,.84,.44,1) both; }
.d1 { animation-delay: 60ms; } .d2 { animation-delay: 130ms; } .d3 { animation-delay: 200ms; }
.d4 { animation-delay: 270ms; } .d5 { animation-delay: 340ms; } .d6 { animation-delay: 410ms; }
@media (prefers-reduced-motion: reduce) {
  .rise { animation: none; opacity: 1; transform: none; }
  .hero-rule { animation: none; transform: none; }
}

/* -- hero ------------------------------------------------------------ */
.hero {
  background: var(--ash);
  color: var(--ash-ink);
  padding: clamp(var(--sp-2xl), 9vw, 104px) var(--sp-lg) clamp(var(--sp-xl), 6vw, 72px);
}
.hero-inner { max-width: var(--shell); margin: 0 auto; }
.eyebrow {
  font-size: 12px; letter-spacing: .18em; text-transform: uppercase;
  color: var(--ember); margin: 0 0 var(--sp-lg) 0;
  display: flex; align-items: center; gap: var(--sp-sm);
}
.flame { display: inline-flex; line-height: 0; flex: 0 0 auto; }
.hero h1 {
  font-size: clamp(34px, 6.6vw, 72px);
  font-weight: 700; line-height: 1.02; letter-spacing: -.03em;
  margin: 0 0 var(--sp-lg) 0; overflow-wrap: anywhere;
}
.hero .tagline {
  font-size: clamp(16px, 2.1vw, 21px); line-height: 1.45;
  color: var(--ash-ink); max-width: var(--measure); margin: 0 0 var(--sp-xl) 0;
}
.hero-rule {
  height: 1px; background: var(--ember); transform-origin: left center;
  animation: draw 900ms cubic-bezier(.16,.84,.44,1) 120ms both;
  margin: 0 0 var(--sp-lg) 0;
}
.hero .meta { color: var(--ash-muted); }

/* -- shell ----------------------------------------------------------- */
main { max-width: var(--shell); margin: 0 auto; padding: clamp(var(--sp-xl), 5vw, 64px) var(--sp-lg) var(--sp-3xl); }
section { margin-bottom: clamp(var(--sp-2xl), 5vw, 72px); }
.meta { font-size: 13px; font-weight: 400; line-height: 1.5; }
.freshness .meta { margin: 0 0 var(--sp-xs) 0; }
a { color: var(--accent); }
.visually-hidden {
  position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0;
}
.section-label {
  font-size: 11.5px; letter-spacing: .17em; text-transform: uppercase;
  color: var(--unchecked); margin: 0 0 var(--sp-lg) 0;
}
/* The label rides the art band's baseline, to the right of Charlie and the
   chart, rather than wrapping in a narrow column of its own. */
#counters .section-art .section-label { margin-bottom: 0; flex: 1 1 auto; align-self: flex-end; }

/* -- counters --------------------------------------------------------
   Column minimum is set by the widest value, not by taste: the supply
   counters run to 18 monospace characters, which at ~0.6em per character
   needs ~260px at 24px type. A narrower column does not wrap them -- they
   cannot wrap, by the rule below -- it makes them overflow the next cell. */
#counters {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: var(--sp-xl) var(--sp-2xl);
}
.counter-cell { min-width: 0; border-top: 1px solid var(--hair); padding-top: var(--sp-md); }
.counter-value {
  font-size: clamp(19px, 2.7vw, 27px);
  font-weight: 700; line-height: 1.2; letter-spacing: -.015em;
  margin: 0 0 var(--sp-sm) 0;
  overflow-wrap: normal; word-break: keep-all;
  font-variant-numeric: tabular-nums;
}
.counter-label { font-size: 15px; font-weight: 400; margin: 0 0 var(--sp-xs) 0; }
.counter-source { margin: 0; color: var(--unchecked); }

/* -- artwork ---------------------------------------------------------
   Original inline sprites. All motion is CSS on elements this module emits,
   so the landing page keeps zero scripts: nothing here can alter a figure,
   and there is no frame loop to get out of step with the record. */
.hero-art { display: flex; align-items: flex-end; gap: var(--sp-xl); flex-wrap: wrap; }
.hero-copy { flex: 1 1 24rem; min-width: 0; }
.charlie { image-rendering: pixelated; display: block; }

/* -- the scene: Charlie crawls, the incinerator burns -----------------
   `.walker` travels the full width of `.scene-track` using the left/
   translateX pair, which resolves against the track at any width without
   the stylesheet needing to know that width. `.facing` flips him at the
   turn; the <img> squashes. Three elements because they each animate
   `transform`, and one element can only hold one. */
.scene { display: flex; align-items: flex-end; gap: var(--sp-lg);
  margin: var(--sp-xl) 0 var(--sp-lg); }
/* Both sizes are driven by HEIGHT, not width, because that is the axis the
   proportion lives on: the track's height IS Charlie's height, and the
   furnace box is sized so its building half -- 59.16% of the box, the rest
   being plume -- lands at roughly three times it. Sizing these by width
   instead had him rendering 131px tall against a 95px building, a slug
   taller than the incinerator he is walking to. */
.scene-track { position: relative; flex: 1 1 auto;
  height: clamp(38px, 5vw, 64px); }
/* Both carry height:100% so the <img>'s own height:100% has something to
   resolve against -- an auto-height ancestor leaves it indeterminate and the
   image falls back to its intrinsic 484px. */
.walker { position: absolute; bottom: 0; left: 0; height: 100%;
  animation: crawl 21s ease-in-out infinite alternate; }
.facing { height: 100%; animation: face 42s steps(1, end) infinite; }
.scene .charlie.walk { height: 100%; width: auto;
  transform-origin: 50% 100%; animation: lurch 1.9s ease-in-out infinite; }

@keyframes crawl { from { left: 0; transform: translateX(0); }
                   to   { left: 100%; transform: translateX(-100%); } }
/* The source art faces LEFT -- his eyes sit left of his body's centre. So
   the mirrored half is the one where he travels RIGHT, which is the first
   half of the crawl's alternate cycle. Getting this backwards makes him
   moonwalk. Stepped, so the turn is a flip and not a smear. */
@keyframes face { 0%, 49.99% { transform: scaleX(-1); }
                  50%, 100%  { transform: scaleX(1); } }
@keyframes lurch { 0%, 100% { transform: scaleY(1) scaleX(1); }
                   45% { transform: scaleY(.93) scaleX(1.05); }
                   70% { transform: scaleY(1.03) scaleX(.98); } }

/* The mark's own aspect ratio, so the two layers stay registered to each
   other at every width: smoke pinned to the top, building to the bottom,
   each keeping its share of the original 333px height. */
.furnace { position: relative; flex: 0 0 auto; line-height: 0;
  height: clamp(190px, 26vw, 325px); width: auto; aspect-ratio: 241 / 333; }
.furnace img { image-rendering: pixelated; display: block;
  position: absolute; left: 0; width: 100%; height: auto; }
.furnace .stack { bottom: 0; }
.smoke-drift { position: absolute; left: 0; top: 0; width: 100%;
  transform-origin: 50% 100%;
  animation: sway 11.9s ease-in-out infinite; }
.furnace .smoke { top: 0; transform-origin: 50% 100%;
  animation: billow 7.3s ease-in-out infinite; }

/* Two periods that do not divide into each other, so the combined drift
   takes a long time to repeat itself and never reads as a short loop. */
@keyframes sway {
  0%   { transform: translateX(0)     skewX(0deg); }
  28%  { transform: translateX(3.5%)  skewX(-2.6deg); }
  54%  { transform: translateX(-2.2%) skewX(1.6deg); }
  79%  { transform: translateX(4.4%)  skewX(-3.1deg); }
  100% { transform: translateX(0)     skewX(0deg); }
}
@keyframes billow {
  0%   { transform: translateY(0)     scale(1);     opacity: .95; }
  38%  { transform: translateY(-3.2%) scale(1.05);  opacity: .74; }
  71%  { transform: translateY(-1.4%) scale(1.018); opacity: .89; }
  100% { transform: translateY(0)     scale(1);     opacity: .95; }
}
.hero-art .charlie { width: clamp(128px, 18vw, 200px); height: auto; flex: 0 0 auto; }
/* Spans every column: the art band introduces the counters, it is not a
   counter itself. Without this it takes the first grid cell and shoves the
   first figure into column two. */
.section-art { grid-column: 1 / -1; display: flex; align-items: flex-end; gap: var(--sp-lg);
  margin-bottom: var(--sp-xl); flex-wrap: wrap; }
.section-art .charlie-gif { image-rendering: pixelated; display: block;
  width: clamp(88px, 11vw, 124px); height: auto; }
.section-art .chart { width: clamp(104px, 13vw, 148px); height: auto; }
.refusal-art { display: flex; gap: var(--sp-lg); align-items: flex-start; flex-wrap: wrap; }
.refusal-copy { flex: 1 1 24rem; min-width: 0; }

/* Blink: the eyes squash to a line and back. Long pause, quick close --
   a steady metronome reads as a machine, not a slug. */


/* The flame gutters: two-frame pixel animation, stepped so it stays crisp
   rather than smearing between states. */
@keyframes gutter {
  0%, 100% { transform: scaleY(1) translateY(0); }
  25%      { transform: scaleY(1.14) translateY(-1px); }
  50%      { transform: scaleY(.92) translateY(1px); }
  75%      { transform: scaleY(1.06) translateY(0); }
}
.flame-svg { width: clamp(16px, 2vw, 22px); height: auto; transform-origin: 50% 100%;
  animation: gutter 640ms steps(2, end) infinite; }

/* The chart moves, because that is what a chart does. Each bar scales from
   its own baseline on a staggered delay. */
@keyframes tick { 0%, 100% { transform: scaleY(1); } 50% { transform: scaleY(.55); } }
.chart .bar { transform-box: fill-box; transform-origin: 50% 100%; animation: tick 2.6s ease-in-out infinite; }
.chart .b0 { animation-delay: 0ms; }   .chart .b1 { animation-delay: 180ms; }
.chart .b2 { animation-delay: 360ms; } .chart .b3 { animation-delay: 540ms; }
.chart .b4 { animation-delay: 720ms; } .chart .b5 { animation-delay: 900ms; }

@media (prefers-reduced-motion: reduce) {
  .flame-svg, .chart .bar, .walker, .facing,
  .scene .charlie.walk, .smoke-drift, .furnace .smoke { animation: none; }
}

/* -- status + refusal ------------------------------------------------- */
.badge { display: inline-flex; align-items: center; gap: var(--sp-xs); padding: 2px 8px; }
.status-pass { color: var(--pass-glyph); background: none; font-size: 14px; font-weight: 400; }
.status-fail { background: var(--destructive); color: #fff; font-size: 20px; font-weight: 700; }
.status-unchecked { border: 1px dashed var(--unchecked); color: var(--unchecked); font-size: 14px; font-weight: 400; }
.supply-refusal {
  padding: var(--sp-lg); margin: 0 0 clamp(var(--sp-2xl), 5vw, 72px) 0;
  background: var(--panel);
}
.supply-refusal p { font-size: 16px; font-weight: 400; margin: 0 0 var(--sp-md) 0; max-width: var(--measure); }
/* The row's spans are separate inline elements with no whitespace between
   them in the markup, so without a flex gap they render as
   BURN_SUPPLYFAILinitial_supply -- three fields read as one string. */
.check-row {
  padding: var(--sp-md) 0 0 0; border-top: 1px solid var(--hair);
  display: flex; flex-wrap: wrap; align-items: baseline;
  gap: var(--sp-xs) var(--sp-md);
}
.check-row .check-detail, .check-row .check-expected-actual { flex-basis: 100%; }
.check-name { font-size: 14px; font-weight: 400; }
.check-equation { font-size: 14px; font-weight: 400; }
.check-detail { font-size: 16px; font-weight: 400; margin: var(--sp-xs) 0 0 0; }
.check-expected-actual { font-size: 14px; font-weight: 400; margin: var(--sp-xs) 0 0 0; }

/* -- prose + links ---------------------------------------------------- */
#what-this-is p { font-size: clamp(16px, 1.9vw, 18px); line-height: 1.6; max-width: var(--measure); }
/* Bordered like the refusal block above it, because it is the same kind of
   statement: a thing this page will not claim yet. */
#launch-soon {
  border: 1px dashed var(--unchecked); padding: var(--sp-lg);
  max-width: var(--measure);
}
.soon-head { display: flex; align-items: center; gap: var(--sp-md);
  flex-wrap: wrap; margin-bottom: var(--sp-md); }
.soon-head h2 { margin: 0; font-size: clamp(19px, 2.4vw, 24px); }
#launch-soon p { font-size: clamp(15px, 1.8vw, 17px); line-height: 1.6;
  margin: 0 0 var(--sp-md) 0; }
#launch-soon p:last-child { margin-bottom: 0; }

#why-counted { max-width: var(--measure); }
#why-counted p { font-size: clamp(15px, 1.8vw, 17px); line-height: 1.6;
  margin: 0 0 var(--sp-md) 0; }
#why-counted p:last-child { margin-bottom: 0; color: var(--unchecked); }

#two-ways-in { display: flex; flex-wrap: wrap; gap: var(--sp-md); }
#two-ways-in p { margin: 0; }
#two-ways-in a {
  display: inline-block; padding: var(--sp-md) var(--sp-lg);
  background: var(--ash); color: var(--ash-ink); text-decoration: none;
  font-size: 15px; border: 1px solid var(--ash);
}
#two-ways-in a:hover, #two-ways-in a:focus-visible { background: var(--ember); border-color: var(--ember); color: var(--ash); }
footer { border-top: 1px solid var(--hair); padding-top: var(--sp-lg); margin-top: var(--sp-2xl); }
footer p { font-size: 13px; font-weight: 400; margin: 0 0 var(--sp-sm) 0; color: var(--unchecked); max-width: var(--measure); }
footer a { word-break: break-all; overflow-wrap: anywhere; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
"""


def _no_split_breakdown(observation) -> str:
    """The split for a coin with no sharing config, stated rather than implied.

    "This coin does not split its creator fees" tells a reader what is absent.
    It does not tell them what IS happening, and what is happening is the whole
    point: every basis point of the creator fee goes to one ordinary wallet,
    and none of it goes to a burn of either kind. Saying only the first half
    leaves the reader to work out the second, or to assume the page could not
    determine it.

    Drawn from `error_kind == "no_sharing_config"`, which is the chain saying
    the creator address is not a fee-sharing config. With no config there is
    nothing to divide the fee between, so the shares are not estimated: they
    follow from the absence.

    Not a gated FIGURE. `split` is published only where CONFIG_MINT and
    SPLIT_SUM pass, and neither can run without a config. This is an observed
    fact about where pump sends the fee, and it names no figure.
    """
    creator = getattr(observation, "creator", None)
    rows = [("SOL burn", 0), ("Token burn", 0), ("To the creator", 10000)]
    # Sized so the whole svg fits a 375px phone without `max-width:100%`
    # scaling it down, which shrinks the label text with it.
    row_h, gap, label_w, bar_w = 26, 8, 118, 140
    height = len(rows) * (row_h + gap)
    bars = []
    for i, (label, bps) in enumerate(rows):
        y = i * (row_h + gap)
        w = int(bar_w * bps / 10000)
        # Deliberately NOT the failure red. Paying the creator is what most
        # coins do and this page does not grade it; borrowing the colour a
        # failed check owns would tell the reader it is wrong.
        cls = "bar-neutral" if bps else "bar-unchecked"
        bars.append(
            f'<text x="0" y="{y + 17}" class="chart-label">{esc(label)}</text>'
            f'<rect x="{label_w}" y="{y}" width="{w}" height="{row_h}" class="{cls}"></rect>'
            f'<text x="{label_w + w + 8}" y="{y + 17}" class="chart-value">'
            f"{bps / 100:g}%</text>"
        )
    destination = (
        f"<p>All of it goes to <code>{esc(creator)}</code>, an ordinary "
        "wallet.</p>" if creator else ""
    )
    cashback = ""
    if getattr(observation, "cashback", None) is True:
        cashback = (
            "<p>Trader Cashback is on for this coin, so part of the fee pump "
            "collects is returned to traders. That is pump's mechanism and sits "
            "outside the creator fee shown here.</p>"
        )
    return (
        '<section id="split-breakdown">'
        "<h2>Where the creator fee goes</h2>"
        f'<svg class="chart" viewBox="0 0 {label_w + bar_w + 60} {height}" '
        f'width="{label_w + bar_w + 60}" height="{height}" role="img" '
        'aria-label="SOL burn 0 percent; token burn 0 percent; to the creator '
        '100 percent"'
        f">{''.join(bars)}</svg>"
        + destination
        + "<p><strong>Nothing is burned.</strong> No part of this fee reaches a "
        "SOL burn vault or buys tokens to destroy them. There is no fee-sharing "
        "config for this coin, so there is nothing to divide the fee between.</p>"
        + cashback
        + "</section>"
    )


def _document(title: str, body: str, *, style: str = _STYLE, description: str = "") -> str:
    """Wraps `body` (already-built HTML) in the page shell -- the one place
    `<!doctype html>`/`<head>`/`<style>` are assembled, shared by the coin
    page's normal render path, its page-level error branch, and the landing
    page (02-03). `title` is the fully-formed `<title>` text (a caller builds
    it, e.g. `f"{mint} -- Charlie Protocol"`); `style` defaults to the coin
    page's own `_STYLE` so every existing call site is unaffected.
    """
    summary = description or SITE_DESCRIPTION
    return (
        "<!doctype html>"
        '<html lang="en">'
        "<head>"
        '<meta charset="utf-8">'
        # Without this a phone lays the page out at ~980px and scales the whole
        # thing down, so every figure on it arrives too small to read. Most of
        # the traffic this site is being pointed at is mobile.
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{title}</title>"
        f'<meta name="description" content="{esc(summary)}">'
        # A link posted without these renders as a bare URL with no card, which
        # reads as a dead or unfinished site -- and this site is being shared
        # by link, in posts, as its primary route in.
        f'<meta property="og:title" content="{esc(title)}">'
        f'<meta property="og:description" content="{esc(summary)}">'
        '<meta property="og:type" content="website">'
        f'<meta property="og:image" content="{SITE_ORIGIN}{META_IMAGE_SRC}">'
        '<meta name="twitter:card" content="summary_large_image">'
        f'<meta name="twitter:title" content="{esc(title)}">'
        f'<meta name="twitter:description" content="{esc(summary)}">'
        f'<meta name="twitter:image" content="{SITE_ORIGIN}{META_IMAGE_SRC}">'
        f"<style>{style}</style>"
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

    if observation.error and observation.config is None and (
        getattr(observation, "error_kind", None) == NO_SHARING_CONFIG
        or NO_FEE_SPLIT_MARKER in (observation.error or "")
    ):
        # NOT an error. The chain was read and it answered: this coin pays its
        # creator fee to an ordinary wallet, so there is no split to verify.
        # That is the majority of pump coins, so it is the page most visitors
        # see, and it used to say "No observation" and "a tick that could not
        # read the chain" -- telling them the tool had broken when it had
        # worked and had an answer.
        # `creator` is stored on the observation now. It used to be pulled
        # back out of the error string with a regex, which made a fact the
        # page renders depend on the wording of an error message.
        if getattr(observation, "creator", None) is None:
            found = re.search(r"its creator ([1-9A-HJ-NP-Za-km-z]{32,44})",
                              observation.error or "")
            if found:
                observation.creator = found.group(1)
        body = (
            header
            + '<section class="error-state">'
            + "<h2>This coin does not split its creator fees</h2>"
            + "<p>There is no fee-sharing config for it. That is a fact about "
            "the coin, not a failure here: the chain was read and this is what "
            "it says.</p>"
            + "</section>"
            + _no_split_breakdown(observation)
            + '<section class="error-state">'
            + "<p>Charlie Protocol checks coins that route their fees through "
            "a split. When a coin does, this page shows where every basis "
            "point goes, what each destination actually is, and which checks "
            "passed, failed, or were never run.</p>"
            + '<form class="verify-form" method="get" action="/verify">'
            '<label for="mint">Try another contract address (CA)</label>'
            '<input id="mint" name="mint" type="text" inputmode="latin" '
            'autocomplete="off" spellcheck="false" '
            'placeholder="paste the CA here" '
            'pattern="[1-9A-HJ-NP-Za-km-z]{32,44}" required>'
            '<button type="submit">Verify</button>'
            "</form>"
            + "</section>"
        )
        return _document(f"{mint} -- Charlie Protocol", body + f"<script>{_COPY_SCRIPT}</script>")

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
        return _document(f"{mint} -- Charlie Protocol", body + f"<script>{_COPY_SCRIPT}</script>")

    publisher = publish.Publisher(observation)
    banner = _sol_burn_failure_banner(observation)
    figure_rows = "".join(_figure_row(publisher, name) for name in invariants.FIGURES)
    checks_rows = "".join(_check_row(check) for check in observation.checks)

    # Structural order follows UI-SPEC's Page Structure & Component Inventory
    # exactly: header (1) -> cannot-enroll (2) -> SOL burn Failure Banner (3) ->
    # Figures (4) -> [checks list, not separately numbered there] -> How It
    # Works/The Burn/Quiet/Log/Risks (5-9, `_sections()`) -> Raw Observation
    # JSON (10, `_raw_record_section()`) -> footer (11).
    body = (
        header
        + _cannot_enroll(observation)
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

    return _document(f"{mint} -- Charlie Protocol", body)


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


# -- 03-01 Task 3: the coin index -- D-27's funnel, not a leaderboard -------
def write_record(observation, out_dir=DEFAULT_OUTPUT_DIR) -> Path:
    """Writes only the record -- `record_json(observation)` at
    `_artifact_name(mint, ".json")`. Does not modify `write()`. D-31 makes
    the record the artifact that must exist for every observed coin; the
    page is generated only where there is a reader.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / _artifact_name(observation.mint, ".json")
    json_path.write_text(record_json(observation) + "\n", encoding="utf-8", newline="\n")
    return json_path


def index_rows(records, known_pages=frozenset()) -> list[str]:
    """One row string per stored record (the shape `Store.read()`-style JSON
    gives), each routed through `publish.gate_stored_record` first -- a
    committed record already carries its own `publishable`/`blocked`
    fields, and gating on those at read time is the mechanism `cli._log_lines`
    already uses to replay a record safely. Registered in `publish.SURFACES`
    (`stored_records` input) and deliberately excluded from
    `tests/test_publication.py`'s `FULL_DETAIL_SURFACES`: an index row shows
    one figure by design, not all five.

    `known_pages` is an optional set of mints known to have a committed
    page; a mint in that set gets a link to its page as well as its record,
    one that is not gets only the record link.
    """
    rows = []
    for record in records:
        gated = publish.gate_stored_record(record)
        mint = gated.get("mint")
        split = gated.get("split")
        blocked = gated.get("blocked") or {}
        record_href = esc(_artifact_name(mint, ".json"))

        if split:
            label = publish.classification(split)
            backing = (gated.get("backed_by") or {}).get(invariants.SPLIT) or []
            backing_text = ", ".join(backing) if backing else "(no check named)"
            figure_html = (
                f'<span class="index-split">SOL burn {esc(split.get("sol_burn"))} bps / '
                f'BURN {esc(split.get("burn"))} bps / OPS {esc(split.get("paid"))} bps '
                f"-- {esc(label)}</span>"
                f'<span class="index-backs">backed by: {esc(backing_text)}</span>'
            )
        else:
            reasons = blocked.get(invariants.SPLIT) or [
                {"check": "NO_CHECK", "status": invariants.UNCHECKED}
            ]
            withholding = ", ".join(f'{r["check"]} ({r["status"]})' for r in reasons)
            figure_html = (
                '<span class="index-withheld">withheld</span>'
                f'<span class="index-backs">withheld by: {esc(withholding)}</span>'
            )

        links = [f'<a href="{record_href}">record</a>']
        if mint in known_pages:
            page_href = esc(_coin_url(mint, ""))
            links.insert(0, f'<a href="{page_href}">page</a>')

        rows.append(
            f'<div class="index-row" data-mint="{esc(mint)}">'
            f'<span class="index-mint">{esc(mint)}</span>'
            f"{figure_html}"
            f'<span class="index-links">{" ".join(links)}</span>'
            "</div>"
        )
    return rows


def coverage_statement(counts: dict) -> str:
    """What this index covers, stated in counts it can actually back.

    **D-35.** This used to name an enumerated total, how many configs had
    more than one shareholder, and how many were still reconfigurable. The
    chain-wide sweep that produced those numbers was cut (D-33), so they are
    no longer measured, and a total this project does not measure has no
    business appearing beside numbers it does. Intake is submission-driven:
    the honest statement is how many coins have been OBSERVED, and
    how many could not be.

    It states **no denominator and no percentage**, deliberately. "N of M
    coins" and "X% covered" are the two shapes that would smuggle back the
    claim the sweep was cut from under. That is the same class of defect as
    a figure with no passing check behind it. The difference is that
    here nothing would even flag it, which is why the refusal is enforced by
    `KEYS` below and by a test rather than left to whoever edits this next.

    Computed, never a constant, for `_non_boost_sentence`'s reason: a
    coverage sentence that stopped being true is exactly as wrong as a
    figure that was never checked.

    Avoids every name in `invariants.FIGURES`, so the sentence renders on the
    landing page too, which asserts none of those five names appears
    anywhere in its document, stylesheet included.
    """
    # The only counts this sentence may state. Anything else a caller hands
    # in is ignored rather than rendered: an enumerated total arriving here
    # by a future edit must produce silence, not a sentence.
    observed = int(counts.get("observed", 0) or 0)
    failed = int(counts.get("failed", 0) or 0)

    coin_word = "coin" if observed == 1 else "coins"
    # "observed", never "submitted and observed". The count is len(records)
    # -- coins with a committed record -- and the reference coin has one
    # without anyone having submitted it. Saying "submitted" asserted a
    # request that never happened, on a page whose whole claim is that a
    # figure names what backs it. Corrected 2026-09-02 before it could
    # describe a stranger's coin.
    sentence = f"{observed:,} {coin_word} observed"
    if failed:
        attempt = "attempt" if failed == 1 else "attempts"
        sentence += f", {failed:,} {attempt} recorded as failed"
    return sentence + ". Coins are measured when someone submits them, so this is not a census."


# Not mint-derived, so routing it through `_artifact_name` would be a second
# naming scheme entering by the back door -- the same reasoning
# `LANDING_FILENAME` already carries. Page one is `coins-1.html`; there is
# no unnumbered file, and `/coins` reaches page one by rewrite
# (`vercel.json`) rather than by a duplicate file.
# Not mint-derived, so it does not route through `_artifact_name`, for the
# reason `LANDING_FILENAME` already carries. `/verify` reaches it by rewrite.
VERIFY_FILENAME = "verify.html"

# The pre-filled submission issue (D-23/D-34). A plain link -- the page ships
# no script, and the deployment holds no secret, so the queue is GitHub's and
# every request in it is public.
SUBMIT_REPO = "needsmorergb/charlie-protocol-site"

# Duplicated from `intake`, NOT imported: `intake` imports this module, so the
# import would be circular. Kept as plain strings the same way
# `EVIDENCE_EXPORT_PATH` mirrors `export.DEFAULT_EXPORT_DIR`, and pinned by
# `tests/test_site.py::TestSubmitIssueUrl` -- if these ever drift from
# `intake`'s, the pre-filled issue this page links to stops being recognised
# as a submission and every request through it is silently dropped.
_SUBMISSION_MARKER = "<!-- charlie-protocol:submission -->"
_SUBMISSION_TITLE_PREFIX = "[coverage]"


def submit_issue_url(repo: str = SUBMIT_REPO) -> str:
    """The pre-filled issue a submitter lands on.

    Carries `intake.SUBMISSION_MARKER` in the body and
    `intake.SUBMISSION_TITLE_PREFIX` in the title so `intake.is_submission`
    recognises it. The label is deliberately NOT set here: GitHub drops a
    `labels` parameter for anyone without triage permission, which is every
    stranger this queue exists for, so relying on it would silently lose the
    submissions that matter most.
    """
    title = f"{_SUBMISSION_TITLE_PREFIX} <paste your mint address>"
    body = "\n\n".join((
        _SUBMISSION_MARKER,
        "Mint address:",
        "(paste the mint address on the line above, nothing else)",
    )) + "\n"
    query = urllib.parse.urlencode({"title": title, "body": body})
    return f"https://github.com/{repo}/issues/new?{query}"


def render_verify(*, now=None, example_mint=None) -> str:
    """`/verify` with no mint after it.

    Carries a real paste box, and it works with NO JavaScript: a plain GET
    form submits to `/verify?mint=...`, and `vercel.json` redirects that to
    `/verify/<mint>`. A redirect rather than a rewrite, so the address the
    visitor ends up on is the shareable one WEB-05 asks for rather than a
    query string.

    Says "contract address (CA)" as well as "mint". They are the same 32-byte
    address, but a pump.fun user copies a thing labelled CA, and a page that
    only says "mint address" asks them to make that connection themselves.
    """
    stamp = _stamp(now() if callable(now) else (now if now is not None else time.time()))
    href = esc(submit_issue_url())
    coins = esc(INDEX_FILENAME_TEMPLATE.format(page=1))
    example = example_mint or ""
    example_block = ""
    if example:
        example_block = (
            f'<p class="meta">Worked example: '
            f'<a href="{esc(COIN_ROUTE_PREFIX.rstrip("/"))}/{esc(example)}">'
            f"/coin/{esc(example)}</a></p>"
        )
    body = (
        "<header>"
        "<h1>Verify a coin</h1>"
        "<p>Paste a pump.fun contract address -- the CA, also called the mint "
        "address -- and get that coin's page: how its creator fees are split, "
        "what each destination actually is, and every check that passed, "
        "failed, or was never run, beside the figure it backs.</p>"
        "</header>"
        "<main>"
        '<div class="scanner"><img class="scanning" src="' + SCANNING_GIF_SRC + '"'
        f' width="{SCANNING_GIF_INTRINSIC[0]}" height="{SCANNING_GIF_INTRINSIC[1]}"'
        ' alt="Charlie watching the candles" decoding="async"></div>'
        '<form class="verify-form" method="get" action="/verify">'
        '<label for="mint">Contract address (CA)</label>'
        '<input id="mint" name="mint" type="text" inputmode="latin" '
        'autocomplete="off" spellcheck="false" '
        'placeholder="paste the CA here" '
        'pattern="[1-9A-HJ-NP-Za-km-z]{32,44}" required>'
        '<button type="submit">Verify</button>'
        "</form>"
        '<p class="meta">No account, nothing to connect, no wallet signature. '
        "You can also go straight to <code>/verify/&lt;CA&gt;</code>.</p>"
        + example_block +
        "<p><strong>It answers for any coin.</strong> If nobody has submitted "
        "that CA before, the chain is read while you wait and the answer is "
        "built from that reading. Most pump coins pay their creator fee to an "
        "ordinary wallet and have no split at all, and the page says so in "
        "those words rather than pretending to grade something that is not "
        "there.</p>"
        f'<p><a href="{href}">Submit a CA for the committed record</a> -- an '
        "answer read live is not stored. Submitting opens a public issue, and "
        "the coin gets a committed page whose figures are backed by recorded "
        "evidence rather than a single reading. No account here, no approval "
        "from us.</p>"
        f'<p><a href="{coins}">Every coin measured so far</a>.</p>'
        '<p><strong>Own a coin?</strong> '
        '<a href="/enroll">Set where its creator fee goes</a> -- connect the '
        "wallet that administers it and route the fee yourself.</p>"
        "</main>"
        f'<p class="meta">generated at {esc(stamp)}</p>'
        f'<p class="meta snapshot-note">{esc(_SNAPSHOT_NOTE)}</p>'
    )
    return _document("Verify a coin -- Charlie Protocol", body, style=_INDEX_STYLE)


# Vercel serves this for any path no rewrite and no file claims. That is
# where a pasted CA lands whenever the coin has not been measured, which is
# the overwhelmingly common case under the submit-driven model -- so this is
# not an error page in practice, it is the second most visited page on the
# site and has to do a job.
NOT_FOUND_FILENAME = "404.html"


def render_not_found(*, now=None) -> str:
    """The page a pasted CA reaches when that coin has no page yet.

    Vercel's own 404 was serving this route: "The page could not be found",
    an id, nothing else. A visitor who did exactly what the site asked got
    something that reads like the site is broken, when the true answer is
    "nobody has submitted this coin yet" -- which is a state the model
    intends, not a fault.

    Cannot name the CA. It is a static file and the site runs no JavaScript
    to do its job, so the mint stays in the address bar and out of the copy.
    The page compensates by making the next step unmissable rather than by
    guessing which coin was asked for.
    """
    stamp = _stamp(now() if callable(now) else (now if now is not None else time.time()))
    href = esc(submit_issue_url())
    coins = esc(INDEX_FILENAME_TEMPLATE.format(page=1))
    body = (
        "<header>"
        "<h1>Nothing at this address</h1>"
        "<p>No page here. If you were trying to check a coin, the box below "
        "answers for any contract address.</p>"
        "</header>"
        "<main>"
        '<form class="verify-form" method="get" action="/verify">'
        '<label for="mint">Contract address (CA)</label>'
        '<input id="mint" name="mint" type="text" inputmode="latin" '
        'autocomplete="off" spellcheck="false" '
        'placeholder="paste the CA here" '
        'pattern="[1-9A-HJ-NP-Za-km-z]{32,44}" required>'
        '<button type="submit">Verify</button>'
        "</form>"
        f'<p><a href="{coins}">Every coin measured so far</a>.</p>'
        f'<p><a href="/{esc(VERIFY_FILENAME)}">Back to Verify</a>.</p>'
        "</main>"
        f'<p class="meta">generated at {esc(stamp)}</p>'
    )
    return _document("No page for that coin yet -- Charlie Protocol", body, style=_INDEX_STYLE)


def render_unavailable(mint: str, *, now=None) -> str:
    """The live route could not read the chain.

    An RPC that will not answer is a fact about the node, never a verdict
    about the coin. Rendering a partial or empty observation here would put
    a coin's name beside blanks that look like findings, which is the exact
    failure the silence rule exists to prevent -- so this page carries no
    figures at all and says which of the two happened.
    """
    stamp = _stamp(now() if callable(now) else (now if now is not None else time.time()))
    body = (
        "<header>"
        "<h1>Could not read the chain just now</h1>"
        f"<p><code>{esc(mint)}</code></p>"
        "</header>"
        "<main>"
        "<p>This is a fact about the RPC node, not about the coin. Nothing "
        "here failed a check and nothing here passed one, so this page shows "
        "no figures rather than blanks that could be mistaken for findings.</p>"
        "<p>Try again in a moment.</p>"
        '<form class="verify-form" method="get" action="/verify">'
        '<label for="mint">Contract address (CA)</label>'
        '<input id="mint" name="mint" type="text" inputmode="latin" '
        'autocomplete="off" spellcheck="false" value="' + esc(mint) + '" '
        'pattern="[1-9A-HJ-NP-Za-km-z]{32,44}" required>'
        '<button type="submit">Try again</button>'
        "</form>"
        "</main>"
        f'<p class="meta">generated at {esc(stamp)}</p>'
    )
    return _document("Could not read the chain -- Charlie Protocol", body, style=_INDEX_STYLE)


def write_not_found(out_dir=DEFAULT_OUTPUT_DIR, *, now=None):
    path = Path(out_dir) / NOT_FOUND_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_not_found(now=now), encoding="utf-8")
    return path


def write_verify(out_dir=DEFAULT_OUTPUT_DIR, *, now=None, example_mint=None):
    path = Path(out_dir) / VERIFY_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_verify(now=now, example_mint=example_mint), encoding="utf-8")
    return path


INDEX_FILENAME_TEMPLATE = "coins-{page}.html"

DEFAULT_INDEX_PAGE_SIZE = 500

_INDEX_STYLE = _TOKENS + _VERIFY_FORM_CSS + """
.scanner {
  display: inline-flex; background: #000; padding: var(--sp-sm);
  margin: var(--sp-md) 0 0 0; line-height: 0;
  border: 1px solid var(--unchecked);
}
.scanner .scanning {
  image-rendering: pixelated; display: block;
  width: clamp(132px, 20vw, 188px); height: auto;
}
.verify-form { display: flex; flex-wrap: wrap; gap: var(--sp-sm);
  align-items: center; margin: var(--sp-lg) 0; }
.verify-form label { flex: 1 1 100%; font-size: 14px; }
.verify-form input {
  flex: 1 1 22em; min-width: 0; padding: var(--sp-sm);
  font-family: inherit; font-size: 15px;
  border: 1px solid var(--unchecked); background: #fff; color: var(--ink);
}
.verify-form button {
  padding: var(--sp-sm) var(--sp-lg); font-family: inherit; font-size: 15px;
  border: 1px solid var(--ink); background: var(--ink); color: var(--paper);
  cursor: pointer;
}
.verify-form button:hover, .verify-form button:focus-visible { background: var(--accent); border-color: var(--accent); }

* { box-sizing: border-box; }
body {
  background: var(--paper);
  color: var(--ink);
  font-family: ui-monospace, "SFMono-Regular", Menlo, Consolas, "Liberation Mono", monospace;
  font-size: 16px;
  line-height: 1.5;
  margin: 0;
  padding: var(--sp-xl) var(--sp-lg);
  overflow-wrap: anywhere;
}
h1 { font-size: 28px; font-weight: 700; margin: 0 0 var(--sp-md) 0; }
.meta { font-size: 14px; font-weight: 400; margin: 0 0 var(--sp-xs) 0; }
a { color: var(--accent); }
.index-row {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--sp-xs) var(--sp-md);
  background: var(--panel);
  padding: var(--sp-md);
  margin-bottom: var(--sp-md);
  border-left: 1px dashed var(--unchecked);
}
.index-mint {
  font-size: 14px;
  overflow-wrap: anywhere;
  word-break: break-all;
  flex: 1 1 260px;
  min-width: 0;
}
.index-split, .index-withheld { font-size: 14px; flex: 2 1 260px; min-width: 0; }
.index-backs { font-size: 13px; color: var(--unchecked); flex-basis: 100%; }
.index-links { font-size: 14px; display: flex; gap: var(--sp-sm); flex: 0 0 auto; }
.index-nav { margin-top: var(--sp-lg); display: flex; gap: var(--sp-md); }
"""


def render_index(records, *, counts, page, pages, now=None, pages_present=None, known_pages=None) -> str:
    """One page of the coin index -- D-27's funnel, not a leaderboard: rows
    are ordered by mint and the page says the order ranks nothing. Contains
    no figure formatting of its own -- `index_rows` owns every
    figure-bearing cell, which is what keeps the sweep over `index_rows`
    sufficient.
    """
    now = now if now is not None else time.time()
    stamp = _stamp(now)
    known_pages = known_pages or frozenset()
    ordered = sorted(records, key=lambda r: r.get("mint") or "")
    rows_html = "".join(index_rows(ordered, known_pages))
    pages_present = pages_present if pages_present is not None else {page}

    nav_links = []
    if (page - 1) in pages_present:
        nav_links.append(
            f'<a href="{esc(INDEX_FILENAME_TEMPLATE.format(page=page - 1))}">previous</a>'
        )
    if (page + 1) in pages_present:
        nav_links.append(
            f'<a href="{esc(INDEX_FILENAME_TEMPLATE.format(page=page + 1))}">next</a>'
        )
    nav_html = f'<nav class="index-nav">{"".join(nav_links)}</nav>' if nav_links else ""

    body = (
        "<header>"
        "<h1>Coins</h1>"
        f'<p class="meta">{esc(coverage_statement(counts))}</p>'
        f'<p class="meta">generated at {esc(stamp)}</p>'
        f'<p class="meta snapshot-note">{esc(_SNAPSHOT_NOTE)}</p>'
        f'<p class="meta">page {page} of {pages} -- this order ranks nothing; rows are '
        "sorted by mint.</p>"
        "</header>"
        f"<main>{rows_html}</main>"
        f"{nav_html}"
    )
    return _document("Coins -- Charlie Protocol", body, style=_INDEX_STYLE)


def write_index(
    records,
    out_dir=DEFAULT_OUTPUT_DIR,
    *,
    counts,
    now=None,
    known_pages=None,
    page_size=DEFAULT_INDEX_PAGE_SIZE,
) -> list[Path]:
    """Writes one file per page of `records` (ordered by mint) under
    `out_dir`, and returns the written paths in page order. Page one is
    always `coins-1.html` -- there is no unnumbered file; `/coins` reaches
    it by rewrite.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ordered = sorted(records, key=lambda r: r.get("mint") or "")
    chunks = [ordered[i : i + page_size] for i in range(0, len(ordered), page_size)] or [[]]
    pages = len(chunks)
    pages_present = set(range(1, pages + 1))
    known_pages = known_pages or frozenset()

    written = []
    for index, chunk in enumerate(chunks, start=1):
        html = render_index(
            chunk,
            counts=counts,
            page=index,
            pages=pages,
            now=now,
            pages_present=pages_present,
            known_pages=known_pages,
        )
        path = out_dir / INDEX_FILENAME_TEMPLATE.format(page=index)
        path.write_text(html, encoding="utf-8", newline="\n")
        written.append(path)
    return written


# -- 02-03 QT-01/QT-02/QT-03: the landing page --------------------------
# `charlieprotocol.fun`'s `/` -- live counters computed at render time from
# one Observation, the BURN_SUPPLY refusal (D-C), and two links into the
# coin page beside it (D-A). Never a second, parallel silence-rule
# implementation: `_counters()` reads exactly four Observation fields, never
# `evidence["burn_total"]` (that key is
# `publish.FIGURE_SOURCES[invariants.SUPPLY_DESTROYED]`'s own path), and
# `render_landing()` never constructs a `publish.Publisher` at all.
def _counters(observation) -> tuple:
    """The six landing-page counters (QT-01), each `{label, value, source,
    raw}`, in the fixed order the plan's observed-values table gives:
    supply remaining, initial supply, recorded burn transactions, tokens in
    those burns, SOL spent buying them, burned by hand (not by boost).

    Reads exactly four Observation fields -- `mint_state.supply`,
    `mint_state.decimals`, `evidence["initial_supply"]`, `burn_events` --
    and never the evidence store's own gated running total (the key
    `publish.FIGURE_SOURCES[invariants.SUPPLY_DESTROYED]` reads), because
    reading that key here would be the PUB-01 bypass class on this new
    surface. A cell whose source data is absent renders `value` "unknown"
    and `raw` `None` -- never `0`, never blank.
    """
    mint_state = observation.mint_state
    evidence = observation.evidence or {}
    initial_supply_row = evidence.get("initial_supply")
    burn_events = observation.burn_events or []

    if mint_state is not None:
        decimals = mint_state.decimals
    elif initial_supply_row is not None and initial_supply_row.get("decimals") is not None:
        decimals = initial_supply_row["decimals"]
    else:
        decimals = 6

    def tokens_ui(raw) -> str:
        return f"{raw / (10 ** decimals):,.{decimals}f}"

    def sol_ui(raw) -> str:
        # Matches report._lamports's own convention -- .9f, no thousands
        # separator (a SOL amount this small never needs one).
        return f"{raw / LAMPORTS_PER_SOL:.9f}"

    cells = []

    if mint_state is not None:
        cells.append({
            "label": "Supply remaining",
            "value": tokens_ui(mint_state.supply),
            "source": "mint_state.supply",
            "raw": mint_state.supply,
        })
    else:
        cells.append({"label": "Supply remaining", "value": "unknown", "source": "mint_state.supply", "raw": None})

    initial_raw = initial_supply_row.get("raw_supply") if initial_supply_row is not None else None
    initial_supply_source = "evidence.initial_supply.raw_supply"
    if initial_raw is not None:
        cells.append({
            "label": "Initial supply",
            "value": tokens_ui(initial_raw),
            "source": initial_supply_source,
            "raw": initial_raw,
        })
    else:
        cells.append({"label": "Initial supply", "value": "unknown", "source": initial_supply_source, "raw": None})

    if burn_events:
        cells.append({
            "label": "Recorded burn transactions",
            "value": str(len(burn_events)),
            "source": "len(burn_events)",
            "raw": len(burn_events),
        })
    else:
        cells.append({"label": "Recorded burn transactions", "value": "unknown", "source": "len(burn_events)", "raw": None})

    tokens_source = "sum(tokens_burned across burn_events)"
    if burn_events:
        tokens_raw = sum(int(row.get("tokens_burned", 0)) for row in burn_events)
        cells.append({
            "label": "Tokens in those burns",
            "value": tokens_ui(tokens_raw),
            "source": tokens_source,
            "raw": tokens_raw,
        })
    else:
        cells.append({"label": "Tokens in those burns", "value": "unknown", "source": tokens_source, "raw": None})

    lamports_source = "sum(sol_spent across burn_events)"
    if burn_events:
        lamports_raw = sum(int(row.get("sol_spent") or 0) for row in burn_events)
        cells.append({
            "label": "SOL spent buying them",
            "value": sol_ui(lamports_raw),
            "source": lamports_source,
            "raw": lamports_raw,
        })
    else:
        cells.append({"label": "SOL spent buying them", "value": "unknown", "source": lamports_source, "raw": None})

    non_boost_source = f"burn_events where source != {BOOST_SOURCE}"
    if burn_events:
        non_boost_rows = [row for row in burn_events if row.get("source") != BOOST_SOURCE]
        non_boost_tokens = sum(int(row.get("tokens_burned", 0)) for row in non_boost_rows)
        count = len(non_boost_rows)
        word = "transaction" if count == 1 else "transactions"
        cells.append({
            "label": "Burned by hand, not by boost",
            "value": f"{count} {word} -- {tokens_ui(non_boost_tokens)} tokens",
            "source": non_boost_source,
            "raw": non_boost_tokens,
        })
    else:
        cells.append({"label": "Burned by hand, not by boost", "value": "unknown", "source": non_boost_source, "raw": None})

    return tuple(cells)


def _hand_burn_note(observation) -> str:
    """Why a stranger's burn is counted at all.

    Computed, not a constant, for the same reason `_non_boost_sentence()` is:
    the sentence changes shape the moment the walk records another one, and a
    claim about "the" hand burn would go stale silently. Says nothing about
    the residual -- the arithmetic gap is unattributed, and guessing that
    holders caused it is precisely the unbacked claim this page exists to
    refuse.
    """
    rows = [r for r in (observation.burn_events or [])
            if r.get("source") != BOOST_SOURCE]
    if not rows:
        return ""
    word = "one of them" if len(rows) == 1 else f"{len(rows)} of them"
    return (
        '<section id="why-counted">'
        f"<p>The last counter is people destroying their own tokens. No mechanism "
        f"ran, nothing asked them to, and no reward was paid for it -- {word} so far. "
        "They are counted anyway, on purpose. The figure they feed asks how much of "
        "this token is gone, not how much of it this protocol destroyed; a protocol "
        "that counted only its own burns would report the ones it caused and quietly "
        "drop everyone else's. So the walk records every burn against the mint, by "
        "anyone.</p>"
        "<p>It does not follow that the shortfall below is more of the same. That gap "
        "is unattributed. Naming a cause for it here is exactly the claim this page "
        "will not make.</p>"
        "</section>"
    )


def _counter_cell(cell: dict) -> str:
    """One counter -- value at display size, label beneath, and the
    Observation field it came from beneath that (the `.meta` class the coin
    page already uses for `observed at`) -- so the source line is never a
    footnote.
    """
    return (
        '<div class="counter-cell">'
        f'<p class="counter-value">{esc(cell["value"])}</p>'
        f'<p class="counter-label">{esc(cell["label"])}</p>'
        f'<p class="counter-source meta">{esc(cell["source"])}</p>'
        "</div>"
    )


_SUPPLY_REFUSAL_INTRO = (
    "Both supply endpoints render above. This page declines to subtract them -- "
    "the difference would be a checked figure, and the check that backs it is "
    "shown below, exactly as it reads right now."
)


def _supply_refusal(observation) -> str:
    """D-C/QT-03: states the refusal and renders BURN_SUPPLY's own live
    fields via `_check_row()` -- never a hardcoded restatement of `detail`.
    The outer wrapper carries the withheld (UNCHECKED, dashed-ochre)
    treatment: a declined number is withheld, not failed, which is a
    property of THIS page's choice not to publish the subtraction, distinct
    from the check's own true status (rendered inline, unmodified, by the
    reused `_check_row()`). If no BURN_SUPPLY check exists in the
    observation, the refusal still renders -- prose without the check block,
    never omitted entirely.
    """
    check = next((c for c in observation.checks if c.name == "BURN_SUPPLY"), None)
    pieces = [
        '<section class="supply-refusal status-unchecked" data-refusal="supply">',
        f"<p>{esc(_SUPPLY_REFUSAL_INTRO)}</p>",
    ]
    if check is not None:
        pieces.append(_check_row(check))
    pieces.append("</section>")
    return "".join(pieces)


# Two sentences, adapted from PROJECT.md's own Project section rather than
# invented marketing copy. The second states the claims rule in the summary
# and not only in the full spec: a burn claim requires a destination that
# passes `SOL_BURN_UNSPENDABLE`, and $CHARLIE's does not. Keeping that
# admission here is what shows the standard is not graded by its author.
_LANDING_DESCRIPTION = (
    "Charlie Protocol is a fee-routing and verification standard for pump.fun "
    "coins, naming three destinations for creator fees -- BURN (SOL), BURN (token), OPS -- "
    "and specifying the part nobody else does: what a coin is permitted to "
    "claim about them in public. Both burn: a SOL burn is deflation, SOL sent where no key can "
    "spend it, a BURN destroys token supply. The word is only permitted where "
    "the destination is provably unspendable -- and $CHARLIE's is not."
)


def _landing_description() -> str:
    return f'<section id="what-this-is"><p>{esc(_LANDING_DESCRIPTION)}</p></section>'


_LANDING_SOON_HEADING = "Launch with Charlie Protocol"

# Deliberately avoids the word for how fees divide: it is a name in
# `invariants.FIGURES`, and the no-figure-names test covers the whole
# rendered document, prose included.
_LANDING_SOON = (
    "When it ships, a coin will name its three fee destinations when it is "
    "created -- SOL_BURN, BURN and OPS -- and its SOL burn vault will be derived by "
    "the program rather than chosen by whoever deploys it. The coin then gets "
    "a page like this one, on which no figure renders unless a passing check "
    "backs it.",
    "None of that is built yet. What produced every number above exists: the "
    "checks, the verifier, and the committed record they read from. The "
    "on-chain program that does the enrolling does not, so no coin can enroll "
    "today. That includes $CHARLIE, which revoked its own admin and can no "
    "longer be reconfigured by anyone but pump.",
    "It will land in the repository linked below. There is nothing to sign up "
    "for and nothing to buy in order to be ready for it.",
)


def _landing_soon() -> str:
    """The enrollment path, and the fact that it does not exist yet.

    Carries the same `status-unchecked` badge the checks use, because that is
    exactly what this is: nothing here has been verified, because there is
    nothing here to verify. A "coming soon" that implied availability would
    contradict the only claim the rest of the page makes.
    """
    paras = "".join(f"<p>{esc(t)}</p>" for t in _LANDING_SOON)
    return (
        '<section id="launch-soon">'
        '<div class="soon-head">'
        f"<h2>{esc(_LANDING_SOON_HEADING)}</h2>"
        '<span class="badge status-unchecked">Coming soon</span>'
        "</div>"
        + paras
        + "</section>"
    )


def _landing_links(observation) -> str:
    """QT-02: the two ways in, both hrefs composed by `_coin_url` so they
    can never disagree with `vercel.json`'s rewrites or `write()`'s own
    filenames. The page link carries no suffix at all -- `vercel.json`'s
    page rewrite is deliberately suffix-free (only the record rewrite
    carries a literal `.json`), so `/coin/<mint>` with nothing after it is
    the page's actual clean URL; the record keeps its `.json`.
    """
    page_href = esc(_coin_url(observation.mint, ""))
    json_href = esc(_coin_url(observation.mint, ".json"))
    return (
        '<section id="two-ways-in">'
        f'<p><a href="{page_href}">View $CHARLIE, checked'
        '<span class="visually-hidden"> (opens the checked coin page)</span></a></p>'
        f'<p><a href="{json_href}">View its raw observation record'
        '<span class="visually-hidden"> (opens the raw JSON record)</span></a></p>'
        "</section>"
    )


def _landing_footer(observation) -> str:
    """Links the repo (via the existing `REPO_URL`) and the coin page's
    record as what the counters above are recomputable from, plus D-20's
    generator-unverified admission verbatim -- deliberately NOT `_footer()`,
    whose closing sentence promises a record published beside *this* page,
    which is true of the coin page and false of the landing page.
    """
    json_href = esc(_coin_url(observation.mint, ".json"))
    return (
        "<footer>"
        f'<p>Spec, code and committed evidence: <a href="{esc(REPO_URL)}">{esc(REPO_URL)}</a></p>'
        f'<p>Every counter above is recomputable from the record published at '
        f'<a href="{json_href}">{json_href}</a>.</p>'
        f"<p>{esc(_GENERATOR_UNVERIFIED)}</p>"
        "</footer>"
    )


_LANDING_TAGLINE = "Every number on this page names the check that could falsify it."


def render_landing(observation, *, now=None) -> str:
    """A complete HTML5 document: the six live counters (QT-01), the
    BURN_SUPPLY refusal (QT-03/D-C), what this is, and two links into the
    coin page beside it (QT-02/D-A). Single required positional parameter --
    `publish.render_surface()` calls `target(subject)` with exactly one
    positional argument for an `"observation"`-input `SURFACES` entry; `now`
    is keyword-only with the same injectable-clock default `render()` uses.
    """
    now = now if now is not None else time.time()
    freshness = _freshness(observation, now)
    header = (
        '<header class="hero">'
        '<div class="hero-inner">'
        '<div class="hero-art">'
        '<div class="hero-copy">'
        '<p class="eyebrow rise"><span class="flame">'
        + _flame_svg()
        + '</span>Fee routing, and what may be claimed about it</p>'
        '<h1 class="rise d1">Charlie Protocol</h1>'
        f'<p class="tagline rise d2">{esc(_LANDING_TAGLINE)}</p>'
        "</div>"
        "</div>"
        + _scene()
        # The landing page had no route to /verify at all. Traffic arrives here
        # from a post about pasting a CA, and the only thing to do on arrival
        # was read about $CHARLIE. The box goes above the counters because it
        # is why most people came.
        + '<div class="hero-verify rise d3">'
        '<form class="verify-form" method="get" action="/verify">'
        '<label for="mint">Check a coin. Paste its contract address (CA)</label>'
        '<input id="mint" name="mint" type="text" inputmode="latin" '
        'autocomplete="off" spellcheck="false" '
        'placeholder="paste the CA here" '
        'pattern="[1-9A-HJ-NP-Za-km-z]{32,44}" required>'
        '<button type="submit">Verify</button>'
        "</form>"
        '<p class="meta">Any pump.fun coin. No wallet, no signup. The chain is '
        "read while you wait.</p>"
        '<p class="meta">Own a coin? <a href="/enroll">Set where its creator '
        "fee goes</a>.</p>"
        "</div>"
        + '<div class="hero-rule"></div>'
        f'<div class="rise d3">{freshness}</div>'
        "</div>"
        "</header>"
    )
    # Each cell carries its own stagger class so the grid resolves in reading
    # order rather than arriving all at once. Capped at the six `.d1`-`.d6`
    # steps the stylesheet declares: a seventh counter arrives un-delayed
    # rather than referencing a class that does not exist.
    counters_section = (
        '<section id="counters" aria-label="live counters">'
        '<div class="section-art">'
        + _charlie_gif(label="Charlie, throwing out the drafts that did not check out")
        + _chart_svg()
        + '<p class="section-label">Observed, and where each one comes from</p>'
        + "</div>"
        + "".join(
            _counter_cell(cell).replace(
                '<div class="counter-cell"',
                f'<div class="counter-cell rise{f" d{i}" if i <= 6 else ""}"',
                1,
            )
            for i, cell in enumerate(_counters(observation), start=1)
        )
        + "</section>"
    )
    body = (
        header
        + "<main>"
        # The loop goes above the counters. A visitor arriving from a link has
        # to know what the mechanism IS before a column of figures means
        # anything to them; the counters are the loop's output, and output
        # shown before the thing that produced it reads as trivia.
        + _flywheel()
        + counters_section
        + _hand_burn_note(observation)
        + _supply_refusal(observation)
        + _landing_description()
        + _landing_soon()
        + _landing_links(observation)
        + _landing_footer(observation)
        + "</main>"
    )
    return _document("Charlie Protocol", body, style=_LANDING_STYLE)


def write_landing(observation, out_dir=DEFAULT_OUTPUT_DIR) -> Path:
    """Writes `render_landing(observation)` to `<out_dir>/index.html` --
    never through the coin-page filename constructor D-19 defines:
    `index.html` is not mint-derived, and routing it through that helper
    would be a second naming scheme entering by the back door. Does not
    modify `write()`.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / LANDING_FILENAME
    path.write_text(render_landing(observation), encoding="utf-8", newline="\n")
    return path

"""`/dilution` -- what issuance costs a reader's own bag, and what a burn does about it.

The one page on this site that argues rather than reports. Everything else
publishes a measured figure with a check beside it; this page takes two
measured figures and does arithmetic a reader can follow, to answer a question
they actually have: what does Solana's issuance cost ME?

Its own module for the same reason `enroll_page` is: `site.py` imports only
`invariants` and `publish` by design and a test holds that, and this page
carries script, which every other generated surface deliberately does not.

WHY THE SCRIPT IS HERE AT ALL. The rest of the site is script-free on purpose
and that property is worth keeping. This page breaks it because the argument
IS the interaction: a reader types their balance and gets their own number,
then drags a lever and watches that number improve while a second bar refuses
to close the gap. Rendering the same content as static prose was tried and it
reads as a claim to be taken on trust rather than a result they derived. The
script is written out rather than minified, ships no third-party code, and
does nothing but arithmetic on constants declared in this file.

THE RULE THIS PAGE EXISTS UNDER. No price anywhere in it: not a dollar value,
not a delta, not a projection. A dollar figure attached to a supply argument
reads as a price call to a trader whatever the caption says, and the page
carries its own counterexample instead -- SOL ran from about $83 to about $256
across 2024 while supply grew the whole way. What is drawn is a share of
supply, which is division, and the arithmetic is stated in the page so a
reader can redo it.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import site

DILUTION_FILENAME = "dilution.html"

# ---------------------------------------------------------------------------
# The model. Two measured inputs, everything else derived in the browser.
#
#   SUPPLY   circulating SOL, implied by market cap / price on 2026-09-10
#   ISSUE    3.66%/yr, solanacompass.com, 2026-09-09, the lower of two sources
#   FEES     $342.2M app revenue for Q1 2026, annualised by four
#   PRICE    used ONLY to turn a dollar fee pool into a SOL buy-back ceiling.
#            It never reaches the reader's own figures, which are all shares.
#
# The ceiling that makes the page honest: those fees buy about 13.69M SOL a
# year against 21.46M minted, so the burn bar stops near 64% and cannot be
# dragged past it. Break-even would need about 157% of all app revenue.
# ---------------------------------------------------------------------------
SUPPLY = 586_300_000
ISSUE_PCT = 0.0366
FEES_YEAR_USD = 342_200_000 * 4
SOL_PRICE_USD = 100

ISSUED_PER_YEAR = SUPPLY * ISSUE_PCT          # 21.46M SOL
BURN_CEILING = FEES_YEAR_USD / SOL_PRICE_USD  # 13.69M SOL
MINTED_PER_DAY = ISSUED_PER_YEAR / 365        # 58,791 SOL

SOURCE_LINE = (
    "issuance: solanacompass.com, 2026-09-09 · app revenue: Q1 2026 Solana "
    "app fee reporting · price and supply: 2026-09-10"
)


_STYLE = """
/* The site is a light paper theme (`_TOKENS`): --paper ground, --ink type,
   --panel for raised blocks, --accent blue, --destructive red, --unchecked
   amber, --ember the brand green. This page uses those and adds none of its
   own, so it cannot drift from the rest of the site. */
.dil-lede{font-size:1.02rem;line-height:1.65;max-width:var(--measure)}
.dil-card{border:1px solid var(--pass-glyph);background:var(--panel);
  padding:var(--sp-md) var(--sp-md) var(--sp-lg);margin:var(--sp-lg) 0}
.dil-k{font-size:13px;letter-spacing:.12em;text-transform:uppercase;
  color:var(--pass-glyph);margin:0 0 var(--sp-sm)}
.dil-row{display:flex;align-items:center;gap:var(--sp-sm);flex-wrap:wrap}
.dil-row input[type=text]{font-family:inherit;font-size:30px;font-weight:700;
  background:#fff;color:var(--ink);border:1px solid var(--unchecked);
  padding:var(--sp-sm) var(--sp-md);width:10em;
  font-variant-numeric:tabular-nums;min-height:44px}
.dil-row input[type=text]:focus{outline:2px solid var(--accent);outline-offset:2px}
.dil-unit{font-size:15px;color:var(--pass-glyph)}
.dil-chips{display:flex;gap:var(--sp-sm);flex-wrap:wrap;margin-top:var(--sp-md)}
.dil-chips button{font-family:inherit;font-size:14px;background:var(--paper);
  color:var(--ink);border:1px solid var(--pass-glyph);
  padding:var(--sp-sm) var(--sp-md);cursor:pointer;min-height:44px}
.dil-chips button:hover{border-color:var(--ink)}
.dil-chips button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.dil-big{font-size:clamp(44px,11vw,72px);font-weight:700;
  color:var(--destructive);line-height:1;margin:0;
  font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.dil-bigu{font-size:17px;margin:var(--sp-md) 0 0;line-height:1.5;max-width:34ch}
.dil-note{font-size:14px;color:var(--pass-glyph);margin:var(--sp-sm) 0 0;
  line-height:1.55;max-width:var(--measure)}
.dil-strip{margin:var(--sp-lg) 0 0;border-top:1px solid var(--pass-glyph);
  padding-top:var(--sp-md)}
.dil-bar{display:grid;grid-template-columns:4.2rem 1fr 4rem;align-items:center;
  gap:var(--sp-sm);margin-bottom:var(--sp-sm)}
.dil-bar .yr{font-size:14px;color:var(--pass-glyph)}
.dil-trk{height:22px;background:#fff;border:1px solid var(--pass-glyph);
  position:relative;overflow:hidden}
.dil-fill{position:absolute;left:0;top:0;bottom:0;background:var(--accent);
  transition:width .45s cubic-bezier(.22,1,.36,1)}
.dil-gone{position:absolute;top:0;bottom:0;right:0;
  background:repeating-linear-gradient(135deg,rgba(163,39,31,.42) 0 6px,rgba(163,39,31,.12) 6px 12px);
  transition:width .45s cubic-bezier(.22,1,.36,1)}
.dil-bar .v{font-size:14px;font-weight:700;text-align:right;
  font-variant-numeric:tabular-nums;color:var(--destructive)}
.dil-bar.now .v{color:var(--accent)}
.dil-lever-top{display:flex;justify-content:space-between;align-items:baseline;
  gap:var(--sp-md);flex-wrap:wrap;margin-bottom:var(--sp-xs)}
.dil-lever-top label{font-weight:700;font-size:17px}
.dil-pc{font-size:28px;font-weight:700;color:var(--ink);
  font-variant-numeric:tabular-nums;line-height:1}
input[type=range].dil-range{-webkit-appearance:none;appearance:none;width:100%;
  height:44px;background:transparent;cursor:pointer;margin:0;display:block}
input[type=range].dil-range::-webkit-slider-runnable-track{height:8px;
  background:linear-gradient(90deg,var(--ink) 0%,var(--ink) var(--f,0%),#D8D2C6 var(--f,0%),#D8D2C6 100%)}
input[type=range].dil-range::-moz-range-track{height:8px;background:#D8D2C6}
input[type=range].dil-range::-moz-range-progress{height:8px;background:var(--ink)}
input[type=range].dil-range::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;
  width:26px;height:26px;margin-top:-9px;border-radius:50%;background:var(--ink);
  border:3px solid var(--panel)}
input[type=range].dil-range::-moz-range-thumb{width:26px;height:26px;border-radius:50%;
  background:var(--ink);border:3px solid var(--panel)}
input[type=range].dil-range:focus-visible{outline:2px solid var(--accent);outline-offset:4px}
.dil-ceil{margin-top:var(--sp-md);border-top:1px solid var(--pass-glyph);
  padding-top:var(--sp-md)}
.dil-ceil-trk{height:28px;background:#fff;border:1px solid var(--pass-glyph);
  position:relative;overflow:hidden}
.dil-ceil-fill{position:absolute;left:0;top:0;bottom:0;background:var(--ink);
  transition:width .35s cubic-bezier(.22,1,.36,1)}
.dil-ceil-hole{position:absolute;top:0;bottom:0;right:0;
  background:repeating-linear-gradient(135deg,rgba(122,90,18,.30) 0 7px,rgba(122,90,18,.08) 7px 14px);
  transition:width .35s cubic-bezier(.22,1,.36,1)}
.dil-ceil-ends{display:flex;justify-content:space-between;margin-top:var(--sp-sm);
  font-size:14px;color:var(--pass-glyph);gap:var(--sp-md)}
.dil-ceil-ends b{color:var(--ink)}
.dil-ceil-note{font-size:16px;margin:var(--sp-md) 0 0;line-height:1.55;
  max-width:var(--measure)}
@media (prefers-reduced-motion: reduce){
  .dil-fill,.dil-gone,.dil-ceil-fill,.dil-ceil-hole{transition:none}
}
"""


_SCRIPT = r"""
(function(){
  "use strict";
  var SUPPLY=__SUPPLY__, ISSUE=__ISSUE__, CEIL=__CEIL__;
  function $(i){return document.getElementById(i);}
  var bag=$("dilBag"), share=$("dilShare");

  function num(s){
    var n=parseFloat(String(s).replace(/[^0-9.]/g,""));
    return isFinite(n)&&n>0?n:0;
  }
  function fmt(n){
    if(n>=1000) return n.toLocaleString("en-US",{maximumFractionDigits:0});
    if(n>=100)  return n.toFixed(0);
    return n.toFixed(1);
  }
  function m(n){return (n/1e6).toFixed(1)+"M";}

  function render(){
    var held=num(bag.value), pct=Number(share.value);
    var burned=CEIL*pct/100, net=ISSUE-burned;
    var g5=Math.pow(1+net/SUPPLY,5), g10=Math.pow(1+net/SUPPLY,10);

    // What you would have to buy to hold the same FRACTION of supply in ten
    // years. Division on a supply ratio: no price, no forecast.
    $("dilNeed").textContent=fmt(held*(g10-1));
    $("dilNeedNote").textContent="Nobody takes your coins. You keep all "+
      (held?fmt(held):"0")+". The network grows around them.";

    var s5=100/g5, s10=100/g10;
    $("dilF5").style.width=s5.toFixed(1)+"%";
    $("dilG5").style.width=(100-s5).toFixed(1)+"%";
    $("dilV5").textContent=s5.toFixed(1)+"%";
    $("dilF10").style.width=s10.toFixed(1)+"%";
    $("dilG10").style.width=(100-s10).toFixed(1)+"%";
    $("dilV10").textContent=s10.toFixed(1)+"%";

    var frac=burned/ISSUE*100;
    $("dilPc").textContent=pct+"%";
    share.style.setProperty("--f",pct+"%");
    $("dilCf").style.width=frac.toFixed(1)+"%";
    $("dilCh").style.width=(100-frac).toFixed(1)+"%";
    $("dilCeilL").textContent=(burned?m(burned):"0")+" SOL burned";
    var note=$("dilCeilNote");
    if(pct>=100){
      note.innerHTML="That is <b>every fee Solana earns</b>, and it still stops "+
        "at "+frac.toFixed(0)+"%. Break-even needs about 157%.";
    } else if(pct===0){
      note.innerHTML="Drag it all the way. The bar <b>still cannot reach the end</b>.";
    } else {
      note.innerHTML="Still <b>"+(100-frac).toFixed(0)+"% short</b> of what gets minted.";
    }
  }

  bag.addEventListener("input",render);
  bag.addEventListener("focus",function(){this.select();});
  share.addEventListener("input",render);
  var chips=document.querySelectorAll(".dil-chips button");
  for(var i=0;i<chips.length;i++){
    chips[i].addEventListener("click",function(){
      bag.value=this.getAttribute("data-b");render();
    });
  }
  render();
})();
"""


def render(*, now=None) -> str:
    """The page. No observation needed: every figure is a constant here or
    derived from one in the browser, so this surface never withholds and never
    goes stale against a scan."""
    esc = site.esc
    stamp = site._stamp(
        now() if callable(now) else (now if now is not None else time.time())
    )

    # Plain replacement rather than %-formatting: the script is full of
    # literal percent signs (widths, the readouts) and every one of them
    # would have to be doubled.
    script = (
        _SCRIPT.replace("__SUPPLY__", repr(float(SUPPLY)))
        .replace("__ISSUE__", repr(float(ISSUED_PER_YEAR)))
        .replace("__CEIL__", repr(float(BURN_CEILING)))
    )

    body = (
        "<header>"
        "<h1>Standing still costs you SOL.</h1>"
        '<p class="dil-lede">'
        f"Solana mints about {MINTED_PER_DAY:,.0f} new SOL a day and hands it "
        "out. Your balance never moves, so your share of the network quietly "
        "shrinks. Here is what that costs your bag."
        "</p>"
        "</header>"
        "<main>"
        # -- the input ---------------------------------------------------
        '<section class="dil-card">'
        '<p class="dil-k"><label for="dilBag">How much SOL do you hold?</label></p>'
        '<div class="dil-row">'
        '<input id="dilBag" type="text" inputmode="decimal" value="100" '
        'autocomplete="off" spellcheck="false">'
        '<span class="dil-unit">SOL</span>'
        "</div>"
        '<div class="dil-chips">'
        '<button type="button" data-b="10">10</button>'
        '<button type="button" data-b="100">100</button>'
        '<button type="button" data-b="500">500</button>'
        '<button type="button" data-b="1000">1,000</button>'
        '<button type="button" data-b="5000">5,000</button>'
        "</div>"
        "</section>"
        # -- the answer --------------------------------------------------
        '<section class="dil-card">'
        '<p class="dil-k">To own the same share of Solana in 10 years</p>'
        '<p class="dil-big" id="dilNeed">43.3</p>'
        '<p class="dil-bigu">more SOL is what you would have to '
        "<b>buy just to stand still</b>.</p>"
        '<p class="dil-note" id="dilNeedNote">Nobody takes your coins. You keep '
        "all 100. The network grows around them.</p>"
        '<div class="dil-strip">'
        '<p class="dil-k">Your share of all SOL, against today</p>'
        '<div class="dil-bar now"><span class="yr">today</span>'
        '<span class="dil-trk"><i class="dil-fill" style="width:100%"></i></span>'
        '<span class="v">100%</span></div>'
        '<div class="dil-bar"><span class="yr">5 yrs</span>'
        '<span class="dil-trk"><i class="dil-fill" id="dilF5"></i>'
        '<i class="dil-gone" id="dilG5"></i></span>'
        '<span class="v" id="dilV5">83.5%</span></div>'
        '<div class="dil-bar"><span class="yr">10 yrs</span>'
        '<span class="dil-trk"><i class="dil-fill" id="dilF10"></i>'
        '<i class="dil-gone" id="dilG10"></i></span>'
        '<span class="v" id="dilV10">69.8%</span></div>'
        "</div>"
        "</section>"
        # -- the lever ---------------------------------------------------
        '<section class="dil-card">'
        '<div class="dil-lever-top">'
        '<label for="dilShare">The only thing that slows it down</label>'
        '<span class="dil-pc" id="dilPc">0%</span>'
        "</div>"
        '<p class="dil-note">Solana’s apps earn real fees. Today every cent '
        "goes to the apps and their backers. Drag to route some of it into "
        "buying SOL and burning it.</p>"
        '<input id="dilShare" class="dil-range" type="range" min="0" max="100" '
        'step="1" value="0" aria-describedby="dilCeilNote">'
        '<div class="dil-ceil">'
        '<p class="dil-k">Burned against minted, per year</p>'
        '<div class="dil-ceil-trk">'
        '<i class="dil-ceil-fill" id="dilCf" style="width:0%"></i>'
        '<i class="dil-ceil-hole" id="dilCh" style="width:100%"></i>'
        "</div>"
        '<div class="dil-ceil-ends">'
        '<span id="dilCeilL">0 SOL burned</span>'
        f"<span><b>{ISSUED_PER_YEAR / 1e6:.1f}M minted</b></span>"
        "</div>"
        '<p class="dil-ceil-note" id="dilCeilNote">Drag it all the way. The bar '
        "<b>still cannot reach the end</b>.</p>"
        "</div>"
        "</section>"
        # -- what this does not say --------------------------------------
        "<section>"
        "<h2>What this does not say</h2>"
        "<p><strong>This is dilution, not price.</strong> It says nothing about "
        "what SOL is worth. SOL went from about $83 to about $256 across 2024 "
        "while supply grew the whole way.</p>"
        "<p><strong>It only hits unstaked SOL.</strong> Stakers receive issuance, "
        "so their share holds or rises. This is the SOL on exchanges, in LP "
        "positions, and in wallets people actually spend from.</p>"
        "<p><strong>Two figures are measured, the rest is arithmetic.</strong> "
        f"Issuance runs {ISSUE_PCT * 100:.2f}% a year on about "
        f"{SUPPLY / 1e6:.0f}M SOL. Solana apps earned $342.2M in Q1 2026; four "
        f"such quarters at about ${SOL_PRICE_USD} a SOL is the "
        f"{BURN_CEILING / 1e6:.1f}M ceiling, which is why the bar stops near "
        f"{BURN_CEILING / ISSUED_PER_YEAR * 100:.0f}%. Break-even would need "
        "about 157% of all app revenue. The ten-year rows assume today’s "
        "rate simply continues, which no rate does exactly.</p>"
        "<p><strong>The burn described here is a proposal.</strong> Our own "
        "program is not deployed. See the front page for what is live today.</p>"
        f'<p class="meta">{esc(SOURCE_LINE)}</p>'
        "</section>"
        "<section>"
        f'<p><a href="/{esc(site.VERIFY_FILENAME)}">Check a coin’s fee '
        'routing</a>, or <a href="/">read what this protocol does</a>.</p>'
        "</section>"
        "</main>"
        "<footer>"
        f'<p class="meta">rendered {esc(stamp)}</p>'
        "</footer>"
        f"<script>{script}</script>"
    )

    return site._document(
        "What Solana’s issuance costs your bag -- Charlie Protocol",
        body,
        style=site._INDEX_STYLE + _STYLE,
        description=(
            "Solana mints about "
            f"{MINTED_PER_DAY:,.0f} new SOL a day. Type in your balance and see "
            "how much more you would have to buy in ten years to own the same "
            "share of the network."
        ),
    )


def write(out_dir=site.DEFAULT_OUTPUT_DIR, *, now=None):
    """Writes `render()` to `<out_dir>/dilution.html`.

    Same ownership rule the other entry pages follow (`site._is_authored`):
    the deploy repository may hand-author this page, and regenerating over it
    would silently discard that work.
    """
    path = Path(out_dir) / DILUTION_FILENAME
    if path.exists() and site._is_authored(path):
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(now=now), encoding="utf-8")
    return path

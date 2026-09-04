"""The one page that needs JavaScript: connect a wallet, set a fee split.

Signing happens in the dev's wallet, and a wallet is a browser extension, so
there is no version of this that works without script. Everything else on the
site stays script-free.

Its own module rather than part of `site.py` for two reasons. `site.py`
imports only `invariants` and `publish` by design and a test holds that; and
this page is the only surface that asks a reader to sign something, so keeping
it separable keeps it reviewable.

No bundled library and no build step. A wallet's `signAndSendTransaction`
request takes a base58-encoded message, which is exactly what
`indexer.enroll.message` produces, so the page ships no third-party code that
a reader would have to trust to know what they are signing.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import site

ENROLL_FILENAME = "enroll.html"

_SCRIPT = r"""
var state = {wallet: null, mint: null, built: null};
function $(id) { return document.getElementById(id); }
function say(id, msg, kind) { var n = $(id); n.textContent = msg; n.className = 'note ' + (kind || ''); }

function provider() {
  var p = (window.phantom && window.phantom.solana) || window.solana;
  if (p && p.isPhantom) { return p; }
  if (window.solflare && window.solflare.isSolflare) { return window.solflare; }
  return p || null;
}

async function connect() {
  var p = provider();
  if (!p) { say('walletNote', 'No Solana wallet found in this browser. Install Phantom or Solflare, then reload.', 'bad'); return; }
  try {
    var r = await p.connect();
    var key = (r && r.publicKey) || p.publicKey;
    state.wallet = key.toString();
    $('walletAddr').textContent = state.wallet;
    $('connected').hidden = false;
    $('connect').hidden = true;
    say('walletNote', '', '');
    if ($('mint').value.trim()) { await inspect(); }
  } catch (e) {
    say('walletNote', 'Wallet connection was refused. Nothing happened.', 'bad');
  }
}

async function inspect() {
  var mint = $('mint').value.trim();
  if (!mint) { say('coinNote', 'Paste the coin address first.', 'bad'); return; }
  if (!state.wallet) { say('coinNote', 'Connect a wallet first.', 'bad'); return; }
  state.mint = mint;
  say('coinNote', 'Reading the chain...', '');
  $('splitBox').hidden = true;
  try {
    var r = await fetch('/api/enroll?mint=' + encodeURIComponent(mint) + '&authority=' + encodeURIComponent(state.wallet));
    var d = await r.json();
    if (d.error) { say('coinNote', d.error, 'bad'); return; }
    var rows = (d.current || []).map(function (c) { return (c.bps / 100) + '% ' + c.address; }).join('   ');
    if (d.cashback === true) {
      say('coinNote', 'This coin has pump Trader Cashback on, chosen at launch and locked on chain. Its whole creator fee goes to traders, so every share of any split would be zero. Enrolling would spend this coin one permanent change for nothing.', 'bad');
      return;
    }
    if (d.reason === 'no_sharing_config') {
      say('coinNote', 'This coin has no pump fee-sharing config yet, which is normal for a new launch. There is nothing to update until one exists, and this page cannot create one for you yet. Set fee sharing up on pump first, then come back.', 'bad');
      return;
    }
    if (d.admin_revoked) {
      say('coinNote', 'This coin has already used its one change, so the split is permanent. pump allows exactly one update and nothing can alter it now, including us. Current: ' + rows, 'bad');
      return;
    }
    if (!d.owns) {
      say('coinNote', 'This wallet does not administer that coin. Its config names ' + d.admin + '. Current split: ' + rows, 'bad');
      return;
    }
    if (d.cashback === null) {
      say('coinNote', 'You administer this coin. One thing first: its bonding curve predates pump cashback flag, so we cannot read it, and absent is not the same as off. If creator fees have never arrived in your wallet, cashback is on and enrolling would waste your one change. Current split: ' + rows, 'caution');
      $('splitBox').hidden = false;
      return;
    }
    say('coinNote', 'You administer this coin. Current split: ' + rows, 'good');
    $('splitBox').hidden = false;
  } catch (e) {
    say('coinNote', 'Could not reach the chain just now. Try again in a moment.', 'bad');
  }
}

function total() {
  var t = 0;
  document.querySelectorAll('.share-bps').forEach(function (i) {
    var v = parseFloat(i.value);
    if (!isNaN(v)) { t += Math.round(v * 100); }
  });
  var el = $('total');
  el.textContent = (t / 100).toFixed(2) + '%';
  el.className = (t === 10000) ? 'ok' : 'bad';
  return t;
}

function addRow(addr, pct) {
  var row = document.createElement('div');
  row.className = 'share-row';
  var a = document.createElement('input');
  a.className = 'share-addr'; a.placeholder = 'destination address';
  a.spellcheck = false; a.autocomplete = 'off'; a.value = addr || '';
  var b = document.createElement('input');
  b.className = 'share-bps'; b.type = 'number'; b.step = '0.01';
  b.min = '0'; b.max = '100'; b.placeholder = '%';
  b.value = (pct === undefined || pct === null) ? '' : pct;
  var rm = document.createElement('button');
  rm.type = 'button'; rm.className = 'rm'; rm.textContent = 'remove';
  rm.onclick = function () { row.remove(); total(); };
  a.oninput = total; b.oninput = total;
  row.appendChild(a); row.appendChild(b); row.appendChild(rm);
  $('shares').appendChild(row);
  total();
}

function shareRows() {
  var out = [];
  document.querySelectorAll('.share-row').forEach(function (row) {
    var a = row.querySelector('.share-addr').value.trim();
    var p = row.querySelector('.share-bps').value.trim();
    if (a && p !== '') { out.push(a + ':' + Math.round(parseFloat(p) * 100)); }
  });
  return out;
}

async function build() {
  var shares = shareRows();
  $('send').hidden = true;
  state.built = null;
  if (!shares.length) { say('buildNote', 'Add at least one destination.', 'bad'); return; }
  say('buildNote', 'Checking this split against pump, before anything is signed...', '');
  try {
    var url = '/api/enroll?mint=' + encodeURIComponent(state.mint) +
              '&authority=' + encodeURIComponent(state.wallet) +
              '&shares=' + encodeURIComponent(shares.join(','));
    var r = await fetch(url);
    var d = await r.json();
    if (d.error) { say('buildNote', d.error, 'bad'); return; }
    state.built = d;
    var lines = d.summary.map(function (s) { return '  ' + (s.bps / 100) + '%   ' + s.address; }).join('\n');
    say('buildNote', 'Simulated against mainnet: no error. This is exactly what you will sign:\n\n' + lines, 'good');
    $('send').hidden = false;
  } catch (e) {
    say('buildNote', 'Could not check that split just now. Nothing was sent.', 'bad');
  }
}

async function send() {
  if (!state.built) { return; }
  var p = provider();
  if (!p || !p.request) {
    say('sendNote', 'This wallet cannot sign from a page like this. Phantom can.', 'bad');
    return;
  }
  say('sendNote', 'Approve it in your wallet...', '');
  try {
    var res = await p.request({method: 'signAndSendTransaction', params: {message: state.built.message}});
    var sig = (res && (res.signature || res)) || '';
    var n = $('sendNote');
    n.className = 'note good';
    n.textContent = '';
    var done = document.createElement('p');
    done.textContent = 'Sent. Your split is set. pump allows this once, so it is now fixed.';
    var txp = document.createElement('p');
    var tx = document.createElement('a');
    tx.href = 'https://solscan.io/tx/' + sig;
    tx.textContent = sig;
    txp.textContent = 'Transaction: ';
    txp.appendChild(tx);
    // The loop closes here. The dev just changed where the fee goes; the next
    // thing they want is the page that reads it back off the chain, and it
    // is the page they will share.
    var check = document.createElement('p');
    var link = document.createElement('a');
    link.href = '/verify/' + state.mint;
    link.textContent = 'See your coin checked';
    check.appendChild(link);
    n.appendChild(done); n.appendChild(txp); n.appendChild(check);
    $('send').hidden = true;
    $('build').hidden = true;
  } catch (e) {
    say('sendNote', 'Not sent: ' + ((e && e.message) || 'the wallet refused it') + '. Nothing changed.', 'bad');
  }
}

document.addEventListener('DOMContentLoaded', function () {
  $('connect').onclick = connect;
  $('inspect').onclick = inspect;
  $('addRow').onclick = function () { addRow('', ''); };
  $('build').onclick = build;
  $('send').onclick = send;
  addRow('1nc1nerator11111111111111111111111111111111', 20);
  addRow('', 80);
});
"""

_STYLE = """
button.primary { padding: var(--sp-sm) var(--sp-lg); font-family: inherit;
  font-size: 16px; min-height: 44px; border: 1px solid var(--ink);
  background: var(--ink); color: var(--paper); cursor: pointer; }
button.primary:hover, button.primary:focus-visible {
  background: var(--accent); border-color: var(--accent); }
.share-row { display: flex; flex-wrap: wrap; gap: var(--sp-sm);
  margin-bottom: var(--sp-sm); align-items: center; }
.share-row .share-addr { flex: 1 1 18em; min-width: 0; padding: var(--sp-sm);
  font-family: inherit; font-size: 16px; border: 1px solid var(--unchecked); background: #fff; }
.share-row .share-bps { flex: 0 0 6em; padding: var(--sp-sm); font-family: inherit;
  font-size: 16px; border: 1px solid var(--unchecked); background: #fff; }
.share-row .rm { padding: var(--sp-sm); font-family: inherit; background: none;
  border: 1px dashed var(--unchecked); cursor: pointer; min-height: 44px; color: var(--ink); }
#total.ok { color: var(--pass-glyph); font-weight: 700; }
#total.bad { color: var(--destructive); font-weight: 700; }
.note { font-size: 14px; white-space: pre-wrap; overflow-wrap: anywhere; }
.note.bad { color: var(--destructive); }
.note.good { color: var(--pass-glyph); }
/* NOT `warn`: `say()` writes class="note <kind>", and `.warn` below is a
   destructive red callout used for the one-way-door notice. A caution the dev
   may act on must not wear the styling of a refusal. */
.note.caution { color: var(--unchecked); }
pre.note { background: var(--panel); padding: var(--sp-md); overflow-x: auto; margin: var(--sp-md) 0; }
.warn { border-left: 4px solid var(--destructive); background: rgba(163,39,31,0.08);
  padding: var(--sp-md); margin: var(--sp-md) 0; }
"""


def render(*, now=None) -> str:
    """Connect a wallet, prove ownership, set the split.

    Ownership is one thing and the chain decides it: the connected wallet must
    be the key the coin's pump sharing config names as `admin`.
    `update_fee_shares_v2` checks that signature and nothing else, so anything
    else this page called ownership would be a claim the chain does not make.

    Three things a dev is told BEFORE signing, each irreversible or expensive
    to learn the hard way: pump allows the split to be changed exactly ONCE, a
    revoked config cannot be changed at all, and the split must total exactly
    100%. The server refuses to return a transaction that breaks any of them,
    and refuses to return one that does not simulate cleanly against mainnet.
    """
    stamp = site._stamp(now() if callable(now) else (now if now is not None else time.time()))
    esc = site.esc
    body = (
        "<header>"
        "<h1>Set your coin&#x27;s fee split</h1>"
        "<p>Connect the wallet that administers the coin and send its creator "
        "fee wherever you want it to go. Your key never leaves your wallet: "
        "this page builds a transaction, your wallet signs it.</p>"
        "</header>"
        "<main>"
        "<section>"
        "<h2>1. Connect</h2>"
        '<button type="button" id="connect" class="primary">Connect wallet</button>'
        '<p id="connected" hidden>Connected: <code id="walletAddr"></code></p>'
        '<p id="walletNote" class="note"></p>'
        "</section>"
        "<section>"
        "<h2>2. Name the coin</h2>"
        '<div class="verify-form">'
        '<label for="mint">Contract address (CA)</label>'
        '<input id="mint" type="text" spellcheck="false" autocomplete="off" '
        'placeholder="paste the CA here">'
        '<button type="button" id="inspect">Check ownership</button>'
        "</div>"
        '<p id="coinNote" class="note"></p>'
        "</section>"
        '<section id="splitBox" hidden>'
        "<h2>3. Set the split</h2>"
        '<div class="warn"><strong>pump lets a coin&#x27;s split be changed '
        "once.</strong> After this is sent, no key can change it again, "
        "including yours. Check every destination before you sign.</div>"
        '<div id="shares"></div>'
        '<p><button type="button" id="addRow">Add a destination</button> '
        '&nbsp; Total: <span id="total">100.00%</span> (must be exactly 100%)</p>'
        '<p><button type="button" id="build" class="primary">Check this split</button></p>'
        '<pre id="buildNote" class="note"></pre>'
        '<p><button type="button" id="send" class="primary" hidden>Sign and send</button></p>'
        '<p id="sendNote" class="note"></p>'
        "</section>"
        "<section>"
        "<h2>What a destination means</h2>"
        "<p>The first row is filled in with "
        "<code>1nc1nerator11111111111111111111111111111111</code>, Solana&#x27;s "
        "incinerator. Its source says lamports credited there are removed from "
        "the total supply at the end of the block. SOL sent there is destroyed, "
        "not stored, and that is what deflation means.</p>"
        "<p>Any other address you put here simply receives the SOL. Only the "
        "incinerator reduces the supply, so if that is what you want, keep it "
        "in the split.</p>"
        "<p>A destination that buys the token and destroys it reduces the "
        "coin&#x27;s own supply instead. Anything else is an ordinary wallet, "
        "and the coin&#x27;s page says so in those words.</p>"
        f'<p><a href="/{esc(site.VERIFY_FILENAME)}">Check any coin</a>.</p>'
        "</section>"
        "</main>"
        f'<p class="meta">generated at {esc(stamp)}</p>'
        f"<script>{_SCRIPT}</script>"
    )
    return site._document(
        "Set your fee split -- Charlie Protocol", body,
        style=site._INDEX_STYLE + _STYLE,
        description="Connect the wallet that administers your pump.fun coin and "
                    "set where its creator fee goes.",
    )


def write(out_dir=site.DEFAULT_OUTPUT_DIR, *, now=None):
    path = Path(out_dir) / ENROLL_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(now=now), encoding="utf-8")
    return path

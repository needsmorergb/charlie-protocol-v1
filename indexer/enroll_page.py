"""The one page that needs JavaScript: connect a wallet, set a fee split.

Signing happens in the dev's wallet, and a wallet is a browser extension, so
there is no version of this that works without script. Everything else on the
site stays script-free.

Its own module rather than part of `site.py` for two reasons. `site.py`
imports only `invariants` and `publish` by design and a test holds that; and
this page is the only surface that asks a reader to sign something, so keeping
it separable keeps it reviewable.

No bundled library and no build step. A wallet's `signAndSendTransaction`
request takes a base58-encoded unsigned transaction (Phantom names the
parameter `message`, and parses it as a transaction: signature count, the
signatures, then the message). `api/enroll.py` returns exactly that as
`signable`, built from `indexer.enroll.message` plus one zero signature, so
the page ships no third-party code that a reader would have to trust to know
what they are signing.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import site

ENROLL_FILENAME = "enroll.html"

_SCRIPT = r"""
var state = {wallet: null, mint: null, built: null, toll: null, create: false};
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
    state.toll = d.toll || null;
    if (d.reason === 'no_sharing_config') {
      if (d.can_create) {
        state.create = true;
        say('coinNote', 'You launched this coin, and it has no pump fee-sharing config yet, which is normal for a new launch. One signature will create the config and set the split at the same time. Its creator fee currently goes to your wallet, ' + d.creator + '.' + (d.graduated ? ' It has graduated to the pump AMM, so the config is created with its pool named, and the fee the pool collects follows the split from then on.' : ''), 'good');
        openForm();
        return;
      }
      say('coinNote', 'This coin has no pump fee-sharing config yet, and pump lets only the wallet that launched it create one.' + (d.creator ? ' That wallet is ' + d.creator + '. Connect it and check again.' : ''), 'bad');
      return;
    }
    state.create = false;
    if (d.admin_revoked) {
      say('coinNote', 'This coin has already used its one change, so the split is permanent. pump allows exactly one update and nothing can alter it now, including us. Current: ' + rows, 'bad');
      return;
    }
    if (!d.owns) {
      say('coinNote', 'This wallet does not administer that coin. Its config names ' + d.admin + '. Current split: ' + rows, 'bad');
      return;
    }
    var cautions = [];
    if (d.cashback === null) {
      cautions.push('Its bonding curve predates pump cashback flag, so we cannot read it, and absent is not the same as off. If creator fees have never arrived in your wallet, cashback is on and enrolling would waste your one change.');
    }
    if (d.graduated) {
      cautions.push('It has graduated to the pump AMM, where the creator fee collects as wrapped SOL in a pool vault instead of as lamports on the config. Setting the split still works and still binds. Paying it out takes an extra pump instruction first, and a payout attempted without that instruction moves nothing at all; the protocol\u0027s crank sends that instruction before every payout.');
    }
    if (cautions.length) {
      say('coinNote', 'You administer this coin. Before you spend the one change: ' + cautions.join(' ') + ' Current split: ' + rows, 'caution');
      openForm();
      return;
    }
    say('coinNote', 'You administer this coin. Current split: ' + rows, 'good');
    openForm();
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

function addRow(addr, pct, locked, label, caption, hint) {
  var row = document.createElement('div');
  row.className = 'share-row' + (locked ? ' locked' : '');
  var a = document.createElement('input');
  a.className = 'share-addr'; a.placeholder = hint || 'destination address';
  a.spellcheck = false; a.autocomplete = 'off'; a.value = addr || '';
  var b = document.createElement('input');
  b.className = 'share-bps'; b.type = 'number'; b.step = '0.01';
  b.min = '0'; b.max = '100'; b.placeholder = '%';
  b.value = (pct === undefined || pct === null) ? '' : pct;
  var rm = document.createElement('button');
  rm.type = 'button'; rm.className = 'rm';
  if (locked) {
    // The protocol's share. Not editable and not removable: it is the price
    // of using the protocol, and the server will not build a split without it.
    a.readOnly = true; b.readOnly = true;
    rm.textContent = label || 'protocol share'; rm.disabled = true;
  } else {
    rm.textContent = 'remove';
    rm.onclick = function () { row.remove(); total(); };
  }
  a.oninput = total; b.oninput = total;
  row.appendChild(a); row.appendChild(b); row.appendChild(rm);
  if (caption) {
    // What this destination does, on the row itself. A split is money and a
    // dev reads the rows, not the prose below them.
    var what = document.createElement('span');
    what.className = 'share-what'; what.textContent = caption;
    row.appendChild(what);
  }
  $('shares').appendChild(row);
  total();
}

function openForm() {
  // Rebuilt on every inspection, so the toll row is the one the server
  // named and the defaults reflect the wallet that is connected.
  $('shares').textContent = '';
  if (!state.toll || !state.toll.address) {
    say('coinNote', 'Enrollment is not open yet: the protocol\u0027s collection address has not been set. Nothing can be built until it is.', 'bad');
    $('splitBox').hidden = true;
    return;
  }
  var tollPct = state.toll.bps / 100;
  addRow(state.toll.address, tollPct, true, 'Charlie Protocol ' + tollPct + '%',
         'The protocol\u0027s share. It buys $CHARLIE and burns it.');
  // The BURN leg has its own row, blank. It is a wallet the dev holds and
  // runs the keeper from, so there is no address to fill in for them; left
  // blank it is simply not sent, and the other rows still total 100.
  addRow('', '', false, null,
         'Buy back and burn. A wallet you hold that runs the keeper against ' +
         'this coin: SOL sent here buys your coin and burns it. Paste that ' +
         'wallet and give it a share, or leave this row blank and it is not sent.',
         'wallet that runs the keeper (optional)');
  addRow('1nc1nerator11111111111111111111111111111111', 20, false, null,
         'Solana\u0027s incinerator. SOL sent here is destroyed.');
  addRow(state.wallet, 100 - 20 - tollPct, false, null,
         'Your wallet. This share is simply yours.');
  $('splitBox').hidden = false;
}

function shareRows() {
  // A blank row is skipped: that is the buy-back row left alone. A row that
  // is half filled is a mistake, and the total above cannot show it because
  // it counts percentages whether or not an address is there.
  var out = [], problems = [];
  document.querySelectorAll('.share-row').forEach(function (row) {
    var a = row.querySelector('.share-addr').value.trim();
    var p = row.querySelector('.share-bps').value.trim();
    if (!a && p === '') { return; }
    if (!a) { problems.push('A row has ' + p + '% but no address. Paste a wallet into it or remove the row.'); return; }
    if (p === '') { problems.push('The row for ' + a + ' has no percentage. Give it one or remove the row.'); return; }
    out.push(a + ':' + Math.round(parseFloat(p) * 100));
  });
  return {shares: out, problems: problems};
}

async function build() {
  var rows = shareRows();
  var shares = rows.shares;
  $('send').hidden = true;
  state.built = null;
  if (rows.problems.length) { say('buildNote', rows.problems.join('\n'), 'bad'); return; }
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
    // Phantom calls it `message` and parses it as a whole transaction:
    // signature count, signatures, then the message. Handed the bare message
    // it answered "Reached end of buffer unexpectedly". `signable` is the
    // unsigned transaction, and it is what the server simulated.
    var res = await p.request({method: 'signAndSendTransaction', params: {message: state.built.signable}});
    var sig = (res && (res.signature || res)) || '';
    var n = $('sendNote');
    n.className = 'note good';
    n.textContent = '';
    var done = document.createElement('p');
    done.textContent = state.create
      ? 'Sent. The fee-sharing config was created and your split is set. pump allows the split to be changed once, so it is now fixed.'
      : 'Sent. Your split is set. pump allows this once, so it is now fixed.';
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
.share-row .share-what { flex: 1 1 100%; font-size: 14px; line-height: 1.4;
  color: var(--ink); opacity: 0.8; margin-top: -2px; }
.share-row.locked .share-addr, .share-row.locked .share-bps { background: var(--panel); color: var(--ink); }
.share-row.locked .rm { border-style: solid; cursor: default; }
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
        "<h1>Enroll your coin</h1>"
        "<p>Connect the wallet that launched the coin and set where its creator "
        "fee goes. If the coin has no pump fee-sharing config yet, which is "
        "normal for a new launch, one signature creates it and sets the split. "
        "Your key never leaves your wallet: this page builds a transaction, "
        "your wallet signs it.</p>"
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
        "<p>Ownership is decided by the chain. For a coin with no fee-sharing "
        "config yet, that means the wallet its bonding curve names as creator; "
        "for a coin that has one, the key its config names as admin.</p>"
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
        "<p>The first row is the protocol&#x27;s share, fixed at 5% of the "
        "creator fee: it is the price of enrolling, and it funds buying and "
        "burning $CHARLIE. Every other row is yours to set. The second row is "
        "the buy-back-and-burn row: a wallet you hold that runs the keeper "
        "against this coin, so the SOL that lands there buys your coin and "
        "burns it. Paste that wallet and give it a share, or leave the row "
        "blank and it is not sent. The defaults send 20% to Solana&#x27;s "
        "incinerator and the rest to your wallet. Each row says what it does; "
        "the section below says how to run the keeper.</p>"
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
        "<p>One row is filled in with "
        "<code>1nc1nerator11111111111111111111111111111111</code>, Solana&#x27;s "
        "incinerator. Its source says lamports credited there are removed from "
        "the total supply at the end of the block. SOL sent there is destroyed, "
        "not stored, and that is what deflation means.</p>"
        "<p>Any other address you put here simply receives the SOL. Only the "
        "incinerator reduces the supply, so if that is what you want, keep it "
        "in the split.</p>"
        "<p>Buying your own coin back and burning it is the protocol&#x27;s BURN "
        "leg: SOL buys the token on its pump pool, then an SPL burn destroys "
        "what was bought, in one transaction, so the coin&#x27;s own supply "
        "falls and nothing is left in a wallet. No on-chain program runs that "
        "leg yet, so there is no address you can paste that does it by "
        "itself. Today it is run from a wallet you hold: put that wallet in "
        "the split, and run the keeper from the repository against it "
        "(<code>python -m indexer buyback &lt;mint&gt; --keypair ... --every "
        "N</code>). It buys on the coin&#x27;s bonding curve while the coin is "
        "on it, and on the pump AMM after it graduates, switching by itself; "
        "the burn is the same either way.</p>"
        "<p>The coin&#x27;s page records every such burn as burned by hand, "
        "counts it toward supply destroyed, and until the program exists names "
        "that wallet an ordinary wallet rather than a protocol burn, because "
        "that is what the chain shows. When the program ships it will derive "
        "a burn address per coin, and a share pointed there will be cranked "
        "for you.</p>"
        "<p>The protocol&#x27;s 5% is collected at the address on the first "
        "row and spent running that same leg on $CHARLIE: buying it and burning "
        "it. No on-chain program "
        "enforces that share yet: this page refuses to build a split without "
        "it, and the program that will enforce it is in the repository linked "
        "from the front page.</p>"
        f'<p><a href="/{esc(site.VERIFY_FILENAME)}">Check any coin</a>.</p>'
        "</section>"
        "</main>"
        f'<p class="meta">generated at {esc(stamp)}</p>'
        f"<script>{_SCRIPT}</script>"
    )
    return site._document(
        "Enroll your coin -- Charlie Protocol", body,
        style=site._INDEX_STYLE + _STYLE,
        description="Connect the wallet that launched your pump.fun coin and "
                    "set where its creator fee goes, in one signature.",
    )


def write(out_dir=site.DEFAULT_OUTPUT_DIR, *, now=None):
    path = Path(out_dir) / ENROLL_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(now=now), encoding="utf-8")
    return path

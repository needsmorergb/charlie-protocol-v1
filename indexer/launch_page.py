"""The launch door: create a coin on pump with its fee split set from block zero.

The second of two pages that need JavaScript (the first is `/enroll`), for
the same reason: signing happens in the dev's wallet, and a wallet is a
browser extension. Same construction as `enroll_page`: no bundled library, no
build step, a base58 transaction handed to the wallet's own
`signAndSendTransaction`, so the page ships no third-party code a reader
would have to trust to know what they are signing.

Two approvals, a few seconds apart, and the page says so before the first:

1. `create` -- pump makes the coin. Built by `api/launch.py`, signed by the
   mint (a throwaway key, powerless after this instruction) and by the dev.
2. the enrollment -- built by `api/enroll.py`, the exact transaction `/enroll`
   sends for a coin with no config. Creates the fee-sharing config and sets
   the split in one instruction pair, spending the one-shot on purpose.

The Telegram bot (`tools/launch_bot.py`) never signs anything. It collects
the same fields this page collects, pins the image, and sends the dev here
with the fields in the URL: `/launch?name=..&symbol=..&uri=..`. The page
reads them and skips to step 2.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import site

LAUNCH_FILENAME = "launch.html"

_SCRIPT = r"""
var state = {wallet: null, toll: null, uri: null, meta: null, built: null, mint: null, createSig: null, enroll: null};
function $(id) { return document.getElementById(id); }
function say(id, msg, kind) { var n = $(id); n.textContent = msg; n.className = 'note ' + (kind || ''); }

function provider() {
  var p = (window.phantom && window.phantom.solana) || window.solana;
  if (p && p.isPhantom) { return p; }
  if (window.solflare && window.solflare.isSolflare) { return window.solflare; }
  return p || null;
}

async function describe() {
  try {
    var r = await fetch('/api/launch');
    var d = await r.json();
    state.toll = d.toll || null;
    if (!d.open) { say('walletNote', 'Launching is not open yet: the protocol\u0027s collection address has not been set.', 'bad'); }
  } catch (e) {
    say('walletNote', 'Could not reach the server just now. Reload in a moment.', 'bad');
  }
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
    openSplit();
  } catch (e) {
    say('walletNote', 'Wallet connection was refused. Nothing happened.', 'bad');
  }
}

function fromQuery() {
  // The Telegram bot lands here with the coin already described. Nothing in
  // the URL is trusted past being shown in the form: the server re-checks
  // every field, and the dev sees every field before a wallet opens.
  var q = new URLSearchParams(window.location.search);
  if (q.get('name')) { $('name').value = q.get('name'); }
  if (q.get('symbol')) { $('symbol').value = q.get('symbol'); }
  if (q.get('uri')) {
    state.uri = q.get('uri');
    $('uriBox').hidden = false;
    $('uriShown').textContent = state.uri;
    say('metaNote', 'Image and description already pinned (from the Telegram bot). Check the name and ticker, then connect.', 'good');
  }
}

async function pin() {
  var name = $('name').value.trim(), symbol = $('symbol').value.trim();
  var file = $('image').files && $('image').files[0];
  if (!name) { say('metaNote', 'Give the coin a name.', 'bad'); return; }
  if (!symbol) { say('metaNote', 'Give the coin a ticker.', 'bad'); return; }
  if (!file) { say('metaNote', 'Attach an image. pump shows it beside the coin everywhere.', 'bad'); return; }
  say('metaNote', 'Pinning the image and description...', '');
  var form = new FormData();
  form.append('file', file);
  form.append('name', name);
  form.append('symbol', symbol);
  form.append('description', $('description').value.trim());
  ['twitter', 'telegram', 'website'].forEach(function (k) { if ($(k).value.trim()) { form.append(k, $(k).value.trim()); } });
  try {
    var r = await fetch('/api/launch', {method: 'POST', body: form});
    var d = await r.json();
    if (d.error) { say('metaNote', d.error, 'bad'); return; }
    state.uri = d.metadataUri;
    $('uriBox').hidden = false;
    $('uriShown').textContent = state.uri;
    say('metaNote', 'Pinned. This is the metadata pump will show for the coin. Nothing has been created yet.', 'good');
  } catch (e) {
    say('metaNote', 'Could not pin the metadata just now. Nothing was created.', 'bad');
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
    // The protocol row. Its value is what makes the total 100, but the
    // figure shown is the fee as it is stated everywhere: per transaction.
    a.readOnly = true; b.readOnly = true; b.hidden = true;
    var fixed = document.createElement('span');
    fixed.className = 'share-fixed'; fixed.textContent = '0.25% of each transaction';
    row.appendChild(fixed);
    rm.textContent = label || 'protocol share'; rm.disabled = true;
  } else {
    rm.textContent = 'remove';
    rm.onclick = function () { row.remove(); total(); };
  }
  a.oninput = total; b.oninput = total;
  row.appendChild(a); row.appendChild(b); row.appendChild(rm);
  if (caption) {
    var what = document.createElement('span');
    what.className = 'share-what'; what.textContent = caption;
    row.appendChild(what);
  }
  $('shares').appendChild(row);
  total();
}

function openSplit() {
  $('shares').textContent = '';
  if (!state.toll || !state.toll.address) {
    say('splitNote', 'Launching is not open yet: the protocol\u0027s collection address has not been set. Nothing can be built until it is.', 'bad');
    $('splitBox').hidden = true;
    return;
  }
  var tollPct = state.toll.bps / 100;
  addRow(state.toll.address, tollPct, true, 'Charlie Protocol',
         'The protocol\u0027s share: 0.25% of each transaction, fixed. It buys $CHARLIE and burns it.');
  addRow('1nc1nerator11111111111111111111111111111111', 20, false, null,
         'Solana\u0027s incinerator. SOL sent here is destroyed. This row is the SOL burn; without it there is none.');
  addRow(state.wallet, 100 - 20 - tollPct, false, null,
         'Your wallet. This share is simply yours.');
  $('splitBox').hidden = false;
}

function shareRows() {
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
  // Step one is built here; step two is checked here too, against a mint that
  // does not exist yet, so only its SPLIT can be validated now. The full
  // simulation of step two happens the moment the coin exists.
  $('send').hidden = true;
  state.built = null;
  var rows = shareRows();
  if (!state.uri) { say('buildNote', 'Pin the image and description first (step 2).', 'bad'); return; }
  if (!state.wallet) { say('buildNote', 'Connect a wallet first.', 'bad'); return; }
  if (rows.problems.length) { say('buildNote', rows.problems.join('\n'), 'bad'); return; }
  if (total() !== 10000) { say('buildNote', 'The split must total exactly 100%.', 'bad'); return; }
  state.shares = rows.shares;
  say('buildNote', 'Building the coin and checking it against pump, before anything is signed...', '');
  try {
    var url = '/api/launch?authority=' + encodeURIComponent(state.wallet) +
              '&name=' + encodeURIComponent($('name').value.trim()) +
              '&symbol=' + encodeURIComponent($('symbol').value.trim()) +
              '&uri=' + encodeURIComponent(state.uri);
    var r = await fetch(url);
    var d = await r.json();
    if (d.error) { say('buildNote', d.error, 'bad'); return; }
    state.built = d;
    state.mint = d.mint;
    var lines = rows.shares.map(function (s) {
      var p = s.split(':');
      if (state.toll && p[0] === state.toll.address) { return '  0.25% of each transaction   ' + p[0] + '   (protocol)'; }
      return '  ' + (parseInt(p[1], 10) / 100) + '% of the creator fee   ' + p[0];
    }).join('\n');
    say('buildNote', 'Simulated against mainnet: no error.\n\nCoin: ' + d.name + ' (' + d.symbol + ')\nMint: ' + d.mint +
        '\nRent: about ' + (d.rent_lamports / 1e9).toFixed(4) + ' SOL for the coin, then about 0.0059 SOL for the fee config.' +
        '\n\nThe split that will be set, permanently, in the second approval:\n\n' + lines +
        '\n\nTwo approvals: first the coin, then its split. Nothing else is asked of you.', 'good');
    $('send').hidden = false;
  } catch (e) {
    say('buildNote', 'Could not build the coin just now. Nothing was sent.', 'bad');
  }
}

function sleep(ms) { return new Promise(function (res) { setTimeout(res, ms); }); }

async function waitForCurve() {
  // Poll until the create has landed AND the bonding curve exists. Up to two
  // minutes, then stop and say so: a dropped transaction is not a failure of
  // the split, and the dev should not sign step two against a coin that is
  // not there.
  for (var i = 0; i < 40; i++) {
    await sleep(3000);
    try {
      var r = await fetch('/api/launch?status=' + encodeURIComponent(state.createSig) + '&mint=' + encodeURIComponent(state.mint));
      var d = await r.json();
      if (d.failed) { return {ok: false, why: 'The create transaction failed on chain: ' + JSON.stringify(d.err) + '. No coin was made and nothing was spent but the fee.'}; }
      if (d.ready) { return {ok: true}; }
      say('sendNote', 'Coin sent. Waiting for the chain to confirm it (' + ((i + 1) * 3) + 's)...', '');
    } catch (e) { /* keep polling */ }
  }
  return {ok: false, why: 'The chain has not confirmed the coin after two minutes. If it lands later, set the split at /enroll with the mint ' + state.mint + '.'};
}

async function send() {
  if (!state.built) { return; }
  var p = provider();
  if (!p || !p.request) { say('sendNote', 'This wallet cannot sign from a page like this. Phantom can.', 'bad'); return; }
  $('send').hidden = true;
  say('sendNote', 'Approval 1 of 2: create the coin. Approve it in your wallet...', '');
  try {
    var res = await p.request({method: 'signAndSendTransaction', params: {message: state.built.signable}});
    state.createSig = (res && (res.signature || res)) || '';
  } catch (e) {
    say('sendNote', 'Not sent: ' + ((e && e.message) || 'the wallet refused it') + '. No coin was created.', 'bad');
    $('send').hidden = false;
    return;
  }
  var landed = await waitForCurve();
  if (!landed.ok) { say('sendNote', landed.why, 'bad'); return; }

  say('sendNote', 'The coin exists: ' + state.mint + '. Checking the split against pump for this exact mint...', '');
  var d;
  try {
    var url = '/api/enroll?mint=' + encodeURIComponent(state.mint) +
              '&authority=' + encodeURIComponent(state.wallet) +
              '&shares=' + encodeURIComponent(state.shares.join(','));
    var r = await fetch(url);
    d = await r.json();
    if (d.error) {
      say('sendNote', 'The coin was created but its split could not be built: ' + d.error + '\n\nThe coin\u0027s one change is still unspent. Set it at /enroll with mint ' + state.mint + '.', 'bad');
      return;
    }
  } catch (e) {
    say('sendNote', 'The coin was created, but the split could not be checked just now. Its one change is still unspent: set it at /enroll with mint ' + state.mint + '.', 'bad');
    return;
  }
  say('sendNote', 'Simulated against mainnet: no error. Approval 2 of 2: set the split, permanently. Approve it in your wallet...', '');
  try {
    var res2 = await p.request({method: 'signAndSendTransaction', params: {message: d.signable}});
    var sig2 = (res2 && (res2.signature || res2)) || '';
    var n = $('sendNote');
    n.className = 'note good';
    n.textContent = '';
    var done = document.createElement('p');
    done.textContent = 'Done. The coin exists and its fee split is set. pump allows the split to be changed once, and that change is now spent, on purpose, on the split above. No key can alter it, including yours.';
    var mintP = document.createElement('p');
    mintP.textContent = 'Mint: ' + state.mint;
    var tx1 = document.createElement('p'); var a1 = document.createElement('a');
    a1.href = 'https://solscan.io/tx/' + state.createSig; a1.textContent = state.createSig;
    tx1.textContent = 'Create: '; tx1.appendChild(a1);
    var tx2 = document.createElement('p'); var a2 = document.createElement('a');
    a2.href = 'https://solscan.io/tx/' + sig2; a2.textContent = sig2;
    tx2.textContent = 'Split: '; tx2.appendChild(a2);
    var pump = document.createElement('p'); var ap = document.createElement('a');
    ap.href = 'https://pump.fun/coin/' + state.mint; ap.textContent = 'Your coin on pump';
    pump.appendChild(ap);
    var check = document.createElement('p'); var link = document.createElement('a');
    link.href = '/verify/' + state.mint; link.textContent = 'Your coin\u0027s page here, read from the chain';
    check.appendChild(link);
    var next = document.createElement('p');
    next.textContent = 'From here the protocol finds the coin on the chain. The crank asks pump to pay the split whenever the creator vault clears pump\u0027s minimum, and the first payout to the incinerator is the first burn.';
    n.appendChild(done); n.appendChild(mintP); n.appendChild(tx1); n.appendChild(tx2); n.appendChild(pump); n.appendChild(check); n.appendChild(next);
    $('build').hidden = true;
  } catch (e) {
    say('sendNote', 'The coin exists but the split was not sent: ' + ((e && e.message) || 'the wallet refused it') + '. Its one change is still unspent. Set it at /enroll with mint ' + state.mint + '.', 'bad');
  }
}

document.addEventListener('DOMContentLoaded', function () {
  $('connect').onclick = connect;
  $('pin').onclick = pin;
  $('addRow').onclick = function () { addRow('', ''); };
  $('build').onclick = build;
  $('send').onclick = send;
  fromQuery();
  describe();
});
"""

_STYLE = """
button.primary { padding: var(--sp-sm) var(--sp-lg); font-family: inherit;
  font-size: 16px; min-height: 44px; border: 1px solid var(--ink);
  background: var(--ink); color: var(--paper); cursor: pointer; }
button.primary:hover, button.primary:focus-visible {
  background: var(--accent); border-color: var(--accent); }
.field { display: flex; flex-direction: column; gap: 4px; margin-bottom: var(--sp-md); }
.field input, .field textarea { padding: var(--sp-sm); font-family: inherit; font-size: 16px;
  border: 1px solid var(--unchecked); background: #fff; max-width: 100%; }
.field textarea { min-height: 4em; }
.field label { font-size: 14px; }
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
.share-row .share-fixed { flex: 0 0 auto; padding: var(--sp-sm); font-size: 14px; font-weight: 700; }
#total.ok { color: var(--pass-glyph); font-weight: 700; }
#total.bad { color: var(--destructive); font-weight: 700; }
.note { font-size: 14px; white-space: pre-wrap; overflow-wrap: anywhere; }
.note.bad { color: var(--destructive); }
.note.good { color: var(--pass-glyph); }
.note.caution { color: var(--unchecked); }
pre.note { background: var(--panel); padding: var(--sp-md); overflow-x: auto; margin: var(--sp-md) 0; }
.warn { border-left: 4px solid var(--destructive); background: rgba(163,39,31,0.08);
  padding: var(--sp-md); margin: var(--sp-md) 0; }
ol.steps li { margin-bottom: var(--sp-sm); }
"""


def render(*, now=None) -> str:
    """Create a coin with its split set from the first block.

    The permanence is stated before the benefit, unprompted, in the dev's own
    interest: it is the first thing on the page after the heading, and it is
    repeated at the button. The marketing does not get to be softer than the
    code, and the code (`enroll.preflight`) says it before a wallet opens.
    """
    stamp = site._stamp(now() if callable(now) else (now if now is not None else time.time()))
    esc = site.esc
    body = (
        "<header>"
        "<h1>Launch a coin with its burn built in</h1>"
        "<p>Create a pump.fun coin here instead of on pump, and its creator fee "
        "split is set in the same minute it is born: a share to Solana&#x27;s "
        "incinerator, a share to the protocol, the rest wherever you say. Your "
        "key never leaves your wallet. This page builds two transactions, your "
        "wallet signs them, and nothing else is asked of you.</p>"
        "</header>"
        "<main>"
        '<div class="warn"><strong>Read this before anything else. pump lets a '
        "coin&#x27;s fee split be changed exactly once.</strong> Every coin "
        "spends that one change the day it sets a split, whether it launches "
        "here or enrolls later. Launching here spends it now, on purpose, on a "
        "split you chose, before anyone has traded. After the second approval "
        "no key can change it again, including yours. If that is a problem, "
        "this is not for you.</div>"
        "<section>"
        "<h2>Before you start</h2>"
        '<ol class="steps">'
        "<li><strong>Do not create the coin on pump first.</strong> This page "
        "creates it. A coin created on pump can still enroll at /enroll, but "
        "one launch choice pump offers, Trader Cashback, is locked at creation "
        "and makes a coin impossible to enroll ever. Coins made here cannot "
        "have it: the instruction this page sends has no such option.</li>"
        "<li><strong>Have about 0.02 SOL in the wallet</strong> beyond what you "
        "plan to buy with: the coin&#x27;s accounts cost about 0.0081 SOL of "
        "rent, the fee config about 0.0059 SOL, plus two network fees. Both "
        "figures were read from mainnet&#x27;s own rent calculator.</li>"
        "<li><strong>Decide the split now.</strong> The protocol&#x27;s row is "
        "fixed at 0.25% of each transaction. Only the incinerator row destroys SOL; "
        "if you remove it there is no SOL burn and this page will still let you, "
        "because the split is yours. Every other row simply receives SOL.</li>"
        "<li><strong>Have the image, name and ticker ready.</strong> Name up to "
        "32 bytes, ticker up to 10, image up to 4 MB. They are pinned to IPFS "
        "through pump&#x27;s own metadata service and cannot be changed after "
        "creation.</li>"
        "<li><strong>Expect two wallet approvals</strong>, a few seconds apart: "
        "first the coin, then its split. Between them the page waits for the "
        "chain to confirm the coin, then checks the split against pump for that "
        "exact mint before asking you to approve it.</li>"
        "</ol>"
        "</section>"
        "<section>"
        "<h2>1. Connect</h2>"
        '<button type="button" id="connect" class="primary">Connect wallet</button>'
        '<p id="connected" hidden>Connected: <code id="walletAddr"></code></p>'
        '<p id="walletNote" class="note"></p>'
        "</section>"
        "<section>"
        "<h2>2. Describe the coin</h2>"
        '<div class="field"><label for="name">Name (up to 32 bytes)</label>'
        '<input id="name" type="text" maxlength="32" autocomplete="off"></div>'
        '<div class="field"><label for="symbol">Ticker (up to 10 bytes, no spaces)</label>'
        '<input id="symbol" type="text" maxlength="10" autocomplete="off" spellcheck="false"></div>'
        '<div class="field"><label for="description">Description</label>'
        '<textarea id="description" maxlength="1000"></textarea></div>'
        '<div class="field"><label for="image">Image (PNG, JPEG, GIF or WebP, up to 4 MB)</label>'
        '<input id="image" type="file" accept="image/png,image/jpeg,image/gif,image/webp"></div>'
        '<div class="field"><label for="twitter">X / Twitter (optional)</label>'
        '<input id="twitter" type="text" autocomplete="off"></div>'
        '<div class="field"><label for="telegram">Telegram (optional)</label>'
        '<input id="telegram" type="text" autocomplete="off"></div>'
        '<div class="field"><label for="website">Website (optional)</label>'
        '<input id="website" type="text" autocomplete="off"></div>'
        '<p><button type="button" id="pin">Pin image and description</button></p>'
        '<p id="metaNote" class="note"></p>'
        '<p id="uriBox" hidden>Metadata URI: <code id="uriShown"></code></p>'
        "</section>"
        '<section id="splitBox" hidden>'
        "<h2>3. Set the split</h2>"
        "<p>The first row is the protocol&#x27;s share: <strong>0.25% of each "
        "transaction</strong>, fixed. It is the price of launching here, and it "
        "funds buying and burning $CHARLIE. The second row is Solana&#x27;s "
        "incinerator: SOL sent there is removed from the supply at the end of "
        "the block, and that row is the burn this page is named for. The third "
        "row is your wallet. Add rows or change the numbers as you like; the "
        "total must be exactly 100%.</p>"
        '<div id="shares"></div>'
        '<p><button type="button" id="addRow">Add a destination</button> '
        '&nbsp; Total: <span id="total">100.00%</span> (must be exactly 100%)</p>'
        '<p id="splitNote" class="note"></p>'
        '<p><button type="button" id="build" class="primary">Build the coin and check it</button></p>'
        '<pre id="buildNote" class="note"></pre>'
        '<div class="warn"><strong>The second approval is permanent.</strong> '
        "Check every destination above before you sign it.</div>"
        '<p><button type="button" id="send" class="primary" hidden>Create the coin and set the split (two approvals)</button></p>'
        '<p id="sendNote" class="note"></p>'
        "</section>"
        "<section>"
        "<h2>What happens on the chain</h2>"
        "<p>Two transactions. The first is pump&#x27;s own <code>create</code>: "
        "it makes the mint, the bonding curve and the metadata, exactly as "
        "pump&#x27;s site does, with your wallet as the coin&#x27;s creator. It "
        "is signed by your wallet and by the mint&#x27;s own throwaway key, "
        "which this server generates, uses once, and has no use for afterward: "
        "pump&#x27;s mint authority takes over the moment the instruction runs.</p>"
        "<p>The second is the same transaction /enroll sends for a coin with no "
        "fee config: pump&#x27;s <code>create_fee_sharing_config</code> then "
        "<code>update_fee_shares_v2</code>, which creates the config and sets "
        "the split. pump enforces it from then on, paying every row from the "
        "coin&#x27;s creator vault whenever the vault is distributed. The "
        "protocol&#x27;s crank asks pump to do that, and anyone else may too: "
        "the instruction has no signer.</p>"
        "<p>Both transactions are simulated against mainnet before your wallet "
        "is asked, and a simulation that reports an error is one you are never "
        "shown. If the first lands and the second does not, you have a normal "
        "pump coin whose one change is still unspent, and /enroll finishes the "
        "job. Nothing in this flow can spend the change on a split you did not "
        "see.</p>"
        "<p>Every payout to the incinerator is a burn a stranger can verify from "
        "the coin&#x27;s own transactions; the coin&#x27;s page here reads the "
        "split from the chain and says whether the coin is enrolled. The coin "
        "does not appear on this site by being created here. It appears the "
        "way every coin does: by being read.</p>"
        f'<p><a href="/enroll">Already launched on pump? Enroll instead</a>. '
        f'<a href="/{esc(site.VERIFY_FILENAME)}">Check any coin</a>.</p>'
        "</section>"
        "</main>"
        f'<p class="meta">generated at {esc(stamp)}</p>'
        f"<script>{_SCRIPT}</script>"
    )
    return site._document(
        "Launch a coin -- Charlie Protocol", body,
        style=site._INDEX_STYLE + _STYLE,
        description="Create a pump.fun coin with its creator fee split set from "
                    "the first block: a SOL burn, the protocol's share, and the "
                    "rest wherever you say. Two approvals, your key stays in your wallet.",
    )


def write(out_dir=site.DEFAULT_OUTPUT_DIR, *, now=None):
    path = Path(out_dir) / LAUNCH_FILENAME
    if path.exists() and site._is_authored(path):
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(now=now), encoding="utf-8")
    return path

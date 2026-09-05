"""The enroll page's own script, executed, against the API's own response shapes.

The defect this guards shipped through a full green suite: the API answered a
coin with no fee-sharing config with `admin: null` and a `reason` the page had
no branch for, so the browser fell through and told a dev "This wallet does
not administer that coin. Its config names null." Nothing in Python could see
it, because the bug was in a JavaScript branch inside a Python string.

So this runs the string. `node` is not a dependency of this project and the
suite still passes without it -- the test skips -- but where it exists, the
page's real branching is driven and read back.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import enroll_page  # noqa: E402

NODE = shutil.which("node")

# Skipping is right on a laptop without node and wrong in CI, where a green
# suite that tested none of the page's branching is the false confidence this
# file exists to remove. Set CHARLIE_REQUIRE_NODE=1 there and a missing node
# fails instead of skipping.
REQUIRE_NODE = os.environ.get("CHARLIE_REQUIRE_NODE") not in (None, "", "0")

# Loads the page script under vm with the smallest DOM it touches, runs
# inspect() against one canned response, and prints what the dev would read.
HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const source = fs.readFileSync(process.argv[2], 'utf8');
const response = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const els = {};
const rows = [];
const el = (id) => (els[id] = els[id] || {value: '', textContent: '', className: '', hidden: false, style: {},
                                           appendChild: (child) => { if (child.className && child.className.indexOf('share-row') === 0) { rows.push(child); } }});
el('mint').value = 'JAMXU2JLraZ3RUhbgc3ttYPc18Kx4ojCnC56XR2zpump';
// An element that keeps what is appended to it and what is set on it, so a
// share row can be read back: its inputs, and whether they are read-only.
function fakeElement() {
  const node = {style: {}, classList: {add: () => {}}, children: [], className: '', value: '',
                readOnly: false, disabled: false, textContent: ''};
  node.appendChild = (child) => { node.children.push(child); };
  node.querySelector = (sel) => node.children.find((c) => ('.' + c.className) === sel) || null;
  return node;
}
const sandbox = {
  document: {getElementById: el, addEventListener: () => {}, querySelectorAll: () => [],
             createElement: () => fakeElement()},
  window: {}, console, setTimeout, encodeURIComponent,
  fetch: async () => ({json: async () => response}),
};
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
sandbox.state.wallet = '9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM';
sandbox.inspect().then(() => {
  process.stdout.write(JSON.stringify({
    note: el('coinNote').textContent,
    kind: el('coinNote').className,
    formShown: el('splitBox').hidden === false,
    rows: rows.map((r) => ({
      address: r.querySelector('.share-addr').value,
      pct: String(r.querySelector('.share-bps').value),
      locked: r.className.indexOf('locked') !== -1 && r.querySelector('.share-addr').readOnly,
    })),
  }));
});
"""


def _response(**overrides) -> dict:
    """The shape `api/enroll.py` returns from its inspection path."""
    body = {
        "mint": "JAMXU2JLraZ3RUhbgc3ttYPc18Kx4ojCnC56XR2zpump",
        "config": "Chx6EJ1QLRnhiyQHfpNNyiEWma8XPazbELPanPff4Nuj",
        "admin": "Chx6EJ1QLRnhiyQHfpNNyiEWma8XPazbELPanPff4Nuj",
        "admin_revoked": False,
        "owns": True,
        "current": [{"address": "Chx6EJ1QLRnhiyQHfpNNyiEWma8XPazbELPanPff4Nuj", "bps": 10000}],
        "reason": None,
        "cashback": False,
        "graduated": False,
        "creator": None,
        "can_create": False,
        "toll": {"address": TOLL, "bps": 500},
    }
    body.update(overrides)
    return body


TOLL = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
CREATOR = "Chx6EJ1QLRnhiyQHfpNNyiEWma8XPazbELPanPff4Nuj"


class TestTheCautionIsNotStyledAsARefusal(unittest.TestCase):
    """The class name is pinned by the node tests above; the RULE is pinned
    here, and needs no node because the stylesheet is rendered by Python.

    Deleting `.note.caution` from the stylesheet left the whole suite green
    while the caution silently lost its styling. A name with no rule behind it
    is the same defect as the wrong name, one file over.
    """

    def setUp(self):
        self.page = enroll_page.render(now=1)

    def test_the_caution_rule_exists(self):
        self.assertIn(".note.caution", self.page)

    def test_the_caution_is_amber_rather_than_destructive(self):
        # `--unchecked` is the colour the checks already use for "not proven
        # either way", which is exactly what an unreadable cashback flag is.
        self.assertIn(".note.caution { color: var(--unchecked); }", self.page)
        self.assertNotIn(".note.caution { color: var(--destructive)", self.page)

    def test_no_note_kind_reuses_the_destructive_callout_name(self):
        # `say()` writes class="note <kind>", so a kind of `warn` would also
        # match the standalone `.warn` callout: red border, red ground, the
        # treatment a refusal wears.
        self.assertNotIn(".note.warn", self.page)
        self.assertNotIn("'warn');", self.page)


@unittest.skipUnless(NODE or REQUIRE_NODE,
                     "node is not installed; set CHARLIE_REQUIRE_NODE=1 to make that a failure")
class TestTheDevIsToldTheTruth(unittest.TestCase):
    def setUp(self):
        if not NODE:
            self.fail("CHARLIE_REQUIRE_NODE is set but node is not on PATH, so the "
                      "page script was never executed")

    def drive(self, response: dict) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "page.js").write_text(enroll_page._SCRIPT, encoding="utf-8")
            (root / "harness.js").write_text(HARNESS, encoding="utf-8")
            (root / "response.json").write_text(json.dumps(response), encoding="utf-8")
            done = subprocess.run(
                [NODE, str(root / "harness.js"), str(root / "page.js"), str(root / "response.json")],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            return json.loads(done.stdout)

    def test_the_page_script_parses_at_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "page.js"
            path.write_text(enroll_page._SCRIPT, encoding="utf-8")
            done = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_coin_with_no_config_is_never_told_it_is_not_theirs(self):
        out = self.drive(_response(config=None, admin=None, admin_revoked=None,
                                   owns=False, current=[], reason="no_sharing_config"))
        self.assertNotIn("null", out["note"])
        self.assertNotIn("does not administer", out["note"])
        self.assertIn("no pump fee-sharing config", out["note"])
        self.assertFalse(out["formShown"])

    def test_a_cashback_coin_is_stopped_with_its_reason(self):
        out = self.drive(_response(cashback=True))
        self.assertIn("Trader Cashback", out["note"])
        self.assertFalse(out["formShown"])

    def test_an_unreadable_cashback_flag_warns_but_lets_them_through(self):
        out = self.drive(_response(cashback=None))
        self.assertIn("absent is not the same as off", out["note"])
        self.assertTrue(out["formShown"])
        # `say()` writes class="note <kind>". The kind must NOT be `warn`,
        # because `.warn` is the destructive red callout used for the one-way
        # door notice: a caution the dev may act on would be dressed as a
        # refusal, with the form open underneath it.
        self.assertEqual(out["kind"], "note caution")
        self.assertNotIn("warn", out["kind"])

    def test_a_graduated_coin_is_told_its_fees_sit_somewhere_else(self):
        """The API had reported `graduated` for a week and no branch read it,
        so the one dev it matters to was told nothing. A graduated coin's
        creator fee accrues as wrapped SOL in an AMM vault, and a payout that
        does not first move it pays out zero.
        """
        out = self.drive(_response(graduated=True))
        self.assertIn("graduated to the pump AMM", out["note"])
        self.assertIn("moves nothing at all", out["note"])
        # It is a caution, not a refusal: the split can still be set, and
        # setting it is still the right thing to do.
        self.assertEqual(out["kind"], "note caution")
        self.assertTrue(out["formShown"])

    def test_an_ungraduated_coin_is_not_warned_about_the_amm(self):
        out = self.drive(_response(graduated=False))
        self.assertNotIn("AMM", out["note"])
        self.assertEqual(out["kind"], "note good")

    def test_both_cautions_are_said_once_each_rather_than_one_hiding_the_other(self):
        """Two independent facts about the same coin. The branch that returned
        early on the first would have silently dropped the second.
        """
        out = self.drive(_response(graduated=True, cashback=None))
        self.assertIn("absent is not the same as off", out["note"])
        self.assertIn("graduated to the pump AMM", out["note"])
        self.assertEqual(out["note"].count("You administer this coin"), 1)
        self.assertEqual(out["note"].count("Current split:"), 1)
        self.assertTrue(out["formShown"])

    def test_a_spent_one_shot_says_which_thing_is_spent(self):
        out = self.drive(_response(admin_revoked=True))
        self.assertIn("one change", out["note"])
        self.assertFalse(out["formShown"])

    def test_a_real_stranger_is_still_told_whose_coin_it_is(self):
        # The fix must not swallow the case the message was written for.
        other = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
        out = self.drive(_response(owns=False, admin=other))
        self.assertIn("does not administer", out["note"])
        self.assertIn(other, out["note"])
        self.assertFalse(out["formShown"])

    def test_an_ordinary_owned_coin_opens_the_form(self):
        out = self.drive(_response())
        self.assertIn("You administer this coin", out["note"])
        self.assertTrue(out["formShown"])

    # -- the create path ----------------------------------------------------
    def test_the_creator_of_a_config_less_coin_is_let_through_to_create_it(self):
        """The ~95% case. It used to be a dead end that sent the dev to pump
        to make a config by hand; now one signature creates it and sets the
        split, and the page says so."""
        out = self.drive(_response(config=None, admin=None, admin_revoked=None, owns=False,
                                   current=[], reason="no_sharing_config",
                                   creator=CREATOR, can_create=True))
        self.assertIn("One signature will create the config", out["note"])
        self.assertIn(CREATOR, out["note"])
        self.assertEqual(out["kind"], "note good")
        self.assertTrue(out["formShown"])

    def test_someone_else_is_told_which_wallet_can(self):
        out = self.drive(_response(config=None, admin=None, admin_revoked=None, owns=False,
                                   current=[], reason="no_sharing_config",
                                   creator=CREATOR, can_create=False))
        self.assertIn("only the wallet that launched it", out["note"])
        self.assertIn(CREATOR, out["note"])
        self.assertFalse(out["formShown"])

    def test_the_toll_row_is_first_locked_and_at_the_server_s_rate(self):
        out = self.drive(_response())
        self.assertGreaterEqual(len(out["rows"]), 3)
        first = out["rows"][0]
        self.assertEqual(first["address"], TOLL)
        self.assertEqual(first["pct"], "5")
        self.assertTrue(first["locked"])
        # And nothing else is locked: the rest is the dev's.
        self.assertTrue(all(not r["locked"] for r in out["rows"][1:]))

    def test_the_defaults_total_one_hundred_with_the_connected_wallet_last(self):
        out = self.drive(_response())
        self.assertAlmostEqual(sum(float(r["pct"]) for r in out["rows"]), 100.0)
        self.assertEqual(out["rows"][-1]["address"], "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM")

    def test_no_toll_address_means_no_form(self):
        """Shipped default until the address is set. The server refuses to
        build in this state; the page must not offer a form that ends in
        that refusal."""
        out = self.drive(_response(toll={"address": None, "bps": 500}))
        self.assertIn("not open yet", out["note"])
        self.assertFalse(out["formShown"])

    def test_a_server_error_is_shown_as_one(self):
        out = self.drive({"error": "Could not read the chain just now. Try again in a moment."})
        self.assertIn("Could not read the chain", out["note"])
        self.assertFalse(out["formShown"])


if __name__ == "__main__":
    unittest.main()

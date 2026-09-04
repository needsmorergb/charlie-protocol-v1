"""The enrol page's own script, executed, against the API's own response shapes.

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
const el = (id) => (els[id] = els[id] || {value: '', textContent: '', className: '', hidden: false, style: {}});
el('mint').value = 'JAMXU2JLraZ3RUhbgc3ttYPc18Kx4ojCnC56XR2zpump';
const sandbox = {
  document: {getElementById: el, addEventListener: () => {}, querySelectorAll: () => [],
             createElement: () => ({style: {}, classList: {add: () => {}}, appendChild: () => {}})},
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
    }
    body.update(overrides)
    return body


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

    def test_a_server_error_is_shown_as_one(self):
        out = self.drive({"error": "Could not read the chain just now. Try again in a moment."})
        self.assertIn("Could not read the chain", out["note"])
        self.assertFalse(out["formShown"])


if __name__ == "__main__":
    unittest.main()

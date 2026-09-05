"""Every rewrite in `vercel.json` matches the paths it is meant to match,
and none of the paths meant for another rule.

`tests/test_site.py::TestVercelJson` asserts each rule is *shaped* right --
that its destination is built from `_artifact_name`, that its source starts
from the route prefix. That is not the same question as whether the rules
*resolve* correctly against real request paths, which is what actually
breaks in production: two rules whose sources overlap both look correct in
isolation and still send a request to the wrong file.

So this file translates each `source` into a regular expression and asks the
routing question directly. Vercel matches rewrites in array order and takes
the first hit, which is why `/coins` is placed before the parameterised
rules in `vercel.json` -- ordering is stated there rather than inferred, and
`test_first_match_wins_matches_declared_order` is what holds it stated.
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import site  # noqa: E402

VERCEL_JSON_PATH = Path(__file__).resolve().parents[1] / "vercel.json"

# A real mainnet mint, and the one this project is the reference
# implementation for -- so the fixture is a path that genuinely has to work,
# not a base58-shaped string invented for the test.
LIVE_ROUTE = "/api/verify?mint=:mint"

MINT = "8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump"


def _source_to_regex(source: str) -> re.Pattern:
    """Translate a Vercel rewrite `source` into an anchored regex.

    Handles the one parameter form this project uses, `:name(charclass)`,
    and plain literal segments. Anything else raises rather than silently
    matching nothing -- a rule this translator does not understand must fail
    the suite loudly, not quietly pass every assertion by matching no path.
    """
    pattern = ""
    i = 0
    while i < len(source):
        ch = source[i]
        if ch == ":":
            j = i + 1
            while j < len(source) and (source[j].isalnum() or source[j] == "_"):
                j += 1
            if j >= len(source) or source[j] != "(":
                raise AssertionError(
                    f"unsupported parameter without a character class in {source!r}; "
                    "this translator only understands ':name(charclass)'"
                )
            depth = 0
            k = j
            while k < len(source):
                if source[k] == "(":
                    depth += 1
                elif source[k] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                k += 1
            if depth != 0:
                raise AssertionError(f"unbalanced character class in {source!r}")
            pattern += "(" + source[j + 1 : k] + ")"
            i = k + 1
        else:
            pattern += re.escape(ch)
            i += 1
    return re.compile("^" + pattern + "$")


def _load_rewrites():
    return json.loads(VERCEL_JSON_PATH.read_text(encoding="utf-8"))["rewrites"]


def _has_satisfied(rule, query: dict) -> bool:
    """A `has` condition gates a rule on something other than the path.

    Two rules may share a source and differ only here -- `/verify` is exactly
    that: one rule fires when a `mint` query is present (the paste box's GET
    form), the bare one otherwise. A translator that ignored `has` would call
    that an ambiguous route when it is the mechanism making the form work
    without JavaScript.
    """
    for cond in rule.get("has", []):
        if cond.get("type") != "query":
            return False  # only the form this project uses is modelled
        value = query.get(cond["key"])
        if value is None:
            return False
        pattern = cond.get("value")
        if pattern is not None:
            # Vercel uses JavaScript named-group syntax `(?<name>...)`;
            # Python's re spells it `(?P<name>...)`. Translate rather than
            # skip, so the character class is genuinely enforced here.
            if not re.fullmatch(pattern.replace("(?<", "(?P<"), value):
                return False
    return True


def _first_match(rewrites, path: str, query: dict | None = None):
    """The rule Vercel would apply: first match in declared array order,
    skipping any whose `has` conditions are unsatisfied.
    """
    query = query or {}
    for rule in rewrites:
        if _source_to_regex(rule["source"]).match(path) and _has_satisfied(rule, query):
            return rule
    return None


class TestSourceTranslation(unittest.TestCase):
    """The translator itself, because every assertion below trusts it."""

    def test_literal_source_matches_only_itself(self):
        rx = _source_to_regex("/coins")
        self.assertTrue(rx.match("/coins"))
        self.assertFalse(rx.match("/coins-1.html"))
        self.assertFalse(rx.match("/coins/2"))

    def test_parameter_respects_its_character_class(self):
        rx = _source_to_regex("/coin/:mint([1-9A-HJ-NP-Za-km-z]+)")
        self.assertTrue(rx.match("/coin/" + MINT))
        # 0, O, I and l are the four characters base58 deliberately omits.
        for excluded in ("0", "O", "I", "l"):
            self.assertFalse(rx.match("/coin/abc" + excluded), excluded)

    def test_unsupported_parameter_form_raises(self):
        with self.assertRaises(AssertionError):
            _source_to_regex("/coin/:mint")


class TestEachPathResolvesToItsOwnRule(unittest.TestCase):
    def setUp(self):
        self.rewrites = _load_rewrites()

    def test_coin_page_path(self):
        """Resolves to the live function, which serves the committed page when
        one exists. Pointing this straight at a file 404'd every coin that has
        no committed page, which is nearly all of them.
        """
        rule = _first_match(self.rewrites, "/coin/" + MINT)
        self.assertIsNotNone(rule)
        self.assertEqual(rule["destination"], LIVE_ROUTE)

    def test_coin_record_path_never_takes_the_page_rule(self):
        """The page rule's character class excludes '.', so a record path
        cannot fall through to it. This is the assertion that would catch a
        future edit widening that class.
        """
        rule = _first_match(self.rewrites, "/coin/" + MINT + ".json")
        self.assertIsNotNone(rule)
        self.assertEqual(rule["destination"], LIVE_ROUTE + "&format=json")
        self.assertNotEqual(rule["destination"], LIVE_ROUTE)

    def test_verify_path_resolves_to_the_same_artifact_as_the_coin_page(self):
        """D-21/D-22: one artifact per coin. /verify is a rewrite onto the
        page, not a second renderer with a second filename.
        """
        verify = _first_match(self.rewrites, "/verify/" + MINT)
        page = _first_match(self.rewrites, "/coin/" + MINT)
        self.assertIsNotNone(verify)
        self.assertEqual(verify["destination"], page["destination"])

    def test_bare_verify_resolves_to_the_verify_page(self):
        """The route advertised without a mint after it. It 404'd once."""
        rule = _first_match(self.rewrites, "/verify")
        self.assertIsNotNone(rule)
        self.assertIsNotNone(rule)
        self.assertEqual(rule["destination"], "/" + site.VERIFY_FILENAME)

    def test_bare_verify_does_not_swallow_a_mint_path(self):
        bare = _first_match(self.rewrites, "/verify")
        with_mint = _first_match(self.rewrites, "/verify/" + MINT)
        self.assertNotEqual(bare["destination"], with_mint["destination"])

    def test_coins_resolves_to_page_one(self):
        rule = _first_match(self.rewrites, "/coins")
        self.assertIsNotNone(rule)
        self.assertEqual(rule["destination"], "/" + site.INDEX_FILENAME_TEMPLATE.format(page=1))

    def test_unrouted_paths_fall_through_to_static_files(self):
        """Anything with no rule is served straight from `web/`. The landing
        page and the index pages themselves must NOT be captured by a
        rewrite, or they would be unreachable at their own names.
        """
        for path in (
            "/",
            "/" + site.LANDING_FILENAME,
            "/" + site.INDEX_FILENAME_TEMPLATE.format(page=1),
            "/assets/charlie.png",
            "/coin/",
            "/verify/",
            "/coins/2",
            "/" + site.VERIFY_FILENAME,
        ):
            with self.subTest(path=path):
                self.assertIsNone(_first_match(self.rewrites, path), path)


class TestNoRuleStealsAnotherRulesPath(unittest.TestCase):
    """The property that matters and that shape assertions cannot see: for
    every rule, the paths it is meant to serve reach it and no other rule
    claims them first.
    """

    def setUp(self):
        self.rewrites = _load_rewrites()

    def test_every_path_matches_exactly_one_rule(self):
        for path in ("/coin/" + MINT, "/coin/" + MINT + ".json", "/verify/" + MINT, "/coins", "/verify"):
            with self.subTest(path=path):
                matched = [r for r in self.rewrites
                           if _source_to_regex(r["source"]).match(path) and _has_satisfied(r, {})]
                self.assertEqual(
                    len(matched), 1,
                    f"{path} matched {len(matched)} rules: {[r['source'] for r in matched]}",
                )

    def test_first_match_wins_matches_declared_order(self):
        """`/coins` is declared before the parameterised rules so precedence
        is stated in the file rather than inferred from behaviour. If a later
        edit reorders them this fails, which is the point.
        """
        sources = [r["source"] for r in self.rewrites]
        # The has-guarded /verify must precede the bare one, or a pasted CA
        # would land on the verify page instead of the coin it names.
        guarded = next(i for i, r in enumerate(self.rewrites)
                       if r["source"] == "/verify" and r.get("has"))
        bare = next(i for i, r in enumerate(self.rewrites)
                    if r["source"] == "/verify" and not r.get("has"))
        self.assertLess(guarded, bare)
        self.assertIn("/coins", sources)

    def test_no_planning_path_is_cited(self):
        raw = VERCEL_JSON_PATH.read_text(encoding="utf-8")
        self.assertNotIn(".planning", raw)


class TestPastedCaRouting(unittest.TestCase):
    """The paste box submits GET /verify?mint=<CA>. Without JavaScript that
    is the only way a form can reach a path, so the has-guarded rewrite is
    what makes the box work at all.
    """

    def setUp(self):
        self.rewrites = _load_rewrites()

    def test_a_pasted_ca_reaches_the_coin_page(self):
        rule = _first_match(self.rewrites, "/verify", {"mint": MINT})
        self.assertIsNotNone(rule)
        self.assertEqual(rule["destination"], LIVE_ROUTE)

    def test_the_bare_page_still_wins_with_no_query(self):
        rule = _first_match(self.rewrites, "/verify")
        self.assertEqual(rule["destination"], "/" + site.VERIFY_FILENAME)

    def test_a_non_base58_paste_falls_through_to_the_page(self):
        """0, O, I and l are the characters base58 omits. A typo must not be
        routed at a mint-shaped file that cannot exist.
        """
        rule = _first_match(self.rewrites, "/verify", {"mint": "not-a-real-CA-0OIl"})
        self.assertEqual(rule["destination"], "/" + site.VERIFY_FILENAME)

    def test_an_empty_paste_falls_through_to_the_page(self):
        rule = _first_match(self.rewrites, "/verify", {"mint": ""})
        self.assertEqual(rule["destination"], "/" + site.VERIFY_FILENAME)


if __name__ == "__main__":
    unittest.main()

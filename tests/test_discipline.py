"""Static guards over the `indexer/` package, and one over `tests/` itself.

`python -m unittest discover -s tests -t tests -p "test_discipline.py"`.

1. Every module under `indexer/` imports only the standard library or another
   `indexer` submodule -- the project's standard-library-only constraint,
   enforced by a test rather than by discipline, and the phase's
   supply-chain control (T-01-SC): no package-manager install occurs
   anywhere in this phase.
2. Every SQL statement this package builds out of anything but a plain string
   literal is named in an allowlist, with the reason it is safe -- values from
   RPC responses reach SQL exclusively through `?` placeholders (T-01-02).
   Three attempts at deciding this by analysis were each got past; the reason
   the allowlist replaced them is written above it.
3. Every test module keeps `unittest.main()` last. Four tests were once
   appended after it, so running that file directly loaded 24 of 28 and said
   OK; discovery found all 28, which is precisely why nobody noticed.
"""

from __future__ import annotations

import ast
import sys
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

INDEXER_DIR = Path(__file__).resolve().parents[1] / "indexer"


def _module_files():
    return sorted(p for p in INDEXER_DIR.glob("*.py") if p.name != "__pycache__")


class TestStdlibOnlyImports(unittest.TestCase):
    def test_every_top_level_import_is_stdlib_or_indexer(self):
        stdlib = set(sys.stdlib_module_names)
        offenders = []
        for path in _module_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    if node.level and node.level > 0:
                        continue  # relative import -- an indexer submodule by construction
                    if node.module:
                        names = [node.module.split(".")[0]]
                for name in names:
                    if name == "indexer" or name in stdlib:
                        continue
                    offenders.append(f"{path.name}: imports '{name}'")
        self.assertEqual(offenders, [], "non-stdlib, non-indexer import(s) found: " + "; ".join(offenders))


# -- SQL statements: an allowlist of sites, not an analysis of names -------
#
# Three attempts at this were a dataflow analysis: given `f"...{name}..."`,
# decide whether `name` can hold something that came off the chain. Each was
# got past, and the third was got past through its own anchor -- it matched
# `for x, y in EXPORT_TABLES:` by the SPELLING of `EXPORT_TABLES`, so a local
# variable of that name, bound to anything at all, minted safe identifiers.
#
# The premise was wrong. This package builds five interpolated statements and
# five non-literal ones, in two modules, and that is the whole of it. Proving
# a general property of names is a much harder problem than the code poses,
# and every version of it that was subtly wrong said "safe" rather than
# "unsure", which is the wrong way for a security check to fail.
#
# So: every site is named here, with the reason it is safe, and the test is
# that the code's sites and this list are the same set. A new interpolation
# anywhere fails until somebody writes down why it is allowed -- which is the
# review this is for. No spelling, no scope, no resolution: a site is either
# on the list or it is not.

# argument 0 of an execute-family call that is not a plain string literal.
PERMITTED_STATEMENTS = {
    ("evidence.py", "current_sharing_configs", "query"): (
        "built in the four lines above from string literals only -- the two "
        "optional clauses are literals and their VALUES are bound through ? "
        "into `params`. Pinned by TestTheAllowlistedSitesStillLookLikeThemselves."
    ),
    ("evidence.py", "submissions", "self._SELECT_ALL"):
        "a string literal defined on the class a few lines above, with no "
        "interpolation of any kind in it",
    ("evidence.py", "submissions", "self._SELECT_BY_REPO"):
        "the same literal with a ? for the repo, whose value is bound, not "
        "interpolated",
    ("evidence.py", "submissions", "self._SELECT_BY_MINT"):
        "the same literal with a ? for the mint, whose value is bound, not "
        "interpolated",
    ("evidence.py", "submissions", "self._SELECT_BY_REPO_AND_MINT"):
        "the same literal with a ? for each, both bound rather than "
        "interpolated",
}

# Each `{...}` inside an f-string handed to an execute-family call.
PERMITTED_INTERPOLATIONS = {
    ("export.py", "export_table", "table"): (
        "the table name from EXPORT_TABLES, which is a module-level literal "
        "tuple in export.py. Nothing else in that module binds `table`."
    ),
    ("export.py", "export_table", "order_by"): (
        "the ORDER BY from the same literal tuple, same reasoning."
    ),
    ("export.py", "import_table", "table"): (
        "the same table name, reaching PRAGMA table_info and INSERT OR IGNORE."
    ),
    ("export.py", "import_table", "placeholders"): (
        "a run of `?` and commas, assigned one line above from the table's own "
        "column count. It carries no identifier and cannot: pinned by "
        "TestTheAllowlistedSitesStillLookLikeThemselves."
    ),
}

SQL_METHODS = ("execute", "executemany", "executescript")


def _sql_sites(source: str, filename: str) -> tuple:
    """`(statements, interpolations)` for one module.

    Each is a set of `(filename, enclosing function, expression source)`.
    A statement site is an execute-family call whose first argument is not a
    plain string literal; an interpolation site is one `{...}` inside an
    f-string that is.
    """
    statements, interpolations = set(), set()

    def walk(node, function):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            function = getattr(node, "name", "<lambda>")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in SQL_METHODS and node.args:
            first = node.args[0]
            if isinstance(first, ast.JoinedStr):
                for piece in first.values:
                    if isinstance(piece, ast.FormattedValue):
                        interpolations.add((filename, function, ast.unparse(piece.value)))
            elif not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                statements.add((filename, function, ast.unparse(first)))
        for child in ast.iter_child_nodes(node):
            walk(child, function)

    walk(ast.parse(source, filename=filename), "<module>")
    return statements, interpolations


def sql_sites(sources: dict) -> tuple:
    statements, interpolations = set(), set()
    for filename, text in sources.items():
        found_statements, found_interpolations = _sql_sites(text, filename)
        statements |= found_statements
        interpolations |= found_interpolations
    return statements, interpolations


def package_sources() -> dict:
    return {path.name: path.read_text(encoding="utf-8") for path in _module_files()}


class TestEverySqlSiteIsOnTheAllowlist(unittest.TestCase):
    """The package's SQL sites and the allowlist above are the same set.

    Both directions on purpose. A site the list does not name is a statement
    nobody reviewed; an entry naming a site that no longer exists is a reason
    that has outlived what it justified, and it is what lets a list rot into
    something nobody trusts.
    """

    def setUp(self):
        self.statements, self.interpolations = sql_sites(package_sources())

    def test_every_interpolated_value_is_named_and_justified(self):
        self.assertEqual(self.interpolations, set(PERMITTED_INTERPOLATIONS))

    def test_every_statement_that_is_not_a_literal_is_named_and_justified(self):
        self.assertEqual(self.statements, set(PERMITTED_STATEMENTS))

    def test_every_reason_says_something(self):
        for key, reason in {**PERMITTED_STATEMENTS, **PERMITTED_INTERPOLATIONS}.items():
            with self.subTest(site=key):
                self.assertGreater(len(reason), 30, key)

    def test_no_percent_formatting_reaches_a_sql_call(self):
        offenders = []
        for filename, source in package_sources().items():
            for node in ast.walk(ast.parse(source, filename=filename)):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in SQL_METHODS and node.args
                        and isinstance(node.args[0], ast.BinOp)
                        and isinstance(node.args[0].op, ast.Mod)):
                    offenders.append(f"{filename}: %-formatting in a SQL call")
        self.assertEqual(offenders, [], "; ".join(offenders))


def _binds(target, name: str) -> bool:
    """Does this assignment target bind `name`?

    Only the positions that actually bind: a bare name, or one inside a
    tuple, list or star. NOT a name inside a subscript or an attribute --
    `loaded[table] = ...` reads `table`, it does not bind it, and counting
    that as a binding made the check below refuse correct code.
    """
    if isinstance(target, ast.Name):
        return target.id == name
    if isinstance(target, (ast.Tuple, ast.List)):
        return any(_binds(element, name) for element in target.elts)
    if isinstance(target, ast.Starred):
        return _binds(target.value, name)
    return False


class TestTheAllowlistedSitesStillLookLikeThemselves(unittest.TestCase):
    """The allowlist says a site is safe FOR A REASON. These check the reason.

    Without them the list degrades into a list of places the check has been
    told not to look: `table` could stop coming from EXPORT_TABLES, or
    `placeholders` stop being a run of `?`, and the entry would still name the
    site and still say why it used to be safe.
    """

    def module(self, name: str) -> ast.AST:
        return ast.parse((INDEXER_DIR / name).read_text(encoding="utf-8"), filename=name)

    def bindings_of(self, tree, name: str) -> list:
        """Every binding of `name` anywhere in the module, in any form."""
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if _binds(target, name):
                        found.append(("assign", node))
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
                if isinstance(node.target, ast.Name) and node.target.id == name:
                    found.append(("assign", node))
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                if _binds(node.target, name):
                    found.append(("for", node))
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars is not None and _binds(item.optional_vars, name):
                        found.append(("with", node))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                arguments = node.args
                for arg in (list(arguments.posonlyargs) + list(arguments.args)
                            + list(arguments.kwonlyargs)
                            + [a for a in (arguments.vararg, arguments.kwarg) if a]):
                    if arg.arg == name:
                        found.append(("parameter", node))
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    if (alias.asname or alias.name.split(".")[0]) == name:
                        found.append(("import", node))
        return found

    def test_export_tables_is_a_literal_tuple_in_export_py(self):
        """The anchor. The analysis this replaced matched the NAME
        `EXPORT_TABLES` wherever it appeared, so a local variable spelled that
        way minted safe identifiers out of anything. Here it has to be the
        module's own literal.
        """
        tree = self.module("export.py")
        bindings = self.bindings_of(tree, "EXPORT_TABLES")
        self.assertEqual(len(bindings), 1, f"EXPORT_TABLES is bound {len(bindings)} times")
        kind, node = bindings[0]
        self.assertEqual(kind, "assign")
        value = node.value
        self.assertIsInstance(value, ast.Tuple)
        for entry in value.elts:
            self.assertIsInstance(entry, ast.Tuple)
            for part in entry.elts:
                self.assertIsInstance(part, ast.Constant)
                self.assertIsInstance(part.value, str)

    def test_table_and_order_by_come_only_from_that_tuple(self):
        """Every binding of either name in export.py, in any form: a loop over
        EXPORT_TABLES, or a parameter of the two functions those loops call.
        """
        tree = self.module("export.py")
        callers = {"export_all", "import_all"}
        for name in ("table", "order_by"):
            bindings = self.bindings_of(tree, name)
            self.assertTrue(bindings, name)
            for kind, node in bindings:
                with self.subTest(name=name, kind=kind):
                    if kind == "for":
                        self.assertIsInstance(node.iter, ast.Name)
                        self.assertEqual(node.iter.id, "EXPORT_TABLES")
                    elif kind == "parameter":
                        self.assertIn(node.name, {"export_table", "import_table"} | callers)
                    else:
                        self.fail(f"{name} is bound by {kind}, which nothing vouches for")

    def test_the_two_functions_are_called_only_by_those_loops(self):
        """A parameter is only as good as its callers. `export_table` and
        `import_table` are called from `export_all`/`import_all`, inside the
        loop, and nowhere else in the package.
        """
        offenders = []
        for filename, source in package_sources().items():
            tree = ast.parse(source, filename=filename)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                called = (node.func.id if isinstance(node.func, ast.Name)
                          else node.func.attr if isinstance(node.func, ast.Attribute) else None)
                if called not in ("export_table", "import_table"):
                    continue
                if filename != "export.py":
                    offenders.append(f"{filename} calls {called}")
                    continue
                second = node.args[1] if len(node.args) > 1 else None
                if not (isinstance(second, ast.Name) and second.id == "table"):
                    offenders.append(f"{filename}: {called} passed {ast.unparse(second)}")
        self.assertEqual(offenders, [], "; ".join(offenders))

    def test_placeholders_is_a_run_of_question_marks(self):
        """`",".join("?" for _ in known)`, and nothing else, anywhere in the
        module. Not a joined column list, which is what the first version of
        `import_table` interpolated and this guard refused."""
        tree = self.module("export.py")
        bindings = self.bindings_of(tree, "placeholders")
        self.assertEqual(len(bindings), 1)
        kind, node = bindings[0]
        self.assertEqual(kind, "assign")
        call = node.value
        self.assertIsInstance(call, ast.Call)
        self.assertIsInstance(call.func, ast.Attribute)
        self.assertEqual(call.func.attr, "join")
        self.assertIsInstance(call.func.value, ast.Constant)
        self.assertEqual(set(call.func.value.value) - {",", " "}, set())
        self.assertEqual(len(call.args), 1)
        joined = call.args[0]
        self.assertIsInstance(joined, (ast.GeneratorExp, ast.ListComp))
        self.assertIsInstance(joined.elt, ast.Constant)
        self.assertEqual(joined.elt.value, "?")

    def test_the_sharing_config_query_is_built_from_literals(self):
        """`query` is concatenation and a join, and every piece of both has to
        be a string literal. A value reaches it only through `?`."""
        tree = self.module("evidence.py")
        bindings = self.bindings_of(tree, "query")
        self.assertEqual(len(bindings), 1)
        _kind, node = bindings[0]
        for piece in ast.walk(node.value):
            if isinstance(piece, ast.Name):
                # The only name allowed is the list of clauses, and every
                # append to it is checked below.
                self.assertEqual(piece.id, "clauses", ast.unparse(node.value))
        appended = []
        for element in ast.walk(tree):
            if (isinstance(element, ast.Call) and isinstance(element.func, ast.Attribute)
                    and element.func.attr == "append"
                    and isinstance(element.func.value, ast.Name)
                    and element.func.value.id == "clauses"):
                appended.append(element.args[0])
        clauses_literal = [n for n in ast.walk(tree)
                           if isinstance(n, ast.Assign)
                           and any(isinstance(t, ast.Name) and t.id == "clauses"
                                   for t in n.targets)]
        self.assertEqual(len(clauses_literal), 1)
        for element in clauses_literal[0].value.elts + appended:
            self.assertIsInstance(element, ast.Constant)
            self.assertIsInstance(element.value, str)


class TestTheAllowlistNoticesANewSite(unittest.TestCase):
    """The guard, driven against source handed to it.

    Every probe below is one that got past a previous version of this check.
    They are all the same shape now -- a site that is not on the list -- which
    is the point: the analysis that had to tell them apart is gone.
    """

    def sites(self, source: str) -> tuple:
        return sql_sites({"probe.py": textwrap.dedent(source)})

    def assertNotAllowlisted(self, source: str):
        statements, interpolations = self.sites(source)
        unknown = ((statements - set(PERMITTED_STATEMENTS))
                   | (interpolations - set(PERMITTED_INTERPOLATIONS)))
        self.assertNotEqual(unknown, set(), "the guard saw nothing to review")

    def test_a_shadowed_export_tables(self):
        # This walked through the previous version: the loop was matched by
        # the spelling of the name it iterated.
        self.assertNotAllowlisted("""
            def probe(conn, attacker_supplied):
                EXPORT_TABLES = attacker_supplied
                for table, order_by in EXPORT_TABLES:
                    conn.execute(f"SELECT * FROM {table} ORDER BY {order_by}")
        """)

    def test_an_impostor_function_borrowing_a_real_one_s_call_sites(self):
        self.assertNotAllowlisted("""
            def export_table(conn, table, order_by, path=None):
                conn.execute(f"SELECT * FROM {table} ORDER BY {order_by}")
        """)

    def test_a_lambda_parameter(self):
        self.assertNotAllowlisted(
            'probe = lambda conn, placeholders: conn.execute(f"SELECT * FROM {placeholders}")'
        )

    def test_a_match_capture(self):
        self.assertNotAllowlisted("""
            def probe(conn, evil):
                match evil:
                    case placeholders:
                        conn.execute(f"SELECT * FROM {placeholders}")
        """)

    def test_a_locally_shadowed_table_name(self):
        self.assertNotAllowlisted("""
            def probe(conn, evil):
                table = evil
                conn.execute(f"SELECT * FROM {table}")
        """)

    def test_a_statement_built_first_and_passed_as_a_name(self):
        """Every previous version was blind to this: it only ever looked at
        f-strings written inline, so building the string a line earlier
        stepped around the whole check.
        """
        self.assertNotAllowlisted("""
            def probe(conn, evil):
                sql = f"SELECT * FROM {evil}"
                conn.execute(sql)
        """)

    def test_string_concatenation(self):
        self.assertNotAllowlisted("""
            def probe(conn, evil):
                conn.execute("SELECT * FROM " + evil)
        """)

    def test_str_format(self):
        self.assertNotAllowlisted("""
            def probe(conn, evil):
                conn.execute("SELECT * FROM {}".format(evil))
        """)

    def test_percent_formatting(self):
        self.assertNotAllowlisted("""
            def probe(conn, evil):
                conn.execute("SELECT * FROM %s" % evil)
        """)

    def test_executescript(self):
        self.assertNotAllowlisted("""
            def probe(conn, evil):
                conn.executescript(f"DROP TABLE {evil}")
        """)

    def test_a_cursor_rather_than_the_connection(self):
        self.assertNotAllowlisted("""
            def probe(conn, evil):
                cursor = conn.cursor()
                cursor.execute(f"SELECT * FROM {evil}")
        """)

    def test_a_plain_literal_is_not_a_site_at_all(self):
        statements, interpolations = self.sites("""
            def probe(conn, mint):
                conn.execute("SELECT * FROM burn_event WHERE mint = ?", (mint,))
        """)
        self.assertEqual(statements | interpolations, set())


class TestTheMainGuardIsLast(unittest.TestCase):
    """A test that runs under discovery but not when its own file is executed
    is a test whose absence looks like success."""

    def test_no_test_module_defines_anything_after_unittest_main(self):
        offenders = []
        for path in sorted(Path(__file__).resolve().parent.glob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            guard = None
            for index, node in enumerate(tree.body):
                if (isinstance(node, ast.If)
                        and isinstance(node.test, ast.Compare)
                        and isinstance(node.test.left, ast.Name)
                        and node.test.left.id == "__name__"):
                    guard = index
            if guard is not None and guard != len(tree.body) - 1:
                after = [type(n).__name__ for n in tree.body[guard + 1:]]
                offenders.append(f"{path.name}: {after} defined after `unittest.main()`")
        self.assertEqual(offenders, [], "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()

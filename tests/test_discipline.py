"""Static guards over the `indexer/` package, and one over `tests/` itself.

`python -m unittest discover -s tests -t tests -p "test_discipline.py"`.

1. Every module under `indexer/` imports only the standard library or another
   `indexer` submodule -- the project's standard-library-only constraint,
   enforced by a test rather than by discipline, and the phase's
   supply-chain control (T-01-SC): no package-manager install occurs
   anywhere in this phase.
2. Every SQL string passed to `execute`/`executemany` interpolates only
   identifiers that resolve to `export.EXPORT_TABLES` -- values from RPC
   responses reach SQL exclusively through `?` placeholders (T-01-02).
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


# -- the SQL identifier analysis -------------------------------------------
#
# What it answers: for one `f"...{name}..."` handed to `execute`, is `name`
# provably an identifier this repository wrote, rather than one that came off
# the chain or out of a file?
#
# It is SCOPE-AWARE, and that is the whole of it. A first version answered per
# NAME, package-wide: `export.py` legitimately loops `for table, order_by in
# EXPORT_TABLES:`, which marked the names `table` and `order_by` safe in every
# module, so `table = whatever_a_stranger_sent` three files away interpolated
# clean. `table` is the most natural name in this codebase for a table
# identifier, so that was not only reachable adversarially -- it was the
# spelling an accident would use. Widening it once more for a placeholder run
# made it worse. A name is now resolved where it is used, against the bindings
# of the scope that actually binds it.

# What a binding tells us about the value.
SAFE_PLACEHOLDER = "placeholder run"       # x = ",".join("?" for _ in ...)
SAFE_EXPORT_LOOP = "EXPORT_TABLES loop"    # for x, y in EXPORT_TABLES:
OPAQUE = "opaque"                          # anything this cannot see into


class _Param:
    """A function parameter, resolved from its call sites rather than guessed.

    Keyed by the function's name and the parameter's position, never by the
    parameter's name: the name is what the package-wide version got wrong.
    """

    __slots__ = ("function", "index")

    def __init__(self, function: str, index: int):
        self.function = function
        self.index = index

    def key(self):
        return (self.function, self.index)


class _Scope:
    """One name-binding region: a module, a function, a lambda, a
    comprehension. `bindings` maps a name to every binding of it HERE."""

    def __init__(self, parent=None, function=None):
        self.parent = parent
        self.function = function
        self.bindings: dict = {}

    def bind(self, name: str, kind) -> None:
        self.bindings.setdefault(name, []).append(kind)

    def resolve(self, name: str):
        """Every binding of `name` in the innermost scope that binds it.

        `None` when nothing in this module binds it at all -- a global from
        somewhere else, which is never safe.
        """
        scope = self
        while scope is not None:
            if name in scope.bindings:
                return scope.bindings[name]
            scope = scope.parent
        return None


def _collect_scopes(tree, filename: str) -> tuple:
    """`(module scope, {node: scope})` for one module.

    Every binding form the language has reaches `bind`, and the ones this
    cannot see the value of bind `OPAQUE`. The list is deliberately
    exhaustive rather than "the forms this codebase uses": a form left out is
    not a false positive, it is a silent pass, which is exactly how the
    package-wide version was got past with a lambda parameter and a `match`
    capture.
    """
    module = _Scope()
    scope_of = {}

    def targets(node, scope, kind):
        if isinstance(node, ast.Name):
            scope.bind(node.id, kind)
        elif isinstance(node, (ast.Tuple, ast.List)):
            for element in node.elts:
                targets(element, scope, OPAQUE)
        elif isinstance(node, ast.Starred):
            targets(node.value, scope, OPAQUE)

    def parameters(args, scope, function):
        every = (list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
                 + [a for a in (args.vararg, args.kwarg) if a is not None])
        positional = list(args.posonlyargs) + list(args.args)
        for arg in every:
            index = positional.index(arg) if arg in positional else -1
            scope.bind(arg.arg, _Param(function, index) if function and index >= 0 else OPAQUE)

    def walk(node, scope):
        scope_of[node] = scope

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope.bind(node.name, OPAQUE)
            inner = _Scope(scope, function=node.name)
            parameters(node.args, inner, node.name)
            for default in node.args.defaults + [d for d in node.args.kw_defaults if d]:
                walk(default, scope)
            for decorator in node.decorator_list:
                walk(decorator, scope)
            for child in node.body:
                walk(child, inner)
            return

        if isinstance(node, ast.Lambda):
            inner = _Scope(scope)
            parameters(node.args, inner, None)
            walk(node.body, inner)
            return

        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            inner = _Scope(scope)
            for generator in node.generators:
                targets(generator.target, inner, OPAQUE)
                walk(generator.iter, scope)
                for condition in generator.ifs:
                    walk(condition, inner)
            for child in ([node.key, node.value] if isinstance(node, ast.DictComp)
                          else [node.elt]):
                walk(child, inner)
            return

        if isinstance(node, ast.ClassDef):
            scope.bind(node.name, OPAQUE)
            inner = _Scope(scope)
            for child in node.body:
                walk(child, inner)
            return

        if isinstance(node, ast.Assign):
            single = len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
            kind = SAFE_PLACEHOLDER if (single and _is_placeholder_run(node.value)) else OPAQUE
            for target in node.targets:
                targets(target, scope, kind)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets(node.target, scope, OPAQUE)
        elif isinstance(node, ast.NamedExpr):
            targets(node.target, scope, OPAQUE)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            from_export = isinstance(node.iter, ast.Name) and node.iter.id == "EXPORT_TABLES"
            if from_export and isinstance(node.target, (ast.Tuple, ast.Name)):
                elements = (node.target.elts if isinstance(node.target, ast.Tuple)
                            else [node.target])
                for element in elements:
                    if isinstance(element, ast.Name):
                        scope.bind(element.id, SAFE_EXPORT_LOOP)
                    else:
                        targets(element, scope, OPAQUE)
            else:
                targets(node.target, scope, OPAQUE)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    targets(item.optional_vars, scope, OPAQUE)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                scope.bind(alias.asname or alias.name.split(".")[0], OPAQUE)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            scope.bind(node.name, OPAQUE)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for name in node.names:
                scope.bind(name, OPAQUE)
        elif node.__class__.__name__ in ("MatchAs", "MatchStar"):
            if getattr(node, "name", None):
                scope.bind(node.name, OPAQUE)
        elif node.__class__.__name__ == "MatchMapping":
            if getattr(node, "rest", None):
                scope.bind(node.rest, OPAQUE)

        for child in ast.iter_child_nodes(node):
            walk(child, scope)

    walk(tree, module)
    return module, scope_of


def _is_placeholder_run(node) -> bool:
    """`",".join("?" for _ in xs)` and nothing else.

    The separator must be a string of only commas and spaces, and every
    element joined must be the literal `"?"`, so the result is a run of
    placeholders however long the iterable is. A join over anything that
    could evaluate to a name -- a variable, a call, an f-string -- is not
    this pattern and is not accepted.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "join"):
        return False
    separator = func.value
    if not (isinstance(separator, ast.Constant) and isinstance(separator.value, str)):
        return False
    if set(separator.value) - {",", " "}:
        return False
    if len(node.args) != 1:
        return False
    joined = node.args[0]
    if isinstance(joined, (ast.GeneratorExp, ast.ListComp)):
        element = joined.elt
    elif isinstance(joined, (ast.List, ast.Tuple)):
        return all(isinstance(e, ast.Constant) and e.value == "?" for e in joined.elts)
    else:
        return False
    return isinstance(element, ast.Constant) and element.value == "?"


class _Analysis:
    """The whole package's scopes, and the question `is_safe(name, scope)`."""

    def __init__(self, sources: dict, allowed: set):
        self.allowed = allowed
        self.modules = {}
        self.scope_of = {}
        for filename, tree in sources.items():
            module, scope_of = _collect_scopes(tree, filename)
            self.modules[filename] = (tree, module)
            self.scope_of.update(scope_of)
        # Set before resolving, because resolving a parameter asks whether the
        # arguments passed to it are safe, and one of those may itself be a
        # parameter already proven in an earlier round.
        self.safe_params = set()
        self.safe_params = self._resolve_parameters()

    def _call_sites(self) -> dict:
        calls: dict = {}
        for _filename, (tree, _module) in self.modules.items():
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = (func.id if isinstance(func, ast.Name)
                        else func.attr if isinstance(func, ast.Attribute) else None)
                if name:
                    calls.setdefault(name, []).append(node)
        return calls

    def _functions(self) -> dict:
        found = {}
        for _filename, (tree, _module) in self.modules.items():
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    found.setdefault(node.name, []).append(node)
        return found

    def _resolve_parameters(self) -> set:
        """`(function name, position)` pairs whose every call site in this
        package passes an allowlisted literal or an already-safe name.

        A function nobody calls proves nothing, so its parameters stay unsafe.
        """
        calls = self._call_sites()
        functions = self._functions()
        safe: set = set()
        for _round in range(4):          # one hop is all this codebase needs
            grew = False
            for name, definitions in functions.items():
                sites = calls.get(name)
                if not sites:
                    continue
                for definition in definitions:
                    positional = list(definition.args.posonlyargs) + list(definition.args.args)
                    for index in range(len(positional)):
                        if (name, index) in safe:
                            continue
                        if all(self._argument_is_safe(site, index) for site in sites):
                            safe.add((name, index))
                            grew = True
            if not grew:
                break
        self.safe_params = safe          # so _argument_is_safe can consult it
        return safe

    def _argument_is_safe(self, call: ast.Call, index: int) -> bool:
        arguments = [a for a in call.args if not isinstance(a, ast.Starred)]
        if len(arguments) != len(call.args) or index >= len(arguments):
            return False
        argument = arguments[index]
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            return argument.value in self.allowed
        if isinstance(argument, ast.Name):
            return self.is_safe(argument.id, self.scope_of.get(call))
        return False

    def is_safe(self, name: str, scope) -> bool:
        """Every binding of `name`, in the innermost scope that binds it."""
        if scope is None:
            return False
        bindings = scope.resolve(name)
        if not bindings:
            return False
        for binding in bindings:
            if binding is SAFE_PLACEHOLDER or binding is SAFE_EXPORT_LOOP:
                continue
            if isinstance(binding, _Param) and binding.key() in self.safe_params:
                continue
            return False
        return True

    def offenders(self) -> list:
        found = []
        for filename, (tree, _module) in self.modules.items():
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (isinstance(func, ast.Attribute)
                        and func.attr in ("execute", "executemany")):
                    continue
                if not node.args:
                    continue
                argument = node.args[0]
                if isinstance(argument, ast.BinOp) and isinstance(argument.op, ast.Mod):
                    found.append(f"{filename}: uses %-formatting in a SQL call")
                    continue
                if not isinstance(argument, ast.JoinedStr):
                    continue
                for piece in argument.values:
                    if not isinstance(piece, ast.FormattedValue):
                        continue
                    expression = piece.value
                    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
                        if expression.value not in self.allowed:
                            found.append(
                                f"{filename}: interpolates literal '{expression.value}'")
                    elif isinstance(expression, ast.Name) and self.is_safe(
                            expression.id, self.scope_of.get(node)):
                        continue
                    else:
                        rendered = ast.unparse(expression)
                        found.append(f"{filename}: interpolates unproven name '{rendered}'")
        return found


def allowed_literals() -> set:
    from indexer.export import EXPORT_TABLES

    allowed = set()
    for table, order_by in EXPORT_TABLES:
        allowed.add(table)
        for column in order_by.split(","):
            allowed.add(column.strip())
    return allowed


def analyse(sources: dict) -> list:
    """`sources` maps a filename to source text; returns the offenders."""
    trees = {name: ast.parse(text, filename=name) for name, text in sources.items()}
    return _Analysis(trees, allowed_literals()).offenders()


class TestSqlIdentifierAllowlist(unittest.TestCase):
    """Every f-string interpolation feeding `execute(...)` or
    `executemany(...)` must trace back to a literal in `export.EXPORT_TABLES`
    -- never a name built from RPC-sourced data (mints, signatures,
    destinations) or read out of a file.

    Three bindings are provably safe, and they are proven where the name is
    USED, in the scope that binds it:

    * a loop variable bound directly from ``for x, y in EXPORT_TABLES:``;
    * a function parameter whose *every* call site in this package passes one
      of those loop variables (or an allowlisted literal) at that position;
    * a name assigned a join of ``"?"`` literals -- a run of placeholders,
      which carries no identifier and cannot carry one.

    Anything else is flagged.
    """

    def test_interpolated_sql_identifiers_are_all_allowlisted(self):
        sources = {path.name: path.read_text(encoding="utf-8") for path in _module_files()}
        offenders = analyse(sources)
        self.assertEqual(offenders, [], "; ".join(offenders))


class TestTheAnalysisItselfCatchesThings(unittest.TestCase):
    """The guard, tested against source it is handed rather than only against
    a package that happens to be clean.

    Every probe below passed silently at one point or another. The first
    version of this analysis answered per NAME, package-wide, so `table` --
    made safe by `export.py`'s own loop -- was safe in every module; a later
    version added placeholder runs and widened the same hole. Reverting either
    left the whole suite green, because nothing exercised the analysis with
    anything but code that was already correct. This does.
    """

    def check(self, source: str) -> list:
        return analyse({"probe.py": textwrap.dedent(source)})

    def check_package(self, **modules) -> list:
        """More than one module, because the hole was BETWEEN modules.

        A single-module probe cannot reproduce it: the safe binding that made
        a name safe lived in `export.py` and the unsafe use lived somewhere
        else entirely. Handed one file, even the broken analysis had nothing
        to draw safety from and caught everything.
        """
        return analyse({f"{name}.py": textwrap.dedent(source)
                        for name, source in modules.items()})

    def assertCaught(self, source: str):
        self.assertNotEqual(self.check(source), [], "the analysis said nothing")

    def assertClean(self, source: str):
        self.assertEqual(self.check(source), [])

    # -- the hole itself: one module made a name safe for another ---------
    def test_a_safe_loop_variable_in_one_module_does_not_bless_another(self):
        """`export.py` loops `for table, order_by in EXPORT_TABLES:`. That is
        the binding that made the name `table` safe package-wide, in every
        module, which is what this whole class exists over.
        """
        offenders = self.check_package(
            exporter="""
                from indexer.export import EXPORT_TABLES
                def export_all(conn):
                    for table, order_by in EXPORT_TABLES:
                        conn.execute(f"SELECT * FROM {table} ORDER BY {order_by}")
            """,
            elsewhere="""
                def probe(conn, evil):
                    table = evil
                    conn.execute(f"SELECT * FROM {table}")
            """,
        )
        self.assertEqual(len(offenders), 1, offenders)
        self.assertIn("elsewhere.py", offenders[0])

    def test_a_placeholder_run_in_one_module_does_not_bless_another(self):
        """The same hole, widened a second time to make room for
        `export.py`'s `placeholders`.
        """
        offenders = self.check_package(
            exporter="""
                def import_table(conn, values):
                    placeholders = ",".join("?" for _ in values)
                    conn.execute(f"INSERT INTO inflow VALUES ({placeholders})")
            """,
            elsewhere="""
                def probe(conn, evil):
                    for placeholders in evil:
                        conn.execute(f"SELECT * FROM {placeholders}")
            """,
        )
        self.assertEqual(len(offenders), 1, offenders)
        self.assertIn("elsewhere.py", offenders[0])

    def test_a_lambda_in_another_module_is_not_blessed_either(self):
        offenders = self.check_package(
            exporter="""
                def import_table(conn, values):
                    placeholders = ",".join("?" for _ in values)
                    conn.execute(f"INSERT INTO inflow VALUES ({placeholders})")
            """,
            elsewhere=(
                'probe = lambda conn, placeholders: '
                'conn.execute(f"SELECT * FROM {placeholders}")'
            ),
        )
        self.assertEqual(len(offenders), 1, offenders)

    def test_the_two_modules_are_clean_when_neither_shadows(self):
        """The companion: the arrangement above, minus the shadowing, must
        still pass -- otherwise the tests above prove nothing about scope.
        """
        self.assertEqual(self.check_package(
            exporter="""
                from indexer.export import EXPORT_TABLES
                def export_all(conn):
                    for table, order_by in EXPORT_TABLES:
                        conn.execute(f"SELECT * FROM {table} ORDER BY {order_by}")
            """,
            elsewhere="""
                def probe(conn, values):
                    placeholders = ",".join("?" for _ in values)
                    conn.execute(f"INSERT INTO inflow VALUES ({placeholders})")
            """,
        ), [])

    # -- the forms that hid an identifier ---------------------------------
    def test_a_local_assignment_shadowing_a_safe_name(self):
        # `table` is safe inside export.py's own loop. It is not safe here,
        # and this is the spelling an accident would use.
        self.assertCaught("""
            def probe(conn, evil):
                table = evil
                conn.execute(f"SELECT * FROM {table}")
        """)

    def test_a_for_target_over_attacker_data(self):
        self.assertCaught("""
            def probe(conn, evil):
                for table in evil:
                    conn.execute(f"DROP TABLE {table}")
        """)

    def test_a_lambda_parameter(self):
        self.assertCaught(
            'probe = lambda conn, placeholders: conn.execute(f"SELECT * FROM {placeholders}")'
        )

    def test_a_match_capture_pattern(self):
        self.assertCaught("""
            def probe(conn, evil):
                match evil:
                    case placeholders:
                        conn.execute(f"SELECT * FROM {placeholders}")
        """)

    def test_a_match_mapping_rest(self):
        self.assertCaught("""
            def probe(conn, evil):
                match evil:
                    case {"t": table}:
                        conn.execute(f"SELECT * FROM {table}")
        """)

    def test_a_with_as_target(self):
        self.assertCaught("""
            def probe(conn, evil):
                with evil as placeholders:
                    conn.execute(f"SELECT * FROM {placeholders}")
        """)

    def test_a_tuple_unpacking(self):
        self.assertCaught("""
            def probe(conn, evil):
                placeholders, _rest = evil
                conn.execute(f"SELECT * FROM {placeholders}")
        """)

    def test_a_walrus(self):
        self.assertCaught("""
            def probe(conn, evil):
                conn.execute(f"SELECT * FROM {(placeholders := evil)}")
        """)

    def test_a_comprehension_target(self):
        self.assertCaught("""
            def probe(conn, evil):
                [conn.execute(f"SELECT * FROM {table}") for table in evil]
        """)

    def test_a_parameter_of_an_uncalled_function(self):
        # Nobody calls it, so nothing constrains what is passed.
        self.assertCaught("""
            def probe(conn, placeholders):
                conn.execute(f"SELECT * FROM {placeholders}")
        """)

    def test_a_parameter_with_one_benign_call_site_and_one_hostile(self):
        self.assertCaught("""
            def helper(conn, sortkey):
                conn.execute(f"SELECT * FROM inflow ORDER BY {sortkey}")
            def safe_call(conn):
                helper(conn, "signature")
            def probe(conn, evil):
                helper(conn, evil)
        """)

    def test_a_name_this_module_never_binds(self):
        self.assertCaught("""
            def probe(conn):
                conn.execute(f"SELECT * FROM {imported_from_somewhere}")
        """)

    def test_a_global_declared_and_assigned(self):
        self.assertCaught("""
            table = "inflow"
            def probe(conn, evil):
                global table
                table = evil
                conn.execute(f"SELECT * FROM {table}")
        """)

    def test_a_joined_column_list_is_not_a_placeholder_run(self):
        self.assertCaught("""
            def probe(conn, columns):
                placeholders = ",".join(columns)
                conn.execute(f"INSERT INTO inflow VALUES ({placeholders})")
        """)

    def test_percent_formatting(self):
        self.assertCaught("""
            def probe(conn, evil):
                conn.execute("SELECT * FROM %s" % evil)
        """)

    def test_a_literal_that_is_not_a_known_table(self):
        self.assertCaught("""
            def probe(conn):
                conn.execute(f"SELECT * FROM {'secrets'}")
        """)

    # -- and the ones that must keep working ------------------------------
    def test_the_export_loop_variable(self):
        self.assertClean("""
            from indexer.export import EXPORT_TABLES
            def probe(conn):
                for table, order_by in EXPORT_TABLES:
                    conn.execute(f"SELECT * FROM {table} ORDER BY {order_by}")
        """)

    def test_a_parameter_every_call_site_passes_a_safe_value_to(self):
        self.assertClean("""
            from indexer.export import EXPORT_TABLES
            def helper(conn, table):
                conn.execute(f"SELECT * FROM {table}")
            def probe(conn):
                for table, _order in EXPORT_TABLES:
                    helper(conn, table)
        """)

    def test_a_real_placeholder_run(self):
        self.assertClean("""
            def probe(conn, values):
                placeholders = ",".join("?" for _ in values)
                conn.execute(f"INSERT INTO inflow VALUES ({placeholders})")
        """)

    def test_an_allowlisted_literal(self):
        self.assertClean("""
            def probe(conn):
                conn.execute(f"SELECT * FROM {'inflow'}")
        """)

    def test_the_package_s_own_import_path_still_passes(self):
        # The real thing, as the test above runs it, so a probe that broke
        # the analysis cannot pass by breaking it for everyone.
        sources = {path.name: path.read_text(encoding="utf-8") for path in _module_files()}
        self.assertEqual(analyse(sources), [])


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

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


class TestSqlIdentifierAllowlist(unittest.TestCase):
    """Every f-string interpolation feeding `execute(...)` or
    `executemany(...)` must trace back to a literal in `export.EXPORT_TABLES`
    -- never a name built from RPC-sourced data (mints, signatures,
    destinations).

    Three things are provably safe by construction and are the only patterns
    this codebase uses:

    * a loop variable bound directly from ``for x, y in EXPORT_TABLES:``;
    * a function parameter whose *every* call site in this package passes
      one of those loop variables (or an allowlisted literal) for that
      parameter position;
    * a name whose every binding in the package is a join of ``"?"``
      literals -- a run of placeholders, which carries no identifier at all
      and cannot carry one, whatever the data it stands in for.

    Anything else interpolated into a SQL string -- a name that cannot be
    traced to one of these -- is flagged.
    """

    def _allowed_literals(self) -> set[str]:
        from indexer.export import EXPORT_TABLES

        allowed = set()
        for table, order_by in EXPORT_TABLES:
            allowed.add(table)
            for column in order_by.split(","):
                allowed.add(column.strip())
        return allowed

    def _safe_names(self, trees: dict[str, ast.AST]) -> set[str]:
        """Identifier names provably bound only to EXPORT_TABLES-derived values,
        or to a run of placeholders."""
        safe = set()

        # Pass 0: names whose EVERY binding in the package is a join of "?"
        # literals. `f"... VALUES ({placeholders})"` interpolates no
        # identifier: the expression can produce nothing but `?` and the
        # separator, whatever it was counting. A name bound to a placeholder
        # run somewhere and to anything else anywhere is not safe, so the
        # bindings are collected across every module before deciding.
        bindings: dict[str, list] = {}
        for tree in trees.values():
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            bindings.setdefault(target.id, []).append(node.value)
                elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                    if isinstance(node.target, ast.Name) and node.value is not None:
                        bindings.setdefault(node.target.id, []).append(node.value)
        for name, values in bindings.items():
            if values and all(_is_placeholder_run(value) for value in values):
                safe.add(name)

        # Pass 1: loop variables bound directly to `for ... in EXPORT_TABLES:`.
        for tree in trees.values():
            for node in ast.walk(tree):
                if not isinstance(node, ast.For):
                    continue
                iterated = node.iter
                if isinstance(iterated, ast.Name) and iterated.id == "EXPORT_TABLES":
                    target = node.target
                    if isinstance(target, ast.Tuple):
                        safe.update(elt.id for elt in target.elts if isinstance(elt, ast.Name))
                    elif isinstance(target, ast.Name):
                        safe.add(target.id)

        # Pass 2: function parameters where every call site in the package
        # passes an already-safe name or an allowlisted literal for that
        # position. One fixed-point pass is sufficient for this codebase's
        # single level of indirection (loop var -> export_table's params).
        allowed = self._allowed_literals()
        function_defs = {}
        for tree in trees.values():
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    function_defs[node.name] = node

        for _ in range(3):  # small fixed-point cap; this codebase needs one hop
            changed = False
            for name, fn in function_defs.items():
                params = [a.arg for a in fn.args.args]
                if not params:
                    continue
                calls = []
                for tree in trees.values():
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Call) and (
                            (isinstance(node.func, ast.Name) and node.func.id == name)
                            or (isinstance(node.func, ast.Attribute) and node.func.attr == name)
                        ):
                            calls.append(node)
                if not calls:
                    continue
                for index, param in enumerate(params):
                    if param in safe:
                        continue
                    if all(_argument_is_safe(call, index, safe, allowed) for call in calls):
                        safe.add(param)
                        changed = True
            if not changed:
                break
        return safe

    def _sql_call_arguments(self, tree):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            attr = func.attr if isinstance(func, ast.Attribute) else None
            if attr not in ("execute", "executemany"):
                continue
            if not node.args:
                continue
            yield node.args[0]

    def test_interpolated_sql_identifiers_are_all_allowlisted(self):
        allowed = self._allowed_literals()
        trees = {
            path.name: ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for path in _module_files()
        }
        safe_names = self._safe_names(trees)

        offenders = []
        for filename, tree in trees.items():
            for arg in self._sql_call_arguments(tree):
                if isinstance(arg, ast.JoinedStr):
                    for value in arg.values:
                        if not isinstance(value, ast.FormattedValue):
                            continue
                        expr = value.value
                        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
                            if expr.value not in allowed:
                                offenders.append(f"{filename}: interpolates literal '{expr.value}'")
                        elif isinstance(expr, ast.Name) and expr.id in safe_names:
                            continue
                        else:
                            source = ast.unparse(expr) if hasattr(ast, "unparse") else "<expr>"
                            offenders.append(f"{filename}: interpolates unproven name '{source}'")
                elif isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Mod):
                    offenders.append(f"{filename}: uses %-formatting in a SQL call")
        self.assertEqual(offenders, [], "; ".join(offenders))


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


def _argument_is_safe(call: ast.Call, index: int, safe: set[str], allowed: set[str]) -> bool:
    args = [a for a in call.args if not isinstance(a, ast.Starred)]
    if index >= len(args):
        return True  # keyword-only or defaulted at this call site -- not this call's concern
    arg = args[index]
    if isinstance(arg, ast.Name):
        return arg.id in safe
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value in allowed
    return False


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

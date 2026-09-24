"""Problem codes are one closed vocabulary, declared in ``kernel/problems.py``.

A code is matched on by the UI and by scripts, so it is API. It used to be
declared beside whatever raised it, which let one code be defined twice (the
resolver's and the authoring validator's ``unknown_agent_ref``), let a raise
site spell a code as a bare string, and left codes declared that nothing
raised. These read the source rather than trusting a list: every
``Problem(code=...)`` -- directly, or through a helper that passes its own
``code`` parameter on -- must name a constant from the vocabulary, and every
constant there must be used.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import quickcode
from quickcode.kernel import problems

ROOT = Path(quickcode.__file__).parent
VOCABULARY = Path(problems.__file__)
DECLARED = {
    name: value for name, value in vars(problems).items()
    if name.isupper() and isinstance(value, str)
}


def _sources() -> list[tuple[Path, ast.Module]]:
    return [(path, ast.parse(path.read_text(encoding="utf-8"), str(path)))
            for path in sorted(ROOT.rglob("*.py"))]


def _vocabulary_names(tree: ast.Module) -> tuple[dict[str, str], set[str]]:
    """Names bound to a vocabulary constant (local -> declared), and names
    bound to the vocabulary module itself."""
    names: dict[str, str] = {}
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "quickcode.kernel.problems":
            names.update({a.asname or a.name: a.name for a in node.names})
        elif isinstance(node, ast.ImportFrom) and node.module == "quickcode.kernel":
            modules.update(a.asname or a.name for a in node.names if a.name == "problems")
    return names, modules


def _callee(call: ast.Call) -> str:
    """The function a call names: ``f(...)``, ``self.f(...)``, or the
    ``Problem`` type however it was imported -- not ``seen.add(...)``."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute) and (
            func.attr == "Problem"
            or isinstance(func.value, ast.Name) and func.value.id in ("self", "cls")):
        return func.attr
    return ""


def _code_arg(call: ast.Call, index: int) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == "code":
            return kw.value
    return call.args[index] if len(call.args) > index else None


def _calls_with_scope(tree: ast.Module):
    """Every call, with the function that encloses it (None at module level)."""
    def walk(node, scope):
        for child in ast.iter_child_nodes(node):
            inner = child if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else scope
            if isinstance(child, ast.Call):
                yield child, scope
            yield from walk(child, inner)

    yield from walk(tree, None)


def _violations(path: Path, tree: ast.Module) -> list[str]:
    names, modules = _vocabulary_names(tree)

    def declared(expr: ast.expr) -> bool:
        if isinstance(expr, ast.IfExp):
            return declared(expr.body) and declared(expr.orelse)
        if isinstance(expr, ast.Name):
            return names.get(expr.id) in DECLARED
        if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
            return expr.value.id in modules and expr.attr in DECLARED
        return False

    # Callee name -> the position of its code argument. A helper that hands
    # its own parameter to one of these joins them, so a code is checked where
    # it is written, however many hops it takes to reach ``Problem``.
    carriers = {"Problem": 0}
    out: list[str] = []
    changed = True
    while changed:
        changed = False
        out = []
        for call, scope in _calls_with_scope(tree):
            index = carriers.get(_callee(call))
            if index is None:
                continue
            expr = _code_arg(call, index)
            params = [a.arg for a in scope.args.args] if scope is not None else []
            if isinstance(expr, ast.Name) and expr.id in params:
                if scope.name not in carriers:
                    carriers[scope.name] = params.index(expr.id)
                    changed = True
                continue
            if expr is None or not declared(expr):
                where = f"{path.relative_to(ROOT.parent)}:{call.lineno}"
                out.append(f"{where}: {ast.unparse(expr) if expr else '(no code)'}")
    return out


def test_every_problem_code_is_a_declared_constant():
    found = [v for path, tree in _sources() if path != VOCABULARY
             for v in _violations(path, tree)]
    assert not found, "use a constant from quickcode/kernel/problems.py:\n" + "\n".join(found)


def test_no_module_declares_a_code_of_its_own():
    codes = set(DECLARED.values())
    found = []
    for path, tree in _sources():
        if path == VOCABULARY:
            continue
        for node in tree.body:
            if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)):
                continue
            if node.value.value in codes:
                found.append(f"{path.relative_to(ROOT.parent)}:{node.lineno}")
    assert not found, found


def test_every_declared_code_is_raised_somewhere():
    text = "\n".join(path.read_text(encoding="utf-8")
                     for path in ROOT.rglob("*.py") if path != VOCABULARY)
    unused = [name for name in DECLARED if not re.search(rf"\b{name}\b", text)]
    assert not unused, unused

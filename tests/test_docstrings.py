"""Every module, class, and function in the product code carries a docstring.

The rule binds every definition in src/crypto_warehouse and scripts, however
brief, with nested functions, methods, and each script's main() included. It
has no exemption for trivial definitions, so it can be checked by walking the
syntax tree. Tests are exempt, since a test's name is its description.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PRODUCT = sorted((REPO / "src" / "crypto_warehouse").glob("*.py")) + sorted(
    (REPO / "scripts").glob("*.py")
)


def _bare(path: Path) -> list[str]:
    """Names of every definition in the file, the module included, with no docstring."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    if ast.get_docstring(tree) is None:
        out.append("<module>")
    for node in ast.walk(tree):
        is_def = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        if is_def and ast.get_docstring(node) is None:
            out.append(f"{node.name} (line {node.lineno})")
    return out


def test_every_definition_has_a_docstring() -> None:
    """Fail naming every bare definition, so the fix is a list and not a hunt."""
    assert PRODUCT, "no product files found"
    offenders = {p.name: _bare(p) for p in PRODUCT if _bare(p)}
    assert not offenders, "definitions without a docstring:\n" + "\n".join(
        f"  {f}: {', '.join(names)}" for f, names in offenders.items()
    )

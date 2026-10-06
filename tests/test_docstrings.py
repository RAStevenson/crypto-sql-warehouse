"""Every module, class, and function in the product code carries a docstring.

The rule binds every definition in src/crypto_warehouse and scripts, however
brief, with nested functions, methods, and each script's main() included. It
has no exemption for trivial definitions, so it can be checked by walking the
syntax tree. Tests are exempt, since a test's name is its description.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PRODUCT_FILES = sorted((REPO_ROOT / "src" / "crypto_warehouse").glob("*.py")) + sorted(
    (REPO_ROOT / "scripts").glob("*.py")
)


def _undocumented_definitions(path: Path) -> list[str]:
    """Names of every definition in the file, the module included, with no docstring."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bare_definitions = []
    if ast.get_docstring(tree) is None:
        bare_definitions.append("<module>")
    for node in ast.walk(tree):
        is_definition = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        if is_definition and ast.get_docstring(node) is None:
            bare_definitions.append(f"{node.name} (line {node.lineno})")
    return bare_definitions


def test_every_definition_has_a_docstring() -> None:
    """Fail naming every bare definition, so the fix is a list and not a hunt."""
    assert PRODUCT_FILES, "no product files found"
    offenders = {
        product_file.name: _undocumented_definitions(product_file)
        for product_file in PRODUCT_FILES
        if _undocumented_definitions(product_file)
    }
    assert not offenders, "definitions without a docstring:\n" + "\n".join(
        f"  {file_name}: {', '.join(names)}" for file_name, names in offenders.items()
    )

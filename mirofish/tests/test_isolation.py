"""Import-isolation guard: no mirofish source file may import arbiter.

Walks every `.py` under the mirofish package and asserts (via AST) that none
contains a real `import arbiter` / `from arbiter ...` statement. Docstring or
comment mentions of "arbiter" are ignored — only actual import statements fail.
"""
from __future__ import annotations

import ast
import pathlib

_PKG_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _arbiter_imports(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "arbiter" or alias.name.startswith("arbiter."):
                    hits.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "arbiter" or mod.startswith("arbiter."):
                hits.append(f"from {mod} import ...")
    return hits


def test_no_mirofish_file_imports_arbiter() -> None:
    py_files = sorted(_PKG_ROOT.rglob("*.py"))
    assert py_files, "expected to find mirofish source files"
    offenders: dict[str, list[str]] = {}
    for path in py_files:
        hits = _arbiter_imports(path)
        if hits:
            offenders[str(path.relative_to(_PKG_ROOT))] = hits
    assert not offenders, f"mirofish files import arbiter: {offenders}"

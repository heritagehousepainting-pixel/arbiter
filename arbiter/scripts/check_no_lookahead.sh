#!/usr/bin/env bash
# check_no_lookahead.sh — enforces INTERFACES.md §11 convention 1 (no look-ahead /
# no wall-clock reads outside the injected Lane-3 clock).
#
# Flags, in the arbiter package:
#   1. get_latest(                     — forbidden everywhere
#   2. datetime.now( / .utcnow(        — forbidden outside clock.py
#      (incl. aliased imports: `import datetime as dt; dt.datetime.now()`)
#   3. time.time(                      — forbidden outside clock.py
#   4. date.today( / datetime.date.today(  — forbidden outside clock.py
#   5. pd.Timestamp.now( / pandas.Timestamp.now(  — forbidden outside clock.py
#
# The sanctioned wall-clock abstraction is ``Clock.now()`` (Lane 3): a call on
# an object named ``clock`` / ``live_clock`` (e.g. ``clock.now()``) is NEVER
# flagged — only reads of the *real* system clock via stdlib/pandas are.
#
# Detection is AST-based (so a pattern that appears only inside a comment or a
# string literal is not flagged) and per-file alias-aware.
#
# Exits non-zero if any violation is found.

set -euo pipefail

PACKAGE_DIR="$(cd "$(dirname "$0")/.." && pwd)/arbiter"

# Prefer the project venv's interpreter (robust regardless of cwd / PATH);
# fall back to python3 only if the venv is absent.  (Fixes the audit note that
# this script previously ran the system `python3`.)
VENV_PY="$(cd "$(dirname "$0")/.." && pwd)/.venv/bin/python"
if [[ -x "$VENV_PY" ]]; then
  PY="$VENV_PY"
else
  PY="$(command -v python3 || command -v python)"
fi

# Run all checks via Python so we get comment/docstring-aware parsing.
# -W ignore silences unrelated SyntaxWarnings from source-tree docstrings.
"$PY" -W ignore - "$PACKAGE_DIR" <<'PYEOF'
import ast
import sys
import pathlib

package_dir = pathlib.Path(sys.argv[1])

FORBIDDEN_ANYWHERE = {"get_latest"}
# Attribute names that read the real clock when called on the datetime class.
DATETIME_CLOCK_ATTRS = {"now", "utcnow"}


def datetime_aliases(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Return (module_aliases, class_aliases) for the stdlib datetime module.

    ``module_aliases`` are names bound to the *module* (``import datetime`` ->
    {"datetime"}; ``import datetime as dt`` -> {"dt"}), so ``<alias>.datetime``
    is the datetime *class*.

    ``class_aliases`` are names bound directly to the datetime *class*
    (``from datetime import datetime`` -> {"datetime"};
    ``from datetime import datetime as DT`` -> {"DT"}).
    """
    module_aliases: set[str] = set()
    class_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "datetime":
                    module_aliases.add(a.asname or "datetime")
        elif isinstance(node, ast.ImportFrom) and node.module == "datetime":
            for a in node.names:
                if a.name == "datetime":
                    class_aliases.add(a.asname or "datetime")
    return module_aliases, class_aliases


def attr_chain(node: ast.AST) -> list[str] | None:
    """Flatten an attribute access (``a.b.c``) into ['a','b','c'] or None."""
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        parts.reverse()
        return parts
    return None


def check_file(path: pathlib.Path) -> list[str]:
    """Return list of violation messages for a single .py file."""
    violations: list[str] = []
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        violations.append(f"{path}: SyntaxError — {exc}")
        return violations

    is_clock = path.name == "clock.py"
    module_aliases, class_aliases = datetime_aliases(tree)

    def flag(node: ast.Call, msg: str) -> None:
        violations.append(f"{path}:{node.lineno}: {msg}")

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func

        # ---- Pattern 1: get_latest(...) — forbidden everywhere ----
        if isinstance(func, ast.Name) and func.id in FORBIDDEN_ANYWHERE:
            flag(node, f"forbidden call — {func.id}()")
            continue
        if isinstance(func, ast.Attribute) and func.attr in FORBIDDEN_ANYWHERE:
            flag(node, f"forbidden call — {func.attr}()")
            continue

        # Everything below is only forbidden OUTSIDE clock.py.
        if is_clock:
            continue
        if not isinstance(func, ast.Attribute):
            continue

        chain = attr_chain(func)

        # ---- Pattern 2: datetime.now()/utcnow() (real & aliased class) ----
        # Class-aliased: `from datetime import datetime[ as DT]` -> DT.now()
        if func.attr in DATETIME_CLOCK_ATTRS:
            base = func.value
            if isinstance(base, ast.Name) and base.id in (class_aliases | {"datetime"}):
                flag(node, f"forbidden outside clock.py — {base.id}.{func.attr}()")
                continue
            # Module-aliased: `import datetime as dt` -> dt.datetime.now()
            if (chain is not None and len(chain) == 3
                    and chain[0] in module_aliases and chain[1] == "datetime"
                    and chain[2] in DATETIME_CLOCK_ATTRS):
                flag(node, f"forbidden outside clock.py — "
                           f"{'.'.join(chain)}()")
                continue

        # ---- Pattern 3: time.time() ----
        if (chain is not None and len(chain) == 2
                and chain[0] == "time" and chain[1] == "time"):
            flag(node, "forbidden outside clock.py — time.time()")
            continue

        # ---- Pattern 4: date.today() / datetime.date.today() ----
        if func.attr == "today":
            if isinstance(func.value, ast.Name) and func.value.id == "date":
                flag(node, "forbidden outside clock.py — date.today()")
                continue
            if (chain is not None and chain[-1] == "today"
                    and "date" in chain[:-1]):
                flag(node, f"forbidden outside clock.py — {'.'.join(chain)}()")
                continue

        # ---- Pattern 5: pd.Timestamp.now() / pandas.Timestamp.now() ----
        if (chain is not None and chain[-1] == "now" and "Timestamp" in chain
                and chain[0] in {"pd", "pandas"}):
            flag(node, f"forbidden outside clock.py — {'.'.join(chain)}()")
            continue

    return violations


all_violations: list[str] = []
for py_file in sorted(package_dir.rglob("*.py")):
    all_violations.extend(check_file(py_file))

if all_violations:
    print("No-lookahead check FAILED:")
    for v in all_violations:
        print(f"  {v}")
    sys.exit(1)
else:
    print("All no-lookahead checks passed (AST-based, comment/docstring-aware).")
    sys.exit(0)
PYEOF

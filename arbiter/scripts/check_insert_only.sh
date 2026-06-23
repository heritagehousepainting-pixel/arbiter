#!/usr/bin/env bash
# check_insert_only.sh — enforces INTERFACES.md §11.2 (insert-only store).
#
# Flags raw SQL mutations executed against the SQLite store:
#   * UPDATE <table> SET ...
#   * DELETE FROM ...
#   * INSERT OR REPLACE ...
#   * REPLACE INTO ...
#
# The store is append-only/insert-only by design: history is preserved by
# inserting new rows (and at most flipping ``is_superseded``), never by
# mutating prior rows in place.  A small, explicitly enumerated set of §11.2
# carve-outs is sanctioned; everything else is a violation.
#
# Detection is AST-based (so the keyword must appear in a *string passed to*
# ``conn.execute()`` / ``executemany`` / ``executescript`` — never in a
# comment, docstring, or prose).  This avoids false positives on the many
# docstrings that merely *mention* "UPDATE" while documenting the rule.
#
# The ``INSERT ... ON CONFLICT (...) DO UPDATE SET`` upsert pattern is NOT a
# raw mutation — it is insert-shaped and is intentionally not flagged.
#
# Allowlisting future code:
#   Either (a) the (file, statement-signature) is added to ALLOWLIST below
#   (used for the existing carve-outs, since their source files are owned by
#   other work-packages and must not carry inline markers right now), or
#   (b) a NEW carve-out site places a ``# insert-only-ok`` marker comment on
#   the ``.execute(...)`` call line (or the line immediately above/below it).
#   The marker convention lets future sanctioned mutations opt in locally
#   without editing this script.
#
# Exits non-zero if any non-allowlisted raw mutation is found.

set -euo pipefail

PACKAGE_DIR="$(cd "$(dirname "$0")/.." && pwd)/arbiter"

# Prefer the project venv's interpreter (robust regardless of cwd / PATH);
# fall back to python3 only if the venv is absent.
VENV_PY="$(cd "$(dirname "$0")/.." && pwd)/.venv/bin/python"
if [[ -x "$VENV_PY" ]]; then
  PY="$VENV_PY"
else
  PY="$(command -v python3 || command -v python)"
fi

"$PY" -W ignore - "$PACKAGE_DIR" <<'PYEOF'
import ast
import re
import sys
import pathlib

package_dir = pathlib.Path(sys.argv[1])

EXEC_METHODS = {"execute", "executemany", "executescript"}

# Raw mutation signatures.  UPDATE must be "UPDATE <table-or-{placeholder}> SET"
# so the bare word "UPDATE" inside an "ON CONFLICT ... DO UPDATE SET" upsert is
# NOT matched (that clause never has a table name between UPDATE and SET).
PATTERNS = {
    "UPDATE":            re.compile(r"\bUPDATE\s+(?:\{?\w[\w}]*)\s+SET\b", re.IGNORECASE),
    "DELETE":            re.compile(r"\bDELETE\s+FROM\b", re.IGNORECASE),
    "INSERT OR REPLACE": re.compile(r"\bINSERT\s+OR\s+REPLACE\b", re.IGNORECASE),
    "REPLACE INTO":      re.compile(r"\bREPLACE\s+INTO\b", re.IGNORECASE),
}

# Sanctioned §11.2 carve-outs, allowlisted by (relative path, signature regex).
# Keyed on a stable SQL signature rather than a line number so the allowlist
# survives unrelated edits to these files by parallel work-packages.
ALLOWLIST = [
    # db/helpers.py — supersede_row / supersede_rows: the ONLY in-place UPDATE,
    # flips is_superseded on the prior row (history-preserving correction).
    ("db/helpers.py", re.compile(r"UPDATE\s+\{?\w[\w}]*\}?\s+SET\s+is_superseded\s*=\s*1", re.IGNORECASE)),
    # execution/position_store.py — DELETE + upsert of the simulated position
    # snapshot (state-not-history table; rebuilt each persist).
    ("execution/position_store.py", re.compile(r"DELETE\s+FROM\s+sim_positions", re.IGNORECASE)),
    # engine/reconcile.py — orders.status promotion (pending -> filled/partial).
    # (Was engine.py before the H1 refactor split engine.py into a package.)
    ("engine/reconcile.py", re.compile(r"UPDATE\s+orders\s+SET\s+status\s*=", re.IGNORECASE)),
    # engine/_engine.py — orders.idea_id back-fill after submission.
    # (Was engine.py before the H1 refactor split engine.py into a package.)
    ("engine/_engine.py", re.compile(r"UPDATE\s+orders\s+SET\s+idea_id\s*=", re.IGNORECASE)),
    # orchestrator/idea_store.py — ideas.state lifecycle transition (§11.2 carve-out).
    ("orchestrator/idea_store.py", re.compile(r"UPDATE\s+ideas\s+SET\s+state\s*=", re.IGNORECASE)),
]

# evaluation/outcome_store.py and ingest/writer.py reach insert-only correction
# via db/helpers.supersede_row, so they carry no raw mutation of their own and
# need no entry here. breaker_state / engine_state / sim_account use the
# INSERT ... ON CONFLICT DO UPDATE upsert form, which is insert-shaped and is
# not matched by the raw-mutation patterns above.


def render(node: ast.AST) -> str | None:
    """Reconstruct the literal text of a str Constant or an f-string template.

    f-string ``{expr}`` slots become a single-char placeholder so a statement
    like ``f"UPDATE {table} SET ..."`` still matches the UPDATE signature.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            else:
                parts.append("X")  # placeholder for an interpolated expression
        return "".join(parts)
    return None


def statement_text(call: ast.Call) -> str:
    """Concatenate every string-ish literal inside the call's positional args.

    Handles plain strings, f-strings, and string concatenation (``a + b``).
    """
    texts = []
    for arg in call.args:
        for sub in ast.walk(arg):
            r = render(sub)
            if r:
                texts.append(r)
    return " ".join(texts)


def matched_pattern(sql: str) -> str | None:
    for name, pat in PATTERNS.items():
        if pat.search(sql):
            return name
    return None


def is_allowlisted(rel_path: str, sql: str) -> bool:
    for allow_path, sig in ALLOWLIST:
        if rel_path.endswith(allow_path) and sig.search(sql):
            return True
    return False


def has_marker(source_lines: list[str], lineno: int) -> bool:
    """True if a ``# insert-only-ok`` marker sits on/near the call line."""
    for ln in (lineno - 1, lineno, lineno + 1):  # 1-based -> tolerate +/-1
        idx = ln - 1
        if 0 <= idx < len(source_lines) and "insert-only-ok" in source_lines[idx]:
            return True
    return False


def check_file(path: pathlib.Path) -> list[str]:
    violations: list[str] = []
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [f"{path}: SyntaxError — {exc}"]

    rel = str(path.relative_to(package_dir.parent))
    source_lines = source.splitlines()

    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in EXEC_METHODS):
            continue
        sql = statement_text(node)
        if not sql:
            continue
        kind = matched_pattern(sql)
        if kind is None:
            continue
        if is_allowlisted(rel, sql):
            continue
        if has_marker(source_lines, node.lineno):
            continue
        snippet = " ".join(sql.split())[:70]
        violations.append(
            f"{path}:{node.lineno}: raw {kind} not in §11.2 allowlist — {snippet}"
        )
    return violations


all_violations: list[str] = []
for py_file in sorted(package_dir.rglob("*.py")):
    all_violations.extend(check_file(py_file))

if all_violations:
    print("Insert-only check FAILED (unsanctioned raw mutation):")
    for v in all_violations:
        print(f"  {v}")
    print()
    print("If this is a deliberate §11.2 carve-out, add it to ALLOWLIST in")
    print("scripts/check_insert_only.sh or mark the .execute() line "
          "with '# insert-only-ok'.")
    sys.exit(1)

print("All insert-only checks passed (AST-based; §11.2 carve-outs allowlisted).")
sys.exit(0)
PYEOF

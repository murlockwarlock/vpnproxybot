#!/usr/bin/env python3
"""Static check script ensuring payment recovery safety, no variable shadowing, and clean scoping."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

TARGET_FILES = [
    "bot/services/yookassa_reconciliation.py",
    "bot/services/problem_payments.py",
    "bot/webhooks.py",
    "bot/services/payment_service.py",
    "bot/services/scheduler.py",
    "bot/handlers/payment.py",
    "bot/handlers/devices.py",
]

FORBIDDEN_INNER_MODULES = {
    "bot.models",
    "bot.database",
    "bot.services.balance_service",
}


def run_ruff_checks(files: list[str]) -> bool:
    print(f"Running ruff checks on {len(files)} files...")
    cmd = [
        sys.executable,
        "-m",
        "ruff",
        "check",
        "--select",
        "F821,F822,F823,B006,B008",
        *files,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print("RUFF CHECK FAILED:")
        print(proc.stdout)
        print(proc.stderr)
        return False
    print(" Ruff check passed!")
    return True


def check_forbidden_inner_imports(filepath: str) -> list[str]:
    errors = []
    path = Path(filepath)
    if not path.is_file():
        return [f"File not found: {filepath}"]

    content = path.read_text(encoding="utf-8")
    tree = ast.parse(content, filename=filepath)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                if isinstance(child, ast.ImportFrom) and child.module in FORBIDDEN_INNER_MODULES:
                    names = [a.name for a in child.names]
                    errors.append(
                        f"{filepath}:{child.lineno}: function '{node.name}' has inner import "
                        f"from '{child.module}' ({', '.join(names)}). "
                        f"All model/database/balance imports must be top-level to prevent scope shadowing."
                    )
    return errors


def scan_payment_safety() -> list[str]:
    """Return unsafe money conversions in payment-critical modules."""
    violations: list[str] = []
    for filepath in TARGET_FILES:
        path = Path(filepath)
        if not path.is_file():
            violations.append(f"File not found: {filepath}")
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=filepath)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "int":
                continue
            if not node.args:
                continue
            candidate = node.args[0]
            if isinstance(candidate, ast.BinOp) and isinstance(candidate.op, ast.Mult):
                constants = [candidate.left, candidate.right]
                if any(isinstance(value, ast.Constant) and value.value == 100 for value in constants):
                    violations.append(
                        f"{filepath}:{node.lineno}: payment-critical int(... * 100) conversion"
                    )
            if isinstance(candidate, ast.Call) and isinstance(candidate.func, ast.Name) and candidate.func.id == "round":
                if candidate.args and isinstance(candidate.args[0], ast.BinOp) and isinstance(candidate.args[0].op, ast.Mult):
                    constants = [candidate.args[0].left, candidate.args[0].right]
                    if any(isinstance(value, ast.Constant) and value.value == 100 for value in constants):
                        violations.append(
                            f"{filepath}:{node.lineno}: payment-critical int(round(... * 100)) conversion"
                        )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "round":
                continue
            if not node.args or not isinstance(node.args[0], ast.BinOp) or not isinstance(node.args[0].op, ast.Mult):
                continue
            constants = [node.args[0].left, node.args[0].right]
            if any(isinstance(value, ast.Constant) and value.value == 100 for value in constants):
                violations.append(
                    f"{filepath}:{node.lineno}: payment-critical round(... * 100) conversion"
                )
    return violations


def main() -> int:
    print("=== Payment & Reconciliation Safety Static Check ===")
    success = True

    # 1. Ruff checks for undefined names & unbound variables
    if not run_ruff_checks(TARGET_FILES):
        success = False

    # 2. AST inner import checks to prevent BalanceTransaction-like shadowing bugs
    ast_errors: list[str] = []
    for f in ["bot/services/yookassa_reconciliation.py", "bot/services/problem_payments.py"]:
        ast_errors.extend(check_forbidden_inner_imports(f))

    if ast_errors:
        print("\nAST INNER IMPORT SAFETY CHECK FAILED:")
        for err in ast_errors:
            print(f"  ❌ {err}")
        success = False
    else:
        print(" AST inner import safety check passed (no model/balance shadowing)!")

    conversion_errors = scan_payment_safety()
    if conversion_errors:
        print("\nUNSAFE PAYMENT MONEY CONVERSIONS FOUND:")
        for err in conversion_errors:
            print(f"  ❌ {err}")
        success = False
    else:
        print(" Canonical money conversion guard passed!")

    if success:
        print("\n All payment safety static checks passed cleanly.")
        return 0
    else:
        print("\n❌ Static checks failed. Fix reported scoping issues before committing.")
        return 1


if __name__ == "__main__":
    sys.exit(main())

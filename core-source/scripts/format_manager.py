"""Format manager.py without modifying its Python AST.

Intended for a review branch: GUI authorization code remains logically unchanged.
The separate lifecycle bug fix and type hints have regression tests.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    source = root / "udb" / "manager.py"
    before = source.read_bytes()
    before_ast = ast.dump(ast.parse(before, filename=str(source)), include_attributes=False)
    try:
        subprocess.run(
            [sys.executable, "-m", "ruff", "format", "--config", str(root / "pyproject.toml"), str(source)],
            check=True,
        )
        after = source.read_bytes()
        after_ast = ast.dump(ast.parse(after, filename=str(source)), include_attributes=False)
        if after_ast != before_ast:
            raise RuntimeError("Formatter changed the manager Python AST")
    except BaseException:
        source.write_bytes(before)
        raise
    before_lines = before.decode("utf-8").splitlines()
    after_lines = after.decode("utf-8").splitlines()
    print(f"MANAGER_AST_UNCHANGED original_lines={len(before_lines)} formatted_lines={len(after_lines)}")
    print(f"MANAGER_LINES_OVER_120 after={sum(len(line) > 120 for line in after_lines)}")


if __name__ == "__main__":
    main()

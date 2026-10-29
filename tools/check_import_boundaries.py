"""Check canonical UDB imports for eager dependency cycles.

Only imports executed at module import time count. Lazy imports inside
functions/methods are intentionally excluded; their runtime call paths are
covered by tests instead. This is a source architecture check, not a
replacement for Python import execution or security review.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path


def module_name(root: Path, file: Path) -> str:
    parts = file.relative_to(root).with_suffix("").parts
    return (
        ".".join(("udb", *parts[:-1]))
        if parts[-1] == "__init__"
        else ".".join(("udb", *parts))
    )


class EagerImports(ast.NodeVisitor):
    """Walk module-level control flow but never function/method bodies."""

    def __init__(self, package: str):
        self.package = package
        self.names: set[str] = set()

    def visit_FunctionDef(self, node):
        return None

    def visit_AsyncFunctionDef(self, node):
        return None

    def visit_Lambda(self, node):
        return None

    def visit_If(self, node):
        # TYPE_CHECKING imports are not executed at runtime.
        type_checking = (
            isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING"
        ) or (
            isinstance(node.test, ast.Attribute)
            and isinstance(node.test.value, ast.Name)
            and node.test.value.id == "typing"
            and node.test.attr == "TYPE_CHECKING"
        )
        if type_checking:
            for statement in node.orelse:
                self.visit(statement)
            return
        self.generic_visit(node)

    def visit_Import(self, node):
        self.names.update(alias.name for alias in node.names)

    def visit_ImportFrom(self, node):
        if node.level:
            package_parts = self.package.split(".")
            keep = len(package_parts) - node.level + 1
            if keep <= 0:
                return
            prefix = ".".join(package_parts[:keep])
            target = ".".join(part for part in (prefix, node.module) if part)
        else:
            target = node.module or ""
        self.names.add(target)
        for alias in node.names:
            if alias.name != "*":
                self.names.add(target + "." + alias.name)


def import_graph(root: Path) -> dict[str, set[str]]:
    files = {
        module_name(root, file): file
        for file in root.rglob("*.py")
        if file.is_file() and not file.is_symlink()
    }
    graph = {name: set() for name in files}
    for name, file in files.items():
        tree = ast.parse(file.read_text(encoding="utf-8-sig"), filename=str(file))
        package = name if file.name == "__init__.py" else name.rsplit(".", 1)[0]
        visitor = EagerImports(package)
        for statement in tree.body:
            visitor.visit(statement)
        graph[name] = (visitor.names & files.keys()) - {name}
    return graph


def import_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan SCC; only components with multiple modules are cycles."""
    index = 0
    indices: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    cycles: list[list[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = low[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for dependency in sorted(graph[node]):
            if dependency not in indices:
                visit(dependency)
                low[node] = min(low[node], low[dependency])
            elif dependency in on_stack:
                low[node] = min(low[node], indices[dependency])
        if low[node] == indices[node]:
            group = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                group.append(member)
                if member == node:
                    break
            if len(group) > 1:
                cycles.append(sorted(group))

    for node in sorted(graph):
        if node not in indices:
            visit(node)
    return sorted(cycles)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1] / "src/udb"
    )
    args = parser.parse_args()
    graph = import_graph(args.root.resolve())
    cycles = import_cycles(graph)
    print(
        json.dumps(
            {
                "modules": len(graph),
                "eager_import_edges": sum(len(edges) for edges in graph.values()),
                "eager_import_cycles": cycles,
            },
            indent=2,
        )
    )
    return 1 if cycles else 0


if __name__ == "__main__":
    raise SystemExit(main())

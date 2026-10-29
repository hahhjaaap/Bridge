"""Import-layer tests for the canonical source package."""

from __future__ import annotations

from pathlib import Path

from tools.check_import_boundaries import import_cycles, import_graph


def test_canonical_udb_has_no_eager_import_cycles():
    root = Path(__file__).resolve().parents[1] / "src/udb"
    graph = import_graph(root)
    assert len(graph) >= 60
    assert import_cycles(graph) == []


def test_eager_import_cycle_detected_but_deferred_import_ignored(tmp_path):
    package = tmp_path / "udb"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "alpha.py").write_text("from . import beta\n")
    (package / "beta.py").write_text("from . import alpha\n")
    (package / "deferred.py").write_text("def work():\n    from . import alpha\n")
    graph = import_graph(package)
    assert graph["udb.alpha"] == {"udb", "udb.beta"}
    assert graph["udb.beta"] == {"udb", "udb.alpha"}
    assert graph["udb.deferred"] == set()
    assert import_cycles(graph) == [["udb.alpha", "udb.beta"]]


def test_type_checking_only_import_does_not_create_runtime_cycle(tmp_path):
    package = tmp_path / "udb"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "first.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from . import second\n"
    )
    (package / "second.py").write_text(
        "import typing\nif typing.TYPE_CHECKING:\n    from . import first\n"
    )
    assert import_cycles(import_graph(package)) == []

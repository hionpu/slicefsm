"""Unit tests for the context engine: map, slice, expand."""

from __future__ import annotations

from pathlib import Path

import pytest

from slicefsm import context_engine as ce


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "src" / "store").mkdir(parents=True)
    (tmp_path / "src" / "ui").mkdir(parents=True)
    (tmp_path / "src" / "other").mkdir(parents=True)
    (tmp_path / "src" / "store" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "store" / "db.py").write_text(
        '''"""storage."""


def save(row):
    """persist one row."""
    x = 1
    return x


class Store:
    """a store."""

    def get(self, key):
        return key

    def _private(self):
        return 0
''',
        encoding="utf-8",
    )
    (tmp_path / "src" / "ui" / "panel.py").write_text(
        '''from src.store.db import save


def open_panel(state):
    """open the panel."""
    return save(state)
''',
        encoding="utf-8",
    )
    (tmp_path / "src" / "other" / "misc.py").write_text(
        "def unrelated():\n    return 1\n", encoding="utf-8"
    )
    return tmp_path


def test_repo_map_lists_symbols(project):
    rmap = ce.build_repo_map(project)
    pkgs = rmap["packages"]
    assert "save" in pkgs["src/store/db.py"]
    assert "Store" in pkgs["src/store/db.py"]
    assert "open_panel" in pkgs["src/ui/panel.py"]
    # methods are not listed in the map (kind=method filtered)
    assert "Store.get" not in pkgs["src/store/db.py"]


def test_slice_context_buckets(project):
    ctx = ce.get_slice_context(project, "src/ui")
    # module bucket = full text of panel.py
    assert "src/ui/panel.py" in ctx["module_files"]
    assert "def open_panel" in ctx["module_files"]["src/ui/panel.py"]
    # dependency = db.py symbols, signature-only, with range
    dep_names = {d["name"] for d in ctx["dependencies"]}
    assert "save" in dep_names
    assert "Store" in dep_names
    assert "Store.get" in dep_names
    save_rec = next(d for d in ctx["dependencies"] if d["name"] == "save")
    assert save_rec["signature"].startswith("def save(row)")
    assert save_rec["range"]["start"] > 0
    # body text must NOT be in the dependency bucket
    assert all("body" not in d for d in ctx["dependencies"])
    # excluded = sibling module not touched
    assert "other" in ctx["excluded"]
    assert "store" not in ctx["excluded"]  # it is a dependency
    assert "ui" not in ctx["excluded"]     # it is the module


def test_slice_context_writes_manifest(project):
    ctx = ce.get_slice_context(project, "src/ui")
    mp = Path(ctx["manifest_path"])
    assert mp.exists()
    import json

    man = json.loads(mp.read_text(encoding="utf-8"))
    assert man["module_files"] == ["src/ui/panel.py"]
    assert any(d["name"] == "save" for d in man["dependencies"])


def test_expand_by_source_path(project):
    out = ce.expand_symbol(project, "save", source_path="src/store/db.py")
    assert out["name"] == "save"
    assert "def save(row):" in out["body"]
    assert "return x" in out["body"]
    # only the function body, not the whole file
    assert "class Store" not in out["body"]


def test_expand_by_manifest(project):
    ctx = ce.get_slice_context(project, "src/ui")
    out = ce.expand_symbol(project, "Store.get", manifest_path=ctx["manifest_path"])
    assert out["name"] == "Store.get"
    assert "def get(self, key):" in out["body"]
    assert "return key" in out["body"]


def test_expand_missing_symbol(project):
    out = ce.expand_symbol(project, "nope", source_path="src/store/db.py")
    assert out["error"] == "symbol_not_found"


def test_single_file_module(project):
    ctx = ce.get_slice_context(project, "src/store/db.py")
    assert "src/store/db.py" in ctx["module_files"]
    assert ctx["counts"]["module_files"] == 1

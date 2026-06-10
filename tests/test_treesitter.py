"""Tree-sitter backend tests: C# and C++ symbols + cross-file resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from slicefsm import backends, context_engine as ce
from slicefsm.backends import treesitter

pytestmark = pytest.mark.skipif(
    not treesitter.available(), reason="tree-sitter grammars not installed"
)


# ── C# ─────────────────────────────────────────────────────────────


@pytest.fixture
def csharp_project(tmp_path: Path) -> Path:
    (tmp_path / "src" / "UI").mkdir(parents=True)
    (tmp_path / "src" / "Core").mkdir(parents=True)
    (tmp_path / "src" / "Other").mkdir(parents=True)
    (tmp_path / "src" / "UI" / "MemoPanel.cs").write_text(
        """using System;
using Game.Core;

namespace Game.UI {
    public class MemoPanel {
        private int _count;
        public void Open(int id) { Store.Save(id); }
        public int Count { get { return _count; } }
    }
}
""",
        encoding="utf-8",
    )
    (tmp_path / "src" / "Core" / "Store.cs").write_text(
        """namespace Game.Core {
    public static class Store {
        public static void Save(int id) { }
        public static int Load() { return 0; }
    }
}
""",
        encoding="utf-8",
    )
    (tmp_path / "src" / "Other" / "Misc.cs").write_text(
        "namespace Game.Other { public class Misc { } }\n", encoding="utf-8"
    )
    return tmp_path


def test_csharp_parses_symbols():
    cs = treesitter.CSharpBackend()
    src = (
        "namespace N { public class C { public void M(int x){} "
        "public int P { get; set; } } public interface I { void Do(); } }"
    )
    recs = {r.name: r for r in cs.parse_symbols(src, "C.cs")}
    assert recs["C"].kind == "class"
    assert recs["C.M"].kind == "method"
    assert recs["C.M"].signature.startswith("public void M(int x)")
    assert recs["C.P"].kind == "property"
    assert recs["I"].kind == "interface"
    assert recs["I.Do"].kind == "method"


def test_csharp_namespace_resolution(csharp_project):
    ctx = ce.get_slice_context(csharp_project, "src/UI")
    assert "src/UI/MemoPanel.cs" in ctx["module_files"]
    dep_names = {d["name"] for d in ctx["dependencies"]}
    # `using Game.Core;` must pull Store's signatures from the other file.
    assert "Store" in dep_names
    assert "Store.Save" in dep_names
    assert "Store.Load" in dep_names
    # Game.Other is untouched -> excluded.
    assert "Other" in ctx["excluded"]
    assert "Core" not in ctx["excluded"]


def test_csharp_expand_symbol(csharp_project):
    ctx = ce.get_slice_context(csharp_project, "src/UI")
    out = ce.expand_symbol(csharp_project, "Store.Save", manifest_path=ctx["manifest_path"])
    assert out["name"] == "Store.Save"
    assert "Save" in out["body"]
    assert "Load" not in out["body"]  # only the one method body


def test_csharp_repo_map(csharp_project):
    rmap = ce.build_repo_map(csharp_project)
    assert "MemoPanel" in rmap["packages"]["src/UI/MemoPanel.cs"]
    assert "Store" in rmap["packages"]["src/Core/Store.cs"]


def test_csharp_global_namespace_resolution(tmp_path):
    # Unity style: no namespaces, no project usings. Resolution must still work
    # via type-name references.
    (tmp_path / "src" / "Player").mkdir(parents=True)
    (tmp_path / "src" / "Items").mkdir(parents=True)
    (tmp_path / "src" / "Player" / "Player.cs").write_text(
        "public class Player {\n  public void Pickup() { Inventory inv = new Inventory(); inv.Add(1); }\n}\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "Items" / "Inventory.cs").write_text(
        "public class Inventory {\n  public void Add(int id) { }\n}\n",
        encoding="utf-8",
    )
    ctx = ce.get_slice_context(tmp_path, "src/Player")
    dep_names = {d["name"] for d in ctx["dependencies"]}
    assert "Inventory" in dep_names  # resolved by type-name, no using/namespace
    assert "Inventory.Add" in dep_names


# ── C++ ────────────────────────────────────────────────────────────


@pytest.fixture
def cpp_project(tmp_path: Path) -> Path:
    (tmp_path / "src" / "ui").mkdir(parents=True)
    (tmp_path / "src" / "core").mkdir(parents=True)
    (tmp_path / "src" / "ui" / "panel.cpp").write_text(
        '#include "core/store.h"\n\nvoid open_panel() { Store s; }\n',
        encoding="utf-8",
    )
    (tmp_path / "src" / "core" / "store.h").write_text(
        "class Store {\npublic:\n  void save(int id);\n  int load() { return 0; }\n};\n",
        encoding="utf-8",
    )
    return tmp_path


def test_cpp_parses_symbols():
    cpp = treesitter.CppBackend()
    src = "namespace ns { class Foo { public: void bar(int x); }; } void freefn(int a) { }"
    recs = {r.name: r for r in cpp.parse_symbols(src, "f.cpp")}
    assert "Foo" in recs and recs["Foo"].kind == "class"
    assert "Foo.bar" in recs
    assert "freefn" in recs and recs["freefn"].kind == "function"


def test_cpp_include_resolution(cpp_project):
    ctx = ce.get_slice_context(cpp_project, "src/ui")
    assert "src/ui/panel.cpp" in ctx["module_files"]
    dep_names = {d["name"] for d in ctx["dependencies"]}
    assert "Store" in dep_names  # resolved via #include "core/store.h"
    assert "Store.save" in dep_names


def test_cpp_expand_symbol(cpp_project):
    ctx = ce.get_slice_context(cpp_project, "src/ui")
    out = ce.expand_symbol(cpp_project, "Store", manifest_path=ctx["manifest_path"])
    assert out["name"] == "Store"
    assert "save" in out["body"]

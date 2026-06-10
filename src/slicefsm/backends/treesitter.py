"""Tree-sitter backends for C# and C++.

Symbols, signatures, and line ranges come from a direct tree walk (no query
language, so it is stable across tree-sitter versions). Dependency resolution
is index-based via provides_keys / import_keys:

  C#  : a file provides its namespace names; `using X.Y;` wants namespace X.Y.
  C++ : a header provides its path/basename; `#include "foo.h"` wants that path.

Grammars ship inside their wheels (tree-sitter-c-sharp / tree-sitter-cpp), so
there is no runtime download. If the wheels are absent, available() is False
and these backends simply do not register; the engine degrades gracefully.
"""

from __future__ import annotations

import re

from .base import ImportRef, SymbolRecord

try:
    from tree_sitter import Language, Parser
    import tree_sitter_c_sharp as _ts_cs
    import tree_sitter_cpp as _ts_cpp

    _AVAILABLE = True
except Exception:  # pragma: no cover - exercised only when wheels missing
    _AVAILABLE = False


def available() -> bool:
    return _AVAILABLE


# ── shared node helpers ────────────────────────────────────────────


def _range(node) -> dict[str, int]:
    return {"start": node.start_point[0] + 1, "end": node.end_point[0] + 1}


def _text(node) -> str:
    return node.text.decode("utf-8", "replace")


def _collapse(s: str) -> str:
    return " ".join(s.split())


def _signature(node) -> str:
    """Declaration text up to the body block, collapsed to one line."""
    body = node.child_by_field_name("body")
    if body is not None:
        raw = node.text[: body.start_byte - node.start_byte].decode("utf-8", "replace")
    else:
        raw = _text(node)
    return _collapse(raw).rstrip("{").strip()


def _find_type(node, type_name: str):
    if node is None:
        return None
    if node.type == type_name:
        return node
    for ch in node.named_children:
        found = _find_type(ch, type_name)
        if found is not None:
            return found
    return None


class _TSBackend:
    EXTENSIONS: tuple[str, ...] = ()

    def __init__(self, ts_language) -> None:
        self._lang = Language(ts_language)
        self._parser = Parser(self._lang)

    def _root(self, source: str):
        return self._parser.parse(bytes(source, "utf-8")).root_node


# ── C# ─────────────────────────────────────────────────────────────


class CSharpBackend(_TSBackend):
    EXTENSIONS = (".cs",)
    _TYPES = {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "struct_declaration": "struct",
        "record_declaration": "record",
        "enum_declaration": "enum",
    }
    _MEMBERS = {
        "method_declaration": "method",
        "constructor_declaration": "method",
        "property_declaration": "property",
    }

    def __init__(self) -> None:
        super().__init__(_ts_cs.language())

    def parse_symbols(self, source: str, rel_path: str) -> list[SymbolRecord]:
        out: list[SymbolRecord] = []

        def name_of(n):
            nm = n.child_by_field_name("name")
            return _text(nm) if nm is not None else None

        def walk(node, enclosing=None):
            for ch in node.named_children:
                if ch.type in self._TYPES:
                    nm = name_of(ch)
                    if nm:
                        out.append(SymbolRecord(nm, self._TYPES[ch.type], _signature(ch), rel_path, _range(ch)))
                    walk(ch, nm or enclosing)
                elif ch.type in self._MEMBERS:
                    nm = name_of(ch)
                    if nm:
                        qual = f"{enclosing}.{nm}" if enclosing else nm
                        out.append(SymbolRecord(qual, self._MEMBERS[ch.type], _signature(ch), rel_path, _range(ch)))
                else:
                    walk(ch, enclosing)

        walk(self._root(source))
        return out

    def parse_imports(self, source: str) -> list[ImportRef]:
        """`using` namespaces PLUS referenced PascalCase identifiers.

        The PascalCase identifiers are candidate type references. They only
        resolve if the repo declares a type with that name (the index filters
        out external types and method/local names), so this covers code that
        uses no namespaces at all — common in Unity.
        """
        refs: list[ImportRef] = []
        seen: set[str] = set()

        def add(mod: str) -> None:
            if mod and mod not in seen:
                seen.add(mod)
                refs.append(ImportRef(module=mod, level=0))

        def walk(node):
            for ch in node.named_children:
                if ch.type == "using_directive":
                    ns = _text(ch).replace("using", "", 1).replace("static", "", 1).strip().rstrip(";").strip()
                    if "=" in ns:  # alias: using Foo = A.B.C;
                        ns = ns.split("=")[-1].strip()
                    if ns and ns[0].isalpha():
                        add(ns)
                    continue
                if ch.type == "identifier":
                    t = _text(ch)
                    if t[:1].isupper():
                        add(t)
                walk(ch)

        walk(self._root(source))
        return refs

    def provides_keys(self, source: str, rel_path: str) -> list[str]:
        """Namespaces declared in the file AND the names of types it declares.

        The type names let dependency resolution work without `using` — a file
        that declares `class Inventory` is the resolver target for any module
        that mentions `Inventory`.
        """
        keys: list[str] = []

        def walk(node):
            for ch in node.named_children:
                if ch.type in ("namespace_declaration", "file_scoped_namespace_declaration"):
                    nm = ch.child_by_field_name("name")
                    if nm is not None:
                        keys.append(_text(nm))
                elif ch.type in self._TYPES:
                    nm = ch.child_by_field_name("name")
                    if nm is not None:
                        keys.append(_text(nm))
                walk(ch)

        walk(self._root(source))
        return keys

    def import_keys(self, ref: ImportRef, from_rel: str) -> list[str]:
        return [ref.module] if ref.module else []


# ── C++ ────────────────────────────────────────────────────────────

_INCLUDE_RE = re.compile(r'#\s*include\s*"([^"]+)"')


class CppBackend(_TSBackend):
    EXTENSIONS = (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".h")

    def __init__(self) -> None:
        super().__init__(_ts_cpp.language())

    def _fn_name(self, declarator):
        fd = _find_type(declarator, "function_declarator")
        if fd is None:
            return None
        base = fd.child_by_field_name("declarator")
        return _text(base) if base is not None else None

    def parse_symbols(self, source: str, rel_path: str) -> list[SymbolRecord]:
        out: list[SymbolRecord] = []

        def walk(node, enclosing=None):
            for ch in node.named_children:
                if ch.type in ("class_specifier", "struct_specifier"):
                    nm = ch.child_by_field_name("name")
                    nmtxt = _text(nm) if nm is not None else None
                    if nmtxt:
                        out.append(SymbolRecord(nmtxt, "class", _signature(ch), rel_path, _range(ch)))
                    walk(ch, nmtxt or enclosing)
                elif ch.type in ("function_definition", "declaration", "field_declaration"):
                    decl = ch.child_by_field_name("declarator")
                    nm = self._fn_name(decl) if decl is not None else None
                    if nm:
                        kind = "function" if ch.type == "function_definition" else "method"
                        qual = f"{enclosing}.{nm}" if (enclosing and "::" not in nm) else nm
                        out.append(SymbolRecord(qual, kind, _signature(ch), rel_path, _range(ch)))
                    else:
                        walk(ch, enclosing)
                else:
                    walk(ch, enclosing)

        walk(self._root(source))
        return out

    def parse_imports(self, source: str) -> list[ImportRef]:
        # Local includes only ("..."); system includes (<...>) are external.
        return [ImportRef(module=m, level=0) for m in _INCLUDE_RE.findall(source)]

    def provides_keys(self, source: str, rel_path: str) -> list[str]:
        return [rel_path, rel_path.split("/")[-1]]

    def import_keys(self, ref: ImportRef, from_rel: str) -> list[str]:
        inc = ref.module
        keys = [inc, inc.split("/")[-1]]
        d = "/".join(from_rel.split("/")[:-1])
        if d:
            keys.append(f"{d}/{inc}")
        return keys

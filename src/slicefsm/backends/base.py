"""Backend interface + shared data types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class SymbolRecord:
    """One public symbol. `range` is 1-based inclusive line span.

    `range` is preserved on dependency records on purpose: expand_symbol uses
    it to slice the body in O(1) without re-parsing.
    """

    name: str
    kind: str  # "function" | "class" | "method"
    signature: str
    source_path: str  # relative to project root
    range: dict[str, int] = field(default_factory=dict)  # {"start": int, "end": int}
    doc: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "signature": self.signature,
            "source_path": self.source_path,
            "range": self.range,
            "doc": self.doc,
        }


@dataclass
class ImportRef:
    """An import found in a file. `level` > 0 means a relative import."""

    module: str  # dotted module, or "" for bare relative ("from . import x")
    level: int = 0


class LanguageBackend(Protocol):
    EXTENSIONS: tuple[str, ...]

    def parse_symbols(self, source: str, rel_path: str) -> list[SymbolRecord]:
        """Top-level public symbols (and their public methods) in a file."""
        ...

    def parse_imports(self, source: str) -> list[ImportRef]:
        """Import refs in a file (for dependency resolution)."""
        ...

    def provides_keys(self, source: str, rel_path: str) -> list[str]:
        """Resolution keys this file provides (e.g. its module / namespace /
        header path). The engine builds a key -> files index from these."""
        ...

    def import_keys(self, ref: ImportRef, from_rel: str) -> list[str]:
        """Resolution keys an import wants. The engine looks these up in the
        index to find the dependency files. Language-uniform string matching."""
        ...

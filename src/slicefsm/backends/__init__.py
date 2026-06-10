"""Language backends for the context engine.

Each backend turns source text into symbol records and import refs. The engine
stays language-agnostic; only the backend is language-specific. Python uses the
stdlib `ast`; C# and C++ use tree-sitter (registered only if the grammar wheels
are present, so a missing grammar degrades gracefully).
"""

from __future__ import annotations

from .base import ImportRef, LanguageBackend, SymbolRecord
from .python_ast import PythonAstBackend
from . import treesitter

# extension -> backend instance
_BACKENDS: dict[str, LanguageBackend] = {}


def register(backend: LanguageBackend) -> None:
    for ext in backend.EXTENSIONS:
        _BACKENDS[ext] = backend


def backend_for(path: str) -> LanguageBackend | None:
    for ext, backend in _BACKENDS.items():
        if path.endswith(ext):
            return backend
    return None


def known_extensions() -> tuple[str, ...]:
    return tuple(_BACKENDS.keys())


register(PythonAstBackend())

if treesitter.available():
    register(treesitter.CSharpBackend())
    register(treesitter.CppBackend())


def available_languages() -> list[str]:
    """Human-readable list of registered languages (for diagnostics)."""
    langs = {"py": "python"}
    if treesitter.available():
        langs.update({"cs": "c#", "cpp": "c++"})
    return sorted(set(langs.values()))


__all__ = [
    "ImportRef",
    "LanguageBackend",
    "SymbolRecord",
    "PythonAstBackend",
    "backend_for",
    "known_extensions",
    "available_languages",
    "register",
]

"""Language backends for the context engine.

Each backend turns source text into symbol records and import refs. The engine
stays language-agnostic; only the backend is language-specific. Python `ast` is
the first concrete backend. A tree-sitter backend can be added behind the same
interface later without touching the engine.
"""

from __future__ import annotations

from .base import ImportRef, LanguageBackend, SymbolRecord
from .python_ast import PythonAstBackend

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

__all__ = [
    "ImportRef",
    "LanguageBackend",
    "SymbolRecord",
    "PythonAstBackend",
    "backend_for",
    "known_extensions",
    "register",
]

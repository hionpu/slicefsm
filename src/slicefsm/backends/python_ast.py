"""Python backend built on the stdlib `ast` module.

No third-party deps, no native build. Good enough to drive repo-map, the
3-bucket slice context, and single-symbol expand for Python projects. A
tree-sitter backend can replace/extend this later behind the same interface.
"""

from __future__ import annotations

import ast

from .base import ImportRef, SymbolRecord


def _is_public(name: str) -> bool:
    return not name.startswith("_") or (name.startswith("__") and name.endswith("__"))


def _node_start(node: ast.AST) -> int:
    """1-based start line including any decorators."""
    decorators = getattr(node, "decorator_list", None)
    if decorators:
        return min(d.lineno for d in decorators)
    return node.lineno


def _fn_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    try:
        args = ast.unparse(node.args)
    except Exception:
        args = "..."
    returns = ""
    if node.returns is not None:
        try:
            returns = f" -> {ast.unparse(node.returns)}"
        except Exception:
            returns = ""
    return f"{prefix} {node.name}({args}){returns}:"


def _cls_signature(node: ast.ClassDef) -> str:
    bases = []
    for b in node.bases:
        try:
            bases.append(ast.unparse(b))
        except Exception:
            pass
    base_str = f"({', '.join(bases)})" if bases else ""
    return f"class {node.name}{base_str}:"


def _doc_first_line(node: ast.AST) -> str | None:
    doc = ast.get_docstring(node)
    if not doc:
        return None
    return doc.strip().splitlines()[0].strip() or None


class PythonAstBackend:
    EXTENSIONS: tuple[str, ...] = (".py",)

    def parse_symbols(self, source: str, rel_path: str) -> list[SymbolRecord]:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []
        records: list[SymbolRecord] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not _is_public(node.name):
                    continue
                records.append(
                    SymbolRecord(
                        name=node.name,
                        kind="function",
                        signature=_fn_signature(node),
                        source_path=rel_path,
                        range={"start": _node_start(node), "end": node.end_lineno or node.lineno},
                        doc=_doc_first_line(node),
                    )
                )
            elif isinstance(node, ast.ClassDef):
                if not _is_public(node.name):
                    continue
                records.append(
                    SymbolRecord(
                        name=node.name,
                        kind="class",
                        signature=_cls_signature(node),
                        source_path=rel_path,
                        range={"start": _node_start(node), "end": node.end_lineno or node.lineno},
                        doc=_doc_first_line(node),
                    )
                )
                # public methods as their own records (kind=method, qualified name)
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_public(sub.name):
                        records.append(
                            SymbolRecord(
                                name=f"{node.name}.{sub.name}",
                                kind="method",
                                signature=_fn_signature(sub),
                                source_path=rel_path,
                                range={"start": _node_start(sub), "end": sub.end_lineno or sub.lineno},
                                doc=_doc_first_line(sub),
                            )
                        )
        return records

    def parse_imports(self, source: str) -> list[ImportRef]:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []
        refs: list[ImportRef] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    refs.append(ImportRef(module=alias.name, level=0))
            elif isinstance(node, ast.ImportFrom):
                refs.append(ImportRef(module=node.module or "", level=node.level or 0))
        return refs

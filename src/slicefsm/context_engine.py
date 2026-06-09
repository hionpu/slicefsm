"""Context engine: repo-map, 3-bucket slice context, single-symbol expand.

This is the token wall. It serves:
  - map mode: top-level packages + public symbol names, no bodies.
  - slice mode: own-module full text, deps as signatures (bodies stripped),
    siblings as names only.
  - expand mode: one symbol's body via its stored line range, O(1).

Language detail lives in the backends. The engine only walks files, resolves
imports, buckets the result, and writes a manifest the hook can read.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import backends

STATE_DIRNAME = ".harness"
_SKIP_DIRS = {
    ".git", ".harness", "__pycache__", ".venv", "venv", "node_modules",
    ".mypy_cache", ".pytest_cache", "dist", "build", ".tox", ".idea", ".vscode",
    ".egg-info",
}
_SRC_PREFIXES = ("src", "lib", "app", "source", "pkg", "internal")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _skip(rel_parts: tuple[str, ...]) -> bool:
    return any(part in _SKIP_DIRS or part.endswith(".egg-info") for part in rel_parts)


def _iter_source_files(root: Path):
    exts = backends.known_extensions()
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel_parts = p.relative_to(root).parts
        if _skip(rel_parts):
            continue
        if p.suffix in exts:
            yield p


def _rel(root: Path, p: Path) -> str:
    return p.relative_to(root).as_posix()


def _top_module(rel: str) -> str:
    parts = [p for p in rel.split("/") if p]
    if parts and parts[0].lower() in _SRC_PREFIXES:
        parts = parts[1:]
    return parts[0] if parts else "."


# ── repo map (map mode) ────────────────────────────────────────────


def build_repo_map(project_root: str | Path) -> dict[str, Any]:
    """Per-file public symbol names. No bodies, no signatures.

    Keyed by file path (not package dir) so the AI knows where each symbol
    lives — still compact, just names.
    """
    root = Path(project_root).resolve()
    packages: dict[str, list[str]] = {}
    for f in _iter_source_files(root):
        backend = backends.backend_for(f.name)
        if backend is None:
            continue
        rel = _rel(root, f)
        try:
            src = f.read_text(encoding="utf-8")
        except OSError:
            continue
        names = [r.name for r in backend.parse_symbols(src, rel) if r.kind != "method"]
        if names:
            packages.setdefault(rel, []).extend(names)
    return {"packages": packages, "created_at": _now()}


# ── import resolution ──────────────────────────────────────────────


def _candidate_paths(root: Path, dotted: str) -> list[Path]:
    if not dotted:
        return []
    rel = Path(*dotted.split("."))
    bases = [root] + [root / pre for pre in _SRC_PREFIXES if (root / pre).is_dir()]
    cands: list[Path] = []
    for base in bases:
        cands.append(base / f"{rel}.py")
        cands.append(base / rel / "__init__.py")
    return cands


def _resolve_import(root: Path, from_file: Path, ref) -> Path | None:
    """Resolve an ImportRef to an in-project file, or None if external."""
    if ref.level and ref.level > 0:
        base = from_file.parent
        for _ in range(ref.level - 1):
            base = base.parent
        if ref.module:
            rel = Path(*ref.module.split("."))
            for cand in (base / f"{rel}.py", base / rel / "__init__.py"):
                if cand.is_file():
                    return cand
            return None
        cand = base / "__init__.py"
        return cand if cand.is_file() else None
    for cand in _candidate_paths(root, ref.module):
        if cand.is_file():
            return cand
    return None


# ── slice context (slice mode) ─────────────────────────────────────


def _resolve_module(root: Path, module: str) -> list[Path]:
    target = (root / module).resolve()
    if target.is_file():
        return [target]
    if target.is_dir():
        return [f for f in _iter_source_files(target) if backends.backend_for(f.name)]
    return []


def _excluded_modules(root: Path, touched_rel: set[str]) -> list[str]:
    touched_roots = {_top_module(rel) for rel in touched_rel}
    all_roots = {_top_module(_rel(root, f)) for f in _iter_source_files(root)}
    return sorted(all_roots - touched_roots)


def _write_manifest(root: Path, module: str, manifest: dict[str, Any]) -> Path:
    d = root / STATE_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    safe = module.replace("/", "-").replace("\\", "-").strip("-") or "root"
    path = d / f"slice-context-{safe}.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def get_slice_context(
    project_root: str | Path,
    module: str,
    depth: int = 1,
    write_manifest: bool = True,
) -> dict[str, Any]:
    """Build the 3-bucket bounded context for one slice."""
    root = Path(project_root).resolve()
    module_files = _resolve_module(root, module)
    module_rel = {_rel(root, f) for f in module_files}

    module_bucket: dict[str, str] = {}
    for f in module_files:
        try:
            module_bucket[_rel(root, f)] = f.read_text(encoding="utf-8")
        except OSError:
            pass

    # BFS imports to `depth`, collecting in-project dependency files.
    dep_files: dict[str, Path] = {}
    seen = set(module_rel)
    frontier = list(module_files)
    for _ in range(max(depth, 1)):
        next_frontier: list[Path] = []
        for f in frontier:
            backend = backends.backend_for(f.name)
            if backend is None:
                continue
            try:
                src = f.read_text(encoding="utf-8")
            except OSError:
                continue
            for ref in backend.parse_imports(src):
                dep = _resolve_import(root, f, ref)
                if dep is None:
                    continue
                rel = _rel(root, dep)
                if rel in seen:
                    continue
                seen.add(rel)
                dep_files[rel] = dep
                next_frontier.append(dep)
        frontier = next_frontier

    dependencies: list[dict[str, Any]] = []
    for rel, dep in sorted(dep_files.items()):
        backend = backends.backend_for(dep.name)
        if backend is None:
            continue
        try:
            src = dep.read_text(encoding="utf-8")
        except OSError:
            continue
        for rec in backend.parse_symbols(src, rel):
            dependencies.append(rec.to_dict())

    excluded = _excluded_modules(root, module_rel | set(dep_files))

    manifest = {
        "module": module,
        "module_files": sorted(module_rel),
        "dependencies": dependencies,
        "excluded": excluded,
        "depth": depth,
        "created_at": _now(),
    }
    manifest_path = _write_manifest(root, module, manifest) if write_manifest else None

    return {
        "module": module,
        "module_files": module_bucket,   # rel -> full text (you edit these)
        "dependencies": dependencies,    # signatures only, range preserved
        "excluded": excluded,            # names only
        "manifest_path": str(manifest_path) if manifest_path else None,
        "counts": {
            "module_files": len(module_bucket),
            "dependencies": len(dependencies),
            "excluded": len(excluded),
        },
    }


# ── expand (expand mode) ───────────────────────────────────────────


def expand_symbol(
    project_root: str | Path,
    name: str,
    source_path: str | None = None,
    manifest_path: str | None = None,
) -> dict[str, Any]:
    """Return exactly one symbol's body via its stored line range.

    Resolve order: explicit source_path > manifest dependency record. The
    manifest path lets the caller expand a dep by name alone (range is stored
    on the dep record, so no re-scan of the whole file is needed to locate it).
    """
    root = Path(project_root).resolve()
    rng: dict[str, int] | None = None
    sp = source_path

    if source_path:
        f = root / source_path
        backend = backends.backend_for(f.name)
        if backend is not None and f.is_file():
            try:
                src = f.read_text(encoding="utf-8")
            except OSError:
                src = ""
            for r in backend.parse_symbols(src, source_path):
                if r.name == name:
                    rng = r.range
                    break
    elif manifest_path and Path(manifest_path).is_file():
        try:
            man = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            man = {}
        for d in man.get("dependencies", []):
            if d.get("name") == name:
                rng = d.get("range")
                sp = d.get("source_path")
                break

    if not rng or not sp:
        return {"error": "symbol_not_found", "name": name}

    f = root / sp
    try:
        lines = f.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {"error": "source_unreadable", "name": name, "source_path": sp}

    start = max(int(rng.get("start", 1)), 1)
    end = min(int(rng.get("end", start)), len(lines))
    body = "\n".join(lines[start - 1 : end])
    return {"name": name, "source_path": sp, "range": {"start": start, "end": end}, "body": body}

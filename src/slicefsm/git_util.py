"""Best-effort git helpers for rollback checkpoints and authorship.

`git stash create` builds a commit object from the working tree without
touching the working tree or the stash stack — a clean rollback ref. Never
raises; returns structured status so callers can degrade on non-git repos.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


def _run(args: list[str], cwd: str | Path) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            args, cwd=str(cwd), capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None


def is_git_repo(root: str | Path) -> bool:
    r = _run(["git", "rev-parse", "--is-inside-work-tree"], root)
    return bool(r and r.returncode == 0 and r.stdout.strip() == "true")


def is_dirty(root: str | Path) -> bool:
    r = _run(["git", "status", "--porcelain"], root)
    return bool(r and r.returncode == 0 and r.stdout.strip())


def make_checkpoint(root: str | Path) -> dict[str, Any]:
    """Create a rollback ref. Returns {ok, ref, dirty} or {ok:False, reason}."""
    if not is_git_repo(root):
        return {"ok": False, "reason": "not_a_git_repo"}
    dirty = is_dirty(root)
    r = _run(["git", "stash", "create", "slicefsm checkpoint"], root)
    ref = r.stdout.strip() if r and r.returncode == 0 else ""
    if not ref:
        # Clean tree: nothing to stash. Use HEAD as the rollback ref.
        h = _run(["git", "rev-parse", "HEAD"], root)
        ref = h.stdout.strip() if h and h.returncode == 0 else ""
    return {"ok": bool(ref), "ref": ref, "dirty": dirty}


def diff_numstat(root: str | Path) -> dict[str, Any]:
    """Working-tree changes vs HEAD: files touched + lines added."""
    r = _run(["git", "diff", "--numstat", "HEAD"], root)
    files: set[str] = set()
    added = 0
    if r and r.returncode == 0:
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                files.add(parts[2])
                try:
                    added += int(parts[0])
                except ValueError:
                    pass
    return {"files": sorted(files), "lines_added": added}

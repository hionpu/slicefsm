"""Authorship telemetry (H2). Surfaced, never gated.

The PostToolUse hook appends one line per AI edit/write to
`<root>/.harness/edits.log`. At slice close, authorship() reports how much of
the slice's diff the AI authored vs the human. A learning signal, not a quality
score: high AI ratio can still be good code.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_DIRNAME = ".harness"
EDITS_FILENAME = "edits.log"
EXPANDS_FILENAME = "expands.log"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append(root: str | Path, feature_id: str, filename: str, entry: dict[str, Any]) -> None:
    # Per-feature: slice ids restart at 1 in each feature, so logs must not mix.
    from . import state

    try:
        d = state.feature_dir(root, feature_id)
        d.mkdir(parents=True, exist_ok=True)
        with (d / filename).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def append_edit(root: str | Path, feature_id: str, slice_id: Any, path: str, tool: str) -> None:
    _append(root, feature_id, EDITS_FILENAME, {"ts": _now(), "slice_id": slice_id, "path": path, "tool": tool})


def append_expand(root: str | Path, feature_id: str, slice_id: Any, symbol: str, reason: str | None) -> None:
    _append(root, feature_id, EXPANDS_FILENAME, {"ts": _now(), "slice_id": slice_id, "symbol": symbol, "reason": reason})


def _read_lines(root: str | Path, feature_id: str, filename: str) -> list[dict[str, Any]]:
    from . import state

    p = state.feature_dir(root, feature_id) / filename
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
    except (OSError, json.JSONDecodeError):
        pass
    return out


def authorship(root: str | Path, feature_id: str, slice_id: Any) -> dict[str, Any]:
    """AI edit count for the slice + total git diff size. Surfaced only."""
    from . import git_util

    ai_edits = sum(1 for e in _read_lines(root, feature_id, EDITS_FILENAME) if e.get("slice_id") == slice_id)
    expands = sum(1 for e in _read_lines(root, feature_id, EXPANDS_FILENAME) if e.get("slice_id") == slice_id)
    diff = git_util.diff_numstat(root) if git_util.is_git_repo(root) else {"files": [], "lines_added": 0}
    return {
        "ai_edits": ai_edits,
        "expand_calls": expands,
        "files_changed": len(diff["files"]),
        "lines_added": diff["lines_added"],
    }

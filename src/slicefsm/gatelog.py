"""Append-only gate log.

Every state transition and gate-bearing tool call appends one JSON line to
`<project_root>/.harness/gates.jsonl`. A human can grep the history of what
the AI hit during a session. Best-effort: never raises.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_DIRNAME = ".harness"
GATES_FILENAME = "gates.jsonl"


def append_gate_event(
    project_root: str | Path, tool: str, decision: dict[str, Any], feature_id: str | None = None
) -> str | None:
    """Append one gate line. Returns the path written, or None on any failure.

    Global audit log across features; `feature_id` tags each entry.
    """
    try:
        root = Path(project_root).resolve()
        if not root.is_dir():
            return None
        log_dir = root / STATE_DIRNAME
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / GATES_FILENAME
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "feature_id": feature_id,
            "tool": tool,
            "decision": decision,
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return str(path)
    except (OSError, TypeError, ValueError):
        return None

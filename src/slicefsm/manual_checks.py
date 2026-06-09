"""Per-feature manual-check ledger.

A green automatic suite is not enough for ui-heavy work. run_verify consults
this ledger and downgrades to `pending_manual` while required checks remain.
Ledger lives at `<root>/.harness/manual-checks/<feature>.json`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

STATE_DIRNAME = ".harness"


def _safe(feature: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", feature).strip("-") or "feature"


def _path(root: str | Path, feature: str) -> Path:
    return Path(root).resolve() / STATE_DIRNAME / "manual-checks" / f"{_safe(feature)}.json"


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"items": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"items": []}


def _save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _summary(items: list[dict[str, Any]]) -> dict[str, int]:
    required = [i for i in items if i.get("required", True)]
    pending = [i for i in required if i.get("status", "pending") == "pending"]
    return {
        "total": len(items),
        "required": len(required),
        "pending": len(pending),
        "resolved": len(required) - len(pending),
    }


def summary(root: str | Path, feature: str) -> dict[str, Any]:
    data = _load(_path(root, feature))
    return _summary(data["items"])


def pending_items(root: str | Path, feature: str) -> list[dict[str, Any]]:
    data = _load(_path(root, feature))
    return [i for i in data["items"] if i.get("status", "pending") == "pending" and i.get("required", True)]


def track_manual_checks(
    project_root: str | Path,
    feature: str,
    op: str = "summary",
    checks: list[dict[str, Any]] | None = None,
    check_id: str | None = None,
    note: str | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    """Operations: declare | confirm | handoff | list | summary."""
    path = _path(project_root, feature)
    data = _load(path)
    items = data["items"]

    if op == "declare":
        if replace:
            items = []
        existing = {i["id"] for i in items}
        for c in checks or []:
            cid = str(c.get("id") or f"check-{len(items) + 1}")
            if cid in existing:
                continue
            items.append({
                "id": cid,
                "description": c.get("description", ""),
                "required": bool(c.get("required", True)),
                "status": "pending",
            })
        data["items"] = items
        _save(path, data)
    elif op in ("confirm", "handoff"):
        for i in items:
            if i["id"] == check_id:
                i["status"] = "confirmed" if op == "confirm" else "handed_off"
                if note:
                    i["note"] = note
        _save(path, data)
    elif op == "list":
        return {"feature": feature, "items": items, "summary": _summary(items)}

    return {"feature": feature, "summary": _summary(items)}

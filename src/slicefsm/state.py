"""State core: the single source of truth + the FSM.

State lives at `<project_root>/.harness/state.json`. Only MCP tools and the
out-of-band `harness` CLI write it. Hooks read it. This module owns:
  - the phase list and the legal transition map,
  - validation (precondition + legal-transition checks),
  - atomic read/write.

Tool- and CLI-specific field changes are passed in as a `mutator` callback so
the FSM stays generic and unit-testable.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

STATE_DIRNAME = ".harness"
STATE_FILENAME = "state.json"
STATE_VERSION = 3

# A repo holds many features; one is active at a time. Each feature carries its
# own phase below. NO_ACTIVE_FEATURE is a root condition (no feature is active),
# surfaced as a synthetic feature phase so callers can gate on it uniformly.
PHASES = (
    "NO_ACTIVE_FEATURE",
    "NO_FEATURE",
    "DISCOVERY",
    "SLICING",
    "AWAITING_APPROVAL",
    "IN_PROGRESS",
    "FEATURE_DONE",
)

# Per-slice status. Slices are sequential: at most one is `implement` at a time.
SLICE_STATUSES = ("proposed", "implement", "stuck", "done")

# from_phase -> set of legal to_phases (feature level)
LEGAL_TRANSITIONS: dict[str, set[str]] = {
    "NO_FEATURE": {"SLICING", "DISCOVERY"},
    "DISCOVERY": {"AWAITING_APPROVAL", "SLICING"},
    "SLICING": {"AWAITING_APPROVAL"},
    "AWAITING_APPROVAL": {"SLICING", "DISCOVERY", "IN_PROGRESS"},
    "IN_PROGRESS": {"FEATURE_DONE", "SLICING", "DISCOVERY"},
    "FEATURE_DONE": {"SLICING", "DISCOVERY"},
}


class TransitionDenied(Exception):
    """Raised when a transition fails its precondition or is illegal."""

    def __init__(
        self,
        current: str | None,
        expected: Any = None,
        reason: str = "",
    ) -> None:
        self.current = current
        self.expected = expected
        self.reason = reason
        super().__init__(reason or f"transition denied from {current}")

    @property
    def payload(self) -> dict[str, Any]:
        return {
            "error": "transition_denied",
            "current_phase": self.current,
            "expected_phase": self.expected,
            "reason": self.reason,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def state_path(project_root: str | Path) -> Path:
    return Path(project_root).resolve() / STATE_DIRNAME / STATE_FILENAME


def new_feature_state() -> dict[str, Any]:
    """A blank single-feature state (phase NO_FEATURE)."""
    return {
        "phase": "NO_FEATURE",
        "feature": None,
        "scale": None,
        "scale_source": None,
        "scale_provisional": None,
        "scale_measured": None,
        "risky": False,
        "read_policy": None,
        "discovery_summary": None,
        "slices": [],
        "paused": False,
        "approved": None,
        "updated_at": _now(),
    }


# Back-compat alias (used by tests and a few callers).
new_state = new_feature_state


def new_root() -> dict[str, Any]:
    """A fresh repo state: no features yet."""
    return {"version": STATE_VERSION, "active_feature_id": None, "features": {}}


def _no_active() -> dict[str, Any]:
    """Synthetic feature-state returned when no feature is active."""
    s = new_feature_state()
    s["phase"] = "NO_ACTIVE_FEATURE"
    return s


def feature_dir(project_root: str | Path, feature_id: str | None) -> Path:
    """Per-feature artifact directory: .harness/features/<id>/."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", str(feature_id or "feature")).strip("-") or "feature"
    return Path(project_root).resolve() / STATE_DIRNAME / "features" / safe


# ── root-level IO ──────────────────────────────────────────────────


def read_root(project_root: str | Path) -> dict[str, Any]:
    """Read the full repo state, migrating older single-feature files."""
    path = state_path(project_root)
    if not path.exists():
        return new_root()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return new_root()
    if not isinstance(data, dict):
        return new_root()
    if "features" in data and isinstance(data["features"], dict):
        data.setdefault("active_feature_id", None)
        data.setdefault("version", STATE_VERSION)
        return data
    # Migrate a v1/v2 flat feature-state into the multi-feature shape.
    if "phase" in data:
        fid = (data.get("feature") or {}).get("id") or "legacy"
        data.pop("version", None)
        return {"version": STATE_VERSION, "active_feature_id": fid, "features": {fid: data}}
    return new_root()


def write_root(project_root: str | Path, root_state: dict[str, Any]) -> Path:
    """Write the full repo state atomically."""
    path = state_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    root_state["version"] = STATE_VERSION
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(root_state, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    return path


def active_feature(root_state: dict[str, Any]) -> dict[str, Any] | None:
    fid = root_state.get("active_feature_id")
    if fid and fid in root_state.get("features", {}):
        return root_state["features"][fid]
    return None


# ── active-feature IO (most callers use these unchanged) ───────────


def read(project_root: str | Path) -> dict[str, Any]:
    """Return the ACTIVE feature's state, or a NO_ACTIVE_FEATURE sentinel."""
    fs = active_feature(read_root(project_root))
    return fs if fs is not None else _no_active()


def write(project_root: str | Path, feature_state: dict[str, Any]) -> Path:
    """Write `feature_state` back into the active feature slot."""
    rs = read_root(project_root)
    fid = rs.get("active_feature_id") or (feature_state.get("feature") or {}).get("id")
    if fid:
        feature_state["updated_at"] = _now()
        rs.setdefault("features", {})[fid] = feature_state
        rs.setdefault("active_feature_id", fid)
    return write_root(project_root, rs)


def transition(
    state: dict[str, Any],
    to_phase: str,
    *,
    expect: str | list[str] | set[str] | None = None,
    mutator: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Validate and apply a phase transition in memory. Does not write.

    expect:   required current phase(s). If current is not in it -> denied.
    to_phase: target; must be a legal transition from current.
    mutator:  optional fn(state) -> None applied before the phase is set.
    """
    current = state.get("phase")

    if expect is not None:
        allowed_from = {expect} if isinstance(expect, str) else set(expect)
        if current not in allowed_from:
            raise TransitionDenied(
                current,
                sorted(allowed_from),
                f"expected phase {sorted(allowed_from)}, but in {current}",
            )

    if to_phase not in LEGAL_TRANSITIONS.get(current, set()):
        raise TransitionDenied(
            current,
            to_phase,
            f"{current} -> {to_phase} is not a legal transition",
        )

    if mutator is not None:
        mutator(state)

    state["phase"] = to_phase
    return state


def find_slice(state: dict[str, Any], slice_id: Any) -> dict[str, Any] | None:
    """Return the slice with the given id, or None."""
    for s in state.get("slices", []):
        if s.get("id") == slice_id:
            return s
    return None


def active_slices(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Slices currently being implemented (editable). May be several at once."""
    return [s for s in state.get("slices", []) if s.get("status") == "implement"]


def all_done(state: dict[str, Any]) -> bool:
    slices = state.get("slices", [])
    return bool(slices) and all(s.get("status") == "done" for s in slices)

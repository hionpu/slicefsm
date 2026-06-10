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
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

STATE_DIRNAME = ".harness"
STATE_FILENAME = "state.json"
STATE_VERSION = 2

# Feature-level phases. Individual slices carry their own status (below) so
# that several slices can be in progress at once.
PHASES = (
    "NO_FEATURE",
    "DISCOVERY",
    "SLICING",
    "AWAITING_APPROVAL",
    "IN_PROGRESS",
    "FEATURE_DONE",
)

# Per-slice status (parallel): many slices may be `implement` at the same time.
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


def new_state() -> dict[str, Any]:
    """A fresh NO_FEATURE state."""
    return {
        "version": STATE_VERSION,
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
        "approved": None,
        "updated_at": _now(),
    }


def read(project_root: str | Path) -> dict[str, Any]:
    """Read state, or return a fresh NO_FEATURE state if absent/corrupt."""
    path = state_path(project_root)
    if not path.exists():
        return new_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return new_state()
    if not isinstance(data, dict) or "phase" not in data:
        return new_state()
    return data


def write(project_root: str | Path, state: dict[str, Any]) -> Path:
    """Write state atomically. Stamps updated_at."""
    path = state_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _now()
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
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

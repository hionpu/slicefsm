"""Hook dispatcher: `slicefsm-hook <event>`.

Reads the Claude-style event JSON on stdin, reads .harness/state.json, and
emits a Claude-style decision. Hooks are read-only enforcers; they never write
state. Four events:

  userpromptsubmit  inject only the current state's prompt (token + determinism)
  pretooluse        deny tools illegal in the current state (the hard block)
  posttooluse       log AI edits for authorship telemetry
  stop              soft reminder only (best-effort; no hard gate)

Tool names are lowercased before matching (Pi emits edit/write/bash; Claude
emits Edit/Write/Bash).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from . import edits, policy, state

MCP_OPS = {
    "submit_feature",
    "propose_slices",
    "get_slice_context",
    "expand_symbol",
    "run_verify",
    "analyze_verify_failure",
    "track_manual_checks",
}

PHASE_MCP: dict[str, set[str]] = {
    "NO_FEATURE": {"submit_feature"},
    "FEATURE_DONE": {"submit_feature"},
    "DISCOVERY": {"propose_slices"},
    "SLICING": {"propose_slices"},
    "AWAITING_APPROVAL": {"propose_slices"},
    "SLICE_SCOPING": {"get_slice_context"},
    "SLICE_IMPLEMENT": {"expand_symbol", "run_verify", "track_manual_checks"},
    "SLICE_VERIFY": {"run_verify", "analyze_verify_failure", "track_manual_checks", "expand_symbol"},
    "STUCK": {"analyze_verify_failure"},
}

_EDIT_TOOLS = {"edit", "write", "multiedit", "notebookedit", "create", "apply_patch", "str_replace", "str_replace_editor"}
_READ_TOOLS = {"read", "cat", "view"}
_SHELL_TOOLS = {"bash", "shell", "sh", "powershell", "pwsh", "cmd"}
# Human-only state changes. Matched broadly so the module form
# (`python -m slicefsm.cli approve`) is caught too, not just `harness approve`.
_HUMAN_ONLY_VERBS = ("approve", "explain", "unstick", "reslice")
_CLI_MARKERS = ("harness", "slicefsm.cli")


# ── pure decision ──────────────────────────────────────────────────


def _mcp_op(tool_lower: str) -> str | None:
    for op in MCP_OPS:
        if op in tool_lower:
            return op
    return None


def _target(tool_input: Any, project_root: str | None = None) -> str | None:
    if not isinstance(tool_input, dict):
        return None
    p = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("notebook_path")
    if not p:
        return None
    if project_root:
        try:
            rel = Path(str(p)).resolve().relative_to(Path(project_root).resolve())
            return rel.as_posix()
        except (ValueError, OSError):
            return Path(str(p)).as_posix()
    return Path(str(p)).as_posix()


def _in_set(rel: str | None, module_files: list[str] | None) -> bool:
    if rel is None or not module_files:
        return False
    norm = Path(rel).as_posix()
    return any(Path(m).as_posix() == norm for m in module_files)


def decide(
    phase: str,
    tool_name: str,
    tool_input: Any,
    read_mode: str = "strict",
    module_files: list[str] | None = None,
    project_root: str | None = None,
) -> tuple[bool, str]:
    """Return (allow, reason). reason is non-empty only on deny."""
    t = (tool_name or "").lower()

    # 1. Self-approve hole: human-only commands can never run as a tool call.
    if t in _SHELL_TOOLS:
        cmd = str((tool_input or {}).get("command", "")).strip().lower()
        is_cli = any(m in cmd for m in _CLI_MARKERS)
        if is_cli and any(v in cmd for v in _HUMAN_ONLY_VERBS):
            return False, "approve / explain / unstick / reslice are human-only, out-of-band commands. Run them yourself in the terminal; the AI cannot."

    # 2. MCP tools gated by phase.
    op = _mcp_op(t)
    if op:
        allowed = PHASE_MCP.get(phase, set())
        if op in allowed:
            return True, ""
        return False, f"{op} is not allowed in {phase}. Allowed MCP here: {sorted(allowed) or 'none'}."

    # 3. Edit/write: only in implement/verify, only within the slice module.
    if t in _EDIT_TOOLS:
        if phase not in ("SLICE_IMPLEMENT", "SLICE_VERIFY"):
            return False, f"edits are not allowed in {phase}."
        rel = _target(tool_input, project_root)
        if module_files is None:
            return False, "no slice context loaded; call get_slice_context first."
        if _in_set(rel, module_files):
            return True, ""
        return False, (
            f"'{rel}' is outside the slice module. Edit only: {sorted(module_files)}. "
            "For an out-of-module change, stop and get human approval."
        )

    # 4. Read: bounded in implement/verify/stuck per read_mode.
    if t in _READ_TOOLS:
        if phase in ("SLICE_IMPLEMENT", "SLICE_VERIFY", "STUCK"):
            rel = _target(tool_input, project_root)
            if module_files and rel and not _in_set(rel, module_files):
                if phase == "STUCK" or read_mode == "strict":
                    return False, (
                        f"strict read: '{rel}' is outside the slice. Use expand_symbol for a "
                        "dependency body, or declare a boundary-cross to the human."
                    )
                return True, ""  # relaxed: allowed (PostToolUse logs it)
        return True, ""

    # 5. Shell and unknown tools: allowed. Honest limit: a shell can bypass the
    #    edit gate. Tracked, not blocked.
    return True, ""


# ── IO / dispatch ──────────────────────────────────────────────────


def _module_files(s: dict[str, Any], project_root: str) -> list[str] | None:
    cs = state.current_slice(s)
    manifest = cs.get("manifest") if cs else None
    if not manifest:
        return None
    try:
        man = json.loads(Path(manifest).read_text(encoding="utf-8"))
        mf = man.get("module_files")
        return mf if isinstance(mf, list) else None
    except (OSError, json.JSONDecodeError):
        return None


def build_state_prompt(s: dict[str, Any]) -> str:
    phase = s.get("phase", "NO_FEATURE")
    read_mode = (s.get("read_policy") or {}).get("mode", "strict")
    cs = state.current_slice(s)
    n = len(s.get("slices", []))
    cur = s.get("current_slice")
    slice_line = ""
    if cs:
        slice_line = f' Slice {cur}/{n}: "{cs.get("title","")}" (module: {cs.get("module","?")}).'

    base = {
        "NO_FEATURE": "No active feature. To start, call submit_feature(desc). No edits until a feature is sliced and approved.",
        "DISCOVERY": "DISCOVERY (read-only). Scan the code to understand structure. No edits. When ready, call propose_slices(slices, discovery_summary=...).",
        "SLICING": "SLICING. Split the feature into vertical, user-visible slices. No edits. Call propose_slices(slices).",
        "AWAITING_APPROVAL": "AWAITING_APPROVAL. Slices are proposed. Only the human can approve (out-of-band: harness approve). Do not start implementing.",
        "SLICE_SCOPING": "SLICE_SCOPING.{slice} First action: get_slice_context(module). No edits until the context is loaded.",
        "SLICE_IMPLEMENT": "SLICE_IMPLEMENT.{slice} Edit only within the loaded module. For a dependency body, call expand_symbol — do not read the whole file. When verify_how is satisfiable, call run_verify.",
        "SLICE_VERIFY": "SLICE_VERIFY.{slice} Run run_verify. On failure, call analyze_verify_failure before patching.",
        "STUCK": "STUCK.{slice} Verify failed repeatedly. STOP editing. Diagnose only (analyze_verify_failure), then ask the human to run: harness unstick.",
        "FEATURE_DONE": "FEATURE_DONE. The feature is closed. Start a new one with submit_feature.",
    }.get(phase, "Unknown phase.")
    text = base.replace("{slice}", slice_line)
    if phase in ("SLICE_IMPLEMENT", "SLICE_VERIFY"):
        text += f" Read mode: {read_mode}."
    return f"[slicefsm] {text}"


def _emit(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))


def _handle_userpromptsubmit(project_root: str, event: dict[str, Any]) -> int:
    s = state.read(project_root)
    _emit({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": build_state_prompt(s),
        }
    })
    return 0


def _handle_pretooluse(project_root: str, event: dict[str, Any]) -> int:
    s = state.read(project_root)
    phase = s.get("phase", "NO_FEATURE")
    read_mode = (s.get("read_policy") or {}).get("mode", "strict")
    module_files = _module_files(s, project_root)
    allow, reason = decide(
        phase,
        event.get("tool_name", ""),
        event.get("tool_input", {}),
        read_mode=read_mode,
        module_files=module_files,
        project_root=project_root,
    )
    if not allow:
        _emit({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        })
    return 0


def _handle_posttooluse(project_root: str, event: dict[str, Any]) -> int:
    s = state.read(project_root)
    if s.get("phase") in ("SLICE_IMPLEMENT", "SLICE_VERIFY"):
        t = str(event.get("tool_name", "")).lower()
        if t in _EDIT_TOOLS:
            rel = _target(event.get("tool_input", {}), project_root)
            if rel:
                edits.append_edit(project_root, s.get("current_slice"), rel, t)
    return 0


def _handle_stop(project_root: str, event: dict[str, Any]) -> int:
    # Soft only. Never block (best-effort event). No output avoids loop risk.
    return 0


_HANDLERS = {
    "userpromptsubmit": _handle_userpromptsubmit,
    "pretooluse": _handle_pretooluse,
    "posttooluse": _handle_posttooluse,
    "stop": _handle_stop,
}


def main() -> None:
    event_name = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        event = {}
    project_root = event.get("cwd") or str(Path.cwd())
    handler = _HANDLERS.get(event_name)
    if handler is None:
        sys.exit(0)
    try:
        sys.exit(handler(project_root, event))
    except Exception:
        # A hook must never crash the host. Fail open (allow).
        sys.exit(0)


if __name__ == "__main__":
    main()

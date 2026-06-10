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
    "list_slices",
    "get_slice_context",
    "expand_symbol",
    "run_verify",
    "analyze_verify_failure",
    "track_manual_checks",
}

# Feature-phase -> allowed MCP ops. Slices run in parallel within IN_PROGRESS,
# so all slice tools are allowed there; per-slice preconditions are enforced by
# the tools themselves.
PHASE_MCP: dict[str, set[str]] = {
    "NO_ACTIVE_FEATURE": {"submit_feature"},
    "NO_FEATURE": {"submit_feature"},
    "FEATURE_DONE": {"submit_feature"},
    "DISCOVERY": {"propose_slices"},
    "SLICING": {"propose_slices"},
    "AWAITING_APPROVAL": {"propose_slices"},
    "IN_PROGRESS": {"list_slices", "get_slice_context", "expand_symbol", "run_verify", "analyze_verify_failure", "track_manual_checks"},
}

_EDIT_TOOLS = {"edit", "write", "multiedit", "notebookedit", "create", "apply_patch", "str_replace", "str_replace_editor"}
_READ_TOOLS = {"read", "cat", "view"}
_SHELL_TOOLS = {"bash", "shell", "sh", "powershell", "pwsh", "cmd"}
# Human-only state changes. Matched broadly so the module form
# (`python -m slicefsm.cli approve`) is caught too, not just `harness approve`.
_HUMAN_ONLY_VERBS = ("approve", "explain", "unstick", "reslice", "pause", "resume", "switch", "cancel")
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


def _in_scope(rel: str | None, module_files: list[str] | None, edit_roots: list[str] | None) -> bool:
    """A path is in scope if it is an exact module file OR sits under an edit root.

    edit_roots cover NEW files inside the approved module directory.
    """
    if rel is None:
        return False
    norm = Path(rel).as_posix()
    if module_files and any(Path(m).as_posix() == norm for m in module_files):
        return True
    for r in edit_roots or []:
        rr = Path(r).as_posix().strip("/")
        if rr and (norm == rr or norm.startswith(rr + "/")):
            return True
    return False


def decide(
    phase: str,
    tool_name: str,
    tool_input: Any,
    read_mode: str = "strict",
    module_files: list[str] | None = None,
    edit_roots: list[str] | None = None,
    project_root: str | None = None,
) -> tuple[bool, str]:
    """Return (allow, reason). reason is non-empty only on deny."""
    t = (tool_name or "").lower()

    # 1. Self-approve hole: human-only commands can never run as a tool call.
    if t in _SHELL_TOOLS:
        cmd = str((tool_input or {}).get("command", "")).strip().lower()
        is_cli = any(m in cmd for m in _CLI_MARKERS)
        if is_cli and any(v in cmd for v in _HUMAN_ONLY_VERBS):
            return False, ("harness state changes (approve / explain / unstick / reslice / pause / resume / "
                           "switch / cancel) are human-only, out-of-band commands. Ask the human to run it in "
                           "the terminal; the AI cannot. (harness status / list are fine.)")

    # 2. MCP tools gated by phase.
    op = _mcp_op(t)
    if op:
        allowed = PHASE_MCP.get(phase, set())
        if op in allowed:
            return True, ""
        return False, f"{op} is not allowed in {phase}. Allowed MCP here: {sorted(allowed) or 'none'}."

    # 3. Edit/write: only while IN_PROGRESS, only within an ACTIVE slice's scope
    #    (existing module files OR new files under an edit root). module_files /
    #    edit_roots here are the UNION over all `implement` slices (parallel).
    if t in _EDIT_TOOLS:
        if phase != "IN_PROGRESS":
            return False, f"edits are not allowed in {phase}."
        rel = _target(tool_input, project_root)
        if not module_files and not edit_roots:
            return False, "no active slice; call get_slice_context(slice_id) to start or resume one."
        if _in_scope(rel, module_files, edit_roots):
            return True, ""
        allowed = edit_roots or sorted(module_files or [])
        return False, (
            f"'{rel}' is outside every active slice. Allowed scope: {allowed}. "
            "For an out-of-scope change, stop and get human approval."
        )

    # 4. Read: bounded to active slices while IN_PROGRESS, per read_mode.
    if t in _READ_TOOLS:
        if phase == "IN_PROGRESS" and (module_files or edit_roots):
            rel = _target(tool_input, project_root)
            if rel and not _in_scope(rel, module_files, edit_roots):
                if read_mode == "strict":
                    return False, (
                        f"strict read: '{rel}' is outside the active slice(s). Use expand_symbol "
                        "for a dependency body, or declare a boundary-cross to the human."
                    )
                return True, ""  # relaxed: allowed (PostToolUse logs it)
        return True, ""

    # 5. Shell and unknown tools: allowed. Honest limit: a shell can bypass the
    #    edit gate. Tracked, not blocked.
    return True, ""


# ── IO / dispatch ──────────────────────────────────────────────────


def _load_manifest(manifest: str | None) -> tuple[list[str], list[str]]:
    if not manifest:
        return [], []
    try:
        man = json.loads(Path(manifest).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], []
    mf = man.get("module_files")
    er = man.get("edit_roots")
    return (mf if isinstance(mf, list) else [], er if isinstance(er, list) else [])


def _active_scope(s: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Union of (module_files, edit_roots) over all `implement` slices."""
    mf: list[str] = []
    er: list[str] = []
    for sl in state.active_slices(s):
        m, e = _load_manifest(sl.get("manifest"))
        mf.extend(m)
        er.extend(e)
    return mf, er


def _slice_for_path(s: dict[str, Any], rel: str | None) -> Any:
    """Which active slice's scope contains rel (for authorship attribution)."""
    if rel is None:
        return None
    for sl in state.active_slices(s):
        mf, er = _load_manifest(sl.get("manifest"))
        if _in_scope(rel, mf, er):
            return sl.get("id")
    return None


def build_state_prompt(s: dict[str, Any]) -> str:
    """Minimal per-turn injection: facts + the valid next tool call(s).

    Workflow *rules* are not repeated here — they live in the tool descriptions
    (loaded once) and in PreToolUse deny reasons (delivered only on a misstep).
    Determinism comes from the hook, not this hint, so it can stay tiny.
    """
    phase = s.get("phase", "NO_ACTIVE_FEATURE")
    fid = (s.get("feature") or {}).get("id", "") or "feature"
    read = (s.get("read_policy") or {}).get("mode", "strict")

    if phase == "IN_PROGRESS":
        slices = s.get("slices", [])
        n = len(slices)
        cur = next((x for x in slices if x.get("status") == "implement"), None)
        stuck = next((x for x in slices if x.get("status") == "stuck"), None)
        nxt = next((x for x in slices if x.get("status") == "proposed"), None)
        if cur is not None:
            i = cur.get("id")
            aff = (f'{fid} - slice {i}/{n} "{cur.get("title","")}" implement '
                   f'({cur.get("module","?")}, {read}) - next: run_verify({i}) | expand_symbol({i},...)')
        elif stuck is not None:
            sid = stuck.get("id")
            aff = f'{fid} - slice {sid} STUCK - diagnose, then human: harness unstick {sid}'
        elif nxt is not None:
            aff = f'{fid} IN_PROGRESS - next: get_slice_context({nxt.get("id")})'
        else:
            aff = f'{fid} - all slices done'
        return f"[slicefsm] {aff}"

    aff = {
        "NO_ACTIVE_FEATURE": "no active feature - next: submit_feature(desc); or human: harness resume <id> / list",
        "NO_FEATURE": f"{fid} - next: propose_slices(slices)",
        "DISCOVERY": f"{fid} DISCOVERY (read-only) - next: propose_slices(slices, discovery_summary=...)",
        "SLICING": f"{fid} SLICING - next: propose_slices(slices)",
        "AWAITING_APPROVAL": f"{fid} AWAITING_APPROVAL - waiting for human: harness approve",
        "FEATURE_DONE": f"{fid} done - next: submit_feature(desc); or human: harness switch <id>",
    }.get(phase, phase)
    return f"[slicefsm] {aff}"


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
    module_files, edit_roots = _active_scope(s)
    allow, reason = decide(
        phase,
        event.get("tool_name", ""),
        event.get("tool_input", {}),
        read_mode=read_mode,
        module_files=module_files,
        edit_roots=edit_roots,
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
    if s.get("phase") == "IN_PROGRESS":
        t = str(event.get("tool_name", "")).lower()
        if t in _EDIT_TOOLS:
            rel = _target(event.get("tool_input", {}), project_root)
            if rel:
                fid = (s.get("feature") or {}).get("id")
                edits.append_edit(project_root, fid, _slice_for_path(s, rel), rel, t)
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
    # The agent reads our stdout as UTF-8 JSON. Force it, so a non-UTF-8 console
    # locale (e.g. Windows cp949) cannot mangle a deny reason with a non-ASCII path.
    for stream in (sys.stdout, sys.stdin):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

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

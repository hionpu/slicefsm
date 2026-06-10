"""Tests for the hook decision logic and dispatch (v2 parallel model)."""

from __future__ import annotations

import json

from slicefsm import hook, state


# ── decide(): self-approve hole ────────────────────────────────────


def test_self_approve_hole_blocked_any_phase():
    for phase in ("NO_FEATURE", "AWAITING_APPROVAL", "IN_PROGRESS"):
        allow, reason = hook.decide(phase, "Bash", {"command": "harness approve --scale large"})
        assert allow is False and "human-only" in reason


def test_module_form_approve_also_blocked():
    allow, _ = hook.decide("AWAITING_APPROVAL", "bash",
                           {"command": "python -m slicefsm.cli --target . approve --scale large"})
    assert allow is False


def test_status_command_allowed():
    allow, _ = hook.decide("IN_PROGRESS", "bash", {"command": "harness status"})
    assert allow is True


# ── decide(): MCP gating by feature phase ──────────────────────────


def test_no_active_feature_gating():
    assert hook.decide("NO_ACTIVE_FEATURE", "mcp__slicefsm__submit_feature", {})[0] is True
    assert hook.decide("NO_ACTIVE_FEATURE", "mcp__slicefsm__get_slice_context", {})[0] is False
    assert hook.decide("NO_ACTIVE_FEATURE", "Edit", {"file_path": "x.py"})[0] is False


def test_pause_switch_blocked():
    assert hook.decide("IN_PROGRESS", "bash", {"command": "harness pause"})[0] is False
    assert hook.decide("IN_PROGRESS", "bash", {"command": "harness switch feat-b"})[0] is False
    assert hook.decide("NO_ACTIVE_FEATURE", "bash", {"command": "harness resume feat-a"})[0] is False


def test_mcp_gating_by_phase():
    assert hook.decide("NO_FEATURE", "mcp__slicefsm__submit_feature", {})[0] is True
    assert hook.decide("NO_FEATURE", "mcp__slicefsm__get_slice_context", {})[0] is False
    assert hook.decide("IN_PROGRESS", "mcp__slicefsm__get_slice_context", {})[0] is True
    assert hook.decide("IN_PROGRESS", "mcp__slicefsm__list_slices", {})[0] is True
    assert hook.decide("IN_PROGRESS", "mcp__slicefsm__run_verify", {})[0] is True


# ── decide(): edits ────────────────────────────────────────────────


def test_edit_denied_outside_in_progress():
    allow, reason = hook.decide("SLICING", "Edit", {"file_path": "src/a.py"}, module_files=["src/a.py"])
    assert allow is False and "not allowed in SLICING" in reason


def test_edit_within_active_scope_allowed():
    allow, _ = hook.decide("IN_PROGRESS", "Edit", {"file_path": "src/a.py"}, module_files=["src/a.py"])
    assert allow is True


def test_edit_outside_active_scope_denied():
    allow, reason = hook.decide("IN_PROGRESS", "Edit", {"file_path": "src/other.py"}, module_files=["src/a.py"])
    assert allow is False and "outside every active slice" in reason


def test_edit_with_no_active_slice_denied():
    allow, reason = hook.decide("IN_PROGRESS", "Edit", {"file_path": "src/a.py"}, module_files=[], edit_roots=[])
    assert allow is False and "no active slice" in reason


def test_new_file_in_edit_root_allowed():
    allow, _ = hook.decide("IN_PROGRESS", "Write", {"file_path": "src/ui/new.py"},
                           module_files=[], edit_roots=["src/ui"])
    assert allow is True


def test_new_file_outside_edit_root_denied():
    allow, _ = hook.decide("IN_PROGRESS", "Write", {"file_path": "src/other/new.py"},
                           module_files=[], edit_roots=["src/ui"])
    assert allow is False


# ── decide(): reads ────────────────────────────────────────────────


def test_read_strict_outside_denied():
    allow, reason = hook.decide("IN_PROGRESS", "Read", {"file_path": "src/other.py"},
                                read_mode="strict", module_files=["src/a.py"])
    assert allow is False and "expand_symbol" in reason


def test_read_relaxed_outside_allowed():
    allow, _ = hook.decide("IN_PROGRESS", "Read", {"file_path": "src/other.py"},
                           read_mode="relaxed", module_files=["src/a.py"])
    assert allow is True


def test_read_within_scope_allowed():
    allow, _ = hook.decide("IN_PROGRESS", "Read", {"file_path": "src/a.py"},
                           read_mode="strict", module_files=["src/a.py"])
    assert allow is True


def test_read_in_readonly_phase_allowed():
    assert hook.decide("DISCOVERY", "Read", {"file_path": "anywhere/x.py"})[0] is True


def test_unknown_tool_allowed():
    assert hook.decide("IN_PROGRESS", "Glob", {"pattern": "**/*.py"})[0] is True


# ── build_state_prompt ─────────────────────────────────────────────


def test_state_prompt_affordance_for_active_slice():
    s = state.new_feature_state()
    s["phase"] = "IN_PROGRESS"
    s["feature"] = {"id": "feat-a"}
    s["slices"] = [
        {"id": 1, "title": "open UI", "module": "src/ui", "status": "implement"},
        {"id": 2, "title": "save", "module": "src/store", "status": "proposed"},
    ]
    s["read_policy"] = {"mode": "relaxed"}
    prompt = hook.build_state_prompt(s)
    # facts + the valid next tool call, no rules paragraph
    assert "open UI" in prompt
    assert "run_verify(1)" in prompt and "expand_symbol(1" in prompt
    assert "relaxed" in prompt
    assert "Edit only" not in prompt  # rules are not re-injected
    assert len(prompt) < 160  # stays tiny


def test_state_prompt_affordance_next_slice():
    s = state.new_feature_state()
    s["phase"] = "IN_PROGRESS"
    s["feature"] = {"id": "feat-a"}
    s["slices"] = [{"id": 1, "title": "x", "module": "src", "status": "proposed"}]
    assert "get_slice_context(1)" in hook.build_state_prompt(s)


def test_state_prompt_affordance_no_active_feature():
    assert "submit_feature" in hook.build_state_prompt(state.new_feature_state() | {"phase": "NO_ACTIVE_FEATURE"})


# ── dispatch handlers ──────────────────────────────────────────────


def test_handle_pretooluse_denies_illegal(tmp_path, capsys):
    state.write(tmp_path, state.new_state())  # NO_FEATURE
    hook._handle_pretooluse(str(tmp_path), {"tool_name": "mcp__slicefsm__get_slice_context", "tool_input": {}})
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_handle_pretooluse_allows_legal_silently(tmp_path, capsys):
    state.write(tmp_path, state.new_state())
    hook._handle_pretooluse(str(tmp_path), {"tool_name": "mcp__slicefsm__submit_feature", "tool_input": {}})
    assert capsys.readouterr().out == ""


def test_handle_userpromptsubmit_injects_context(tmp_path, capsys):
    state.write(tmp_path, state.new_state())
    hook._handle_userpromptsubmit(str(tmp_path), {})
    out = json.loads(capsys.readouterr().out)
    assert "slicefsm" in out["hookSpecificOutput"]["additionalContext"]


def test_handle_posttooluse_logs_edit_with_slice(tmp_path):
    man = tmp_path / ".harness" / "m.json"
    man.parent.mkdir(parents=True, exist_ok=True)
    man.write_text(json.dumps({"module_files": ["src/a.py"], "edit_roots": ["src"]}), encoding="utf-8")
    rs = state.new_root()
    fs = state.new_feature_state()
    fs["phase"] = "IN_PROGRESS"
    fs["feature"] = {"id": "feat-x"}
    fs["slices"] = [{"id": 1, "title": "t", "module": "src", "status": "implement", "manifest": str(man)}]
    rs["features"]["feat-x"] = fs
    rs["active_feature_id"] = "feat-x"
    state.write_root(tmp_path, rs)
    hook._handle_posttooluse(str(tmp_path), {"tool_name": "edit", "tool_input": {"file_path": "src/a.py"}})
    log = (state.feature_dir(tmp_path, "feat-x") / "edits.log").read_text(encoding="utf-8")
    assert "src/a.py" in log and '"slice_id": 1' in log

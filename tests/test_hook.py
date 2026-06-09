"""Tests for the hook decision logic and dispatch."""

from __future__ import annotations

import json

from slicefsm import hook, state


# ── decide() ───────────────────────────────────────────────────────


def test_self_approve_hole_blocked_any_phase():
    for phase in ("NO_FEATURE", "AWAITING_APPROVAL", "SLICE_IMPLEMENT"):
        allow, reason = hook.decide(phase, "Bash", {"command": "harness approve --scale large"})
        assert allow is False
        assert "human-only" in reason


def test_unstick_and_explain_blocked():
    allow, _ = hook.decide("STUCK", "bash", {"command": "harness unstick"})
    assert allow is False
    allow2, _ = hook.decide("SLICE_VERIFY", "bash", {"command": "harness explain 2"})
    assert allow2 is False


def test_status_command_allowed():
    allow, _ = hook.decide("SLICE_IMPLEMENT", "bash", {"command": "harness status"})
    assert allow is True


def test_module_form_approve_also_blocked():
    allow, _ = hook.decide("AWAITING_APPROVAL", "bash",
                           {"command": "python -m slicefsm.cli --target . approve --scale large"})
    assert allow is False


def test_reslice_blocked():
    allow, _ = hook.decide("SLICE_IMPLEMENT", "bash", {"command": "harness reslice"})
    assert allow is False


def test_mcp_gating_by_phase():
    allow, _ = hook.decide("NO_FEATURE", "mcp__slicefsm__submit_feature", {})
    assert allow is True
    deny, reason = hook.decide("NO_FEATURE", "mcp__slicefsm__get_slice_context", {})
    assert deny is False
    assert "not allowed" in reason
    allow2, _ = hook.decide("SLICE_SCOPING", "mcp__slicefsm__get_slice_context", {})
    assert allow2 is True


def test_edit_denied_outside_implement():
    allow, reason = hook.decide("SLICING", "Edit", {"file_path": "src/a.py"}, module_files=["src/a.py"])
    assert allow is False
    assert "not allowed in SLICING" in reason


def test_edit_within_module_allowed():
    allow, _ = hook.decide("SLICE_IMPLEMENT", "Edit", {"file_path": "src/a.py"}, module_files=["src/a.py"])
    assert allow is True


def test_edit_outside_module_denied():
    allow, reason = hook.decide("SLICE_IMPLEMENT", "Edit", {"file_path": "src/other.py"}, module_files=["src/a.py"])
    assert allow is False
    assert "outside the slice module" in reason


def test_edit_without_manifest_denied():
    allow, reason = hook.decide("SLICE_IMPLEMENT", "Edit", {"file_path": "src/a.py"}, module_files=None)
    assert allow is False
    assert "no slice context" in reason


def test_read_strict_outside_denied():
    allow, reason = hook.decide("SLICE_IMPLEMENT", "Read", {"file_path": "src/other.py"},
                                read_mode="strict", module_files=["src/a.py"])
    assert allow is False
    assert "expand_symbol" in reason


def test_read_relaxed_outside_allowed():
    allow, _ = hook.decide("SLICE_IMPLEMENT", "Read", {"file_path": "src/other.py"},
                           read_mode="relaxed", module_files=["src/a.py"])
    assert allow is True


def test_read_within_module_allowed():
    allow, _ = hook.decide("SLICE_IMPLEMENT", "Read", {"file_path": "src/a.py"},
                           read_mode="strict", module_files=["src/a.py"])
    assert allow is True


def test_read_in_stuck_outside_denied_even_relaxed():
    allow, _ = hook.decide("STUCK", "Read", {"file_path": "src/other.py"},
                           read_mode="relaxed", module_files=["src/a.py"])
    assert allow is False


def test_read_in_readonly_phase_allowed():
    allow, _ = hook.decide("DISCOVERY", "Read", {"file_path": "anywhere/x.py"})
    assert allow is True


def test_new_file_in_module_dir_allowed():
    # GPT issue #5: a new file under the approved module dir must be writable.
    allow, _ = hook.decide("SLICE_IMPLEMENT", "Write", {"file_path": "src/ui/new.py"},
                           module_files=[], edit_roots=["src/ui"])
    assert allow is True


def test_new_file_outside_edit_root_denied():
    allow, reason = hook.decide("SLICE_IMPLEMENT", "Write", {"file_path": "src/other/new.py"},
                                module_files=[], edit_roots=["src/ui"])
    assert allow is False
    assert "outside the slice" in reason


def test_read_new_file_in_module_dir_allowed_strict():
    allow, _ = hook.decide("SLICE_IMPLEMENT", "Read", {"file_path": "src/ui/helper.py"},
                           read_mode="strict", module_files=[], edit_roots=["src/ui"])
    assert allow is True


def test_unknown_tool_allowed():
    allow, _ = hook.decide("SLICE_IMPLEMENT", "Glob", {"pattern": "**/*.py"})
    assert allow is True


# ── build_state_prompt() ───────────────────────────────────────────


def test_state_prompt_mentions_phase_and_slice():
    s = state.new_state()
    s["phase"] = "SLICE_IMPLEMENT"
    s["slices"] = [{"id": 1, "title": "open UI", "module": "src/ui", "status": "implement"}]
    s["current_slice"] = 1
    s["read_policy"] = {"mode": "relaxed"}
    prompt = hook.build_state_prompt(s)
    assert "SLICE_IMPLEMENT" in prompt
    assert "open UI" in prompt
    assert "relaxed" in prompt


# ── dispatch handlers ──────────────────────────────────────────────


def test_handle_pretooluse_denies_illegal(tmp_path, capsys):
    s = state.new_state()  # NO_FEATURE
    state.write(tmp_path, s)
    rc = hook._handle_pretooluse(str(tmp_path), {
        "tool_name": "mcp__slicefsm__get_slice_context", "tool_input": {},
    })
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_handle_pretooluse_allows_legal_silently(tmp_path, capsys):
    s = state.new_state()
    state.write(tmp_path, s)
    hook._handle_pretooluse(str(tmp_path), {
        "tool_name": "mcp__slicefsm__submit_feature", "tool_input": {},
    })
    assert capsys.readouterr().out == ""  # allow = no output


def test_handle_userpromptsubmit_injects_context(tmp_path, capsys):
    s = state.new_state()
    state.write(tmp_path, s)
    hook._handle_userpromptsubmit(str(tmp_path), {})
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "slicefsm" in out["hookSpecificOutput"]["additionalContext"]


def test_handle_posttooluse_logs_edit(tmp_path):
    s = state.new_state()
    s["phase"] = "SLICE_IMPLEMENT"
    s["slices"] = [{"id": 1, "title": "t", "module": "src", "status": "implement"}]
    s["current_slice"] = 1
    state.write(tmp_path, s)
    hook._handle_posttooluse(str(tmp_path), {"tool_name": "edit", "tool_input": {"file_path": "src/a.py"}})
    log = (tmp_path / ".harness" / "edits.log")
    assert log.exists()
    assert "src/a.py" in log.read_text(encoding="utf-8")

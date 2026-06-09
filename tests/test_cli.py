"""Tests for the out-of-band harness CLI core commands."""

from __future__ import annotations

from pathlib import Path

import pytest

from slicefsm import cli, ops, state


YES = lambda *a: True
NO = lambda *a: False


@pytest.fixture
def awaiting(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    ops.submit_feature(str(tmp_path), "medium feature with several behaviors")
    ops.propose_slices(str(tmp_path), [
        {"title": "behavior one", "module": "src/a.py", "verify_how": "t", "ac_count": 3},
        {"title": "behavior two", "module": "src/a.py", "verify_how": "t", "ac_count": 3},
        {"title": "behavior three", "module": "src/a.py", "verify_how": "t", "ac_count": 3},
    ], discovery_summary="scanned")
    return tmp_path


def test_approve_requires_confirmation(awaiting):
    out = cli.cmd_approve(str(awaiting), confirm=NO)
    assert out["ok"] is False
    assert out["reason"] == "not_confirmed"
    assert state.read(awaiting)["phase"] == "AWAITING_APPROVAL"  # unchanged


def test_approve_advances_and_sets_policy(awaiting):
    out = cli.cmd_approve(str(awaiting), confirm=YES)
    assert out["ok"] is True
    s = state.read(awaiting)
    assert s["phase"] == "SLICE_SCOPING"
    assert s["scale"] == "Medium"  # from scale_measured (3 slices)
    assert s["scale_source"] == "human_approved"
    assert s["read_policy"]["mode"] == "relaxed"  # Medium
    assert s["current_slice"] == 1


def test_approve_scale_override_and_risky(awaiting):
    out = cli.cmd_approve(str(awaiting), scale="Large", risky=True, confirm=YES)
    s = state.read(awaiting)
    assert s["scale"] == "Large"
    assert s["risky"] is True
    assert s["read_policy"]["mode"] == "strict"  # risky forces strict


def test_approve_wrong_phase(tmp_path):
    out = cli.cmd_approve(str(tmp_path), confirm=YES)  # NO_FEATURE
    assert out["ok"] is False
    assert out["reason"] == "wrong_phase"


def test_explain_records_file(awaiting):
    cli.cmd_approve(str(awaiting), confirm=YES)
    out = cli.cmd_explain(str(awaiting), 1, text="I hand-wrote the core loop because X.")
    assert out["ok"] is True
    assert Path(out["path"]).exists()
    s = state.read(awaiting)
    target = next(x for x in s["slices"] if x["id"] == 1)
    assert target["explanation"] == out["path"]


def test_explain_empty_rejected(awaiting):
    out = cli.cmd_explain(str(awaiting), 1, text="   ")
    assert out["ok"] is False
    assert out["reason"] == "empty_explanation"


def test_explain_slice_not_found(awaiting):
    out = cli.cmd_explain(str(awaiting), 99, text="x")
    assert out["reason"] == "slice_not_found"


def test_unstick_from_stuck(awaiting):
    cli.cmd_approve(str(awaiting), confirm=YES)
    s = state.read(awaiting)
    s["phase"] = "STUCK"
    cs = state.current_slice(s)
    cs["verify_fail_count"] = 3
    state.write(awaiting, s)
    out = cli.cmd_unstick(str(awaiting), confirm=YES)
    assert out["ok"] is True
    s2 = state.read(awaiting)
    assert s2["phase"] == "SLICE_IMPLEMENT"
    assert state.current_slice(s2)["verify_fail_count"] == 0


def test_unstick_needs_confirm(awaiting):
    cli.cmd_approve(str(awaiting), confirm=YES)
    s = state.read(awaiting)
    s["phase"] = "STUCK"
    state.write(awaiting, s)
    out = cli.cmd_unstick(str(awaiting), confirm=NO)
    assert out["ok"] is False


def test_reslice_clears_slices(awaiting):
    cli.cmd_approve(str(awaiting), confirm=YES)
    out = cli.cmd_reslice(str(awaiting), confirm=YES)
    assert out["ok"] is True
    s = state.read(awaiting)
    assert s["phase"] in ("SLICING", "DISCOVERY")
    assert s["slices"] == []
    assert s["current_slice"] is None


def test_status_is_readonly(awaiting):
    out = cli.cmd_status(str(awaiting))
    assert out["phase"] == "AWAITING_APPROVAL"
    assert len(out["slices"]) == 3
    # status must not change state
    assert state.read(awaiting)["phase"] == "AWAITING_APPROVAL"


def test_main_status_smoke(awaiting, capsys):
    rc = cli.main(["--target", str(awaiting), "status"])
    assert rc == 0
    assert "AWAITING_APPROVAL" in capsys.readouterr().out

"""Tests for the out-of-band harness CLI core commands (v2 parallel model)."""

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
    assert state.read(awaiting)["phase"] == "AWAITING_APPROVAL"


def test_approve_advances_and_sets_policy(awaiting):
    out = cli.cmd_approve(str(awaiting), confirm=YES)
    assert out["ok"] is True
    s = state.read(awaiting)
    assert s["phase"] == "IN_PROGRESS"
    assert s["scale"] == "Medium"
    assert s["read_policy"]["mode"] == "relaxed"
    assert all(sl["status"] == "proposed" for sl in s["slices"])


def test_approve_scale_override_and_risky(awaiting):
    cli.cmd_approve(str(awaiting), scale="Large", risky=True, confirm=YES)
    s = state.read(awaiting)
    assert s["scale"] == "Large" and s["risky"] is True
    assert s["read_policy"]["mode"] == "strict"


def test_approve_wrong_phase(tmp_path):
    assert cli.cmd_approve(str(tmp_path), confirm=YES)["reason"] == "wrong_phase"


def test_explain_records_file(awaiting):
    cli.cmd_approve(str(awaiting), confirm=YES)
    out = cli.cmd_explain(str(awaiting), 1, text="I hand-wrote the core loop.")
    assert out["ok"] is True and Path(out["path"]).exists()
    assert state.find_slice(state.read(awaiting), 1)["explanation"] == out["path"]


def test_explain_empty_rejected(awaiting):
    assert cli.cmd_explain(str(awaiting), 1, text="   ")["reason"] == "empty_explanation"


def test_explain_slice_not_found(awaiting):
    assert cli.cmd_explain(str(awaiting), 99, text="x")["reason"] == "slice_not_found"


def test_unstick_from_stuck(awaiting):
    cli.cmd_approve(str(awaiting), confirm=YES)
    s = state.read(awaiting)
    sl = state.find_slice(s, 2)
    sl["status"] = "stuck"
    sl["verify_fail_count"] = 3
    state.write(awaiting, s)
    out = cli.cmd_unstick(str(awaiting), 2, confirm=YES)
    assert out["ok"] is True
    s2 = state.read(awaiting)
    assert state.find_slice(s2, 2)["status"] == "implement"
    assert state.find_slice(s2, 2)["verify_fail_count"] == 0


def test_unstick_not_stuck(awaiting):
    cli.cmd_approve(str(awaiting), confirm=YES)
    assert cli.cmd_unstick(str(awaiting), 1, confirm=YES)["reason"] == "not_stuck"


def test_unstick_needs_confirm(awaiting):
    cli.cmd_approve(str(awaiting), confirm=YES)
    s = state.read(awaiting)
    state.find_slice(s, 2)["status"] = "stuck"
    state.write(awaiting, s)
    assert cli.cmd_unstick(str(awaiting), 2, confirm=NO)["ok"] is False


def test_reslice_clears_slices(awaiting):
    cli.cmd_approve(str(awaiting), confirm=YES)
    out = cli.cmd_reslice(str(awaiting), confirm=YES)
    assert out["ok"] is True
    s = state.read(awaiting)
    assert s["phase"] in ("SLICING", "DISCOVERY")
    assert s["slices"] == []


def test_status_is_readonly(awaiting):
    out = cli.cmd_status(str(awaiting))
    assert out["phase"] == "AWAITING_APPROVAL"
    assert len(out["slices"]) == 3
    assert state.read(awaiting)["phase"] == "AWAITING_APPROVAL"


def test_main_status_smoke(awaiting, capsys):
    rc = cli.main(["--target", str(awaiting), "status"])
    assert rc == 0
    assert "AWAITING_APPROVAL" in capsys.readouterr().out

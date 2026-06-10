"""Tests for the out-of-band harness CLI core commands (v2 parallel model)."""

from __future__ import annotations

from pathlib import Path

import pytest

from slicefsm import cli, git_util, ops, state


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


# ── multi-feature: pause / switch / list / cancel ──────────────────


def test_pause_then_new_feature_then_switch_back(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    ops.submit_feature(str(tmp_path), "first feature")
    fid1 = state.read_root(tmp_path)["active_feature_id"]

    out = cli.cmd_pause(str(tmp_path), confirm=YES)
    assert out["ok"] is True and out["paused"] == fid1
    assert state.read_root(tmp_path)["active_feature_id"] is None
    assert state.read(tmp_path)["phase"] == "NO_ACTIVE_FEATURE"

    ops.submit_feature(str(tmp_path), "second feature")  # allowed now
    rs = state.read_root(tmp_path)
    assert len(rs["features"]) == 2
    fid2 = rs["active_feature_id"]
    assert fid2 != fid1

    sw = cli.cmd_switch(str(tmp_path), fid1, confirm=YES)
    assert sw["ok"] is True and sw["active_feature"] == fid1
    rs2 = state.read_root(tmp_path)
    assert rs2["active_feature_id"] == fid1
    assert rs2["features"][fid2]["paused"] is True


def test_pause_refuses_dirty_tree(tmp_path, monkeypatch):
    ops.submit_feature(str(tmp_path), "first")
    monkeypatch.setattr(git_util, "is_git_repo", lambda r: True)
    monkeypatch.setattr(git_util, "is_dirty", lambda r: True)
    out = cli.cmd_pause(str(tmp_path), confirm=YES)
    assert out["ok"] is False and out["reason"] == "dirty_tree"
    assert state.read_root(tmp_path)["active_feature_id"] is not None  # unchanged


def test_pause_needs_confirm(tmp_path):
    ops.submit_feature(str(tmp_path), "first")
    assert cli.cmd_pause(str(tmp_path), confirm=NO)["ok"] is False


def test_pause_no_active(tmp_path):
    assert cli.cmd_pause(str(tmp_path), confirm=YES)["reason"] == "no_active_feature"


def test_list_and_cancel(tmp_path):
    ops.submit_feature(str(tmp_path), "first")
    fid = state.read_root(tmp_path)["active_feature_id"]
    rows = cli.cmd_list(str(tmp_path))["features"]
    assert any(f["id"] == fid and f["active"] for f in rows)
    out = cli.cmd_cancel(str(tmp_path), fid, confirm=YES)
    assert out["ok"] is True
    rs = state.read_root(tmp_path)
    assert fid not in rs["features"]
    assert rs["active_feature_id"] is None


def test_switch_unknown_feature(tmp_path):
    assert cli.cmd_switch(str(tmp_path), "nope", confirm=YES)["reason"] == "feature_not_found"


def test_pause_warns_outside_git(tmp_path):
    ops.submit_feature(str(tmp_path), "first")  # tmp_path is not a git repo
    out = cli.cmd_pause(str(tmp_path), confirm=YES)
    assert out["ok"] is True
    assert "git" in out.get("warning", "")

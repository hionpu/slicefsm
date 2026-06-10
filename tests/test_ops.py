"""Workflow tests driving the FSM through ops.* (MCP-free), v2 parallel model."""

from __future__ import annotations

from pathlib import Path

import pytest

from slicefsm import ops, policy, state, verify


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    for name in ("a", "b", "c"):
        (tmp_path / "src" / f"{name}.py").write_text(
            f"def {name}_fn():\n    return '{name}'\n", encoding="utf-8"
        )
    return tmp_path


def _approve(root: Path, scale: str, risky: bool = False) -> None:
    """Simulate the out-of-band human approve: feature -> IN_PROGRESS."""
    s = state.read(root)
    s["phase"] = "IN_PROGRESS"
    s["scale"] = scale
    s["scale_source"] = "human_approved"
    s["risky"] = risky
    s["read_policy"] = policy.derive_read_policy(scale, risky)
    for sl in s["slices"]:
        sl["status"] = "proposed"
    state.write(root, s)


def _pass(monkeypatch):
    monkeypatch.setattr(verify, "run_verify_suite", lambda *a, **k: {"overall": "pass", "steps": []})


def _fail(monkeypatch):
    monkeypatch.setattr(verify, "run_verify_suite", lambda *a, **k: {"overall": "fail", "steps": [], "failed_steps": ["x"]})


def _three(project):
    ops.submit_feature(str(project), "medium feature with several behaviors")
    ops.propose_slices(str(project), [
        {"title": "behavior one", "module": "src/a.py", "verify_how": "t", "ac_count": 3},
        {"title": "behavior two", "module": "src/b.py", "verify_how": "t", "ac_count": 3},
        {"title": "behavior three", "module": "src/c.py", "verify_how": "t", "ac_count": 3},
    ])


# ── proposal / approval ────────────────────────────────────────────


def test_submit_routes_small_to_slicing(project):
    out = ops.submit_feature(str(project), "rename the save label")
    assert out["phase"] == "SLICING"


def test_submit_routes_large_to_discovery(project):
    out = ops.submit_feature(str(project), "add memo feature that persists to database and syncs to server with undo")
    assert out["phase"] == "DISCOVERY"


def test_propose_validation_blocks_bad_slices(project):
    ops.submit_feature(str(project), "small thing")
    out = ops.propose_slices(str(project), [{"title": "x", "module": "", "ac_count": 1}])
    assert out["error"] == "validation_failed"


def test_propose_discovery_requires_summary(project):
    ops.submit_feature(str(project), "add memo feature that persists to database and syncs to server with undo")
    slices = [{"title": "t", "module": "src/a.py", "verify_how": "test", "ac_count": 3}]
    assert ops.propose_slices(str(project), slices)["error"] == "discovery_summary_required"
    assert ops.propose_slices(str(project), slices, discovery_summary="found a,b,c")["phase"] == "AWAITING_APPROVAL"


def test_scale_mismatch_surfaced(project):
    ops.submit_feature(str(project), "small tweak")
    slices = [{"title": f"behavior {i}", "module": "src/a.py", "verify_how": "t", "ac_count": 3} for i in range(7)]
    out = ops.propose_slices(str(project), slices)
    assert out["measured_scale"] == "Large" and out["scale_mismatch"] is True


# ── single-slice happy path ────────────────────────────────────────


def test_micro_happy_path_to_done(project, monkeypatch):
    _pass(monkeypatch)
    ops.submit_feature(str(project), "tiny tweak")
    ops.propose_slices(str(project), [
        {"title": "do the thing", "module": "src/a.py", "verify_how": "test", "ac_count": 3},
    ])
    _approve(project, "Micro")
    ctx = ops.get_slice_context(str(project), 1)
    assert ctx["slice_status"] == "implement"
    out = ops.run_verify(str(project), 1)
    assert out["overall"] == "pass"
    assert out["feature_phase"] == "FEATURE_DONE"


def test_get_slice_context_wrong_phase_denied(project):
    ops.submit_feature(str(project), "tiny")
    out = ops.get_slice_context(str(project), 1)
    assert out["error"] == "transition_denied"
    assert out["current_phase"] == "SLICING"


def test_module_mismatch_denied(project, monkeypatch):
    _pass(monkeypatch)
    ops.submit_feature(str(project), "tiny")
    ops.propose_slices(str(project), [
        {"title": "do thing", "module": "src/a.py", "verify_how": "t", "ac_count": 3},
    ])
    _approve(project, "Micro")
    out = ops.get_slice_context(str(project), 1, module="src/b.py")
    assert out["error"] == "module_mismatch"
    assert ops.get_slice_context(str(project), 1, module="src/a.py")["slice_status"] == "implement"


# ── parallel + resume ──────────────────────────────────────────────


def test_slices_are_sequential(project, monkeypatch):
    _pass(monkeypatch)
    _three(project)
    _approve(project, "Medium")
    ops.get_slice_context(str(project), 1)
    # cannot start another slice while one is in progress
    out = ops.get_slice_context(str(project), 2)
    assert out["error"] == "another_slice_active"
    assert out["active_slice"] == 1
    # finish slice 1, then slice 2 may start
    ops.run_verify(str(project), 1)
    assert ops.get_slice_context(str(project), 2)["slice_status"] == "implement"


def test_last_slice_explanation_gate(project, monkeypatch):
    _pass(monkeypatch)
    _three(project)
    _approve(project, "Medium")
    for sid in (1, 2):
        ops.get_slice_context(str(project), sid)
        ops.run_verify(str(project), sid)
    ops.get_slice_context(str(project), 3)
    out = ops.run_verify(str(project), 3)
    assert out["overall"] == "pending_explanation"
    s = state.read(project)
    state.find_slice(s, 3)["explanation"] = ".harness/x.md"
    state.write(project, s)
    assert ops.run_verify(str(project), 3)["feature_phase"] == "FEATURE_DONE"


def test_submit_while_active_denied(project):
    ops.submit_feature(str(project), "first feature")
    out = ops.submit_feature(str(project), "second feature")
    assert out["error"] == "active_feature_exists"


def test_resume_paused_slice(project, monkeypatch):
    _pass(monkeypatch)
    _three(project)
    _approve(project, "Medium")
    first = ops.get_slice_context(str(project), 2)
    assert first["slice_status"] == "implement"
    # "new session" resumes the same slice
    again = ops.get_slice_context(str(project), 2)
    assert again["slice_status"] == "implement"
    assert again["module"] == "src/b.py"


def test_cannot_start_stuck_slice(project, monkeypatch):
    _fail(monkeypatch)
    ops.submit_feature(str(project), "tiny")
    ops.propose_slices(str(project), [
        {"title": "do thing", "module": "src/a.py", "verify_how": "t", "ac_count": 3},
    ])
    _approve(project, "Micro")
    ops.get_slice_context(str(project), 1)
    for _ in range(3):
        out = ops.run_verify(str(project), 1)
    assert out["slice_status"] == "stuck"
    # cannot re-acquire a stuck slice without unstick
    assert ops.get_slice_context(str(project), 1)["error"] == "slice_not_startable"
    assert ops.run_verify(str(project), 1)["error"] == "slice_not_active"


# ── gates ──────────────────────────────────────────────────────────


def test_no_checks_does_not_advance(project, monkeypatch):
    monkeypatch.setattr(verify, "run_verify_suite", lambda *a, **k: {"overall": "no_checks", "steps": []})
    ops.submit_feature(str(project), "tiny")
    ops.propose_slices(str(project), [
        {"title": "do thing", "module": "src/a.py", "verify_how": "t", "ac_count": 3},
    ])
    _approve(project, "Micro")
    ops.get_slice_context(str(project), 1)
    out = ops.run_verify(str(project), 1)
    assert out["overall"] == "no_checks" and out["slice_id"] == 1
    assert state.find_slice(state.read(project), 1)["status"] == "implement"


def test_analyze_reachable_after_fail(project, monkeypatch):
    _fail(monkeypatch)
    ops.submit_feature(str(project), "tiny")
    ops.propose_slices(str(project), [
        {"title": "do thing", "module": "src/a.py", "verify_how": "t", "ac_count": 3},
    ])
    _approve(project, "Micro")
    ops.get_slice_context(str(project), 1)
    o1 = ops.run_verify(str(project), 1)
    assert o1["slice_status"] == "implement"
    a = ops.analyze_verify_failure(str(project), "test", slice_id=1)
    assert "classification" in a and "error" not in a


def test_expand_symbol_phase_gate(project):
    ops.submit_feature(str(project), "tiny")
    assert ops.expand_symbol(str(project), 1, "a_fn")["error"] == "transition_denied"


def test_manual_checks_block_pass(project, monkeypatch):
    _pass(monkeypatch)
    ops.submit_feature(str(project), "tiny")
    ops.propose_slices(str(project), [
        {"title": "do thing", "module": "src/a.py", "verify_how": "manual", "ac_count": 3},
    ])
    _approve(project, "Micro")
    ops.get_slice_context(str(project), 1, feature="feat1")
    ops.track_manual_checks(str(project), "feat1", op="declare",
                            checks=[{"id": "c1", "description": "looks right", "required": True}])
    assert ops.run_verify(str(project), 1, feature="feat1")["overall"] == "pending_manual"
    ops.track_manual_checks(str(project), "feat1", op="confirm", check_id="c1")
    out = ops.run_verify(str(project), 1, feature="feat1")
    assert out["overall"] == "pass" and out["feature_phase"] == "FEATURE_DONE"

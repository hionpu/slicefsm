"""Workflow tests driving the FSM through ops.* (MCP-free)."""

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


def _approve(root: Path, scale: str, current: int = 1, risky: bool = False) -> None:
    """Simulate the out-of-band human approve (CLI does this for real)."""
    s = state.read(root)
    s["phase"] = "SLICE_SCOPING"
    s["scale"] = scale
    s["scale_source"] = "human_approved"
    s["risky"] = risky
    s["read_policy"] = policy.derive_read_policy(scale, risky)
    s["current_slice"] = current
    cs = state.current_slice(s)
    if cs:
        cs["status"] = "scoping"
    state.write(root, s)


def _pass(monkeypatch):
    monkeypatch.setattr(verify, "run_verify_suite", lambda *a, **k: {"overall": "pass", "steps": []})


def _fail(monkeypatch):
    monkeypatch.setattr(verify, "run_verify_suite", lambda *a, **k: {"overall": "fail", "steps": [], "failed_steps": ["x"]})


def test_submit_routes_small_to_slicing(project):
    out = ops.submit_feature(str(project), "rename the save label")
    assert out["phase"] == "SLICING"
    assert out["needs_discovery"] is False
    assert "packages" in out["repo_map"]


def test_submit_routes_large_to_discovery(project):
    out = ops.submit_feature(str(project), "add memo feature that persists to database and syncs to server with undo")
    assert out["phase"] == "DISCOVERY"
    assert out["needs_discovery"] is True


def test_propose_validation_blocks_bad_slices(project):
    ops.submit_feature(str(project), "small thing")
    out = ops.propose_slices(str(project), [{"title": "x", "module": "", "ac_count": 1}])
    assert out["error"] == "validation_failed"
    assert any("module" in e for e in out["errors"])
    assert any("ac_count" in e for e in out["errors"])


def test_propose_discovery_requires_summary(project):
    ops.submit_feature(str(project), "add memo feature that persists to database and syncs to server with undo")
    slices = [{"title": "t", "module": "src/a.py", "verify_how": "test", "ac_count": 3}]
    out = ops.propose_slices(str(project), slices)  # no summary
    assert out["error"] == "discovery_summary_required"
    out2 = ops.propose_slices(str(project), slices, discovery_summary="found modules a,b,c")
    assert out2["phase"] == "AWAITING_APPROVAL"


def test_propose_flags_layer_noun_warning(project):
    ops.submit_feature(str(project), "small thing")
    out = ops.propose_slices(str(project), [
        {"title": "ViewModel", "module": "src/a.py", "verify_how": "test", "ac_count": 3},
    ])
    assert out["phase"] == "AWAITING_APPROVAL"  # warning, not block
    assert any("layer" in w for w in out["warnings"])


def test_scale_mismatch_surfaced(project):
    # provisional Small (short text), measured Large (7 slices)
    ops.submit_feature(str(project), "small tweak")
    slices = [{"title": f"behavior {i}", "module": "src/a.py", "verify_how": "t", "ac_count": 3} for i in range(7)]
    out = ops.propose_slices(str(project), slices)
    assert out["measured_scale"] == "Large"
    assert out["scale_mismatch"] is True


def test_micro_happy_path_to_done(project, monkeypatch):
    _pass(monkeypatch)
    ops.submit_feature(str(project), "tiny tweak")
    ops.propose_slices(str(project), [
        {"title": "do the thing", "module": "src/a.py", "verify_how": "test", "ac_count": 3},
    ])
    _approve(project, "Micro", current=1)
    ctx = ops.get_slice_context(str(project), "src/a.py")
    assert ctx["phase"] == "SLICE_IMPLEMENT"
    out = ops.run_verify(str(project))
    assert out["overall"] == "pass"
    assert out["phase"] == "FEATURE_DONE"  # Micro: no explanation gate


def test_get_slice_context_wrong_phase_denied(project):
    ops.submit_feature(str(project), "tiny")
    out = ops.get_slice_context(str(project), "src/a.py")
    assert out["error"] == "transition_denied"
    assert out["current_phase"] == "SLICING"


def test_medium_multi_slice_with_explanation_gate(project, monkeypatch):
    _pass(monkeypatch)
    ops.submit_feature(str(project), "medium feature with several behaviors")
    ops.propose_slices(str(project), [
        {"title": "behavior one", "module": "src/a.py", "verify_how": "t", "ac_count": 3},
        {"title": "behavior two", "module": "src/b.py", "verify_how": "t", "ac_count": 3},
        {"title": "behavior three", "module": "src/c.py", "verify_how": "t", "ac_count": 3},
    ])
    _approve(project, "Medium", current=1)

    # slice 1 -> advance to slice 2
    ops.get_slice_context(str(project), "src/a.py")
    out1 = ops.run_verify(str(project))
    assert out1["phase"] == "SLICE_SCOPING"
    assert out1["current_slice"] == 2

    # slice 2 -> advance to slice 3
    ops.get_slice_context(str(project), "src/b.py")
    out2 = ops.run_verify(str(project))
    assert out2["current_slice"] == 3

    # slice 3 (last) -> explanation gate blocks FEATURE_DONE
    ops.get_slice_context(str(project), "src/c.py")
    out3 = ops.run_verify(str(project))
    assert out3["overall"] == "pending_explanation"
    assert out3["phase"] == "SLICE_VERIFY"

    # human supplies explanation, then it closes
    s = state.read(project)
    state.current_slice(s)["explanation"] = ".harness/explain-3.md"
    state.write(project, s)
    out4 = ops.run_verify(str(project))
    assert out4["phase"] == "FEATURE_DONE"


def test_verify_fail_reaches_stuck(project, monkeypatch):
    _fail(monkeypatch)
    ops.submit_feature(str(project), "tiny")
    ops.propose_slices(str(project), [
        {"title": "do thing", "module": "src/a.py", "verify_how": "test", "ac_count": 3},
    ])
    _approve(project, "Micro", current=1)
    ops.get_slice_context(str(project), "src/a.py")

    o1 = ops.run_verify(str(project))
    assert o1["phase"] == "SLICE_IMPLEMENT" and o1["verify_fail_count"] == 1
    o2 = ops.run_verify(str(project))
    assert o2["verify_fail_count"] == 2
    o3 = ops.run_verify(str(project))
    assert o3["phase"] == "STUCK"  # threshold 3 for non-risky


def test_expand_symbol_phase_gate(project):
    ops.submit_feature(str(project), "tiny")
    out = ops.expand_symbol(str(project), "a_fn")
    assert out["error"] == "transition_denied"


def test_manual_checks_block_pass(project, monkeypatch):
    _pass(monkeypatch)
    ops.submit_feature(str(project), "tiny")
    ops.propose_slices(str(project), [
        {"title": "do thing", "module": "src/a.py", "verify_how": "manual", "ac_count": 3},
    ])
    _approve(project, "Micro", current=1)
    ops.get_slice_context(str(project), "src/a.py", feature="feat1")
    ops.track_manual_checks(str(project), "feat1", op="declare",
                            checks=[{"id": "c1", "description": "looks right", "required": True}])
    out = ops.run_verify(str(project), feature="feat1")
    assert out["overall"] == "pending_manual"
    ops.track_manual_checks(str(project), "feat1", op="confirm", check_id="c1")
    out2 = ops.run_verify(str(project), feature="feat1")
    assert out2["overall"] == "pass"
    assert out2["phase"] == "FEATURE_DONE"

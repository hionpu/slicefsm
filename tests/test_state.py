"""Unit tests for the state core + FSM."""

from __future__ import annotations

import pytest

from slicefsm import state as st


def test_new_state_is_no_feature():
    s = st.new_state()
    assert s["phase"] == "NO_FEATURE"
    assert s["slices"] == []
    assert s["risky"] is False
    assert s["version"] == st.STATE_VERSION


def test_read_missing_returns_fresh(tmp_path):
    s = st.read(tmp_path)
    assert s["phase"] == "NO_FEATURE"
    assert not st.state_path(tmp_path).exists()  # read never creates


def test_write_then_read_roundtrip(tmp_path):
    s = st.new_state()
    s["phase"] = "SLICING"
    s["feature"] = {"id": "feat-1", "desc": "x"}
    st.write(tmp_path, s)
    back = st.read(tmp_path)
    assert back["phase"] == "SLICING"
    assert back["feature"]["id"] == "feat-1"
    assert "updated_at" in back


def test_read_corrupt_returns_fresh(tmp_path):
    p = st.state_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not json", encoding="utf-8")
    s = st.read(tmp_path)
    assert s["phase"] == "NO_FEATURE"


def test_legal_transition_applies():
    s = st.new_state()
    st.transition(s, "SLICING", expect="NO_FEATURE")
    assert s["phase"] == "SLICING"


def test_illegal_transition_denied():
    s = st.new_state()  # NO_FEATURE
    with pytest.raises(st.TransitionDenied) as ei:
        st.transition(s, "FEATURE_DONE")
    assert ei.value.payload["error"] == "transition_denied"
    assert ei.value.payload["current_phase"] == "NO_FEATURE"
    assert s["phase"] == "NO_FEATURE"  # unchanged


def test_wrong_precondition_denied():
    s = st.new_state()
    s["phase"] = "SLICE_IMPLEMENT"
    with pytest.raises(st.TransitionDenied) as ei:
        st.transition(s, "SLICING", expect="NO_FEATURE")
    assert "expected" in ei.value.reason


def test_mutator_runs_before_phase_set():
    s = st.new_state()
    seen = {}

    def mut(state):
        seen["phase_during"] = state["phase"]
        state["scale"] = "Small"

    st.transition(s, "SLICING", expect="NO_FEATURE", mutator=mut)
    assert seen["phase_during"] == "NO_FEATURE"  # mutator saw the old phase
    assert s["scale"] == "Small"
    assert s["phase"] == "SLICING"


def test_full_happy_path():
    s = st.new_state()
    st.transition(s, "DISCOVERY", expect="NO_FEATURE")
    st.transition(s, "AWAITING_APPROVAL", expect="DISCOVERY")
    st.transition(s, "SLICE_SCOPING", expect="AWAITING_APPROVAL")
    st.transition(s, "SLICE_IMPLEMENT", expect="SLICE_SCOPING")
    st.transition(s, "SLICE_VERIFY", expect="SLICE_IMPLEMENT")
    st.transition(s, "FEATURE_DONE", expect="SLICE_VERIFY")
    assert s["phase"] == "FEATURE_DONE"


def test_verify_fail_to_stuck_and_unstick():
    s = st.new_state()
    s["phase"] = "SLICE_VERIFY"
    st.transition(s, "STUCK", expect="SLICE_VERIFY")
    assert s["phase"] == "STUCK"
    # human unstick
    st.transition(s, "SLICE_IMPLEMENT", expect="STUCK")
    assert s["phase"] == "SLICE_IMPLEMENT"


def test_reslice_escape_from_each_slice_phase():
    for ph in ("SLICE_SCOPING", "SLICE_IMPLEMENT", "SLICE_VERIFY", "STUCK"):
        s = st.new_state()
        s["phase"] = ph
        st.transition(s, "SLICING")  # reslice
        assert s["phase"] == "SLICING"


def test_current_slice_lookup():
    s = st.new_state()
    s["slices"] = [{"id": 1, "title": "a"}, {"id": 2, "title": "b"}]
    s["current_slice"] = 2
    assert st.current_slice(s)["title"] == "b"
    s["current_slice"] = None
    assert st.current_slice(s) is None

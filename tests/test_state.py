"""Unit tests for the state core + feature-level FSM (v2, parallel slices)."""

from __future__ import annotations

import pytest

from slicefsm import state as st


def test_new_state_is_no_feature():
    s = st.new_state()
    assert s["phase"] == "NO_FEATURE"
    assert s["slices"] == []
    assert s["version"] == st.STATE_VERSION == 2
    assert "current_slice" not in s


def test_read_missing_returns_fresh(tmp_path):
    s = st.read(tmp_path)
    assert s["phase"] == "NO_FEATURE"
    assert not st.state_path(tmp_path).exists()


def test_write_then_read_roundtrip(tmp_path):
    s = st.new_state()
    s["phase"] = "SLICING"
    s["feature"] = {"id": "feat-1", "desc": "x"}
    st.write(tmp_path, s)
    back = st.read(tmp_path)
    assert back["phase"] == "SLICING"
    assert back["feature"]["id"] == "feat-1"


def test_read_corrupt_returns_fresh(tmp_path):
    p = st.state_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not json", encoding="utf-8")
    assert st.read(tmp_path)["phase"] == "NO_FEATURE"


def test_feature_happy_path():
    s = st.new_state()
    st.transition(s, "DISCOVERY", expect="NO_FEATURE")
    st.transition(s, "AWAITING_APPROVAL", expect="DISCOVERY")
    st.transition(s, "IN_PROGRESS", expect="AWAITING_APPROVAL")
    st.transition(s, "FEATURE_DONE", expect="IN_PROGRESS")
    assert s["phase"] == "FEATURE_DONE"


def test_illegal_transition_denied():
    s = st.new_state()
    with pytest.raises(st.TransitionDenied) as ei:
        st.transition(s, "FEATURE_DONE")
    assert ei.value.payload["error"] == "transition_denied"
    assert s["phase"] == "NO_FEATURE"


def test_wrong_precondition_denied():
    s = st.new_state()
    s["phase"] = "IN_PROGRESS"
    with pytest.raises(st.TransitionDenied):
        st.transition(s, "SLICING", expect="NO_FEATURE")


def test_reslice_from_in_progress():
    s = st.new_state()
    s["phase"] = "IN_PROGRESS"
    st.transition(s, "SLICING")
    assert s["phase"] == "SLICING"


def test_mutator_runs_before_phase_set():
    s = st.new_state()
    seen = {}

    def mut(state):
        seen["phase_during"] = state["phase"]

    st.transition(s, "SLICING", expect="NO_FEATURE", mutator=mut)
    assert seen["phase_during"] == "NO_FEATURE"
    assert s["phase"] == "SLICING"


def test_slice_helpers():
    s = st.new_state()
    s["slices"] = [
        {"id": 1, "status": "done"},
        {"id": 2, "status": "implement"},
        {"id": 3, "status": "proposed"},
    ]
    assert st.find_slice(s, 2)["status"] == "implement"
    assert st.find_slice(s, 9) is None
    assert [x["id"] for x in st.active_slices(s)] == [2]
    assert st.all_done(s) is False
    for x in s["slices"]:
        x["status"] = "done"
    assert st.all_done(s) is True


def test_all_done_empty_is_false():
    assert st.all_done(st.new_state()) is False

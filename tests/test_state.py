"""Unit tests for the state core (v3: multi-feature root + feature FSM)."""

from __future__ import annotations

import json

import pytest

from slicefsm import state as st


def test_new_root_empty():
    rs = st.new_root()
    assert rs["active_feature_id"] is None
    assert rs["features"] == {}
    assert rs["version"] == st.STATE_VERSION == 3


def test_new_feature_state_is_no_feature():
    fs = st.new_feature_state()
    assert fs["phase"] == "NO_FEATURE"
    assert fs["slices"] == []
    assert "current_slice" not in fs


def test_read_missing_returns_no_active(tmp_path):
    s = st.read(tmp_path)
    assert s["phase"] == "NO_ACTIVE_FEATURE"
    assert not st.state_path(tmp_path).exists()


def test_root_roundtrip(tmp_path):
    rs = st.new_root()
    fs = st.new_feature_state()
    fs["feature"] = {"id": "feat-a", "desc": "x"}
    fs["phase"] = "SLICING"
    rs["features"]["feat-a"] = fs
    rs["active_feature_id"] = "feat-a"
    st.write_root(tmp_path, rs)
    back = st.read(tmp_path)  # active-aware read
    assert back["phase"] == "SLICING"
    assert back["feature"]["id"] == "feat-a"


def test_active_write_targets_active_feature(tmp_path):
    rs = st.new_root()
    rs["features"]["feat-a"] = st.new_feature_state()
    rs["active_feature_id"] = "feat-a"
    st.write_root(tmp_path, rs)
    s = st.read(tmp_path)
    s["phase"] = "IN_PROGRESS"
    st.write(tmp_path, s)  # active-aware write
    assert st.read(tmp_path)["phase"] == "IN_PROGRESS"
    assert st.read_root(tmp_path)["features"]["feat-a"]["phase"] == "IN_PROGRESS"


def test_migrates_v2_flat_state(tmp_path):
    # a pre-v3 flat feature-state on disk
    p = st.state_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"version": 2, "phase": "IN_PROGRESS",
                             "feature": {"id": "old-feat"}, "slices": []}), encoding="utf-8")
    rs = st.read_root(tmp_path)
    assert rs["active_feature_id"] == "old-feat"
    assert rs["features"]["old-feat"]["phase"] == "IN_PROGRESS"


def test_two_features_coexist(tmp_path):
    rs = st.new_root()
    rs["features"]["a"] = st.new_feature_state()
    rs["features"]["b"] = st.new_feature_state()
    rs["active_feature_id"] = "a"
    st.write_root(tmp_path, rs)
    assert set(st.read_root(tmp_path)["features"]) == {"a", "b"}
    assert st.read(tmp_path) is not None  # active = a


def test_feature_happy_path_transitions():
    s = st.new_feature_state()
    st.transition(s, "DISCOVERY", expect="NO_FEATURE")
    st.transition(s, "AWAITING_APPROVAL", expect="DISCOVERY")
    st.transition(s, "IN_PROGRESS", expect="AWAITING_APPROVAL")
    st.transition(s, "FEATURE_DONE", expect="IN_PROGRESS")
    assert s["phase"] == "FEATURE_DONE"


def test_illegal_transition_denied():
    s = st.new_feature_state()
    with pytest.raises(st.TransitionDenied):
        st.transition(s, "FEATURE_DONE")


def test_slice_helpers():
    s = st.new_feature_state()
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


def test_feature_dir_handles_none(tmp_path):
    assert st.feature_dir(tmp_path, None).name == "feature"
    assert st.feature_dir(tmp_path, "feat-a").name == "feat-a"

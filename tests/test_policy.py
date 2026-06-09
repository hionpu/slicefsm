"""Unit tests for scale triage and derived policy."""

from __future__ import annotations

from slicefsm import policy as pol


def test_triage_tiny_edit_is_micro():
    scale, sig = pol.triage_provisional("rename the Save label to Store")
    assert scale == "Micro"
    assert sig["smallish"] is True


def test_triage_persistence_escalates():
    scale, _ = pol.triage_provisional(
        "add a memo feature that persists to the database and syncs to the server"
    )
    assert scale in ("Medium", "Large")


def test_triage_empty():
    scale, _ = pol.triage_provisional("")
    assert scale == "Micro"


def test_measure_scale_by_slice_count():
    one = [{"module": "src/a", "verify_how": "test"}]
    assert pol.measure_scale(one)[0] == "Micro"
    two = one * 2
    assert pol.measure_scale(two)[0] == "Small"
    four = [{"module": f"src/m{i}", "verify_how": "t"} for i in range(4)]
    assert pol.measure_scale(four)[0] == "Medium"
    seven = [{"module": f"src/m{i}", "verify_how": "t"} for i in range(7)]
    assert pol.measure_scale(seven)[0] == "Large"


def test_measure_scale_signals():
    slices = [
        {"module": "src/ui/view", "verify_how": "click the button"},
        {"module": "src/store/db", "verify_how": "saved row persists"},
        {"module": "src/store/db", "verify_how": "row reload"},
    ]
    scale, sig = pol.measure_scale(slices)
    assert sig["touches_ui"] is True
    assert sig["touches_persistence"] is True
    assert sig["crosses_module_boundary"] is True
    assert scale == "Large"  # medium + persist + crosses -> large


def test_slice_smell_flags_layer_noun():
    assert pol.slice_smell("ViewModel 수정") is not None
    assert pol.slice_smell("Service") is not None
    assert pol.slice_smell("E 누르면 미니게임 UI 열림") is None
    assert pol.slice_smell("") is not None


def test_derive_read_policy():
    assert pol.derive_read_policy("Micro", False)["mode"] == "strict"
    assert pol.derive_read_policy("Small", False)["mode"] == "strict"
    assert pol.derive_read_policy("Medium", False)["mode"] == "relaxed"
    assert pol.derive_read_policy("Large", False)["mode"] == "relaxed"
    # risky forces strict at any scale
    assert pol.derive_read_policy("Large", True)["mode"] == "strict"
    assert pol.derive_read_policy("Large", True)["derived_from"]["risky"] is True


def test_fail_threshold():
    assert pol.fail_threshold(False) == 3
    assert pol.fail_threshold(True) == 2


def test_needs_discovery():
    assert pol.needs_discovery("Micro") is False
    assert pol.needs_discovery("Small") is False
    assert pol.needs_discovery("Medium") is True
    assert pol.needs_discovery("Large") is True


def test_explanation():
    assert pol.explanation_required("Micro", False) is False
    assert pol.explanation_required("Medium", False) is True
    assert pol.explanation_required("Small", True) is True
    assert pol.explanation_depth("Micro", False) == "none"
    assert pol.explanation_depth("Medium", False) == "three_line_summary"
    assert pol.explanation_depth("Large", False) == "key_decision_per_slice"
    assert pol.explanation_depth("Small", True) == "root_cause_invariant_rollback"

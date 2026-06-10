"""Tool orchestration. Each function reads state, validates the phase, applies
a transition, writes, logs a gate line, and returns a structured result.

These are MCP-free so they can be unit-tested directly. server.py wraps each
one as an @mcp.tool.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import (
    context_engine,
    edits,
    failure,
    gatelog,
    git_util,
    manual_checks,
    policy,
    state,
    verify,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:24] or "feature")


def _feature_id(desc: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"feat-{_slug(desc)}-{stamp}"


def _effective_scale(s: dict[str, Any]) -> str:
    return (
        s.get("scale")
        or (s.get("scale_measured") or {}).get("value")
        or (s.get("scale_provisional") or {}).get("value")
        or "Small"
    )


# ── submit_feature ─────────────────────────────────────────────────


def submit_feature(project_root: str, desc: str) -> dict[str, Any]:
    s = state.read(project_root)
    scale_prov, signals = policy.triage_provisional(desc)
    to_phase = "DISCOVERY" if policy.needs_discovery(scale_prov) else "SLICING"

    def mut(st: dict[str, Any]) -> None:
        st["feature"] = {"id": _feature_id(desc), "desc": desc, "submitted_at": _now()}
        st["scale_provisional"] = {
            "by": "ai", "value": scale_prov, "at": "submit_feature", "signals": signals,
        }
        st["scale"] = None
        st["scale_source"] = None
        st["scale_measured"] = None
        st["read_policy"] = None
        st["risky"] = False
        st["slices"] = []
        st["approved"] = None
        st["discovery_summary"] = None

    try:
        state.transition(s, to_phase, expect=["NO_FEATURE", "FEATURE_DONE"], mutator=mut)
    except state.TransitionDenied as e:
        return e.payload

    state.write(project_root, s)
    gatelog.append_gate_event(project_root, "submit_feature", {"phase": to_phase, "provisional_scale": scale_prov})
    return {
        "phase": to_phase,
        "provisional_scale": scale_prov,
        "needs_discovery": to_phase == "DISCOVERY",
        "repo_map": context_engine.build_repo_map(project_root),
        "next": (
            "Read-only scan, then propose_slices(slices, discovery_summary=...)."
            if to_phase == "DISCOVERY"
            else "propose_slices(slices)."
        ),
    }


# ── propose_slices ─────────────────────────────────────────────────


def _module_resolves(project_root: str, module: str) -> bool:
    p = (Path(project_root).resolve() / module)
    return p.exists()


def _write_discovery(project_root: str, feature_id: str, summary: str) -> str:
    d = Path(project_root).resolve() / state.STATE_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"discovery-{_slug(feature_id)}.md"
    path.write_text(summary, encoding="utf-8")
    return str(path)


def propose_slices(
    project_root: str,
    slices: list[dict[str, Any]],
    discovery_summary: str | None = None,
) -> dict[str, Any]:
    s = state.read(project_root)
    cur = s.get("phase")

    errors: list[str] = []
    warnings: list[str] = []
    for i, sl in enumerate(slices):
        if not sl.get("module"):
            errors.append(f"slice {i + 1}: missing module")
        elif not _module_resolves(project_root, sl["module"]):
            warnings.append(f"slice {i + 1}: module '{sl['module']}' not found on disk")
        if not sl.get("verify_how"):
            errors.append(f"slice {i + 1}: missing verify_how")
        ac = sl.get("ac_count")
        if not isinstance(ac, int) or not (3 <= ac <= 7):
            errors.append(f"slice {i + 1}: ac_count must be an int in [3,7]")
        smell = policy.slice_smell(sl.get("title", ""))
        if smell:
            warnings.append(f"slice {i + 1}: {smell}")

    if errors:
        return {"error": "validation_failed", "errors": errors, "warnings": warnings}
    if cur == "DISCOVERY" and not (discovery_summary and discovery_summary.strip()):
        return {"error": "discovery_summary_required",
                "reason": "DISCOVERY must produce a discovery_summary before slicing"}

    scale_meas, sig = policy.measure_scale(slices)
    prov = (s.get("scale_provisional") or {}).get("value")
    mismatch = prov is not None and prov != scale_meas

    def mut(st: dict[str, Any]) -> None:
        st["slices"] = [
            {
                "id": i + 1,
                "title": sl.get("title", ""),
                "module": sl["module"],
                "verify_how": sl["verify_how"],
                "ac_count": sl["ac_count"],
                "status": "proposed",
                "verify_fail_count": 0,
            }
            for i, sl in enumerate(slices)
        ]
        st["scale_measured"] = {"by": "harness", "value": scale_meas, "at": "propose_slices", "signals": sig}
        if discovery_summary and discovery_summary.strip():
            fid = (st.get("feature") or {}).get("id", "feature")
            st["discovery_summary"] = _write_discovery(project_root, fid, discovery_summary)

    try:
        state.transition(s, "AWAITING_APPROVAL", expect=["SLICING", "DISCOVERY", "AWAITING_APPROVAL"], mutator=mut)
    except state.TransitionDenied as e:
        return e.payload

    state.write(project_root, s)
    gatelog.append_gate_event(project_root, "propose_slices", {"measured_scale": scale_meas, "mismatch": mismatch})
    return {
        "phase": "AWAITING_APPROVAL",
        "provisional_scale": prov,
        "measured_scale": scale_meas,
        "scale_mismatch": mismatch,
        "warnings": warnings,
        "slices": s["slices"],
        "read_policy_preview": policy.derive_read_policy(scale_meas, False),
        "next": "Human runs out-of-band: harness approve [--scale S] [--risky]",
    }


# ── get_slice_context ──────────────────────────────────────────────


def get_slice_context(
    project_root: str,
    slice_id: int,
    module: str | None = None,
    depth: int = 1,
    feature: str | None = None,
) -> dict[str, Any]:
    """Load (or reload) one slice's bounded context and mark it `implement`.

    Works on a `proposed` slice (start it) or an `implement` slice (resume it).
    A stuck slice needs `harness unstick` first; a done slice is closed.
    """
    s = state.read(project_root)
    if s.get("phase") != "IN_PROGRESS":
        return {"error": "transition_denied", "current_phase": s.get("phase"), "expected_phase": "IN_PROGRESS"}
    sl = state.find_slice(s, slice_id)
    if sl is None:
        return {"error": "slice_not_found", "slice_id": slice_id}
    if sl.get("status") not in ("proposed", "implement"):
        return {"error": "slice_not_startable", "slice_id": slice_id, "status": sl.get("status"),
                "reason": "a stuck slice needs `harness unstick`; a done slice is closed."}

    approved = sl.get("module")
    use_module = module or approved
    if approved and module:  # may scope equal-or-narrower, never swap/broaden
        a = approved.replace("\\", "/").strip("/")
        m = module.replace("\\", "/").strip("/")
        if not (m == a or m.startswith(a + "/")):
            return {"error": "module_mismatch", "approved_module": approved, "requested": module,
                    "reason": "requested module is outside the approved slice module. Use `harness reslice`."}

    ctx = context_engine.get_slice_context(project_root, use_module, depth=depth, write_manifest=True)
    # Checkpoint once, on first activation; resume keeps the original rollback ref.
    if sl.get("checkpoint_ref"):
        ckpt = {"ok": True, "ref": sl["checkpoint_ref"], "resumed": True}
    else:
        ckpt = git_util.make_checkpoint(project_root)

    sl["module"] = use_module
    sl["manifest"] = ctx["manifest_path"]
    if not sl.get("checkpoint_ref"):
        sl["checkpoint_ref"] = ckpt.get("ref", "")
    sl["status"] = "implement"
    state.write(project_root, s)
    gatelog.append_gate_event(project_root, "get_slice_context", {"slice_id": slice_id, "module": use_module, "checkpoint": ckpt})

    out: dict[str, Any] = dict(ctx)
    out["slice_id"] = slice_id
    out["phase"] = "IN_PROGRESS"
    out["slice_status"] = "implement"
    out["checkpoint"] = ckpt
    if ckpt.get("dirty"):
        out["warning"] = "working tree was dirty at checkpoint; review uncommitted changes"
    if feature:
        out["pending_manual_checks"] = manual_checks.pending_items(project_root, feature)
    return out


def list_slices(project_root: str) -> dict[str, Any]:
    """All slices and their status, so a session can pick one to start/resume."""
    s = state.read(project_root)
    return {
        "phase": s.get("phase"),
        "scale": s.get("scale"),
        "read_policy": s.get("read_policy"),
        "slices": [
            {"id": x.get("id"), "title": x.get("title"), "module": x.get("module"),
             "status": x.get("status"), "verify_how": x.get("verify_how"),
             "fails": x.get("verify_fail_count", 0)}
            for x in s.get("slices", [])
        ],
        "hint": "get_slice_context(slice_id) to start a 'proposed' slice or resume an 'implement' one.",
    }


# ── expand_symbol ──────────────────────────────────────────────────


def expand_symbol(project_root: str, slice_id: int, name: str, source_path: str | None = None, reason: str | None = None) -> dict[str, Any]:
    s = state.read(project_root)
    if s.get("phase") != "IN_PROGRESS":
        return {"error": "transition_denied", "current_phase": s.get("phase"), "expected_phase": "IN_PROGRESS"}
    sl = state.find_slice(s, slice_id)
    if sl is None or sl.get("status") != "implement":
        return {"error": "slice_not_active", "slice_id": slice_id, "status": (sl or {}).get("status")}
    out = context_engine.expand_symbol(project_root, name, source_path=source_path, manifest_path=sl.get("manifest"))
    edits.append_expand(project_root, slice_id, name, reason)
    gatelog.append_gate_event(project_root, "expand_symbol", {"slice_id": slice_id, "symbol": name, "found": "error" not in out})
    return out


# ── run_verify ─────────────────────────────────────────────────────


def run_verify(project_root: str, slice_id: int, feature: str | None = None) -> dict[str, Any]:
    s = state.read(project_root)
    if s.get("phase") != "IN_PROGRESS":
        return {"error": "transition_denied", "current_phase": s.get("phase"), "expected_phase": "IN_PROGRESS"}
    sl = state.find_slice(s, slice_id)
    if sl is None or sl.get("status") != "implement":
        return {"error": "slice_not_active", "slice_id": slice_id, "status": (sl or {}).get("status")}

    result = verify.run_verify_suite(project_root)
    automatic = result["overall"]  # pass | fail | no_checks
    manual_required = manual_pending = 0
    if feature:
        mc = manual_checks.summary(project_root, feature)
        manual_required, manual_pending = mc["required"], mc["pending"]

    # "no_checks" must NOT count as a pass on its own.
    if automatic == "fail":
        overall = "fail"
    elif manual_pending > 0:
        overall = "pending_manual"
    elif automatic == "pass":
        overall = "pass"
    elif manual_required > 0:
        overall = "pass"
    else:
        overall = "no_checks"

    risky = bool(s.get("risky"))
    scale = _effective_scale(s)
    threshold = policy.fail_threshold(risky)

    if overall == "fail":
        fails = sl.get("verify_fail_count", 0) + 1
        sl["verify_fail_count"] = fails
        if fails >= threshold:
            sl["status"] = "stuck"
            state.write(project_root, s)
            gatelog.append_gate_event(project_root, "run_verify", {"slice_id": slice_id, "overall": "fail", "stuck": True, "fails": fails})
            return {"overall": "fail", "slice_id": slice_id, "slice_status": "stuck", "verify_fail_count": fails,
                    "guidance": f"Stop editing this slice. Diagnose, then ask the human: harness unstick {slice_id}.",
                    "result": result}
        state.write(project_root, s)
        gatelog.append_gate_event(project_root, "run_verify", {"slice_id": slice_id, "overall": "fail", "fails": fails})
        return {"overall": "fail", "slice_id": slice_id, "slice_status": "implement", "verify_fail_count": fails,
                "remaining_before_stuck": threshold - fails, "result": result}

    if overall == "pending_manual":
        state.write(project_root, s)
        return {"overall": "pending_manual", "slice_id": slice_id,
                "pending_manual_checks": manual_checks.pending_items(project_root, feature) if feature else [],
                "result": result}

    if overall == "no_checks":
        state.write(project_root, s)
        return {"overall": "no_checks", "slice_id": slice_id,
                "guidance": "Nothing verified. Add a verify.sh or test, or declare a manual check, then run_verify again.",
                "result": result}

    # Pass. If closing this slice would finish the feature, apply the H3 gate.
    others_done = all(o.get("status") == "done" for o in s.get("slices", []) if o.get("id") != slice_id)
    if others_done and policy.explanation_required(scale, risky) and not sl.get("explanation"):
        state.write(project_root, s)
        return {"overall": "pending_explanation", "slice_id": slice_id,
                "needs": f"harness explain {slice_id}  (human, out-of-band)",
                "explanation_depth": policy.explanation_depth(scale, risky), "result": result}

    sl["authorship"] = edits.authorship(project_root, slice_id)
    sl["status"] = "done"
    feature_done = state.all_done(s)
    if feature_done:
        try:
            state.transition(s, "FEATURE_DONE", expect="IN_PROGRESS")
        except state.TransitionDenied:
            pass
    state.write(project_root, s)
    gatelog.append_gate_event(project_root, "run_verify", {"slice_id": slice_id, "overall": "pass", "feature_done": feature_done})
    remaining = [o.get("id") for o in s.get("slices", []) if o.get("status") != "done"]
    return {"overall": "pass", "slice_id": slice_id, "slice_status": "done",
            "feature_phase": s.get("phase"), "remaining_slices": remaining,
            "authorship": sl["authorship"],
            "next": "Feature done." if feature_done else "Open a fresh session for another slice (list_slices).",
            "result": result}


# ── analyze_verify_failure ─────────────────────────────────────────


def analyze_verify_failure(
    project_root: str,
    failed_step: str,
    slice_id: int | None = None,
    suspect_paths: list[str] | None = None,
    contract_paths: list[str] | None = None,
) -> dict[str, Any]:
    s = state.read(project_root)
    if s.get("phase") != "IN_PROGRESS":
        return {"error": "transition_denied", "current_phase": s.get("phase"), "expected_phase": "IN_PROGRESS"}
    out = failure.analyze_verify_failure(failed_step, suspect_paths, contract_paths)
    gatelog.append_gate_event(project_root, "analyze_verify_failure", {**out, "slice_id": slice_id})
    return out


# ── track_manual_checks ────────────────────────────────────────────


def track_manual_checks(
    project_root: str,
    feature: str,
    op: str = "summary",
    checks: list[dict[str, Any]] | None = None,
    check_id: str | None = None,
    note: str | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    out = manual_checks.track_manual_checks(
        project_root, feature, op=op, checks=checks, check_id=check_id, note=note, replace=replace,
    )
    gatelog.append_gate_event(project_root, "track_manual_checks", {"op": op, "summary": out.get("summary")})
    return out

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
        st["current_slice"] = None
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


def get_slice_context(project_root: str, module: str, depth: int = 1, feature: str | None = None) -> dict[str, Any]:
    s = state.read(project_root)
    if s.get("phase") != "SLICE_SCOPING":
        return {"error": "transition_denied", "current_phase": s.get("phase"), "expected_phase": "SLICE_SCOPING"}

    ctx = context_engine.get_slice_context(project_root, module, depth=depth, write_manifest=True)
    ckpt = git_util.make_checkpoint(project_root)

    def mut(st: dict[str, Any]) -> None:
        cs = state.current_slice(st)
        if cs is not None:
            cs["module"] = module
            cs["manifest"] = ctx["manifest_path"]
            cs["checkpoint_ref"] = ckpt.get("ref", "")
            cs["status"] = "implement"

    try:
        state.transition(s, "SLICE_IMPLEMENT", expect="SLICE_SCOPING", mutator=mut)
    except state.TransitionDenied as e:
        return e.payload

    state.write(project_root, s)
    gatelog.append_gate_event(project_root, "get_slice_context", {"module": module, "checkpoint": ckpt})

    out: dict[str, Any] = dict(ctx)
    out["phase"] = "SLICE_IMPLEMENT"
    out["checkpoint"] = ckpt
    if ckpt.get("dirty"):
        out["warning"] = "working tree was dirty at checkpoint; review uncommitted changes"
    if feature:
        out["pending_manual_checks"] = manual_checks.pending_items(project_root, feature)
    return out


# ── expand_symbol ──────────────────────────────────────────────────


def expand_symbol(project_root: str, name: str, source_path: str | None = None, reason: str | None = None) -> dict[str, Any]:
    s = state.read(project_root)
    if s.get("phase") not in ("SLICE_IMPLEMENT", "SLICE_VERIFY"):
        return {"error": "transition_denied", "current_phase": s.get("phase"),
                "expected_phase": ["SLICE_IMPLEMENT", "SLICE_VERIFY"]}
    cs = state.current_slice(s)
    manifest = cs.get("manifest") if cs else None
    out = context_engine.expand_symbol(project_root, name, source_path=source_path, manifest_path=manifest)
    edits.append_expand(project_root, s.get("current_slice"), name, reason)
    gatelog.append_gate_event(project_root, "expand_symbol", {"symbol": name, "found": "error" not in out})
    return out


# ── run_verify ─────────────────────────────────────────────────────


def run_verify(project_root: str, feature: str | None = None) -> dict[str, Any]:
    s = state.read(project_root)
    cur = s.get("phase")
    if cur not in ("SLICE_IMPLEMENT", "SLICE_VERIFY"):
        return {"error": "transition_denied", "current_phase": cur,
                "expected_phase": ["SLICE_IMPLEMENT", "SLICE_VERIFY"]}

    result = verify.run_verify_suite(project_root)
    overall = result["overall"]
    if feature and overall in ("pass", "no_checks"):
        mc = manual_checks.summary(project_root, feature)
        if mc["pending"] > 0:
            overall = "pending_manual"

    # Step 1: ensure we are in SLICE_VERIFY.
    if cur == "SLICE_IMPLEMENT":
        def to_verify(st: dict[str, Any]) -> None:
            c = state.current_slice(st)
            if c:
                c["status"] = "verify"
        state.transition(s, "SLICE_VERIFY", expect="SLICE_IMPLEMENT", mutator=to_verify)

    risky = bool(s.get("risky"))
    scale = _effective_scale(s)
    n_slices = len(s.get("slices", []))
    cur_id = s.get("current_slice")
    is_last = cur_id == n_slices
    threshold = policy.fail_threshold(risky)

    # Failure path.
    if overall == "fail":
        cs = state.current_slice(s)
        fails = (cs.get("verify_fail_count", 0) + 1) if cs else 1
        if cs:
            cs["verify_fail_count"] = fails
        if fails >= threshold:
            state.transition(s, "STUCK", expect="SLICE_VERIFY")
            state.write(project_root, s)
            gatelog.append_gate_event(project_root, "run_verify", {"overall": "fail", "stuck": True, "fails": fails})
            return {"overall": "fail", "phase": "STUCK", "verify_fail_count": fails,
                    "guidance": "Stop. No new edits. Diagnose and run: harness unstick (human).",
                    "result": result}
        state.transition(s, "SLICE_IMPLEMENT", expect="SLICE_VERIFY")
        state.write(project_root, s)
        gatelog.append_gate_event(project_root, "run_verify", {"overall": "fail", "fails": fails})
        return {"overall": "fail", "phase": "SLICE_IMPLEMENT", "verify_fail_count": fails,
                "remaining_before_stuck": threshold - fails, "result": result}

    # Pending: stay in SLICE_VERIFY.
    if overall == "pending_manual":
        state.write(project_root, s)
        return {"overall": "pending_manual", "phase": "SLICE_VERIFY",
                "pending_manual_checks": manual_checks.pending_items(project_root, feature) if feature else [],
                "result": result}

    # Pass. Explanation gate on the last slice (Medium+ / risky).
    cs = state.current_slice(s)
    if is_last and policy.explanation_required(scale, risky) and not (cs and cs.get("explanation")):
        state.write(project_root, s)
        return {"overall": "pending_explanation", "phase": "SLICE_VERIFY",
                "needs": f"harness explain {cur_id}  (human, out-of-band)",
                "explanation_depth": policy.explanation_depth(scale, risky), "result": result}

    author = edits.authorship(project_root, cur_id)
    if cs:
        cs["authorship"] = author

    if is_last:
        def done(st: dict[str, Any]) -> None:
            c = state.current_slice(st)
            if c:
                c["status"] = "done"
        state.transition(s, "FEATURE_DONE", expect="SLICE_VERIFY", mutator=done)
        state.write(project_root, s)
        gatelog.append_gate_event(project_root, "run_verify", {"overall": "pass", "feature_done": True})
        return {"overall": "pass", "phase": "FEATURE_DONE", "authorship": author, "result": result}

    # Advance to next slice scoping.
    def advance(st: dict[str, Any]) -> None:
        c = state.current_slice(st)
        if c:
            c["status"] = "done"
        st["current_slice"] = (cur_id or 0) + 1
        nxt = state.current_slice(st)
        if nxt:
            nxt["status"] = "scoping"

    state.transition(s, "SLICE_SCOPING", expect="SLICE_VERIFY", mutator=advance)
    state.write(project_root, s)
    gatelog.append_gate_event(project_root, "run_verify", {"overall": "pass", "advance_to": (cur_id or 0) + 1})
    return {"overall": "pass", "phase": "SLICE_SCOPING", "current_slice": (cur_id or 0) + 1,
            "authorship": author, "next": "Open a fresh session; first call get_slice_context.", "result": result}


# ── analyze_verify_failure ─────────────────────────────────────────


def analyze_verify_failure(
    project_root: str,
    failed_step: str,
    suspect_paths: list[str] | None = None,
    contract_paths: list[str] | None = None,
) -> dict[str, Any]:
    s = state.read(project_root)
    if s.get("phase") not in ("SLICE_VERIFY", "STUCK"):
        return {"error": "transition_denied", "current_phase": s.get("phase"),
                "expected_phase": ["SLICE_VERIFY", "STUCK"]}
    out = failure.analyze_verify_failure(failed_step, suspect_paths, contract_paths)
    gatelog.append_gate_event(project_root, "analyze_verify_failure", out)
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

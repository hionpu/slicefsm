"""The out-of-band `harness` CLI. Human-only writers of state.

approve / explain / unstick / reslice change state and require an interactive
confirmation on the controlling terminal. A non-interactive tool call has no
tty, so it fails closed — the AI cannot self-approve. This is layer two; the
PreToolUse hook already denies these commands as Bash calls (layer one).

status is read-only and safe for the AI to run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import gatelog, git_util, policy, state


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _tree_blocks(project_root: str) -> dict[str, Any] | None:
    """Tree policy A: refuse a feature switch while the git tree is dirty."""
    if git_util.is_git_repo(project_root) and git_util.is_dirty(project_root):
        return {"ok": False, "reason": "dirty_tree",
                "guidance": "Commit or `git stash` your changes before switching features "
                            "(prevents one feature's diff leaking into another)."}
    return None


# ── interactive confirmation (fails closed without a tty) ──────────


def _read_tty(prompt: str) -> str | None:
    """Read one line from the controlling terminal, or None if there is none."""
    try:
        f = open("CONIN$" if os.name == "nt" else "/dev/tty", "r", encoding="utf-8")
    except OSError:
        return None
    try:
        sys.stderr.write(prompt)
        sys.stderr.flush()
        return f.readline()
    except OSError:
        return None
    finally:
        f.close()


def _tty_confirm(prompt: str) -> bool:
    line = _read_tty(prompt)
    if line is None:
        sys.stderr.write(
            "No interactive terminal. Human confirmation is required — refusing.\n"
        )
        return False
    return line.strip().lower() in ("y", "yes")


# ── commands (core logic; confirm injected for testability) ────────


def cmd_approve(
    project_root: str,
    scale: str | None = None,
    risky: bool = False,
    note: str | None = None,
    confirm: Callable[[str], bool] = _tty_confirm,
) -> dict[str, Any]:
    s = state.read(project_root)
    if s.get("phase") != "AWAITING_APPROVAL":
        return {"ok": False, "reason": "wrong_phase", "phase": s.get("phase"),
                "expected": "AWAITING_APPROVAL"}
    final_scale = scale or (s.get("scale_measured") or {}).get("value") or "Small"
    n = len(s.get("slices", []))
    if not confirm(f"Approve {n} slice(s) at scale={final_scale}, risky={risky}? [y/N] "):
        return {"ok": False, "reason": "not_confirmed"}

    def mut(st: dict[str, Any]) -> None:
        st["scale"] = final_scale
        st["scale_source"] = "human_approved"
        st["risky"] = bool(risky)
        st["read_policy"] = policy.derive_read_policy(final_scale, bool(risky))
        # All slices become startable; any session can pick one (parallel).
        for sl in st.get("slices", []):
            sl["status"] = "proposed"
        st["approved"] = {"at": _now(), "by": "human", "note": note or ""}

    try:
        state.transition(s, "IN_PROGRESS", expect="AWAITING_APPROVAL", mutator=mut)
    except state.TransitionDenied as e:
        return e.payload
    state.write(project_root, s)
    gatelog.append_gate_event(project_root, "harness_approve", {"scale": final_scale, "risky": bool(risky)})
    return {"ok": True, "phase": "IN_PROGRESS", "scale": final_scale, "read_policy": s["read_policy"]}


def cmd_explain(
    project_root: str,
    slice_id: int,
    file: str | None = None,
    text: str | None = None,
) -> dict[str, Any]:
    s = state.read(project_root)
    target = next((x for x in s.get("slices", []) if x.get("id") == slice_id), None)
    if target is None:
        return {"ok": False, "reason": "slice_not_found", "slice_id": slice_id}

    content = text
    if file:
        try:
            content = Path(file).read_text(encoding="utf-8")
        except OSError:
            return {"ok": False, "reason": "file_unreadable", "file": file}
    if content is None:
        content = _read_tty(f"Explain slice {slice_id} (one line): ") or ""
    if not content.strip():
        return {"ok": False, "reason": "empty_explanation"}

    fid = (s.get("feature") or {}).get("id") or "feature"
    d = state.feature_dir(project_root, fid)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"explain-slice-{slice_id}.md"
    path.write_text(content, encoding="utf-8")
    target["explanation"] = str(path)
    state.write(project_root, s)
    gatelog.append_gate_event(project_root, "harness_explain", {"slice_id": slice_id}, feature_id=fid)
    return {"ok": True, "path": str(path)}


def cmd_unstick(
    project_root: str,
    slice_id: int,
    note: str | None = None,
    confirm: Callable[[str], bool] = _tty_confirm,
) -> dict[str, Any]:
    s = state.read(project_root)
    sl = state.find_slice(s, slice_id)
    if sl is None:
        return {"ok": False, "reason": "slice_not_found", "slice_id": slice_id}
    if sl.get("status") != "stuck":
        return {"ok": False, "reason": "not_stuck", "slice_id": slice_id, "status": sl.get("status")}
    if not confirm(f"Unstick slice {slice_id} and let the AI retry it? [y/N] "):
        return {"ok": False, "reason": "not_confirmed"}
    sl["verify_fail_count"] = 0
    sl["status"] = "implement"
    state.write(project_root, s)
    gatelog.append_gate_event(project_root, "harness_unstick", {"slice_id": slice_id, "note": note or ""})
    return {"ok": True, "slice_id": slice_id, "slice_status": "implement"}


def cmd_reslice(
    project_root: str,
    note: str | None = None,
    confirm: Callable[[str], bool] = _tty_confirm,
) -> dict[str, Any]:
    s = state.read(project_root)
    scale_prov = (s.get("scale_provisional") or {}).get("value", "Small")
    to_phase = "DISCOVERY" if policy.needs_discovery(scale_prov) else "SLICING"
    if not confirm(f"Re-slice the feature (back to {to_phase})? [y/N] "):
        return {"ok": False, "reason": "not_confirmed"}

    def mut(st: dict[str, Any]) -> None:
        st["slices"] = []
        st["approved"] = None
        st["scale"] = None
        st["scale_source"] = None

    try:
        state.transition(s, to_phase, mutator=mut)
    except state.TransitionDenied as e:
        return e.payload
    state.write(project_root, s)
    gatelog.append_gate_event(project_root, "harness_reslice", {"to": to_phase})
    return {"ok": True, "phase": to_phase}


def cmd_status(project_root: str) -> dict[str, Any]:
    s = state.read(project_root)
    rs = state.read_root(project_root)
    return {
        "active_feature": rs.get("active_feature_id"),
        "phase": s.get("phase"),
        "feature": (s.get("feature") or {}).get("desc"),
        "scale": s.get("scale"),
        "risky": s.get("risky"),
        "read_policy": s.get("read_policy"),
        "active_slices": [x.get("id") for x in state.active_slices(s)],
        "slices": [
            {"id": x.get("id"), "title": x.get("title"), "status": x.get("status"),
             "module": x.get("module"), "fails": x.get("verify_fail_count", 0),
             "authorship": x.get("authorship")}
            for x in s.get("slices", [])
        ],
        "features": _feature_rows(rs),
    }


# ── multi-feature management (root level) ──────────────────────────


def _feature_rows(rs: dict[str, Any]) -> list[dict[str, Any]]:
    active = rs.get("active_feature_id")
    rows = []
    for fid, fs in rs.get("features", {}).items():
        slices = fs.get("slices", [])
        done = sum(1 for x in slices if x.get("status") == "done")
        rows.append({
            "id": fid,
            "active": fid == active,
            "paused": bool(fs.get("paused")),
            "phase": fs.get("phase"),
            "desc": (fs.get("feature") or {}).get("desc"),
            "slices": f"{done}/{len(slices)}" if slices else "0/0",
        })
    return rows


def cmd_list(project_root: str) -> dict[str, Any]:
    return {"features": _feature_rows(state.read_root(project_root))}


def cmd_pause(project_root: str, confirm: Callable[[str], bool] = _tty_confirm) -> dict[str, Any]:
    rs = state.read_root(project_root)
    fid = rs.get("active_feature_id")
    if not fid:
        return {"ok": False, "reason": "no_active_feature"}
    blocked = _tree_blocks(project_root)
    if blocked:
        return blocked
    if not confirm(f"Pause feature {fid}? [y/N] "):
        return {"ok": False, "reason": "not_confirmed"}
    rs["features"][fid]["paused"] = True
    rs["active_feature_id"] = None
    state.write_root(project_root, rs)
    gatelog.append_gate_event(project_root, "harness_pause", {}, feature_id=fid)
    return {"ok": True, "paused": fid}


def cmd_switch(project_root: str, feature_id: str, confirm: Callable[[str], bool] = _tty_confirm) -> dict[str, Any]:
    """Make feature_id active (resume). Pauses the current one (clean tree only)."""
    rs = state.read_root(project_root)
    if feature_id not in rs.get("features", {}):
        return {"ok": False, "reason": "feature_not_found", "feature_id": feature_id}
    cur = rs.get("active_feature_id")
    if cur == feature_id:
        return {"ok": True, "active_feature": feature_id, "note": "already active"}
    if cur is not None:
        blocked = _tree_blocks(project_root)
        if blocked:
            return blocked
    if not confirm(f"Switch active feature to {feature_id}? [y/N] "):
        return {"ok": False, "reason": "not_confirmed"}
    if cur is not None:
        rs["features"][cur]["paused"] = True
    rs["features"][feature_id]["paused"] = False
    rs["active_feature_id"] = feature_id
    state.write_root(project_root, rs)
    gatelog.append_gate_event(project_root, "harness_switch", {"from": cur}, feature_id=feature_id)
    return {"ok": True, "active_feature": feature_id, "phase": rs["features"][feature_id].get("phase")}


def cmd_cancel(project_root: str, feature_id: str, confirm: Callable[[str], bool] = _tty_confirm) -> dict[str, Any]:
    rs = state.read_root(project_root)
    if feature_id not in rs.get("features", {}):
        return {"ok": False, "reason": "feature_not_found", "feature_id": feature_id}
    if not confirm(f"Cancel and discard feature {feature_id}? [y/N] "):
        return {"ok": False, "reason": "not_confirmed"}
    rs["features"].pop(feature_id, None)
    if rs.get("active_feature_id") == feature_id:
        rs["active_feature_id"] = None
    state.write_root(project_root, rs)
    gatelog.append_gate_event(project_root, "harness_cancel", {}, feature_id=feature_id)
    return {"ok": True, "cancelled": feature_id}


# ── argparse entry point ───────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="harness", description="slicefsm out-of-band human controls")
    p.add_argument("--target", default=".", help="project root (default: cwd)")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("approve", help="approve the proposed slices and start work")
    sp.add_argument("--scale", choices=list(policy.SCALES))
    sp.add_argument("--risky", action="store_true")
    sp.add_argument("--note")

    se = sub.add_parser("explain", help="record a human explanation for a slice (H3 gate)")
    se.add_argument("slice_id", type=int)
    se.add_argument("--file")
    se.add_argument("--text")

    su = sub.add_parser("unstick", help="release a stuck slice for one more try")
    su.add_argument("slice_id", type=int)
    su.add_argument("--note")

    sr = sub.add_parser("reslice", help="discard slices and re-slice the feature")
    sr.add_argument("--note")

    sub.add_parser("status", help="print current feature/slices + all features (read-only)")
    sub.add_parser("list", help="list all features (read-only)")
    sub.add_parser("pause", help="pause the active feature (clears active; clean tree required)")

    swp = sub.add_parser("resume", help="make a feature active (alias: switch)")
    swp.add_argument("feature_id")
    swp2 = sub.add_parser("switch", help="switch active feature")
    swp2.add_argument("feature_id")

    scp = sub.add_parser("cancel", help="discard a feature")
    scp.add_argument("feature_id")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.target
    if args.command == "approve":
        result = cmd_approve(root, scale=args.scale, risky=args.risky, note=args.note)
    elif args.command == "explain":
        result = cmd_explain(root, args.slice_id, file=args.file, text=args.text)
    elif args.command == "unstick":
        result = cmd_unstick(root, args.slice_id, note=args.note)
    elif args.command == "reslice":
        result = cmd_reslice(root, note=args.note)
    elif args.command == "status":
        result = cmd_status(root)
    elif args.command == "list":
        result = cmd_list(root)
    elif args.command == "pause":
        result = cmd_pause(root)
    elif args.command in ("resume", "switch"):
        result = cmd_switch(root, args.feature_id)
    elif args.command == "cancel":
        result = cmd_cancel(root, args.feature_id)
    else:  # pragma: no cover
        return 2
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())

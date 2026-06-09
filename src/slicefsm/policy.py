"""Scale triage and derived policy.

Scale drives read strictness, discovery, fail threshold, and explanation depth.
It is set in three steps (see DESIGN section 3):
  1. triage_provisional  - AI-side guess from the feature text (rough).
  2. measure_scale       - harness recompute from real slice signals.
  3. human sets final    - via `harness approve` (this module only derives).

All functions are pure. No IO.
"""

from __future__ import annotations

from typing import Any

SCALES = ("Micro", "Small", "Medium", "Large")

# Words that push scale up (state, boundaries, integration).
_ESCALATE_KW = (
    "persist", "save", "store", "database", " db ", "server", "sync",
    "schema", "api", "auth", "migration", "concurren", "realtime",
    "websocket", "undo", "redo", "permission",
)
# Words that signal a tiny edit.
_SMALL_KW = (
    "rename", "typo", "label", "tweak", "color", "copy", "wording",
    "tooltip", "rephrase",
)
_PERSIST_KW = ("save", "persist", "db", "database", "store", "schema", "file", "sync", "api")
# Leading source roots stripped before deriving a module's identity, so
# "src/ui/view" and "src/store/db" count as two modules, not one ("src").
_SRC_PREFIXES = ("src", "lib", "app", "source", "pkg", "internal")
_UI_KW = ("ui", "view", "screen", "button", "popup", "xaml", "render", "key", "click", "menu", "dialog")
# Layer nouns: a slice titled only with one of these is a horizontal-slice smell.
_LAYER_NOUNS = ("viewmodel", "service", "controller", "xaml", "repository", "dao", "model", "dto", "helper", "util", "manager")


def _score_to_scale(score: int) -> str:
    if score <= 0:
        return "Micro"
    if score <= 1:
        return "Small"
    if score <= 3:
        return "Medium"
    return "Large"


def triage_provisional(desc: str) -> tuple[str, dict[str, Any]]:
    """Guess scale from the raw feature description. AI-asserted, rough.

    Only decides whether DISCOVERY runs. The harness recomputes later.
    """
    text = f" {(desc or '').lower()} "
    words = len((desc or "").split())
    conj = text.count(" and ") + text.count(",") + text.count("그리고") + text.count(" 및 ")
    escalate = sum(1 for kw in _ESCALATE_KW if kw in text)
    smallish = any(kw in text for kw in _SMALL_KW)

    score = 0
    if words > 30:
        score += 1
    if words > 80:
        score += 1
    if conj >= 2:
        score += 1
    score += min(escalate, 3)
    # A short rename/typo/label edit is Micro even if it names a "save"/"store"
    # word, because that word is usually a UI label, not real persistence work.
    if smallish and words <= 15:
        score = 0

    scale = _score_to_scale(score)
    signals = {
        "word_count": words,
        "conjunctions": conj,
        "escalate_hits": escalate,
        "smallish": smallish,
        "score": score,
    }
    return scale, signals


def _module_root(module: str) -> str:
    """Module identity: first path segment after any leading source prefix."""
    parts = [p for p in module.replace("\\", "/").strip("/").split("/") if p]
    if parts and parts[0].lower() in _SRC_PREFIXES:
        parts = parts[1:]
    return parts[0] if parts else ""


def measure_scale(slices: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """Recompute scale from real slice signals (post propose_slices)."""
    n = len(slices)
    modules = [str(s.get("module", "")) for s in slices]
    roots = {_module_root(m) for m in modules if m}
    blob = " ".join(
        f"{s.get('module', '')} {s.get('verify_how', '')} {s.get('title', '')}"
        for s in slices
    ).lower()
    touches_persistence = any(k in blob for k in _PERSIST_KW)
    touches_ui = any(k in blob for k in _UI_KW)
    crosses = len(roots) > 1

    if n >= 7:
        scale = "Large"
    elif n >= 3:
        scale = "Medium"
    elif n == 2:
        scale = "Small"
    else:
        scale = "Micro"

    # A persistence-touching, boundary-crossing Medium reads as Large risk.
    if scale == "Medium" and touches_persistence and crosses:
        scale = "Large"

    signals = {
        "actual_slices": n,
        "modules_resolved": len(roots),
        "touches_persistence": touches_persistence,
        "touches_ui": touches_ui,
        "crosses_module_boundary": crosses,
    }
    return scale, signals


def slice_smell(title: str) -> str | None:
    """Return a warning if a slice title is only a layer noun, else None.

    A flag, not a block. The human still decides at approval.
    """
    t = (title or "").strip().lower()
    if not t:
        return "empty title"
    # A bare layer noun (or 'X 수정'/'edit X' over a layer noun) is a smell.
    tokens = [tok for tok in t.replace("/", " ").split() if tok]
    layer_hits = [tok for tok in tokens if tok in _LAYER_NOUNS]
    if layer_hits and len(tokens) <= 3:
        return f"title looks layer-based ({', '.join(layer_hits)}); prefer a user-visible behavior"
    return None


def derive_read_policy(scale: str, risky: bool) -> dict[str, Any]:
    """strict for Micro/Small or any risky; relaxed for Medium/Large."""
    if risky or scale in ("Micro", "Small"):
        mode = "strict"
    else:
        mode = "relaxed"
    return {"mode": mode, "derived_from": {"scale": scale, "risky": bool(risky)}}


def fail_threshold(risky: bool) -> int:
    """Verify failures before STUCK."""
    return 2 if risky else 3


def needs_discovery(scale: str) -> bool:
    return scale in ("Medium", "Large")


def explanation_required(scale: str, risky: bool) -> bool:
    """H3: must capture a human explanation before closing the last slice."""
    return bool(risky) or scale in ("Medium", "Large")


def explanation_depth(scale: str, risky: bool) -> str:
    if risky:
        return "root_cause_invariant_rollback"
    if scale == "Large":
        return "key_decision_per_slice"
    if scale == "Medium":
        return "three_line_summary"
    return "none"

"""Classify a verify failure as contract-sensitive or routine.

Contract-sensitive failures (a failing test, a touched contract file) block a
blind patch: the human must approve the fix strategy first. Routine failures
(lint, format) may be fixed and reported.
"""

from __future__ import annotations

from typing import Any

_CONTRACT_MARKERS = (
    "spec", "invariant", "interface", "contract", "_test", "test_", ".spec.", "tests/",
)


def _looks_contract(path: str) -> bool:
    p = path.lower()
    return any(m in p for m in _CONTRACT_MARKERS)


def analyze_verify_failure(
    failed_step: str,
    suspect_paths: list[str] | None = None,
    contract_paths: list[str] | None = None,
) -> dict[str, Any]:
    signals: list[str] = []
    sensitive = False

    if failed_step.strip().lower() in ("test", "tests", "pytest"):
        sensitive = True
        signals.append("failed step is the test suite")

    for p in (suspect_paths or []) + (contract_paths or []):
        if _looks_contract(p):
            sensitive = True
            signals.append(f"contract-shaped path: {p}")

    return {
        "classification": "contract_sensitive" if sensitive else "routine",
        "patch_allowed": not sensitive,
        "signals": signals,
        "guidance": (
            "Root cause only. Do not patch until the human approves a fix strategy."
            if sensitive
            else "Routine: fix and report."
        ),
    }

"""FastMCP server: 7 tools, thin wrappers over ops.py.

Docstrings are kept short on purpose. Tool definitions load every session
(see DESIGN section 10 token budget), so detail goes into the runtime result,
not the schema.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from . import ops

mcp = FastMCP("slicefsm")


@mcp.tool()
def submit_feature(project_root: str, desc: str) -> dict[str, Any]:
    """Start a feature. Guesses scale, routes to DISCOVERY or SLICING, returns a repo-map. Phase NO_FEATURE/FEATURE_DONE only."""
    return ops.submit_feature(project_root, desc)


@mcp.tool()
def propose_slices(project_root: str, slices: list[dict[str, Any]], discovery_summary: str | None = None) -> dict[str, Any]:
    """Propose vertical slices for human approval. Each slice needs module + verify_how + ac_count(3-7). Does not start work."""
    return ops.propose_slices(project_root, slices, discovery_summary=discovery_summary)


@mcp.tool()
def list_slices(project_root: str) -> dict[str, Any]:
    """List all slices and their status (proposed/implement/stuck/done). Use to pick a slice to start or resume."""
    return ops.list_slices(project_root)


@mcp.tool()
def get_slice_context(project_root: str, slice_id: int, module: str | None = None, depth: int = 1, feature: str | None = None) -> dict[str, Any]:
    """Start or resume a slice: returns own-module full text + dep signatures + excluded names, writes a rollback checkpoint. Feature phase IN_PROGRESS."""
    return ops.get_slice_context(project_root, slice_id, module=module, depth=depth, feature=feature)


@mcp.tool()
def expand_symbol(project_root: str, slice_id: int, name: str, source_path: str | None = None, reason: str | None = None) -> dict[str, Any]:
    """Reveal one dependency symbol's body for a slice (logged). Use instead of reading the whole file."""
    return ops.expand_symbol(project_root, slice_id, name, source_path=source_path, reason=reason)


@mcp.tool()
def run_verify(project_root: str, slice_id: int, feature: str | None = None) -> dict[str, Any]:
    """Run the verify suite for a slice. On pass: mark it done (and finish the feature if it was the last). On repeated fail: stuck."""
    return ops.run_verify(project_root, slice_id, feature=feature)


@mcp.tool()
def analyze_verify_failure(project_root: str, failed_step: str, slice_id: int | None = None, suspect_paths: list[str] | None = None, contract_paths: list[str] | None = None) -> dict[str, Any]:
    """Classify a failure as contract-sensitive (no blind patch) or routine. Feature phase IN_PROGRESS."""
    return ops.analyze_verify_failure(project_root, failed_step, slice_id=slice_id, suspect_paths=suspect_paths, contract_paths=contract_paths)


@mcp.tool()
def track_manual_checks(project_root: str, feature: str, op: str = "summary", checks: list[dict[str, Any]] | None = None, check_id: str | None = None, note: str | None = None, replace: bool = False) -> dict[str, Any]:
    """Manual-check ledger: declare | confirm | handoff | list | summary. run_verify blocks 'done' while required checks are pending."""
    return ops.track_manual_checks(project_root, feature, op=op, checks=checks, check_id=check_id, note=note, replace=replace)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

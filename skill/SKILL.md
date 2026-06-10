---
name: slicefsm
description: Deterministic slice harness. A hook-enforced state machine that bounds tools per state and serves only the current slice's context. Apply when implementing features, fixing bugs, or refactoring. Skip for Q&A.
---

# slicefsm

> Split a feature into vertical slices. The human approves the set and owns scale. Slices are implemented **one at a time, in order**, each inside a bounded context. A repo can hold **several features**; one is active. The human can pause the active feature and switch to another.

On hook-capable clients (Pi, Claude Code, opencode) the current state's rules are injected each turn — follow the injected `[slicefsm]` line. This file is the fallback when hooks are off.

## State machine

One **active** feature at a time (others may be paused). The active feature's phase:

```
NO_FEATURE → [DISCOVERY] → SLICING → AWAITING_APPROVAL → IN_PROGRESS → FEATURE_DONE
```

When no feature is active: `NO_ACTIVE_FEATURE` (only submit_feature / resume / read).

Inside `IN_PROGRESS`, slices are **sequential** — at most one is `implement`:

```
proposed → implement ⇄ (run_verify) → done
implement → stuck (after N failed verifies) → (harness unstick) → implement
```

DISCOVERY runs only for Medium/Large (read-only scan first). The feature is `FEATURE_DONE` when every slice is `done`.

## Tools (MCP)

| When | Call |
|---|---|
| start a feature | `submit_feature(project_root, desc)` → guesses scale, returns repo-map. Refused while another feature is active — the human pauses first. |
| after discovery/slicing | `propose_slices(project_root, slices, discovery_summary?)` |
| IN_PROGRESS: pick work | `list_slices(project_root)` → statuses; then start/resume one |
| start/resume a slice | `get_slice_context(project_root, slice_id, module?)` — first action in a session |
| while implementing | edit within the slice module; `expand_symbol(project_root, slice_id, name)` for a dep body |
| verify a slice | `run_verify(project_root, slice_id, feature?)`; `analyze_verify_failure(project_root, failed_step, slice_id)` on fail |
| any | `track_manual_checks(...)` for ui-heavy checks |

Each slice in `propose_slices` needs: `title` (a user-visible behavior, not a layer noun), `module`, `verify_how`, `ac_count` (3–7).

## Rules

- **Human owns approval and scale.** After `propose_slices`, stop. The human runs `harness approve [--scale S] [--risky]` out-of-band. You cannot self-approve.
- **One slice at a time, in order.** Call `get_slice_context(slice_id)` first and work that slice. You cannot start another slice until the current one passes verify (or gets stuck). To resume after a break, open a fresh session and call `get_slice_context` on the same slice.
- **Bounded reads.** For a dependency body, call `expand_symbol` — do not read the whole file. Strict mode (Micro/Small/risky) denies out-of-slice reads.
- **Edits stay in active slices**, every scale (new files inside the slice dir are fine). Out-of-scope change needs human approval.
- **Verify before done.** `run_verify(slice_id)` must pass. On Medium+/risky the slice that finishes the feature also needs `harness explain <id>`.
- **On stuck:** stop editing that slice. Diagnose, then ask the human to run `harness unstick <id>`.

## Human commands (out-of-band, terminal only)

Per-feature: `harness approve` · `harness explain <id>` · `harness unstick <id>` · `harness reslice`.

Across features: `harness pause` · `harness resume <feature_id>` · `harness switch <feature_id>` · `harness cancel <feature_id>` · `harness list` / `harness status` (read-only). Switching features requires a clean git tree (commit or stash first).

See `references/` for slicing guidance and the context-scoping model.

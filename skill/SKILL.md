---
name: slicefsm
description: Deterministic slice harness. A hook-enforced state machine that bounds tools per state and serves only the current slice's context. Apply when implementing features, fixing bugs, or refactoring. Skip for Q&A.
---

# slicefsm

> Split a feature into vertical slices. The human approves the set and owns scale. Slices then run in parallel — one slice per session, inside a bounded context — and any session can start a new slice or resume a paused one.

On hook-capable clients (Pi, Claude Code, opencode) the current state's rules are injected each turn — follow the injected `[slicefsm]` line. This file is the fallback when hooks are off.

## State machine

Feature phase:

```
NO_FEATURE → [DISCOVERY] → SLICING → AWAITING_APPROVAL → IN_PROGRESS → FEATURE_DONE
```

Inside `IN_PROGRESS`, each slice has its own status and they run **in parallel**:

```
proposed → implement ⇄ (run_verify) → done
implement → stuck (after N failed verifies) → (harness unstick) → implement
```

DISCOVERY runs only for Medium/Large (read-only scan first). The feature is `FEATURE_DONE` when every slice is `done`.

## Tools (MCP)

| When | Call |
|---|---|
| start a feature | `submit_feature(project_root, desc)` → guesses scale, returns repo-map |
| after discovery/slicing | `propose_slices(project_root, slices, discovery_summary?)` |
| IN_PROGRESS: pick work | `list_slices(project_root)` → statuses; then start/resume one |
| start/resume a slice | `get_slice_context(project_root, slice_id, module?)` — first action in a session |
| while implementing | edit within the slice module; `expand_symbol(project_root, slice_id, name)` for a dep body |
| verify a slice | `run_verify(project_root, slice_id, feature?)`; `analyze_verify_failure(project_root, failed_step, slice_id)` on fail |
| any | `track_manual_checks(...)` for ui-heavy checks |

Each slice in `propose_slices` needs: `title` (a user-visible behavior, not a layer noun), `module`, `verify_how`, `ac_count` (3–7).

## Rules

- **Human owns approval and scale.** After `propose_slices`, stop. The human runs `harness approve [--scale S] [--risky]` out-of-band. You cannot self-approve.
- **One slice per session.** In a session, call `get_slice_context(slice_id)` first and work that slice. To work another slice or resume a paused one, open a fresh session and pick it via `list_slices`.
- **Bounded reads.** For a dependency body, call `expand_symbol` — do not read the whole file. Strict mode (Micro/Small/risky) denies out-of-slice reads.
- **Edits stay in active slices**, every scale (new files inside the slice dir are fine). Out-of-scope change needs human approval.
- **Verify before done.** `run_verify(slice_id)` must pass. On Medium+/risky the slice that finishes the feature also needs `harness explain <id>`.
- **On stuck:** stop editing that slice. Diagnose, then ask the human to run `harness unstick <id>`.

## Human commands (out-of-band, terminal only)

`harness approve` · `harness explain <id>` · `harness unstick <id>` · `harness reslice` · `harness status` (read-only).

See `references/` for slicing guidance and the context-scoping model.

---
name: slicefsm
description: Deterministic slice harness. A hook-enforced state machine that bounds tools per state and serves only the current slice's context. Apply when implementing features, fixing bugs, or refactoring. Skip for Q&A.
---

# slicefsm

> Split a feature into vertical slices. Implement one slice per session, inside a bounded context. The human approves slices and owns scale.

On hook-capable clients (Pi, Claude Code, opencode) the current state's rules are injected each turn — follow the injected `[slicefsm]` line. This file is the fallback when hooks are off.

## State machine

```
NO_FEATURE → [DISCOVERY] → SLICING → AWAITING_APPROVAL
  → SLICE_SCOPING → SLICE_IMPLEMENT ⇄ SLICE_VERIFY → FEATURE_DONE
                          (3 fails → STUCK)
```

DISCOVERY runs only for Medium/Large (read-only scan first). STUCK bans new edits after repeated verify failure.

## Tools (MCP)

| Phase | Call |
|---|---|
| NO_FEATURE | `submit_feature(project_root, desc)` → guesses scale, returns repo-map |
| DISCOVERY / SLICING | `propose_slices(project_root, slices, discovery_summary?)` |
| SLICE_SCOPING | `get_slice_context(project_root, module)` — first action of a slice |
| SLICE_IMPLEMENT | edit within the module; `expand_symbol(name)` for a dep body |
| SLICE_VERIFY | `run_verify(project_root, feature?)`; `analyze_verify_failure(...)` on fail |
| any | `track_manual_checks(...)` for ui-heavy checks |

Each slice in `propose_slices` needs: `title` (a user-visible behavior, not a layer noun), `module`, `verify_how`, `ac_count` (3–7).

## Rules

- **Human owns approval and scale.** After `propose_slices`, stop. The human runs `harness approve [--scale S] [--risky]` out-of-band. You cannot self-approve.
- **One slice = one session.** Call `get_slice_context` first; work only inside the returned context.
- **Bounded reads.** For a dependency body, call `expand_symbol` — do not read the whole file. Strict mode (Micro/Small/risky) denies out-of-module reads.
- **Edits stay in the module**, every scale. Out-of-module change needs human approval.
- **Verify before done.** `run_verify` must pass. On Medium+/risky the last slice also needs `harness explain <id>` (a short human explanation).
- **On STUCK:** stop editing. Diagnose, then ask the human to run `harness unstick`.

## Human commands (out-of-band, terminal only)

`harness approve` · `harness explain <id>` · `harness unstick` · `harness reslice` · `harness status` (read-only).

See `references/` for slicing guidance and the context-scoping model.

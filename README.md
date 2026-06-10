# slicefsm

A deterministic slice harness for AI coding agents. It turns the workflow into a
finite state machine. Hooks gate which tools run in each state and inject only
the current state's prompt. The result: the AI cannot skip steps, cannot edit
outside the current slice, and cannot read the whole codebase — and each session
loads far fewer tokens.

## Why

Typical AI agents read too many files, change too much at once, push past
unapproved directions, and call it done without tests. slicefsm bounds all four:

- **Vertical slices.** A feature is split into small, user-visible slices. The
  human approves them before any code is written.
- **Bounded context.** `get_slice_context` serves the slice's own module in full,
  dependencies as signatures only, and siblings as names. Dependency bodies come
  one at a time via `expand_symbol`.
- **Hook-enforced FSM.** A PreToolUse hook denies tools that are illegal in the
  current state. This is a real block, not a prompt request.
- **Human owns approval and scale.** Approval is an out-of-band terminal command
  the AI cannot run.

See `DESIGN - Deterministic Slice Harness.md` for the full model.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/hionpu/slicefsm/master/install.sh | bash
```

This installs the MCP server, the skill, and — on hook-capable clients (Pi,
Claude Code, opencode) — the four hooks. Use `--cli pi` to target one client,
`--skill-only` / `--mcp-only` to limit scope.

Update: `.../slicefsm/master/update.sh`. Uninstall: `.../slicefsm/master/uninstall.sh`.

## State machine

A repo holds several features; one is active. The active feature's phase:

```
NO_FEATURE → [DISCOVERY] → SLICING → AWAITING_APPROVAL → IN_PROGRESS → FEATURE_DONE

IN_PROGRESS holds N slices, implemented one at a time (sequential):
  proposed → implement ⇄ (run_verify) → done
  implement → stuck (N fails) → (harness unstick) → implement
```

`harness pause` / `resume <id>` / `switch <id>` move between features (clean git tree required).

## Components

| Module | Role |
|---|---|
| `state.py` | the FSM + atomic state IO (`.harness/state.json`) |
| `policy.py` | scale triage, read-policy derivation, thresholds |
| `context_engine.py` + `backends/` | repo-map, 3-bucket slice context, expand (Python, C#, C++ via tree-sitter) |
| `ops.py` / `server.py` | the 8 MCP tools |
| `hook.py` | the `slicefsm-hook` dispatcher (4 events) |
| `cli.py` | the human-only `harness` CLI |

## Develop

```bash
pip install -e ".[dev]"
pytest
```

Apache-2.0.

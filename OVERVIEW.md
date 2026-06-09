# How It Works, and How It Compares

> A reader's guide to slicefsm — what it does, how it does it, and how it stacks up against other AI-coding workflows, including its sibling `contractfirst`.

* * *

## At a glance

slicefsm adds three things to your project:

1. **A skill stub** (`.claude/skills/slicefsm/SKILL.md`) — one compact screen. It is a fallback, not the main driver. On hook-capable clients the real instructions are injected per turn (see below).
2. **An MCP server** (7 tools) — the AI calls these to move through the workflow: submit a feature, propose slices, load a slice's bounded context, expand one symbol, run verify, classify a failure, track manual checks.
3. **Four hooks** — a state machine the agent runtime enforces. The hooks gate which tools run in each state and inject only the current state's prompt.

You install with one curl line. A human approves slices and owns scale, out-of-band in the terminal. State lives in `.harness/state.json` under your project root.

## A note on what "enforcement" means here

This is where slicefsm differs from most frameworks, and from `contractfirst`. Three strengths, strongest first:

- **Hook block** (PreToolUse): the agent runtime asks the hook before each tool call. The hook reads the current state and returns `deny`. The tool **does not run**. This is a real block, not a request — and it covers the failures that matter most: editing outside the current slice, reading the whole codebase, starting work before the human approves.
- **Tool precondition** (MCP): each tool checks its phase. Call `get_slice_context` in the wrong state and it returns `transition_denied` and writes nothing. The FSM cannot be advanced out of order.
- **Skill prose** (SKILL.md): the fallback for hook-less clients. Same compliance assumption as any markdown rule.

`contractfirst` bet on **auditability**: its MCP tools return verdicts (`proceed: false`) the AI is *assumed* to honour, and the verdict is saved so a human can audit it later. slicefsm bets on **determinism**: the hook physically denies the illegal tool call in the moment, so there is usually nothing to audit after the fact. The bet is that *blocking the bad call beats logging it.*

One honest caveat up front: the hook gates known tools (edit, write, read, the MCP tools). A shell command can still write files the edit-gate would have stopped, because the hook cannot parse arbitrary bash. And hook-less clients (codex, gemini) get no block at all. See "What's NOT in scope."

* * *

## The problem

AI coding agents are willing but not bounded. The recurring failures:

- They read far more files than the task needs — burning tokens and context.
- They change a large surface at once, so the diff is hard to review.
- They push past the user's intent: "I'll just implement it this way" — no checkpoint.
- They report "done" without running the tests.
- The user only notices the drift later, reading the diff.

Most frameworks treat these as a discipline problem: write better markdown and hope. Models drift back to default the moment context gets long. slicefsm takes a different position: **bound the agent's freedom, in the runtime, so the bad move is not available.**

* * *

## The thesis: determinism and low tokens from one mechanism

The same hook that makes the workflow deterministic also makes it cheap.

- **Determinism.** Each turn, a UserPromptSubmit hook injects *only the current state's* ~15-line prompt. A PreToolUse hook denies tools that are illegal in that state. The AI cannot skip a step because the tool for the next step is blocked until the state is right.
- **Low tokens.** Because the prompt is per-state, there is no 200-line skill loaded every session. Because edits and reads are bounded to the current slice's module — with dependency bodies fetched one at a time via `expand_symbol` — the AI never loads the whole codebase.

The harness runs **many short sessions** (one slice, one session), so fixed per-session load matters more than usual. Measured: MCP tool definitions ~1,200 tokens, skill near-zero on the hook path, state injection ~1 line. That is roughly a third of a prose-heavy harness's per-session overhead — and the saving compounds across slices.

* * *

## The state machine

```
NO_FEATURE → [DISCOVERY] → SLICING → AWAITING_APPROVAL
  → SLICE_SCOPING → SLICE_IMPLEMENT ⇄ SLICE_VERIFY → FEATURE_DONE
                          (3 fails → STUCK)
```

- **DISCOVERY** runs only for Medium/Large features: a read-only scan to slice well before any edit. Micro/Small skip it.
- **AWAITING_APPROVAL** is the human gate. Only `harness approve` (a terminal command the AI cannot run) advances it.
- **SLICE_IMPLEMENT ⇄ SLICE_VERIFY** is the work loop. Edits stay inside the slice's module.
- **STUCK** triggers after repeated verify failures. It bans new edits — the AI must diagnose and ask the human to `harness unstick`, instead of layering bad patches.

Scale is set in three steps so no single actor owns it silently: the AI *guesses* it from the feature text, the harness *measures* it from the real slice signals at proposal time, and the human *confirms* it at approval. Scale then derives read-strictness, whether DISCOVERY runs, and how much explanation the last slice needs.

* * *

## What the harness actually does

### The hooks (the enforcement layer)

One dispatcher, `python -m slicefsm.hook <event>`, reads the current state and answers four events:

| Event | Role |
|---|---|
| UserPromptSubmit | inject only the current state's prompt (determinism + token win) |
| PreToolUse | deny tools illegal in the current state (the hard block) |
| PostToolUse | log AI edits for authorship telemetry (surfaced, not gated) |
| Stop | soft reminder only — best-effort, no hard gate hangs on it |

The PreToolUse matrix is scale-aware. **Edits are strict at every scale** — outside the slice's module, denied. **Reads are tiered**: strict (Micro/Small/risky) denies out-of-module reads and forces `expand_symbol`; relaxed (Medium/Large) allows logged reads but keeps edits strict. Edit-safety and token-cost are separate dials.

### The MCP server (7 tools)

| Tool | Phase | What it does |
|---|---|---|
| `submit_feature` | NO_FEATURE | record the feature, guess scale, return a repo-map (names only, no bodies) |
| `propose_slices` | DISCOVERY / SLICING | validate slices, measure scale, flag layer-noun titles, stage for human approval |
| `get_slice_context` | SLICE_SCOPING | serve own-module full text + dependency signatures + sibling names; write a git rollback checkpoint |
| `expand_symbol` | SLICE_IMPLEMENT | reveal one dependency body via its stored line range (logged) |
| `run_verify` | IMPLEMENT / VERIFY | run the suite; on pass advance or finish; on repeated fail go STUCK |
| `analyze_verify_failure` | VERIFY / STUCK | classify contract-sensitive vs routine; block a blind patch on the former |
| `track_manual_checks` | implement+ | per-feature manual-check ledger; blocks "done" while required items pend |

Every transition appends one line to `.harness/gates.jsonl`.

### The human's out-of-band commands

`harness approve [--scale S] [--risky]` · `harness explain <id>` · `harness unstick` · `harness reslice` · `harness status` (read-only). The first four change state and require an interactive terminal confirmation, so a non-interactive tool call fails closed. The AI cannot self-approve: the PreToolUse hook also denies these as Bash calls.

* * *

## A worked session

```
User: "Add a viewport memo: press M to open a note, it saves, reopens on next run."

[submit_feature] → guesses Medium (persistence + UI) → phase DISCOVERY.

AI scans read-only, then:
[propose_slices, discovery_summary=...] with 3 slices:
  1. Press M opens an empty memo panel; M again does not duplicate it.
  2. Typing persists to local store.
  3. On startup the saved note reloads.
  → measured scale Medium; phase AWAITING_APPROVAL.
  → "Human runs: harness approve"

[The AI stops. It cannot proceed.]

Human (terminal): harness approve
  → scale Medium, read_policy relaxed, phase SLICE_SCOPING (slice 1).

--- fresh session, slice 1 ---
[get_slice_context module=src/memo/ui] → own module full, deps as signatures,
   siblings as names; git checkpoint written. Phase SLICE_IMPLEMENT.

AI edits only within src/memo/ui. It needs the Store.save body:
[expand_symbol "Store.save"] → one method, not the whole file.

[run_verify] → pass → phase SLICE_SCOPING (slice 2). Session ends.

--- slice 3 (last) ---
[run_verify] → pass, but Medium ⇒ explanation gate:
  overall: pending_explanation → "harness explain 3"

Human: harness explain 3  (one line on what the persistence path does)
[run_verify] → pass → FEATURE_DONE.
```

If the AI tries to edit `src/render/` mid-slice, PreToolUse denies it: "outside the slice module." If verify fails three times, the state goes STUCK and further edits are blocked until the human looks.

* * *

## Comparison with other approaches

### vs. its sibling, `contractfirst`

Same author, same problem space, **opposite bet.**

| | contractfirst | slicefsm |
|---|---|---|
| **Core bet** | Auditability — MCP returns verdicts the AI is assumed to honour, saved for later audit | Determinism — the hook denies the illegal tool call in the moment |
| **Enforcement strength** | Structured verdict + OS lock on contract files | PreToolUse hard block + tool preconditions |
| **Token posture** | Standard: skill loaded each session | Aggressive: per-state injection, bounded slice context, ~1/3 the per-session load |
| **Unit of work** | A contract (spec/invariant/interface/test) | A vertical slice, one per session |
| **Human gate** | Reviews contract files, `chmod 444` | Approves slices out-of-band; cannot be self-approved |
| **Requires** | MCP-capable client | Hook-capable client for real enforcement (Pi / Claude / opencode) |
| **Weak point** | A non-compliant agent can ignore a verdict | A shell can bypass the edit-gate; hook-less clients get no block |

Use contractfirst when you want a rich contract model and an audit trail and your client only does MCP. Use slicefsm when you want the workflow physically gated, minimal tokens, and many small slices — and you run a hook-capable client.

### vs. a bare session / plain CLAUDE.md rules

Markdown rules get followed when convenient and dropped under context pressure. "Only edit these files" and "always run the tests" have a high failure rate over a long session because nothing stops the violation. slicefsm stops it: the edit outside the slice does not run, and "done" without a green `run_verify` cannot advance the state.

### vs. [obra/superpowers](https://github.com/obra/superpowers)

A broad, mature, prose-mandatory workflow framework. superpowers choreographs a full SDLC (brainstorm → plan → TDD → subagent dispatch → review) across many CLIs, with no programmatic gate. slicefsm is narrower and does one thing superpowers does not: it makes the gates *binding* via hooks, and it minimizes tokens by design. superpowers is ahead on workflow breadth and TDD depth; slicefsm is ahead on "will the AI actually stay inside the lines."

The two are not exclusive — the MCP tools and hooks are independent of any skill framework.

* * *

## When to use slicefsm

Strong fit:

- You run a hook-capable client (Pi, Claude Code, opencode) — Pi especially.
- You add small features to an existing codebase and want each change bounded and reviewable.
- You've watched an AI read 40 files, rewrite half a module, and call it done. You want that physically prevented.
- You care about token cost across many short sessions.

Weak fit:

- A first-pass prototype where you don't yet know the design — the FSM's structure is friction before you know the shape (use DISCOVERY, or start looser).
- A wide refactor or a render-bug hunt where the cause is in an unknown file — strict slicing fights you.
- A hook-less client (codex, gemini) — you get MCP + skill only, no hard enforcement.

* * *

## What's NOT in scope

Honest limits:

- **Shell bypass.** The edit-gate blocks the edit/write tools. A `bash` command can still write a file. The hook cannot parse arbitrary shell, so this is an acknowledged hole, not a guarantee.
- **Hook-less clients.** codex/gemini have no hook system. There, slicefsm degrades to MCP + skill with no hard block.
- **Slice quality.** The harness flags a layer-noun title and requires `verify_how`, but it cannot decide that a slice is genuinely a user-visible behavior. The human approval is the real check.
- **Understanding.** Authorship telemetry and the explanation gate measure and prompt, but cannot make the human understand. Will is not replaceable.
- **`CONTRACT:` comments and intent** can be deleted silently; only executable checks truly hold.

These are deliberate. The harness gates the failures that are common, high-cost, and cheaply blockable in the runtime. The rest stays in the skill prose where it belongs.

* * *

## Further reading

- [`README.md`](./README.md) — install and component reference.
- [`skill/SKILL.md`](./skill/SKILL.md) — the fallback behaviour spec.
- [`skill/references/`](./skill/references/) — slicing guide, context-scoping model.
- `DESIGN - Deterministic Slice Harness.md` — the full design, FSM tables, and token budget.

---
created: 2026-06-09
status: design
tags:
  - harness
  - fsm
  - design
---

# DESIGN — Deterministic Slice Harness

> Working name: **slicefsm** (final name TBD). A from-scratch harness that keeps the V5 philosophy's *enforceable* core (human-owned contract, bounded change, evolving verification, vertical slicing) and adds a new pillar: **a hook-enforced finite state machine** that makes the workflow deterministic and **minimizes tokens** by injecting only the current state's prompt and the current slice's bounded context.

This supersedes the prose-only `contractfirst` skill. It does **not** reuse `contractfirst`'s code (the existing `context.py` was never exercised). Philosophy is inherited; code is new.

---

## 0. The thesis

`contractfirst` bet on **auditability** (MCP returns verdicts; the AI is *assumed* to honor them). This harness bets on **determinism**: hooks *physically gate* what tools run in each state, and inject *only* the current state's instructions. Determinism and token-minimization come from the **same mechanism** — the hook never loads the whole SKILL.md or the whole codebase; it serves exactly the current state's mini-prompt and the current slice's bounded context.

Two token sources, both bounded:
1. **Workflow context** — each turn, inject only the current state's ~15-line prompt, not a 200-line skill. The skill body stays near-empty on purpose. Workflow rules live in per-state injection, not in an always-loaded file.
2. **Implementation context** — per slice, `get_slice_context` serves own-module-full + deps-signature-only. Outside that set, dependency bodies come through the logged `expand_symbol` call. Read-strictness is **tiered to scale** (§4.2): Micro/Small deny outside reads; Medium/Large allow logged reads but keep edits strict.

The harness runs **many short sessions** — one slice, one session. So fixed per-session load (skill + MCP tool defs) is paid again every session, not amortized over one long run. Cutting it is a first-order goal. See §10.

---

## 1. What is kept / dropped / replaced (from V5)

| V5 element | Status here | Rationale |
|---|---|---|
| Human owns contract (spec/invariant/interface/test) | **Kept, relocated** | "Three homes": auto-verifiable → test/assert; UI/UX → `track_manual_checks`; rationale → `CONTRACT:` comment. Enforcement (`run_verify` re-runs) intact; per-feature doc-writing ceremony dropped. |
| Vertical slicing | **Kept, enforced** | Was prose (S0–S3); now an FSM phase with a human approval gate. |
| Evolving verification (manual→auto) | **Kept** | `run_verify` + `track_manual_checks` ledger. |
| TDD as modularity forcing-function | **Dropped as ceremony; replaced** | Modularity pressure now comes from `get_slice_context`'s **cost signal** (coupling = token bloat + per-session declare friction) and per-slice `verify_how`. Weaker than TDD's hard wall, but always-on and quantified. |
| Human hand-codes interface/tests (muscle preservation) | **Upgraded from honor-system to measure+gate** | H2 → measurable authorship ratio; H3 → out-of-band explanation gate (non-fakeable). Cannot *compel* understanding — honest limit. |

---

## 2. The state machine

Flat states + a `current_slice` pointer. Transitions are written **only** by MCP tools and the out-of-band `harness` CLI. Hooks are **read-only** enforcers.

```
NO_FEATURE ─┬─(Micro/Small)──────────────→ SLICING ─┐
            └─(Medium/Large)─→ DISCOVERY ─→ SLICING ─┤
                                                     ▼
                                          AWAITING_APPROVAL
                                                     │  (human approve, out-of-band)
        ┌────────────────────────────────────────────┘
        ▼
   ┌─ SLICE_SCOPING → SLICE_IMPLEMENT ⇄ SLICE_VERIFY ─┐   (per current_slice)
   │                        ▲              │           │
   │                        │   3 fails    ▼           │
   │                      STUCK ◄──────────┘           │
   │                   (human unstick)                 │
   └──────── more slices: SCOPING(k+1) ◄──────────────┘
        │ last slice verify pass
        ▼
   FEATURE_DONE
```

DISCOVERY is a read-only scan state. It runs only for Medium/Large, before slicing. It builds a short Discovery Summary that drives better slices. Micro/Small skip it. STUCK is a stop-and-ask state. It triggers after repeated verify failures and bans new edits.

### Transition table (who triggers, precondition → postcondition)

| From | To | Trigger | By | Precondition |
|---|---|---|---|---|
| NO_FEATURE | SLICING | `submit_feature(desc)`, provisional scale ∈ {Micro,Small} | AI | phase==NO_FEATURE |
| NO_FEATURE | DISCOVERY | `submit_feature(desc)`, provisional scale ∈ {Medium,Large} | AI | phase==NO_FEATURE |
| DISCOVERY | AWAITING_APPROVAL | `propose_slices(slices[], discovery_summary)` | AI | phase==DISCOVERY, `discovery_summary` present |
| SLICING | AWAITING_APPROVAL | `propose_slices(slices[])` | AI | phase==SLICING, every slice has `module`+`verify_how`, `ac_count`∈[3,7] |
| AWAITING_APPROVAL | SLICING\|DISCOVERY | `propose_slices` (revise) | AI | phase==AWAITING_APPROVAL |
| **AWAITING_APPROVAL** | **SLICE_SCOPING(1)** | **`harness approve [--scale S] [--risky]`** | **Human (out-of-band, interactive)** | phase==AWAITING_APPROVAL |
| SLICE_SCOPING(k) | SLICE_IMPLEMENT(k) | `get_slice_context(module_k)` (also writes checkpoint) | AI | phase==SLICE_SCOPING |
| SLICE_IMPLEMENT(k) | SLICE_VERIFY(k) | `run_verify` | AI | phase==SLICE_IMPLEMENT |
| SLICE_VERIFY(k) | SLICE_SCOPING(k+1) | `run_verify` pass & k<N | AI | phase==SLICE_VERIFY |
| SLICE_VERIFY(k) | FEATURE_DONE | `run_verify` pass & k==N & (H3 done if Medium+) | AI | phase==SLICE_VERIFY |
| SLICE_VERIFY(k) | SLICE_IMPLEMENT(k) | `run_verify` fail & `verify_fail_count` < threshold | AI | phase==SLICE_VERIFY |
| SLICE_VERIFY(k) | STUCK | `run_verify` fail & `verify_fail_count` ≥ threshold | AI | phase==SLICE_VERIFY |
| STUCK | SLICE_IMPLEMENT(k) | `harness unstick` (resets fail count) | Human (out-of-band) | phase==STUCK |
| STUCK | SLICING\|DISCOVERY | `harness reslice` | Human (out-of-band) | phase==STUCK |
| any slice phase | SLICING\|DISCOVERY | `harness reslice` | Human (out-of-band) | — (re-slice escape) |

Fail threshold = 3 by default, 2 when `risky`. Any tool whose precondition fails returns `{"error":"transition_denied","current_phase":X,"expected_phase":Y}` and writes nothing.

---

## 3. `.harness/state.json` (the single source of truth)

```json
{
  "version": 1,
  "phase": "SLICE_IMPLEMENT",
  "feature": { "id": "feat-minigame-20260609T1530", "desc": "<verbatim user request>", "submitted_at": "ISO8601" },

  "scale": "large",
  "scale_source": "human_approved",
  "scale_provisional": { "by": "ai", "value": "medium", "at": "submit_feature" },
  "scale_measured": {
    "by": "harness", "value": "large", "at": "propose_slices",
    "signals": { "actual_slices": 7, "modules_resolved": 5, "touches_persistence": true, "touches_ui": true, "crosses_module_boundary": true }
  },
  "risky": false,
  "read_policy": { "mode": "relaxed", "derived_from": { "scale": "large", "risky": false } },

  "discovery_summary": ".harness/discovery-feat-minigame.md",

  "slices": [
    {
      "id": 1,
      "title": "E 누르면 빈 UI 열림/닫힘",
      "module": "src/minigame/ui",
      "verify_how": "playtest: E 3회 → UI 1개 유지",
      "ac_count": 4,
      "status": "done",
      "manifest": ".harness/slice-context-ui-20260609T1601.json",
      "checkpoint_ref": "a1b2c3d",
      "verify_fail_count": 0,
      "explanation": ".harness/explain-slice-1.md",
      "authorship": { "ai_edits": 12, "human_edits": 3 }
    },
    { "id": 2, "title": "...", "module": "src/minigame/logic", "verify_how": "...", "ac_count": 5, "status": "implement", "checkpoint_ref": "d4e5f6a", "verify_fail_count": 1 }
  ],
  "current_slice": 2,
  "approved": { "at": "ISO8601", "by": "human", "note": "boundaries ok" },
  "updated_at": "ISO8601"
}
```

**Scale lifecycle — set once, but in three steps.** Scale drives read_policy, discovery, and explanation depth, so its source must be auditable.
- `scale_provisional` — AI guesses scale from the feature text at `submit_feature`. This is an **AI claim**, not a measurement. It only decides whether DISCOVERY runs.
- `scale_measured` — at `propose_slices`, the harness now has real signals (slice count, modules resolve, paths touch ui/persistence). It recomputes scale. A mismatch with `scale_provisional` is shown on the approval screen.
- `scale` + `scale_source` — the human sets the final value at approval. `harness approve --scale large` overrides; default accepts `scale_measured`.

**read_policy** is derived, not free: Micro/Small → `strict`; Medium/Large → `relaxed`; `risky:true` forces `strict`. `derived_from` records why, for later audit.

- **Writers**: MCP tools (transitions) and the `harness` CLI (approve / explain / reslice / unstick). Nothing else.
- **Readers**: the four hooks.
- Every transition appends one line to `.harness/gates.jsonl` (audit, reused concept from `contractfirst`'s gatelog).
- Per-slice bounded-context manifests, explanation files, and the AI-edit log all live under `.harness/`.

---

## 4. Hooks (Claude-style; runs on Claude Code natively, Pi via `@hsingjui/pi-hooks`, opencode native)

All hooks call one dispatcher, `harness-hook <event>`, which reads the event JSON on stdin, reads `.harness/state.json`, and emits Claude-style JSON. **Tool names are lowercased before matching** (Pi emits `edit`/`write`/`bash`/`mcp__*`; Claude emits `Edit`/`Write`).

`settings.json` (the installer writes this; `python -m` form avoids PATH issues):
```json
{
  "hooks": {
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "python -m slicefsm.hook userpromptsubmit" }] }],
    "PreToolUse":       [{ "matcher": ".*", "hooks": [{ "type": "command", "command": "python -m slicefsm.hook pretooluse" }] }],
    "PostToolUse":      [{ "matcher": "edit|write|Edit|Write|MultiEdit|NotebookEdit", "hooks": [{ "type": "command", "command": "python -m slicefsm.hook posttooluse" }] }],
    "Stop":             [{ "hooks": [{ "type": "command", "command": "python -m slicefsm.hook stop" }] }]
  }
}
```

### 4.1 UserPromptSubmit — inject only the current state's prompt  (token win + determinism)
Emits `hookSpecificOutput.additionalContext` = the current phase's mini-prompt + a one-line current-slice pointer. The AI never sees the other states. (SessionStart-injection also works via the same path — verified in `pi-hooks` `hook-context.ts:74-101` — but we drive injection from UserPromptSubmit so it re-arms every turn and survives best-effort SessionStart edge cases.)

Per-phase mini-prompt (illustrative, SLICE_IMPLEMENT):
```
You are in SLICE_IMPLEMENT for slice 2/3 "미니게임 로직" (module: src/minigame/logic).
Allowed: edit/write within the loaded module, run tests, expand_symbol(<dep>) for a dep body.
The bounded context was served by get_slice_context — it is already in your context. Do not re-read it.
To touch anything in `excluded` or read a file outside the module: STOP and declare (symbol + why).
When the slice's verify_how is satisfiable, call run_verify.
```

### 4.2 PreToolUse — deny tools illegal in the current state (the hard block)
Returns `permissionDecision: "deny"` with a reason when the tool is not allowed in `phase`. Allowed-set per phase:

| Phase | Allow | Deny |
|---|---|---|
| NO_FEATURE | read, `mcp__*submit_feature` | edit, write, get_slice_context, run_verify |
| DISCOVERY | read (any file, read-only scan), repo-map, `mcp__*propose_slices` | **edit, write**, get_slice_context, run_verify |
| SLICING | read(repo-map only), `mcp__*propose_slices` | **edit, write**, get_slice_context, run_verify |
| AWAITING_APPROVAL | read, `mcp__*propose_slices` | all code tools; **Bash(`harness approve*`/`harness explain*`/`harness unstick*`)** |
| SLICE_SCOPING | read, `mcp__*get_slice_context` | **edit, write** (until context loaded) |
| SLICE_IMPLEMENT | edit/write **within `manifest.module_files`**, bash(tests), `mcp__*expand_symbol`, read (per read_policy) | edit/write outside the set; read outside set **denied if strict** |
| SLICE_VERIFY | `mcp__*run_verify`, `mcp__*analyze_verify_failure`, targeted edit within module, read (per read_policy) | new-feature edits, out-of-module edits |
| STUCK | read within manifest, `mcp__*analyze_verify_failure` | **edit, write, new patch** (stop and ask human) |
| FEATURE_DONE | read | edit, write (feature closed) |

**Read-strictness is tiered by `read_policy` (derived from scale + risky), not a global flag.** Two reasons split here: edit-safety vs token cost.
- **Edit is always strict, every scale.** In SLICE_IMPLEMENT/SLICE_VERIFY, edit/write outside `manifest.module_files` is denied. Out-of-set change always needs human approval. This is the safety wall.
- **Read strictness depends on scale.** This is the token wall, and it is separate.
  - **strict** (Micro/Small, or any `risky`): deny `Read` outside the set. Dependency bodies must go through `expand_symbol` (one symbol, logged); `excluded` needs a declared boundary-cross. Context is tiny here, so this costs almost no friction. This makes "the AI cannot silently read the whole codebase" *true*.
  - **relaxed** (Medium/Large): allow `Read` outside the set, but log it and surface it. Heavy out-of-set reading is shown as a *slicing-too-coarse* signal. Edits stay strict. This trades a little token leak for real exploration room in coupled legacy code.

So: relaxed-read is **not** the global default. It turns on only for Medium/Large, where DISCOVERY and wide coupling justify it. Small clean work keeps the hard token wall.

**Self-approve hole, closed:** `harness approve` / `harness explain` are shell commands, so the AI could in principle run them via Bash. PreToolUse denies `Bash(harness approve*)` and `Bash(harness explain*)` in all phases, **and** the CLI itself requires an interactive confirmation on the controlling terminal (`/dev/tty`), which a non-interactive tool call cannot satisfy. Two independent layers.

### 4.3 PostToolUse — authorship telemetry (H2 measure)
On every `edit`/`write` tool success, append `{slice_id, path, tool, ts}` to `.harness/edits.log`. At slice close, `run_verify` computes `authorship = git-diff-stat(slice changes) − logged AI edits` ⇒ the human-authored portion. Surfaced, not gated.

### 4.4 Stop — soft only (kept minimal by design)
Stop in `pi-hooks` is best-effort (verified: `stop-hooks.ts` uses `sendMessage(triggerTurn:true)`). **No hard gate hangs on it.** It emits at most a soft reminder ("slice 2 mid-implement; state saved; resume next session") and never blocks normal multi-session pauses. Must check `stop_hook_active` to avoid the documented infinite-loop. All real gates live in PreToolUse + tool preconditions, which do not depend on Stop.

---

## 5. MCP tool set

Each tool validates its phase precondition, performs its action, writes the state transition + a `gates.jsonl` line, and returns a structured result.

| Tool | Phase in→out | Action |
|---|---|---|
| `submit_feature(project_root, desc)` | NO_FEATURE → SLICING \| DISCOVERY | Record feature. Guess **provisional scale** from `desc` (lightweight heuristic, AI-asserted — see §3). Route: Micro/Small → SLICING; Medium/Large → DISCOVERY. Return a **lightweight repo-map** (top-level packages + public symbol *names*, no bodies — context engine in "map mode") so the next state needs zero file reads. |
| `propose_slices(project_root, slices[], discovery_summary=null)` | SLICING\|DISCOVERY\|AWAITING_APPROVAL → AWAITING_APPROVAL | Validate each slice: `module` resolves, `verify_how` present, `ac_count`∈[3,7]. **Slice-smell check**: a title that is only a layer noun (ViewModel/Service/XAML/Controller) is flagged, not blocked — the human still decides. From DISCOVERY, `discovery_summary` is required (and required to be non-empty for Large). Compute **`scale_measured`** from real signals (slice count, modules resolved, paths touched); flag any mismatch with `scale_provisional`. Persist proposal. Return formatted proposal + scale + derived read_policy for human review. **Does not advance to scoping** — only the human can. |
| `get_slice_context(project_root, module, depth=1, feature=null)` | SLICE_SCOPING → SLICE_IMPLEMENT | 3-bucket assembly (own=full, deps=signature-only, excluded=names). Write manifest, set `slice.manifest`. **Write a rollback checkpoint**: `git stash create` → store the ref in `slice.checkpoint_ref` (non-destructive; does not touch the working tree or stash stack). If the tree is dirty, surface that state first. If not a git repo, skip + warn. Surface `pending_manual_checks` if feature maps to a ledger. |
| `expand_symbol(project_root, name, source_path=null)` | SLICE_IMPLEMENT (in-state) | Return **one** symbol's body via its `range` (O(1); requires preserving `range` on dep records). Log `{slice_id, symbol, reason, ts}`. Frequent calls per slice = boundary/contract smell → surfaced as a re-slice signal. |
| `run_verify(project_root, feature=null)` | SLICE_IMPLEMENT\|SLICE_VERIFY → SLICE_VERIFY / advance / STUCK | Run `verify.sh` or language defaults + manual-check ledger. On pass: compute authorship, advance `current_slice` (→ SCOPING(k+1) or FEATURE_DONE). On fail: increment `slice.verify_fail_count`; at threshold (3, or 2 if `risky`) move to STUCK. Closing the last slice on Medium+ requires `slice.explanation` to exist (H3 gate) else returns `overall: pending_explanation`. |
| `analyze_verify_failure(project_root, ...)` | SLICE_VERIFY\|STUCK | Classify contract-sensitive vs routine; `patch_allowed:false` for contract-sensitive (carried over from contractfirst). |
| `track_manual_checks(project_root, op, ...)` | SLICE_IMPLEMENT+ | Per-feature manual-check ledger (declare/confirm/handoff). |

---

## 6. Out-of-band `harness` CLI (human-only, non-fakeable)

A small console script (shipped with the MCP server package). Run by the human in the terminal or via the in-session `!` prefix. **Never callable by the AI** (PreToolUse denies the Bash forms; commands require interactive `/dev/tty` confirmation).

| Command | Effect |
|---|---|
| `harness approve [--scale S] [--risky] [--note "..."]` | phase AWAITING_APPROVAL → SLICE_SCOPING(1); write `approved`, final `scale` (default = `scale_measured`; `--scale` overrides), `risky`, derived `read_policy`. Interactive y/N. The approved slice list + scale become a **human-owned artifact** (V5: "human owns contract"). |
| `harness explain <slice_id> [--file P]` | Capture the human's core-logic / root-cause explanation → `.harness/explain-slice-N.md`. Satisfies the H3 gate that `run_verify` checks before FEATURE_DONE. Interactive (opens `$EDITOR` or reads tty). |
| `harness unstick [--note]` | phase STUCK → SLICE_IMPLEMENT(k); reset `verify_fail_count`. The human has looked and wants the AI to try again. Interactive. |
| `harness status` | Print current phase, slice, scale, read_policy, authorship ratios. Read-only; AI may run this. |
| `harness reslice [--note]` | phase → SLICING\|DISCOVERY (re-slice mid-feature). Interactive. |

---

## 7. Session lifecycle (planning 1 + per-slice 1)

1. **Planning session**: `NO_FEATURE → SLICING → AWAITING_APPROVAL`. After `propose_slices`, Stop is permissive (gate = await human). Session closes.
2. **Human approves out-of-band** (`! harness approve`) — no session needed.
3. **Slice session k** (fresh): UserPromptSubmit injects "slice k, SCOPING, module=X, first action get_slice_context" → `SCOPING → IMPLEMENT → VERIFY`. On verify pass, state advances to k+1 but the **session ends**. Multi-session resume: an unfinished slice persists; the next session resumes the same k.

---

## 8. Honest limits (carried forward explicitly)

- **Muscle preservation** is measured (H2) and gated on explanation (H3), but the harness cannot force the human to *understand*. Will is not replaceable.
- **Modularity pressure** is softer than TDD's hard wall — a continuous token/friction tax, not an impossibility.
- **Stop** is best-effort; we depend on it for nothing hard.
- **Strict-mode Read denial** can be wrong (a legitimately needed file outside the module). It runs only for Micro/Small/risky, where context is small. The `expand_symbol` + declare path is the escape; if it fires a lot, the *slicing* was wrong — surfaced, not silently absorbed.
- **`scale_provisional` is an AI guess, not a measurement.** Real signals exist only after slicing. The harness recomputes scale at `propose_slices` and the human owns the final value, so a bad guess only changes whether DISCOVERY runs — it never silently sets policy.
- **`CONTRACT:` comments** can be deleted silently; only executable checks (test/assert/arch-test) truly hold. Comments carry rationale only.
- **Authorship telemetry** is a learning/muscle signal, not a quality score. High AI-edit ratio can still be good code. Surfaced, never gated on.

---

## 9. Build order (vertical slices of the harness itself)

1. **State core**: `.harness/state.json` schema + a `state.py` (read/validate/transition/append-gate, scale lifecycle, read_policy derivation, fail-count/STUCK logic). Pure, unit-testable.
2. **Context engine (map + slice + expand)**: tree-sitter-based backend so it is language-agnostic from day one (Python `ast` as the first concrete backend behind the same interface). Produces repo-map, 3-bucket slice context, single-symbol expand. Preserve `range` on dep records (for `expand_symbol`).
3. **MCP tools**: register the seven tools, each wired to `state.py` transitions.
4. **`harness-hook` dispatcher** + the four hook outputs; the scale-tiered PreToolUse read/edit gate; checkpoint write on scope→implement.
5. **`harness` CLI**: approve / explain / unstick / status / reslice, interactive + tty-gated.
6. **Dogfood**: run the harness on its own next feature.

**Resolved decisions:** read-strictness is **scale-tiered**, not a single default (Micro/Small strict, Medium/Large relaxed-read + strict-edit, risky forces strict). Name stays **slicefsm** for now — folder is `specdriven`; pick the final name before the MCP package is published (low cost to defer).

---

## 10. Token budget (per session)

The harness runs many short sessions. Fixed per-session load is paid every time, so we keep it small. Measured baseline from the old `contractfirst` assets:

| Item | contractfirst | slicefsm target |
|---|---|---|
| Skill body | ~2,600 tok (full SKILL.md loads on invoke) | **~150 tok** stub, or 0 |
| MCP tool defs | ~2,400 tok (8 verbose tools, all load at connect) | **~1,200 tok** (7 terse tools) |
| First state injection | — | **~250 tok** |
| **Fixed overhead / session** | **~5,000 tok** | **~1,600 tok** |
| `get_slice_context` output | bounded work payload | same (already bounded) |

The three costs behave differently:
- **MCP tool defs** load once at connect, for *all* tools, no matter the state. MCP has no per-state tool gating, so we cannot shrink this by state. Levers: keep 7 tools, keep each docstring ≤3 lines, push detail into runtime output, not the schema. Target ~1,200 tok.
- **Skill body** can be near-zero. The FSM injects the current state's rules each turn, so no big always-loaded workflow file is needed. The skill is a ~150-tok stub: "this project uses slicefsm; follow the injected state prompt." Or nothing at all.
- **Per-state injection** is small but per-turn, so it adds up over a session. Keep each state prompt ≤250 tok.

This is the same mechanism as determinism: the hook serves only the current state's prompt. That makes the workflow deterministic *and* cuts the skill to near-zero. One lever, both wins.

**Limit (honest):** a hook-less client (plain codex, no hook support) gets no per-state injection and no skill, so it sees no workflow at all. Such clients need the full SKILL.md as a fallback, which restores the ~2,600-tok cost. The user runs Pi (hook-capable), so we optimize the lean path and note this fallback in §8.

---

## 11. Scale + explanation reference

**Scale signals** (heuristic, not deterministic — see §3 lifecycle):

| Scale | Slices | Files | New interface | New state | Discovery | read_policy |
|---|---|---|---|---|---|---|
| Micro | 1 | 1–2 | none | none | skip | strict |
| Small | 1–2 | 2–4 | small/none | small | skip | strict |
| Medium | 3–6 | 5–10 | yes | yes | default on | relaxed (edit strict) |
| Large | 7+ | 10+ | several | yes (persist/API/schema) | on | relaxed (edit strict) |

`risky:true` forces `strict` read + lower fail threshold + required explanation, at any scale.

**Explanation depth (H3), tiered so it stays light:**
- Micro/Small: none.
- Medium: 3-line summary at the last slice.
- Large: key decision per slice.
- risky: root cause / invariant / rollback note, required before FEATURE_DONE.

---

## 12. Implementation status (built)

Package: `slicefsm/` (Python, deps: `mcp` only — Python `ast` backend, no native build). Build order slices 1–5 done and unit-tested (74 tests pass).

| Slice | Module | State |
|---|---|---|
| 1 State core | `state.py` (FSM + atomic IO), `policy.py` (scale/read_policy/thresholds), `gatelog.py` | done |
| 2 Context engine | `context_engine.py` + `backends/` (interface + Python `ast`; tree-sitter deferred behind same interface) | done |
| 3 MCP tools | `ops.py` (logic) + `server.py` (7 thin tools), helpers `verify.py` / `failure.py` / `manual_checks.py` / `git_util.py` / `edits.py` | done |
| 4 Hook dispatcher | `hook.py` — `python -m slicefsm.hook <event>`, pure `decide()` + IO | done |
| 5 Human CLI | `cli.py` — `harness` (approve/explain/unstick/reslice/status), tty-gated | done |
| 6 Dogfood | run on its own next feature | pending (next) |

Install/uninstall/update: `slicefsm/{install,uninstall,update}.sh`, one-liner via `raw.githubusercontent.com/.../slicefsm/install.sh`. Per-agent config: skill + MCP for all 5 CLIs; **hooks for Pi / Claude / opencode** (codex/gemini have no hook system → MCP+skill degraded, no hard enforcement). Hook merge/strip is idempotent and preserves foreign entries.

**Measured token budget (built):** MCP tool defs serialize to **~1,166 tokens** (target ~1,200). Skill is one compact screen (fallback for hook-less clients). Per-turn state injection ~1 line.

**Deltas from the design above:**
- Hook command is `python -m slicefsm.hook <event>`, not a `harness-hook` console script (PATH-safe, matches the MCP `python -m` form).
- DISCOVERY exit folds into `propose_slices(discovery_summary=...)` — no separate tool, to hold tool count at 7.
- Self-approve block also catches the `python -m slicefsm.cli <verb>` form and `reslice`, not just `harness approve`.
- Checkpoint writes on the `get_slice_context` (SCOPING→IMPLEMENT) call.

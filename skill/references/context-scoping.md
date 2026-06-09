# Context Scoping

## Purpose

`get_slice_context` limits what an implementation session loads. Instead of foraging the whole codebase, the session works within a bounded set computed from the slice's own module.

## When to call it

At the **entry to `[8] Implement`**, once per slice/session, before reading any code:

```
get_slice_context(project_root=<root>, module=<slice_module>)
```

`module` is the folder or file you are implementing — relative to `project_root`.

- **New module (empty):** pass the interface file (`IInventory.cs`, `store_types.py`) — it has the type references the implementation will need.
- **Existing module:** pass the module itself — import graph drives dep collection.
- **Unknown deps:** pass `explicit_deps=[...]` if the module has no imports yet.

Primary use: **Medium+** scale. Micro/Small context is trivially small — may skip.

## Three buckets

| Bucket | Content | Use |
|---|---|---|
| `module_files` | Full source text | Files you edit |
| `dependencies` | Public signatures + doc, bodies stripped | Files you call — read signatures, do not load full text |
| `excluded` | Sibling module names | Not loaded; declare before using |

## Context Boundary rule

Operate only within the loaded context. Before touching anything in `excluded` or outside the project:

1. **STOP**
2. State the symbol name and why it is needed
3. Wait for human acknowledgement
4. Then proceed

This is a soft guard — the tool does not block reads. The rule makes boundary-crossing a visible, declared decision instead of silent forage.

## Session discipline

- **One slice = one session.** Open a fresh session at `[8] Implement`.
- **Close after Verify Gate passes.** Do not carry context into the next slice.
- Cross-slice context accumulation is the primary cause of long-session compliance drift.

## Three homes — contract without separate spec/invariant docs

The four contract parts (spec, invariant, interface, test) do not all need separate documents. Place each piece of content in the home that enforces it:

| Content | Home | Enforced by |
|---|---|---|
| Auto-verifiable AC, state/safety invariants, boundary rules | tests / `assert` / architecture tests | `run_verify` re-runs on every change |
| Human-verifiable UI/UX (layout, focus, animation feel) | `track_manual_checks` items | `run_verify(feature=...)` re-flags as `pending_manual` after any change |
| Design intent / rationale ("why this choice") | Short `CONTRACT:` comment next to the code | Co-located so it is read; `get_slice_context` delivers it with the module |

**Spec mapping:** Acceptance Criteria → test names. Goal/Non-goals → docstring at top of test file. Non-testable UI/UX → `track_manual_checks` items.

**Invariant mapping:** state/safety → `assert` or property test at enforcement location. Boundary/architecture → architecture test. "Why" → `CONTRACT:` comment.

**What still warrants a written doc:** only cross-cutting, project-wide principles with no single module to live in (e.g. "all destructive actions use undo, not a confirm dialog"). One shared, slowly-changing doc — not a per-feature spec.

## Module granularity convention

- **Single file:** `src/inventory/store.py` — scopes to that file and its imports.
- **Package folder:** `src/inventory/` — collects all `.py` files in the folder, union of their imports as deps.
- Prefer the **smallest scope that covers the slice**. A folder module loads more context than a single file.

## depth parameter

Default `depth=1` (direct imports only). Increase only when the AI reports a needed symbol is missing from `dependencies`. `depth=2` follows one more hop but can significantly increase context size — use sparingly.

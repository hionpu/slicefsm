# Vertical Slicing Guide

## Core Rule

**Never split horizontally.** A horizontal slice ("DB first, then logic, then UI") delivers nothing until all layers are done. A vertical slice delivers end-to-end value immediately.

## Horizontal vs Vertical

❌ Horizontal (avoid):
```
Slice 1: All database models
Slice 2: All business logic
Slice 3: All UI
→ Nothing works until Slice 3 is done
```

✅ Vertical (correct):
```
Slice 1: Minimal end-to-end — one happy path works completely
Slice 2: Add the next most important behavior
Slice 3: Add edge cases and error handling
→ Each slice ships something usable
```

## Slice Sizing Criteria

Each slice should satisfy all three:

| Criterion | Target |
|-----------|--------|
| Delivery size | 4–8 hours of work |
| Acceptance Criteria | 3–7 verifiable AC |
| User value | Delivers standalone value the user can observe |

If a slice exceeds 8 hours, split it further. If it has fewer than 3 AC, it may be too small to warrant a full slice — fold into an adjacent slice.

## When to Slice

Slice **before** Scale Triage. Run S0–S3 always — even after scope has been narrowed.

| S | Question | Signal |
|---|---|---|
| S0 | More than one user-observable behavior end-to-end? | Slice |
| S1 | More than 7 AC, or more than one happy path? | Slice |
| S2 | Estimate > one working day? | Slice |
| S3 | Multiple systems that could be verified independently? | Slice |

## Scope Reduction ≠ Slicing

Dropping future features (e.g., "no upgrades, no enemies, just move and collect") narrows **what** gets built. It does not slice **what remains** into independently verifiable delivery units. After any scope reduction, re-run S0–S3 — the narrowed scope may still be a fat horizontal layer covering multiple systems at once.

## Slicing Examples

### Example: "Build a minigame lobby system"

❌ Horizontal split:
- Slice 1: All server-side data models
- Slice 2: All client-side UI components
- Slice 3: Wire everything together

✅ Vertical split:
- Slice 1: Player can open lobby UI from an interactable object (happy path only)
- Slice 2: Duplicate open is prevented; close cleans up state
- Slice 3: Lobby shows player count; updates in real time
- Slice 4: Player can join/leave; server tracks membership

### Example: "Add inventory system"

✅ Vertical split:
- Slice 1: Player can pick up one item type; item appears in inventory slot
- Slice 2: Player can drop items; slot becomes empty
- Slice 3: Stack limit enforced; overflow rejected
- Slice 4: Inventory persists across sessions

## Note on Scale vs Slice Size

- **Scale** is judged by risk and boundary complexity (Q0–Q3)
- **Slice size** is judged by delivery size (4–8 hours, 3–7 AC)

These are independent. A Small-scale slice can still be further split if it has too many AC. A Large-scale slice cannot be downgraded to Medium by splitting — the risk classification stays.

# tasks

**Parent context:** `../AGENTS.md`
**Generated:** 2026-11-24 · **Updated:** 2026-11-24

## Purpose

The implementation plan and task checklist for the aHoy MVP. They are derived from `../SPEC.md` v5.1 (hosted, GitHub sign-in, authz, sandbox), revised after an architect review and two critic passes. Downstream commands such as `/build` read `todo.md` as the task list.

## Key Files

| File | Description |
|------|-------------|
| `plan.md` | The plan itself: the installed toolchain, the architecture decisions (including the flat-vs-union Turn schema fallback), the dependency graph, an index of the 7 phases, what can run in parallel, the risks, and 5 open questions |
| `todo.md` | The 38 tasks (T1–T38, listed in build order) and checkpoints A–G. Each task lists its acceptance criteria, how to verify it, the files it touches, its dependencies and its size |

## For AI Agents

### Working In This Directory
- **Never overwrite or delete unchecked tasks.** They may be in progress in another session. To change the plan, edit it in place and note why.
- **Tick `- [ ]` → `- [x]` only after the task's Verify command passes.**
- **Checkpoints are human gates.** Stop at each one and report.
- **Record the outcome of the T2 spike in `plan.md`** → Architecture Decisions. Did Gemini accept the union schema, or did we fall back to the flat one?
- **Open questions in `plan.md`:** who writes the Python, the fixture repo, the dev GitHub App, the VM provider, Auto mode. Q2 must be answered **before T31**; Q1 and Q3 at Checkpoint A.
- **Every new route that takes a session/step id must be added to the authz sweep table** (`BT/test_authz_sweep.py`), or that test fails.
- **Keep task IDs stable** (T1…T38). Commits and later notes refer to them.

### Common Patterns
- Paths in `todo.md` use shorthand: `B` = `backend/src/ahoy`, `BT` = `backend/tests`, `F` = `frontend/src`.
- Sizes follow S (1–2 files) / M (3–5 files), counting hand-written files; tool-generated files and one-line wiring edits are named but not counted. Split a task that grows past 5.
- `FE` = `frontend/e2e`.

## Dependencies

### Internal
- `../SPEC.md`, which every task traces back to (user stories, success criteria 1–14)

## Manual Notes

Notes under this heading are written by humans and preserved on regeneration.

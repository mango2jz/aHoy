# aHoy

**Generated:** 2026-11-24 · **Updated:** 2026-11-24

## Purpose

aHoy is a steerable agent harness. The human is the "captain" and steers an LLM through a codebase by holding the mouse on graph nodes to open a radial steering wheel. Every explanation becomes a node in a branchable tree, laid out like the Obsidian graph view, and every tool call is shown as a dot on the route.

**Status: pre-code.** The spec is v5.1 (draft, waiting for approval): v4 was approved 2026-11-24, and v5 turns aHoy into a hosted multi-user web app with GitHub sign-in, authorization and GitHub repo access. The plan and task list in `tasks/` match v5.1 (T1–T38, checkpoints A–G), revised after an architect review and two critic passes. No implementation exists yet.

## Key Files

| File | Description |
|------|-------------|
| `SPEC.md` | The spec (v5.1 draft, waiting for approval). It is the source of truth for decisions D1–D17, the user stories, the architecture, authentication, authorization, GitHub integration, the sandbox, the data models, the API sketch, the system prompt draft, the boundaries and the 14 success criteria |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `tasks/` | The implementation plan and the 38-task checklist (see `tasks/AGENTS.md`) |

Planned, but not created yet (see `SPEC.md` → Project Structure):
- `backend/`: Python FastAPI app with the whole harness
- `frontend/`: React + TypeScript UI only
- `.env.example`

## For AI Agents

### Working In This Directory
- **Read `SPEC.md` before any change.** If a decision changes, update the spec first, then the code.
- **Pick work from `tasks/todo.md` in dependency order.** Check off acceptance criteria as you go. Stop at each **Checkpoint** for human review.
- **The Python owner is still undecided** (plan.md Open Question 1). The user is learning Python and may want to write the harness core themselves: T5 (context builder), T6 (step loop) and T12 (gate); plan.md recommends option (c): the user writes T5, T6 and T12, and Claude writes every other Python task. **Ask before implementing those tasks.**
- **Git repo on `main`, no commits yet.** Commit only when the user asks.

### Architecture Invariants (from SPEC.md, never break these)
- **All agent logic lives in the Python backend.** The frontend only renders and calls the API.
- **Pydantic models are the single source of truth.** TypeScript types are generated with `npm run gen:types` and never hand-written.
- **Every access to a session, its nodes, files or events goes through `authorize()`** (`authz.py`). Owner steers; viewers of a shared session only read; everyone must currently have GitHub read access to the repo (≤5 min cache). Unseeable sessions return 404.
- **Auth is GitHub App sign-in.** Opaque cookie (`__Host-ahoy_sid` in prod, `ahoy_sid` in dev/test), stored only as a hash. Mock sign-in (`/auth/mock/login`) exists only with `--github mock` in dev/test. State-changing requests need `Origin == APP_ORIGIN` and `X-Ahoy-CSRF: 1`.
- **The GitHub App has `Contents: read` + `Metadata: read` only.** aHoy never writes to GitHub. Sessions are pinned to a commit SHA.
- **Every shell command runs in the per-step sandbox container** via `ahoy-runner` (gVisor in prod/CI, runc in macOS dev; no network, empty env, read-only `/repo`, Auto mode works on a tmpfs copy). No GitHub token or secret ever enters it. Denylisted files are stripped from every snapshot.
- **Every steer is a `steps` row**; nodes carry `step_id`. Repo access is checked by numeric repo id.
- **A step never pauses to ask the captain.** It always ends in exactly one waypoint or error node.
- **A node's context is built only from its own path from the root.** Sibling branches must never leak into it.
- **Graph nodes are append-only**, enforced by Postgres grants (no `UPDATE`/`DELETE` for `ahoy_app`). Only whole-session or whole-account purge, via `purge_sessions()`. Roles come from `deploy/db/bootstrap.sh`; Alembic runs as `ahoy_owner`.
- **The command mode is a per-user setting** (Read-only / Safe / Auto), separate from the wheel. The denylist (`.env*`, `*.pem`, `*.key`, `id_*`) and the scrubbed shell environment apply in every mode.
- **Secrets (`GEMINI_API_KEY`, GitHub App key/secrets, `TOKEN_ENCRYPTION_KEY`) live only in the backend environment.** They never reach the browser, the sandbox, the model, logs, or the DB in plaintext. GitHub user tokens are stored AES-GCM encrypted.

### Testing Requirements
- **Backend:** `cd backend && uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy src`
- **Frontend:** `cd frontend && npm test && npm run typecheck && npm run lint`
- **E2E:** `cd frontend && npm run test:e2e`, against the backend started with `--provider mock --github mock`
- Automated tests never call real Gemini or real GitHub. Use the mock provider and mock GitHub. Real Gemini is only for manual checkpoints and `ahoy-eval`.

### Ask First (from SPEC.md → Boundaries)
- Changing the default allowlist or the default mode
- Adding tools, modes, providers or dependencies beyond the Tech Stack
- Adding GitHub App permissions or webhook events; changing sandbox flags or image
- Changing the database schema or the API shape

## Dependencies

### External (planned, pinned at scaffold)
- **Backend:**
  - Python ≥3.12 (3.15 installed) managed with uv
  - FastAPI, uvicorn, Pydantic v2, pydantic-settings, `google-genai`, `pathspec`, `sse-starlette`
  - SQLAlchemy 2 async + asyncpg, Alembic, httpx, PyJWT[crypto], cryptography
  - pytest, ruff, mypy
- **Frontend:**
  - Node ≥22 (26 installed), Vite, React 19, TypeScript 5
  - Cytoscape.js + `cytoscape-fcose`, `react-markdown`, `shiki`, `openapi-typescript`
  - Vitest, Playwright
- **Infra:** PostgreSQL 16, Docker + gVisor (sandbox runner), TLS reverse proxy
- **Optional:** ripgrep for `search`

## Manual Notes

Notes under this heading are written by humans and preserved on regeneration.

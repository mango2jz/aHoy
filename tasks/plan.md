# Implementation Plan: aHoy MVP

Spec: [../SPEC.md](../SPEC.md) (v5.1 draft: hosted, GitHub sign-in, authorization, GitHub repos, sandbox, Postgres)
Task details and checkboxes: [todo.md](todo.md)

> **History.** Rewritten 2026-11-24 for SPEC v5 (no task had started). Revised the same day after an architect review ("sound with fixes") and a critic review ("reject", fixable): steps table, DB roles, fixture and CI moved to Phase 1, oversized tasks split, private-repo notice moved before the first real run, dev-origin and mock sign-in rules, E2E infrastructure. A second critic pass ("approve with changes") then fixed task ordering (T35 before T20, E2E setup in its own task T38), added T37 for the domain models, and fixed DB grants, CI bootstrap and Auto-mode writes. Task IDs T1–T18 keep their v4 meaning where the work still exists; T19–T38 are new.

## Overview

aHoy is a hosted web app. A Python (FastAPI) backend holds the whole agent harness plus authentication, authorization and the GitHub integration. A React + TypeScript frontend draws a branching graph of knowledge nodes and a hold-to-steer wheel. Users sign in with GitHub through a GitHub App, open a repo the App is installed on, pinned to a commit, and Gemini explores and explains it. Shell commands run in a per-step sandbox container. Sessions are private to their owner and can be shared view-only.

We build it as **vertical slices**:

1. Foundation (scaffold, CI, fixture repo, provider spike, tools, store, gate, GitHub clients)
2. Identity and GitHub (sign-in, cookies, CSRF, authz, repos, snapshots, webhooks)
3. The Overview node working end to end, signed in, on a real repo
4. Steering and branching
5. Sandbox, shell commands and modes
6. Multi-user features (sharing, limits, deletion)
7. Evals, secret scan, deploy and polish

Security-critical pieces (auth, authz, CSRF) come **before** the first end-to-end slice, so no API route ever exists without them. The private-repo notice exists before any real repo is sent to Gemini (Checkpoint C).

## Environment (checked 2026-11-24)

| Tool | Installed | Note |
|---|---|---|
| uv | 0.11.6 | ok |
| python3 | 3.15.1 | ok. `requires-python = ">=3.12"`; uv pins the version via `.python-version` |
| node / npm | 26.12.1 / 11.20.0 | Spec says Node 22 LTS. We use the installed 26 and set `engines` to `>=22` |
| rg | 14.1.1 | `search` uses rg and falls back to pure Python |
| git | 2.50.1 | Repo initialised on `main`, no commits yet |
| docker | 29.9.2 (CLI) | **Daemon not running.** Needed for local Postgres (T4) and the runner (T24). Start Docker Desktop first |
| gVisor (`runsc`) | — | **Linux only.** macOS dev uses `runc`; `runsc` runs in CI (T32) and production |
| Postgres | via Docker | No local `psql`. `docker compose up -d postgres` |
| gh | 2.86.0 | Handy for the dev GitHub App and CI |

## Architecture Decisions

- **Pydantic models are the single source of truth for every API shape.** TypeScript types are generated from FastAPI's OpenAPI schema (`npm run gen:types`). SQLAlchemy models mirror the Pydantic ones; a test asserts the field sets match.
- **The Turn schema is flat** if Gemini's `response_schema` can't handle a discriminated union with free-form `args`, which T2 checks. Flat form: `action` plus optional typed fields (`path`, `start_line`, `query`, `glob`, `command`, `depth`, `answer`), turned into `ToolTurn | AnswerTurn` in Python. **T2 status (2026-11-24): spike pending real probe** (no `GEMINI_API_KEY` was available). Found offline with google-genai 3.6.1: the SDK's `response_schema` rejects the union client-side in Developer API mode, before any request (`Schema` validation: `discriminator`/`oneOf` "Extra inputs are not permitted"; without the discriminator: "additionalProperties is only supported in Gemini Enterprise Agent Platform mode … not in Gemini Developer API mode", caused by `args: dict[str, Any]`). `FlatTurn` and `AnswerTurn` pass the SDK conversion. Both wire shapes are built behind one switch in `providers/gemini.py` (`wire="union"` default: the `Turn` JSON Schema sent via `response_json_schema`; `wire="flat"`: `FlatTurn` via `response_schema`); the public `Turn` type is the same either way. To decide: `uv run python -m ahoy.providers.gemini --probe` (union), then `--wire flat` if it fails; record the model id (default `gemini-2.5-flash`), Gemini's error text and the decision here, and `--record backend/tests/fixtures/gemini` to add real fixtures.
- **A provider is a `Protocol`** with one method: `async def next_turn(messages, schema) -> Turn`. Gemini and Mock now; Ollama later adds one file.
- **GitHub is behind two Protocols**, `GitHubApp` and `GitHubUser`. Real implementations use `httpx`; `--github mock` swaps in an in-process fake (users alice, bob, mallory; repos `fixture/demo` private and `fixture/public` public; tokens shaped like `ghu_…` so the secret scan is meaningful). `respx` tests pin the real client's HTTP shapes. The mock refuses to start unless `AHOY_ENV` is `dev` or `test`.
- **Repo access is checked by numeric id** (`GET /repositories/{id}`) plus "the installation can still mint a token for it", cached together for 5 minutes.
- **`authorize()` is the only door.** Routes never load a `Session` from the store directly. A sweep test enumerates every route from the FastAPI app: all `/api/*` must 401 signed out, all state-changing routes must 403 without CSRF, and every route with a session/step id must appear in the authz table, else the test fails.
- **Steps are rows.** `steer` inserts a `steps` row (queued/running) and returns its id; nodes carry `step_id`; the row is closed in the same transaction as the step's final node. Quotas count rows; startup marks open rows `interrupted` with an error node.
- **Steps run as background asyncio tasks** in the single backend instance. Nodes are **inserted before they are broadcast**. Every SSE event carries a per-step sequence number as its `id`. On connect: a `status` event, then persisted node events above `Last-Event-ID`, then live. Transient `queued`/`tool_started` events are never replayed. Turning sharing off or deleting a session closes its SSE subscribers in-process at once; access is re-checked uncached every 60 s.
- **A step captures the owner's policy at admission** (mode, allowlist, model, installation, snapshot lease). Mid-step settings changes apply to the next step.
- **Session creation is asynchronous.** `POST /api/sessions` validates and returns `preparing`; a background task fetches the snapshot, sets `ready`, and starts the Overview. The UI polls `GET /api/sessions/{id}`.
- **Snapshots** are fetched with `git fetch --depth=20` at the pinned SHA (token only on that process's argv), denylisted files are deleted, then the tree is made read-only. Cache keyed by `(repo_id, sha)`, shared across users, leased by preparation and running steps; eviction skips leased entries.
- **Sandbox runner.** A Python service in the same package (`ahoy-runner`), driving the `docker` CLI via `create_subprocess_exec` (no Docker SDK). Unix-socket API: `start_step(step_id, snapshot_id, mode)`, `run(step_id, argv|shell_cmd, timeout_s)`, `end_step(step_id)`. In dev it runs on the host next to the backend; in prod both run on the VM with `REPO_CACHE_DIR` at the same absolute path everywhere, because the Docker daemon resolves bind-mount sources on the host.
- **Sandbox runtime.** Production and CI use gVisor (`runsc`); macOS dev uses `runc` with the same flags. **Auto mode copies `/repo` into a 512 MB tmpfs `/work`** (`rw,exec,uid=65534`) at container start and runs `chmod -R u+w /work`, which behaves identically on both runtimes. The runner refuses `runc` when `AHOY_ENV=prod`.
- **Database roles** are created by `deploy/db/bootstrap.sh` (a shell init script calling `psql -v`, so it can read `AHOY_OWNER_PASSWORD` / `AHOY_APP_PASSWORD`), not by Alembic. It also grants `ahoy_purge` to `ahoy_owner` and creates the database `OWNER ahoy_owner`. Locally it is a compose init script; in CI a workflow step runs it with `psql` after checkout (service containers start before checkout and can't mount repo files). Alembic runs as `ahoy_owner`; the app connects as `ahoy_app`; `purge_sessions()` is owned by `ahoy_purge`.
- **Test database.** `BT/conftest.py` (from T36) resets the test database per session: drop and recreate as `ahoy_owner`, migrate, then hand tests an `ahoy_app` connection. Every later DB-backed test uses it.
- **Dev origin.** The browser only talks to Vite on `127.0.0.1:5173`, which proxies `/api`, `/auth`, `/webhooks`. `APP_ORIGIN` and the dev App's callback both use 5173. In dev/test the cookie is `ahoy_sid` without `Secure`; prod uses `__Host-ahoy_sid` and refuses a non-https `APP_ORIGIN`.
- **Mock sign-in.** `GET /auth/mock/login?user=alice` exists only with `--github mock` and `AHOY_ENV` dev/test. Playwright uses it to switch users.
- **E2E infrastructure (T38).** Playwright `globalSetup`: `docker compose up -d postgres`, reset the test DB (as in `conftest.py`), then start `ahoy serve --provider mock --github mock` (`AHOY_ENV=test`) as a **managed child process** and wait on `/api/health`. Vite is a `webServer` entry. A `backend` fixture exposes `restart()` (kill, start, wait on health) for the restart test. A `signIn(page, user)` fixture uses mock login. The runner joins in T24: `globalSetup` starts `ahoy-runner` and waits until its Unix socket accepts a connection, since it has no HTTP port.
- **Refresh tokens** are single-use; refresh runs under a per-user `asyncio.Lock` with re-read and one-transaction save.
- **Cytoscape node dragging is disabled** (`autoungrabify`). Press-and-hold always means "steer". Dragging the background pans.
- **Flesch reading ease is a small in-house function** in the eval scorer, so we don't add `textstat`.

## Dependency Graph

```
T33 repo files ── T1 backend scaffold ──┬── T32 CI baseline (human: create remote, push)
                                        ├── T31 fixture repo
                                        ├── T2 provider spike ⚠ ── T37 domain models
                                        ├── T3 path resolution + read-only tools
                                        │      T37 ── T4 DB models + migration + roles ── T36 store API + conftest
                                        ├── T12 gate (pure, any time)
                                        ├── T24 sandbox runner (any time; placed in Phase 5)
                                        └── T19 GitHub clients + mock (needs T31, T3)
                                                 │
                         T36 + T19 ── T35 cookie sessions + crypto + refresh lock + /api/me
                                                 │
                                     T20 sign-in flow + install redirect + mock login
                                                 │
                                     T21 CSRF + headers + redaction
                                                 │
                                     T22 authorize() + access cache + sweep harness
                                                 │
                       T3 + T22 ── T23 repos + session create + snapshots + private notice API
                                                 └── T25 webhooks
T2 + T3 + T36 ── T5 context builder ── T6 step loop ──┐
                                      T23 ────────── T7 steer API + SSE (behind authz)
                                                      │
                          T8 frontend scaffold + type gen ── T34 graph view + SSE
                                                      │
                          T38 E2E infrastructure (Playwright, Postgres reset, backend process)
                                                      │
                          T26 sign-in page, home, picker, notice UI + signin E2E
                          T9 side panel + files API
                                                      │
                          T10 wheel (pure UI; after T8)
                                                      │
                          T11 steering wired + branching E2E
                                                      │
      T12 + T24 runner ── T13 shell tool via runner + block-and-continue
                                                      │
                          T14 settings API + page + mode UI ── T15 Allow blocked…
                                                      │
          T27 sharing + viewer   T28 limits + queue   T29 deletion
                                                      │
                          T16 evals ── T17 prompt tuning
                                                      │
                          T30 secret-leak scan
                                                      │
                          T18 deploy artefacts + full E2E + README
```

## Phases (index; details in todo.md)

### Phase 1: Foundation
- T33 Repo files (`.gitignore`, `.env.example`, compose)
- T1 Backend scaffold, config, CLI
- T32 CI baseline (backend + frontend jobs; grows with later tasks)
- T31 Fixture repo (needed by the mock GitHub, E2E, evals)
- T2 Provider spike ⚠
- T37 Domain Pydantic models (`User`, `Session`, `Step`, `GraphNode`)
- T3 Path resolution + read-only tools
- T4 DB models, first migration, DB roles bootstrap (+ CI DB steps)
- T36 Store API, grant tests, test-DB `conftest.py`
- T12 Gate
- T19 GitHub clients + mock GitHub

**Checkpoint A:** CI green (needs the GitHub remote from T32). The spike shows Gemini returning a valid Turn for both actions. `ahoy_app` cannot update or delete nodes. **Human:** answer Open Question 1, register the dev GitHub App (Open Question 3). Open Question 2 must be answered **before T31**.

### Phase 2: Identity and GitHub
- T35 Cookie sessions, token crypto, refresh lock, logout, `/api/me`
- T20 Sign-in flow (incl. install redirect) + mock login
- T21 CSRF, security headers, log redaction
- T22 `authorize()`, access cache, sweep harness
- T23 Repo listing, session creation, snapshot cache, private-repo notice API
- T25 GitHub webhooks

**Checkpoint B:** with the **real dev GitHub App**, sign in (including straight after installing), see installed repos, create a session that reaches `ready`. Sweep green over `/api/me`, `/api/repos`, `/api/sessions`, `/api/sessions/{id}`. **Human:** the GitHub screens show Contents read + Metadata read only.

### Phase 3: Overview end to end
- T5 Context builder
- T6 Step loop (read-only tools)
- T7 Steer API + SSE, behind authz
- T8 Frontend scaffold + type generation
- T34 Graph view + live SSE
- T38 E2E infrastructure
- T26 Sign-in page, session list, repo picker, private-repo notice UI
- T9 Side panel + code viewer

**Checkpoint C:** with real GitHub and real Gemini, a new user signs in, installs the App on one repo, acknowledges the private-repo notice if needed, and gets the Overview (success criterion 1). **Human review of tone and readability.**

### Phase 4: Steering and branching
- T10 Steering wheel component
- T11 Steering wired end to end, branching E2E (incl. restart and second browser context)

**Checkpoint D:** first / second-a / second-b works in the browser; dots within 1 s; tree identical after restart and in a second browser context. **Human review of the wheel's feel.**

### Phase 5: Sandbox, shell and command modes
- T24 Sandbox runner service + image + sandbox tests + CI `runsc` job + runner in E2E setup
- T13 Shell tool via the runner + block-and-continue
- T14 Settings API + settings page + mode UI + badges
- T15 "Allow blocked…" escalation

**Checkpoint E:** success criteria 4 and 5 pass locally; success criterion 11 passes on `runsc` **in CI**.

### Phase 6: Multi-user
- T27 Sharing + viewer mode
- T28 Limits and step queue
- T29 Session and account deletion

**Checkpoint F:** success criteria 8 and 9 pass. **Human:** two real GitHub accounts; share; remove the second account's repo access on GitHub; the session disappears for them within 5 minutes.

### Phase 7: Quality and finish
- T16 Eval harness
- T17 Prompt tuning to the eval targets
- T30 Secret-leak scan
- T18 Deploy artefacts, full E2E, README

**Checkpoint G:** success criteria 2–11, 13 and 14 pass in CI; 1 and 12 were checked by hand (Checkpoint C and T17). Ready for review and a first deploy.

## Parallelization

- **After T1:** T2, T3, T12, T31 and T32 are independent. T2 first, because it carries the model risk; T37 → T4 → T36 follow it. T19 waits for T31 and T3.
- **After T22:** T23 and T25 in parallel. T5 → T6 can run alongside Phase 2 once T2, T3 and T36 are done (the loop does not need auth).
- **Frontend:** T8, T10, T38 and T26 can run alongside T5–T7 once the `GraphNode`, `Step`, event and `/api/me` schemas are frozen.
- **T24 (runner)** depends only on T1 and can be built any time.
- **Phase 6:** T27, T28 and T29 touch mostly different files; T27 and T29 both touch `B/api/sessions.py`, so merge carefully or run them in sequence.
- **Must be sequential:** T2 → T37 → T4 → T36, T35 → T20 → T21 → T22, T5 → T6 → T7, T8 → T34 → T38 → T26, T13 → T15.

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Gemini `response_schema` rejects the union or `dict[str, Any]` args | High | T2 spike first, flat-schema fallback designed |
| **A route skips `authorize()`** | High | Single door, route-enumeration sweep, 100% branch coverage on `authz.py` |
| **Stale repo access** after a user leaves an org | High | Id-based check every ≤5 min, webhook cache drops, uncached SSE re-check every 60 s |
| **Token leak** via logs, DB, sandbox, `.git/config` | High | Encrypted at rest, hash-only cookies, redaction filter, token only on one `git` argv, T30 scan with canaries |
| **Committed secrets in history** reachable via git | Med | Working-tree stripping, Safe-mode `rev:path` block and exclude pathspecs; documented as a guard, not a boundary |
| **Safe-mode programs that execute** (`find -exec`, `rg --pre`, `git --ext-diff`) | High | Flag denylist table in the gate, ≥1 test per row |
| **Sandbox escape or abuse** (prompt-injected Auto commands) | High | gVisor in prod, no network, no secrets, non-root, read-only `/repo`, limits; runner sole holder of the Docker socket |
| gVisor unavailable on macOS dev | Med | `runc` with identical flags and the tmpfs-copy Auto mode; `runsc` job in CI; runner refuses `runc` in prod |
| Snapshot evicted under a running step | Med | Leases; eviction skips leased entries |
| GitHub install redirect without `state` | High | Explicit bounce to `/auth/github/login`, tested |
| Concurrent refresh logs users out | Med | Per-user lock, re-read, single transaction |
| Managed Postgres forbids `CREATE ROLE` in migrations | Med | Roles in an admin bootstrap shell script (`psql -v`); Alembic only creates tables, grants and functions |
| Prompt injection in repo content exfiltrating via waypoints | Med | No network in sandbox, no raw HTML, CSP blocks remote images and connects, system-prompt rule |
| GitHub API rate limits | Med | Caches (60 s listing, 5 min access), `Retry-After` handling, shared snapshots |
| Sibling context leaks | High | Context test (T5) plus E2E (T11) |
| Gemini cost on a shared key | Med | Quotas and global cap (T28); mock provider in all automated tests |
| Hold-to-steer conflicts with Cytoscape pan and drag | Med | Disable node grab; hold timer + movement threshold; tune at Checkpoint D |
| Node 26 vs a toolchain that expects LTS | Low | Pin versions that support 26; fall back to `nvm use 22` |

## Open Questions

1. **Who writes the Python?** You're learning Python. Options:
   - (a) I implement everything and you review.
   - (b) I write the tests and stubs for each Python task, you implement, and I review.
   - (c) Mixed: you take the harness core (T5 context, T6 loop, T12 gate); I take every other Python task (all of T1–T4, T7, T13–T17, T19–T25, T27–T31, T35–T37), including all security-critical ones.

   Recommendation: (c). Auth and authz are unforgiving to learn on in a hosted app.

   **Answered 2026-11-24: (a).** Subagents implement every Python task, including T5, T6 and T12; the user reviews.
2. **Fixture repo** (answer **before T31**): a small purpose-built repo (about 30 files, known facts, runnable tests), or a real small repo you know well (license permitting, vendored into `backend/evals/fixture/`)? It backs the mock GitHub, the E2E suite and the evals.

   **Answered 2026-11-24:** a curated subset of the user's own repo `speak-lab` (TS/Next.js): about 40–60 git-tracked source files (one feature slice of `src/`, a vitest test, `package.json`). No media, no `.docx`, and **never** its real untracked `.env`. T31 plants a fake `.env` and a deep nested directory. Consequence: the fixture's test command needs Node, so the sandbox image (T24) gains Node. That is a sandbox-image change, to be confirmed at T24.
3. **Dev GitHub App** (at Checkpoint A): register one with callback `http://127.0.0.1:5173/auth/github/callback`, "Request user authorization during installation" on, expiring user tokens on, Contents read + Metadata read, and events `installation`, `installation_repositories`, `github_app_authorization`. Webhooks to localhost need a tunnel (e.g. `smee.io`) or can be skipped in dev because correctness does not depend on them. I'll write the exact field values when we get there.
4. **VM provider** for the first deploy (affects T18 only): the design needs a Linux VM with Docker + gVisor plus managed Postgres. Which provider?
5. **Auto mode in the MVP?** Kept per D16. Deferring it would remove the tmpfs copy and most Auto-only risk. Your call.

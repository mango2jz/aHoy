# aHoy MVP — Task List

Plan: [plan.md](plan.md) · Spec: [../SPEC.md](../SPEC.md) (v5.1)

Paths are relative to the repo root. `B` = `backend/src/ahoy`, `BT` = `backend/tests`, `F` = `frontend/src`, `FE` = `frontend/e2e`.

> Rewritten 2026-11-24 for SPEC v5, then revised after architect review and two critic passes. T1–T18 keep their v4 IDs (T4, T8, T11, T13 and T20 changed scope); T19–T38 are new. Tasks are listed in **build order**, not ID order.

**Sizes:** S = 1–2 files, M = 3–5 files, counting hand-written files only. Not counted, but named in the task: files generated unchanged by a tool (`npm create vite`, `alembic init`, lockfiles), and one-line wiring edits (registering a router in `B/app.py`, adding a row to the authz sweep table, a step in `ci.yml`, a script entry in `pyproject.toml`).

**Definition of done for every task:**
- Its tests pass, and every new route is in the authz sweep table.
- Backend: `uv run ruff check . && uv run ruff format --check . && uv run mypy src` is clean. Frontend: `npm run typecheck && npm run lint` is clean.
- CI (once T32 exists) is green on the branch.
- No unchecked acceptance criteria remain.
- No secret, token or cookie value appears in test output or logs.

---

## Phase 1: Foundation

- [x] **T33: Repo files**
  - **Acceptance:**
    - `.gitignore` covers `.env`, `.venv`, `node_modules`, `dist`, `__pycache__`, `.repo-cache/`, `.coverage`.
    - `.env.example` lists every variable in SPEC → Privacy and safety (secrets and non-secrets), names only, with dev defaults as comments (`APP_ORIGIN=http://127.0.0.1:5173`, `AHOY_ENV=dev`).
    - `docker-compose.yml` with a `postgres:16` service and a named volume. The bootstrap init-script mount is added by T4 together with the script, so `compose up` never creates a directory in its place.
  - **Verify:** `docker compose config` succeeds; `git check-ignore .env` prints `.env`.
  - **Files:** `.gitignore`, `.env.example`, `docker-compose.yml`
  - **Deps:** none · **Size:** M

- [x] **T1: Backend scaffold, config, CLI**
  - **Acceptance:**
    - `config.py` (pydantic-settings) loads all settings; secrets are `SecretStr`, never in `repr` or logs. Startup fails clearly if a required secret is missing (unless `--provider mock --github mock`). `AHOY_ENV=prod` refuses a non-https `APP_ORIGIN`.
    - `uv run ahoy serve [--port 4317] [--provider gemini|mock] [--github real|mock] [--reload]`. `--github mock` is refused unless `AHOY_ENV` is `dev` or `test`.
    - `app.py` is an app factory; `GET /api/health` returns 200 with `{provider, model, github}` and no secrets (the only `/api/*` route exempt from auth).
  - **Verify:** `uv run pytest BT/test_app_config.py`
  - **Files:** `backend/pyproject.toml`, `B/cli.py`, `B/app.py`, `B/config.py`, `BT/test_app_config.py` (generated `uv.lock`)
  - **Deps:** T33 · **Size:** M

- [ ] **T32: CI baseline** — workflow written and reviewed 2026-11-25; waiting on the human push for the first green run
  - **Acceptance:**
    - `.github/workflows/ci.yml` on Linux: backend job (`uv sync`, `pytest --cov=ahoy --cov-branch`, ruff, ruff format check, mypy); frontend job (skipped until `frontend/package.json` exists). Later tasks add to it: Postgres service + bootstrap + migrations (T4), E2E (T38), sandbox on `runsc` (T24), coverage thresholds (T12, T22, T18).
    - **Human step:** create the GitHub repository and push `main` (this project commits and pushes only when you ask). Until then CI can't run, and the checkpoints that need CI wait.
  - **Verify:** after the push, the backend job runs and passes (frontend job skipped).
  - **Files:** `.github/workflows/ci.yml`
  - **Deps:** T1 · **Size:** S

- [x] **T31: Fixture repo** — answer Open Question 2 first
  - **Acceptance:**
    - `backend/evals/fixture/` holds about 30 files in one language, with a runnable test command (for "run the tests" checks), at least one committed `.env`-style file to test denylist stripping, and a nested directory deep enough to exercise repo-map collapsing.
    - `backend/evals/facts.yaml` holds 10 facts (keywords + expected file).
    - A small script `backend/evals/make_fixture_git.py` turns the fixture into a git repo with ~5 commits (used by T19's mock and `fetch_snapshot` tests).
  - **Verify:** `uv run python backend/evals/make_fixture_git.py /tmp/fx && git -C /tmp/fx log --oneline | wc -l` ≥ 5; the fixture's own tests pass.
  - **Files:** `backend/evals/fixture/**` (fixture content, exempt from the size rule), `backend/evals/facts.yaml`, `backend/evals/make_fixture_git.py`
  - **Deps:** T1 · **Size:** M

- [ ] **T2: Provider spike — Gemini + Mock + Turn schema** ⚠ do first after T1
  - **Acceptance:**
    - `schemas.py` defines `Waypoint`, `CodeRef`, `NextMove` and `Turn`. The union-vs-flat decision is made by trying it on real Gemini and recorded in plan.md.
    - `providers/base.py` defines a `Provider` Protocol. `gemini.py` calls `google-genai` with `response_schema` and returns a parsed `Turn`. `mock.py` replays a scripted list of turns.
    - `uv run python -m ahoy.providers.gemini --probe` returns a valid tool turn and a valid answer turn.
  - **Verify:** `uv run pytest BT/test_providers.py` (mock, plus Gemini parsing against recorded fixtures), and a manual probe against real Gemini.
  - **Files:** `B/harness/schemas.py`, `B/providers/base.py`, `B/providers/gemini.py`, `B/providers/mock.py`, `BT/test_providers.py`
  - **Deps:** T1 · **Size:** M

- [x] **T37: Domain Pydantic models**
  - **Acceptance:**
    - `schemas.py` gains the API and domain models from SPEC → Data model: `UserSettings`, `User`, `Session`, `Step`, `Steer`, `ToolCall`, `Blocked`, `GraphNode` (with `step_id`), and the SSE event models (with a per-step `seq`).
    - `GitHubTokens` and `AuthSession` are **not** Pydantic API models; they exist only as DB models (T4).
  - **Verify:** `uv run pytest BT/test_schemas.py` (round-trip, discriminators, `Literal` values match SPEC)
  - **Files:** `B/harness/schemas.py`, `BT/test_schemas.py`
  - **Deps:** T2 (same file) · **Size:** S

- [x] **T3: Path resolution + read-only tools**
  - **Acceptance:**
    - `sandbox.paths.resolve(snapshot_root, path)` rejects `../` escapes, absolute paths, symlinks whose target is outside the snapshot, and denylisted names (`.env*`, `*.pem`, `*.key`, `id_*`).
    - `list_dir` (depth ≤3, `.gitignore` via `pathspec`), `read_file` (≤500 lines from `start_line`), `search` (≤50 hits; rg when present, else Python; both skip denylisted files and never follow symlinks out).
    - Tools take a `Snapshot` (read-only root) argument, never a global repo path. The repo map notes empty submodule directories and LFS pointer files.
  - **Verify:** `uv run pytest BT/test_paths.py BT/test_tools_fs.py` (table-driven escape suite on a `tmp_path` repo with planted symlinks)
  - **Files:** `B/sandbox/paths.py`, `B/tools/fs.py`, `B/tools/search.py`, `BT/test_paths.py`, `BT/test_tools_fs.py`
  - **Deps:** T1 · **Size:** M

- [x] **T4: DB models, first migration, DB roles** *(v4: JSONL store; split with T36 and T37)*
  - **Acceptance:**
    - `deploy/db/bootstrap.sh` (shell, `psql -v`, `set -eu`) creates `ahoy_owner` and `ahoy_app` with passwords from `AHOY_OWNER_PASSWORD` / `AHOY_APP_PASSWORD`, `ahoy_purge` as `NOLOGIN`, runs `GRANT ahoy_purge TO ahoy_owner`, and creates the database `OWNER ahoy_owner`. Not run by Alembic. Compose mounts it into `/docker-entrypoint-initdb.d/`. CI gains a Postgres service plus steps that run it with `psql` after checkout and then `alembic upgrade head`.
    - SQLAlchemy models for `users`, `github_tokens`, `auth_sessions`, `sessions` (incl. `private`, `status`, `status_reason`), `steps`, `graph_nodes` (incl. `step_id`), `webhook_deliveries`.
    - Migration `0001_initial` (run as `ahoy_owner` via `MIGRATION_DATABASE_URL`) creates the tables, the per-table grants to `ahoy_app` and `ahoy_purge` from SPEC → Database roles (incl. `SELECT, DELETE` for `ahoy_purge`), and `purge_sessions(owner_id, session_ids)` with `ALTER FUNCTION … OWNER TO ahoy_purge`, `SECURITY DEFINER`, `SET search_path = pg_catalog, public, pg_temp`, `EXECUTE` revoked from `PUBLIC` and granted to `ahoy_app`.
  - **Verify:** `docker compose up -d postgres && uv run alembic upgrade head && uv run alembic downgrade base && uv run alembic upgrade head`; CI backend job runs the bootstrap and migrations.
  - **Files:** `deploy/db/bootstrap.sh`, `B/store/models.py`, `backend/alembic/env.py` (edited from `alembic init`), `backend/alembic/versions/0001_initial.py` (generated `alembic.ini`; wiring edits to `docker-compose.yml` and `ci.yml`)
  - **Deps:** T33, T32, T37 · **Size:** M

- [x] **T36: Store API + grant tests**
  - **Acceptance:**
    - `db.py`: async engine for `DATABASE_URL` (`ahoy_app`).
    - `BT/conftest.py`: per test session, drop and recreate the test database as `ahoy_owner`, run migrations, and give tests an `ahoy_app` session that is rolled back after each test. Every later DB-backed test uses it.
    - Store API: `create_session`, `set_session_status`, `create_step`, `close_step`, `open_steps()`, `append_node` (with `step_id`), `load_nodes(session_id)`, `step_nodes(step_id)`, `children(id)`, `root_path(id)`, `purge(owner_id, session_ids)`.
    - Connected as `ahoy_app`: `UPDATE` and `DELETE` on `graph_nodes` raise permission errors; `purge` removes whole sessions (nodes, steps) and refuses sessions of another owner.
    - The SQLAlchemy and Pydantic field sets of `GraphNode`, `Step` and `Session` match.
  - **Verify:** `uv run pytest BT/test_store.py BT/test_grants.py`
  - **Files:** `B/store/db.py`, `B/store/repo.py`, `BT/conftest.py`, `BT/test_store.py`, `BT/test_grants.py`
  - **Deps:** T4 · **Size:** M

- [x] **T12: Gate**
  - **Acceptance:**
    - `gate.check(cmd, mode, policy, pre_authorized) -> Decision(run|block, reason, argv | shell_cmd)`.
    - Read-only blocks every command. Safe runs only single, simple, allowlisted commands and returns an argv. Auto returns a `shell_cmd` for everything except commands that name a denylisted path.
    - Chaining tokens (`;` `&&` `||` `|` `>` `<` `` ` `` `$(` and newline) are blocked in Safe mode.
    - Every row of SPEC's **Safe-mode flag denylist** is enforced; `git show <rev>:<denylisted>` is blocked; exclude pathspecs are appended to `git log/diff/show` without a pathspec.
    - Pre-authorization matches the exact string only.
  - **Verify:** `uv run pytest BT/test_gate.py --cov=ahoy.harness.gate --cov-branch --cov-fail-under=100` (table-driven, ≥60 cases incl. every command in success criterion 4)
  - **Files:** `B/harness/gate.py`, `BT/test_gate.py`
  - **Deps:** T1 (any time) · **Size:** S

- [x] **T19: GitHub clients + mock GitHub**
  - **Acceptance:**
    - `GitHubApp` Protocol + real impl: App JWT (RS256, `PyJWT`), installation token narrowed to one repo and `contents: read`, `can_mint_for(installation_id, repo_id)`, `resolve_ref`, `tree(sha)` (exposes `truncated`), `fetch_snapshot(repo, sha, dest, depth)` (token via `-c http.extraHeader` on that one process, scrubbed env; deletes denylisted files using `sandbox.paths.is_denylisted` from T3, never a second matcher; `chmod -R a-w`).
    - `GitHubUser` Protocol + real impl: `exchange_code` (PKCE), `refresh`, `get_user`, `list_installations`, `list_installation_repos`, `can_read_repo(repo_id)` via `GET /repositories/{id}`, `revoke_grant`.
    - One `httpx` client honouring `X-RateLimit-Remaining` and `Retry-After`.
    - `github/mock.py`: users alice, bob, mallory; `fixture/demo` (private, alice + bob) and `fixture/public` (public); both built from T31's fixture via `make_fixture_git.py`; tokens shaped `ghu_…`; test hooks to revoke access and remove a repo from the installation.
    - After `fetch_snapshot`: `.git/config` holds no token, denylisted files are gone, the tree is read-only.
  - **Verify:** `uv run pytest BT/test_github_client.py BT/test_github_mock.py` (`respx` for real HTTP shapes; a local bare repo for `fetch_snapshot`)
  - **Files:** `B/github/app_client.py`, `B/github/user_client.py`, `B/github/mock.py`, `BT/test_github_client.py`, `BT/test_github_mock.py`
  - **Deps:** T1, T3, T31 · **Size:** M

### Checkpoint A
- [ ] CI green (backend job, with bootstrap + migrations); the GitHub remote exists (T32)
- [ ] The Gemini probe works, and the schema decision is recorded in plan.md
- [x] `ahoy_app` cannot update or delete nodes
- [ ] Human: answer Open Question 1; register the dev GitHub App (Open Question 3). (Open Question 2 was needed before T31.)

---

## Phase 2: Identity and GitHub

- [ ] **T35: Cookie sessions, token crypto, refresh lock**
  - **Acceptance:**
    - `crypto.py`: AES-256-GCM with `TOKEN_ENCRYPTION_KEY`, user id as associated data.
    - `sessions.start(user, tokens)` (called by T20's callback and mock login) stores encrypted tokens, creates an `auth_sessions` row (SHA-256 of the cookie value only), sets the cookie (`__Host-ahoy_sid` prod; `ahoy_sid` without `Secure` in dev/test), clears the pre-auth cookie, redirects to `/`. Idle 7 days, absolute 30 days, new value at every sign-in.
    - `current_user` dependency: 401 without a valid cookie; refreshes the GitHub token when <5 min from expiry under a per-user `asyncio.Lock` (re-read, refresh once, save both tokens in one transaction); ends the session if refresh fails.
    - `POST /auth/logout` deletes the row and clears the cookie. `B/api/me.py` is created here with `GET /api/me` (id, login, avatar); T23 and T14 add routes to it.
  - **Verify:** `uv run pytest BT/test_auth_core.py` (drives `sessions.start` directly with a mock-GitHub user: expiry, rotation, hash-only storage, no plaintext token in DB, wrong AAD fails, 2 concurrent near-expiry requests → 1 refresh, both 200)
  - **Files:** `B/auth/sessions.py`, `B/auth/crypto.py`, `B/auth/deps.py`, `B/api/me.py`, `BT/test_auth_core.py`
  - **Deps:** T36, T19 · **Size:** M

- [ ] **T20: Sign-in flow + mock login** *(split with T35)*
  - **Acceptance:**
    - `GET /auth/github/login` sets a 10-minute signed HttpOnly pre-auth cookie holding `state` + PKCE verifier and redirects to GitHub with the S256 challenge.
    - `GET /auth/github/callback`: valid `state` → exchange code, `get_user`, upsert user by **numeric GitHub id**, hand off to `sessions.start(user, tokens)` from T35. Request with `setup_action` and no `state` → redirect to `/auth/github/login`, code discarded. Any other missing/mismatched `state` (constant-time) or missing verifier → 400.
    - `GET /auth/mock/login?user=` exists only with `--github mock` and `AHOY_ENV` dev/test, and uses the same `sessions.start` path.
  - **Verify:** `uv run pytest BT/test_auth_flow.py` (wrong state, replayed code, install redirect bounce, mock login absent in prod config)
  - **Files:** `B/auth/github_oauth.py`, `B/auth/mock_login.py`, `BT/test_auth_flow.py`
  - **Deps:** T35 · **Size:** M

- [ ] **T21: CSRF, security headers, log redaction**
  - **Acceptance:**
    - Middleware: `POST`/`PUT`/`DELETE` need `Origin == APP_ORIGIN` **and** `X-Ahoy-CSRF: 1`, else 403. `/webhooks/github` is exempt.
    - Every response carries the CSP, HSTS (prod), `X-Content-Type-Options` and `Referrer-Policy` from SPEC.
    - `X-Forwarded-*` trusted only from `TRUSTED_PROXY_IPS`.
    - Logging filter redacts GitHub token patterns, cookie values and `Authorization` headers. Request logs hold user id, route, status, latency only.
  - **Verify:** `uv run pytest BT/test_security_middleware.py BT/test_log_redaction.py`
  - **Files:** `B/security.py`, `B/logging.py`, `BT/test_security_middleware.py`, `BT/test_log_redaction.py` (+ wiring in `B/app.py`)
  - **Deps:** T20 · **Size:** M

- [ ] **T22: `authorize()` + access cache + sweep harness**
  - **Acceptance:**
    - `authz.load_session(user, session_id, action)` implements SPEC's authorization table exactly: 404 for invisible sessions, 403 for viewer write actions, owner delete skips the repo check, everything else needs both access parts (user `can_read_repo(repo_id)` with their own token + installation `can_mint_for`).
    - Cache per `(user_id, repo_id)`, 5 min, with `invalidate(repo_id)`, `invalidate_user(user_id)`, and a `fresh=True` bypass for SSE.
    - `BT/test_authz_sweep.py` enumerates every route: all `/api/*` (except `/api/health`) → 401 signed out; all state-changing → 403 without CSRF; every route with `{id}`/`{session_id}`/`{step_id}` must be in the sweep table (else fail) and is checked for owner / shared viewer / stranger per SPEC.
    - Revocation test with a frozen clock: hidden after the TTL, not before (success criterion 9).
  - **Verify:** `uv run pytest BT/test_authz.py BT/test_authz_sweep.py --cov=ahoy.authz --cov-branch --cov-fail-under=100`
  - **Files:** `B/authz.py`, `BT/test_authz.py`, `BT/test_authz_sweep.py` (adds user/session fixtures to `BT/conftest.py` from T36)
  - **Deps:** T21 · **Size:** M

- [ ] **T23: Repos, session creation, snapshot cache, private-repo notice API**
  - **Acceptance:**
    - `GET /api/repos`: repos from the user's installations (cached 60 s per user) plus the install URL.
    - `POST /api/me/private-repo-notice` (in `B/api/me.py` from T35) sets `private_repo_notice_ack_at`.
    - `POST /api/sessions {repo_id, ref?}` follows SPEC → Creating a session, steps 1, 2 and 4–7 (step 3, the session limit, comes in T28): access check; **409** for a private repo without acknowledgement; ref → SHA; tree size check (`truncated` → 422; >500 source files by SPEC's vendored/binary rules → 422; >200 MB → 422); create as `preparing` (201); background fetch under a lease, on-disk size check, `ready` or `failed`. (Starting the Overview is wired in T7.)
    - `GET /api/sessions` lists own sessions with current access; `GET /api/sessions/{id}` returns metadata incl. `status`.
    - `repo_cache.py`: `(repo_id, sha)` keyed, single-flight fetch, leases, LRU eviction above `REPO_CACHE_MAX_GB` that skips leased entries, transparent re-fetch.
    - **Spike:** check whether `GET /repos/{o}/{r}/commits/{sha}` resolves a commit that exists only in a fork; record the result in plan.md and, if so, require pasted SHAs to be reachable from a branch or tag.
  - **Verify:** `uv run pytest BT/test_api_repos.py BT/test_api_sessions.py BT/test_repo_cache.py`
  - **Files:** `B/api/repos.py`, `B/api/sessions.py`, `B/github/repo_cache.py`, `BT/test_api_sessions.py`, `BT/test_repo_cache.py` (`BT/test_api_repos.py` is small; the ack route is a short addition to `B/api/me.py`)
  - **Deps:** T3, T22 · **Size:** M

- [ ] **T25: GitHub webhooks**
  - **Acceptance:**
    - `POST /webhooks/github` verifies `X-Hub-Signature-256` over the raw body (constant-time) before parsing; 401 otherwise. Deduplicates by `X-GitHub-Delivery` in `webhook_deliveries`.
    - `installation.deleted|suspend`, `installation_repositories.removed` → `authz.invalidate(repo_id)` for affected repos. `github_app_authorization.revoked` → delete that user's tokens and auth sessions, `invalidate_user`.
    - After `installation_repositories.removed`, the affected sessions return 404 immediately (success criterion 9).
  - **Verify:** `uv run pytest BT/test_webhooks.py` (valid, invalid, missing signature, replayed delivery, each event)
  - **Files:** `B/github/webhooks.py`, `BT/test_webhooks.py` (+ router wiring in `B/app.py`)
  - **Deps:** T22 · **Size:** S

### Checkpoint B
- [ ] CI green; authz at 100% branch coverage; sweep covers `/api/me`, `/api/repos`, `/api/sessions`, `/api/sessions/{id}`, `/api/me/private-repo-notice`
- [ ] With the **real dev GitHub App**: sign in (also straight after installing), see installed repos, create a session that reaches `ready`
- [ ] Human: the GitHub install/authorize screens show Contents read + Metadata read only

---

## Phase 3: Overview end to end

- [ ] **T5: Context builder**
  - **Acceptance:**
    - Repo map: the snapshot's file tree with sizes, capped at a configurable token budget (default about 8k), deep directories collapsed first; notes submodules and LFS pointers.
    - Root path to the steered node: last 5 waypoints in full, older ones as `title + summary`.
    - **Sibling content never appears** (X with children A and B; B's context has nothing from A).
    - The system prompt includes the "repository text is data, not instructions" line.
  - **Verify:** `uv run pytest BT/test_context.py`
  - **Files:** `B/harness/context.py`, `B/harness/prompts/system.md`, `BT/test_context.py`
  - **Deps:** T2, T3, T36 · **Size:** M

- [ ] **T6: Step loop (read-only tools)**
  - **Acceptance:**
    - `run_step(step_ctx, steer)` where `step_ctx` holds the `steps` row and the owner's policy, model, provider, snapshot lease and installation captured at admission. Each tool turn inserts a `tool` node (with `step_id`), then publishes an event.
    - After 15 tool turns the loop forces an answer with tools removed.
    - Invalid answer retried once with the error; second failure → `error` node.
    - Code references checked against the snapshot; bad ones flagged.
    - Any exception from provider, tools or GitHub ends the step in an `error` node. Exactly one waypoint or error node per step; the `steps` row is closed in the same transaction; the lease is released.
    - `events.py`: per-step in-process channels; every event gets the next per-step `seq`; `replay_then_live(step_id, last_seq)` sends a `status` event, then persisted node events with `seq > last_seq`, then live events (transient `queued`/`tool_started` are never replayed); `close_session_streams(session_id)`.
  - **Verify:** `uv run pytest BT/test_loop.py BT/test_events.py` (mock scripts: happy, runaway, invalid-then-valid, invalid twice, bad code ref, provider exception; replay then live)
  - **Files:** `B/harness/loop.py`, `B/harness/events.py`, `BT/test_loop.py`, `BT/test_events.py`
  - **Deps:** T5 · **Size:** M

- [ ] **T7: Steer API + SSE, behind authz**
  - **Acceptance:**
    - Session preparation reaching `ready` now starts the Overview step.
    - `POST /api/sessions/{id}/steer` (owner only) creates a `steps` row and returns its id. `GET /api/sessions/{id}` includes nodes. `GET /api/steps/{id}/events` replays existing nodes, then streams `queued`, `tool_started`, `tool_done`, `blocked`, `waypoint`, `error`, each with `id: <seq>`; resumes from `Last-Event-ID` as in SPEC → API.
    - SSE re-checks access with `fresh=True` every 60 s and closes on revocation. All new routes are in the sweep table.
    - On startup, every `queued`/`running` step gets an `interrupted` error node and status.
  - **Verify:** `uv run pytest BT/test_api_steer.py BT/test_authz_sweep.py` (create → ready → Overview; mid-step subscriber gets replay; viewer cannot steer; revoked SSE closes; restart marks interrupted)
  - **Files:** `B/api/steer.py`, `B/api/events.py`, `B/api/sessions.py`, `BT/test_api_steer.py`
  - **Deps:** T6, T23 · **Size:** M

- [ ] **T8: Frontend scaffold + type generation** *(split with T34)*
  - **Acceptance:**
    - Vite + React + TS strict (from `npm create vite`), ESLint, Prettier, Vitest. Dev server on `127.0.0.1:5173` proxies `/api`, `/auth`, `/webhooks` to `127.0.0.1:4317`.
    - `npm run gen:types` writes `F/api/schema.d.ts` from `/openapi.json`.
    - `F/api/client.ts`: always sends `X-Ahoy-CSRF: 1` and `credentials: 'same-origin'`; on 401 navigates to `/signin` (placeholder route until T26).
    - CI frontend job is enabled.
  - **Verify:** `npm run typecheck && npm run lint && npm test`; CI frontend job green.
  - **Files:** `frontend/package.json`, `frontend/vite.config.ts`, `F/api/client.ts`, `F/app/app.tsx`, `frontend/tests/client.test.ts` (generated: `index.html`, `tsconfig*.json`, `eslint.config.js`, `main.tsx`)
  - **Deps:** T7 (OpenAPI; can be stubbed earlier) · **Size:** M

- [ ] **T34: Graph view + live SSE**
  - **Acceptance:**
    - Session page renders nodes with Cytoscape + fcose (large waypoints, small tool dots, grey blocked, red error), node grab disabled.
    - `useStep(stepId)` subscribes to SSE and adds dots live; a `queued` event or `status: queued` shows "Queued" on the steered node; a session in `preparing` shows "Preparing `owner/repo` @ `sha`…" and polls every second.
  - **Verify:** `npm test` (graph data mapping). Manually: backend `--provider mock --github mock`, open `/auth/mock/login?user=alice` via 5173, create a session on `fixture/public` with `curl -X POST -b <cookie> -H 'Origin: http://127.0.0.1:5173' -H 'X-Ahoy-CSRF: 1' -H 'Content-Type: application/json' -d '{"repo_id": <id>}' http://127.0.0.1:5173/api/sessions`, open it; Overview appears with dots streaming.
  - **Files:** `F/graph/graph-view.tsx`, `F/graph/to-elements.ts`, `F/api/use-step.ts`, `frontend/tests/graph.test.ts`
  - **Deps:** T8 · **Size:** M

- [ ] **T38: E2E infrastructure**
  - **Acceptance:**
    - `playwright.config.ts` with `globalSetup`/`globalTeardown`. `globalSetup`: `docker compose up -d postgres`, reset the test database (same steps as `BT/conftest.py`), start `ahoy serve --provider mock --github mock` with `AHOY_ENV=test` as a managed child process, and wait for `/api/health`. Vite is a Playwright `webServer` entry.
    - `FE/fixtures.ts`: `signIn(page, user)` (mock login), and a worker-scoped `backend` fixture with `restart()` (kill the child, start it again, wait for health).
    - A smoke spec signs in as alice and sees `/api/me` return her login. CI gains an E2E job (Postgres service, bootstrap, migrations, Playwright Chromium).
    - The runner is **not** started here; T24 adds it.
  - **Verify:** `npm run test:e2e` runs the smoke spec locally and in CI.
  - **Files:** `frontend/playwright.config.ts`, `FE/global-setup.ts`, `FE/fixtures.ts`, `FE/smoke.spec.ts` (+ CI job)
  - **Deps:** T34 · **Size:** M

- [ ] **T26: Sign-in page, session list, repo picker, private-repo notice**
  - **Acceptance:**
    - Sign-in page: "Sign in with GitHub" and the one-sentence data notice from SPEC.
    - Home: own sessions (repo, ref, short SHA, status, last activity, "Shared"); "New session" opens the repo picker (search, branch dropdown, paste tag/SHA), and "Install on GitHub" when no repos. A 409 `private_repo_notice_required` shows the notice; acknowledging calls the ack route and retries.
    - Avatar menu with Sign out.
    - Shared markdown component: **no raw HTML**, links get `rel="noopener noreferrer nofollow"`.
  - **Verify:** `npm test` (raw HTML dropped, link rel, notice flow) and `FE/signin.spec.ts` (mock: sign in, pick `fixture/demo`, acknowledge notice, Overview appears, sign out → `/api/me` 401)
  - **Files:** `F/auth/sign-in-page.tsx`, `F/home/session-list.tsx`, `F/home/repo-picker.tsx`, `F/app/markdown.tsx`, `FE/signin.spec.ts` (notice dialog lives in `repo-picker.tsx`)
  - **Deps:** T38 · **Size:** M

- [ ] **T9: Side panel + code viewer**
  - **Acceptance:**
    - Clicking a waypoint shows title, summary (shared markdown component), key points, confidence, and code refs with ⚠ on invalid ones.
    - Clicking a ref opens a shiki-highlighted viewer fed by `GET /api/sessions/{id}/files` (authz + path resolution + denylist), plus "Open on GitHub" at the pinned SHA.
    - Clicking a tool dot shows args and full output.
  - **Verify:** `uv run pytest BT/test_api_files.py` (escape, denylist, stranger 404; route in sweep) and `npm test` (panel render)
  - **Files:** `B/api/files.py`, `F/panel/waypoint-panel.tsx`, `F/panel/code-viewer.tsx`, `F/panel/tool-panel.tsx`, `BT/test_api_files.py`
  - **Deps:** T26 · **Size:** M

### Checkpoint C
- [ ] CI green (backend + frontend)
- [ ] **Real GitHub + real Gemini:** a new user signs in, installs the App on one repo, acknowledges the notice if private, and the Overview arrives (summary ≤150 words) — success criterion 1
- [ ] Human: review the readability and tone of 2–3 real waypoints; adjust `prompts/system.md` if needed

---

## Phase 4: Steering and branching

- [ ] **T10: Steering wheel component**
  - **Acceptance:**
    - Pure `angleToWedge(dx, dy, n)` and a `useHoldGesture` hook: hold ≥300 ms with <6 px movement opens the wheel; Esc or release in the centre cancels.
    - SVG wheel, ≤8 wedges: fixed verbs + model moves (trimmed first) + conditional "Allow blocked…".
    - "Custom…" opens an inline text box.
    - Keyboard path: focus a node, Space/Enter opens, arrows move, Enter confirms, Esc cancels.
  - **Verify:** `npm test` (angle mapping, hold threshold, cancel, trimming, keyboard)
  - **Files:** `F/wheel/wheel.tsx`, `F/wheel/use-hold-gesture.ts`, `F/wheel/wedges.ts`, `frontend/tests/wheel.test.ts`
  - **Deps:** T8 · **Size:** M

- [ ] **T11: Steering wired + branching E2E**
  - **Acceptance:**
    - Holding a waypoint opens the wheel; releasing on a wedge POSTs `/steer` and creates exactly one child, which streams in. Fixed verbs map to instruction templates. Selecting a node highlights its root path and dims the rest. The steered node pulses while running.
    - `FE/branching.spec.ts` (uses T38's fixtures): first / second-a / second-b; each route dot appears within 1 s of its `tool_done` timestamp (success criterion 6); after a reload, after `backend.restart()`, and in a second browser context signed in as the same user, the tree is identical (success criterion 13).
  - **Verify:** `npm test` (path-highlight unit test) and `npm run test:e2e`; CI E2E job green.
  - **Files:** `F/graph/path-highlight.ts`, `F/app/session-page.tsx`, `frontend/tests/path-highlight.test.ts`, `FE/branching.spec.ts`
  - **Deps:** T9, T10 · **Size:** M

### Checkpoint D
- [ ] The E2E branching scenario is green in CI
- [ ] Human: try the wheel by hand. Hold time right? Wedges big enough? Layout readable after ~15 nodes?

---

## Phase 5: Sandbox, shell and command modes

- [ ] **T24: Sandbox runner + image + sandbox tests**
  - **Acceptance:**
    - `ahoy-runner`: Unix-socket server with `start_step(step_id, snapshot_id, mode)`, `run(step_id, argv | shell_cmd, timeout_s)`, `end_step(step_id)`; drives the `docker` CLI via `create_subprocess_exec`.
    - Container flags exactly as SPEC → Sandbox. Auto mode: `--tmpfs /work:rw,exec,size=512m,uid=65534,gid=65534`, `/repo` copied in at start, then `chmod -R u+w /work`; commands run there. Step container killed after 10 min.
    - Runtime from `AHOY_SANDBOX_RUNTIME` (`runsc` | `runc`); refuses `runc` when `AHOY_ENV=prod`. Re-checks the denylist on visible path arguments.
    - `runner/Dockerfile`: pinned base, git, ripgrep, coreutils, python3, node; system gitconfig `safe.directory=*`.
    - CI gains a `sandbox` job on Ubuntu that installs `runsc`, registers it with Docker and runs `-m sandbox` with `AHOY_SANDBOX_RUNTIME=runsc`.
    - E2E: T38's `globalSetup` also starts `ahoy-runner` and waits until its Unix socket accepts a connection (it has no HTTP port); the CI E2E job builds the sandbox image and uses `runc`.
  - **Verify:** `uv run pytest BT/test_sandbox.py -m sandbox` locally (runc): no network (curl + DNS), env is only PATH/HOME/LANG, no host env via `/proc`, `git log` works, `/repo` not writable in any mode, Auto mode can create a file inside a copied subdirectory and run a copied `./script.sh`, Auto writes in `/work` vanish, other snapshots invisible, fork bomb and memory hog killed. Same suite green on `runsc` in CI.
  - **Files:** `B/runner/server.py`, `B/runner/docker.py`, `B/sandbox/runner_client.py`, `runner/Dockerfile`, `BT/test_sandbox.py` (wiring: `ahoy-runner` script entry in `pyproject.toml`, CI job, `FE/global-setup.ts`)
  - **Deps:** T1, T32, T38 · **Size:** M

- [ ] **T13: Shell tool via runner + block-and-continue** *(v4: local subprocess + env scrub)*
  - **Acceptance:**
    - `shell` passes the gate decision to the runner: argv for Safe and pre-authorized commands, `/bin/sh -c` for Auto. 30 s timeout; output to the model capped at 300 lines / 24 KB; full output (≤1 MB) in the node.
    - A blocked command inserts a `blocked` node, the model gets "blocked by captain's policy; continue without it", and the step still ends in a waypoint. Mode, provider, model and `steered_by` are on the `steps` row.
    - Runner failure, or a command killed by limits, → the step still ends in a node, never hangs. `end_step` is always called.
  - **Verify:** `uv run pytest BT/test_shell.py BT/test_loop.py` (+ `-m sandbox` case for the limits kill)
  - **Files:** `B/tools/shell.py`, `B/harness/loop.py`, `BT/test_shell.py`
  - **Deps:** T6, T12, T24 · **Size:** M

- [ ] **T14: Settings API + settings page + mode UI**
  - **Acceptance:**
    - `B/api/me.py` takes over `/api/me`, the notice ack route, and adds `GET`/`PUT /api/me/settings`: mode, allowlist, model id from `ALLOWED_MODELS`; never a key. Default Safe. Only the caller's future steps are affected.
    - Settings page: mode, allowlist editor, model picker.
    - Top bar: `owner/repo @ sha`, mode switch (Shift+Tab), red Auto badge, always-on "Remote: Gemini" badge. Blocked dots grey; waypoints show "N blocked".
  - **Verify:** `uv run pytest BT/test_api_settings.py`, `npm test`, `FE/modes.spec.ts`
  - **Files:** `B/api/me.py`, `F/settings/settings-page.tsx`, `F/app/top-bar.tsx`, `BT/test_api_settings.py`, `FE/modes.spec.ts`
  - **Deps:** T11, T13 · **Size:** M

- [ ] **T15: "Allow blocked…" escalation**
  - **Acceptance:**
    - The wedge appears only on waypoints whose step had blocked commands, only for the owner; picking a command sends `allow_command`.
    - The child step runs exactly that string once in the sandbox, with `approved_by=<owner id>`. A second identical call in the same step is blocked.
  - **Verify:** `uv run pytest BT/test_escalation.py` and `FE/blocked.spec.ts`
  - **Files:** `B/api/steer.py`, `B/harness/loop.py`, `F/wheel/wedges.ts`, `BT/test_escalation.py`, `FE/blocked.spec.ts`
  - **Deps:** T14 · **Size:** M

### Checkpoint E
- [ ] Success criteria 4 and 5 pass locally and in CI
- [ ] Success criterion 11 passes on `runsc` in the CI sandbox job
- [ ] Manual: in Safe mode, ask "run the tests" on the fixture repo; it is blocked, then "Allow blocked…" runs it in the sandbox

---

## Phase 6: Multi-user

- [ ] **T27: Sharing + viewer mode**
  - **Acceptance:**
    - `PUT /api/sessions/{id}/sharing {shared}` (owner only). Turning sharing off calls `close_session_streams` so viewers' SSE closes immediately, and their next request gets 404.
    - Owner UI: share toggle and the session URL with a Copy button.
    - Viewer UI: "View only" badge; no wheel, mode switch, share toggle or "Allow blocked…".
    - E2E: alice shares `fixture/demo`; bob views read-only and gets 403 on steer; mallory gets "Not found"; alice unshares and bob's open view closes.
  - **Verify:** `uv run pytest BT/test_sharing.py BT/test_authz_sweep.py`, `npm test` (viewer mode hides controls), `FE/sharing.spec.ts`
  - **Files:** `B/api/sharing.py`, `F/app/share-control.tsx`, `F/app/viewer-mode.tsx`, `BT/test_sharing.py`, `FE/sharing.spec.ts`
  - **Deps:** T7, T22, T26 · **Size:** M

- [ ] **T28: Limits and step queue**
  - **Acceptance:**
    - Per user: ≤`USER_MAX_RUNNING_STEPS` (2), ≤`USER_MAX_DAILY_STEPS` (200, counted from `steps` rows since 00:00 UTC), ≤`USER_MAX_SESSIONS` (50, checked as SPEC → Creating a session, step 3). Over quota → 429 with a message before any session, step or node is created.
    - Global `MAX_CONCURRENT_STEPS` (8): extra steps stay `queued`, start in FIFO order, emit `queued` (the "Queued" UI already exists from T34).
    - `/api/me` reports quota left. (Per-IP `/auth/*` limiting is in the Caddyfile, T18.)
  - **Verify:** `uv run pytest BT/test_limits.py`
  - **Files:** `B/harness/limits.py`, `B/api/steer.py`, `B/api/sessions.py`, `B/api/me.py`, `BT/test_limits.py`
  - **Deps:** T7, T14 · **Size:** M

- [ ] **T29: Session and account deletion**
  - **Acceptance:**
    - `DELETE /api/sessions/{id}` (owner, no repo check) purges via `purge_sessions()` and closes its SSE streams.
    - `GET /api/me/inaccessible-sessions` lists own sessions hidden by lost access; Settings shows them with Delete.
    - `DELETE /api/me {confirm_login}` purges all sessions, tokens, auth sessions and settings, and best-effort revokes the grant (refresh first; failure logged without the token). Wrong `confirm_login` → 400.
  - **Verify:** `uv run pytest BT/test_account.py` and `npm test`
  - **Files:** `B/api/me.py`, `B/api/sessions.py`, `F/settings/danger-zone.tsx`, `BT/test_account.py` (wiring: mount `danger-zone` in `F/settings/settings-page.tsx`)
  - **Deps:** T14, T27 (both touch `sessions.py`/`me.py`) · **Size:** M

### Checkpoint F
- [ ] Success criteria 8 and 9 pass in CI
- [ ] Human: two real GitHub accounts; share a session; remove the second account's repo access on GitHub; the session disappears for them within 5 minutes

---

## Phase 7: Quality and finish

- [ ] **T16: Eval harness**
  - **Acceptance:** `uv run ahoy-eval --model gemini/<id>` runs a fixed 5-step steer script on T31's fixture directly against the harness (no auth, local snapshot) and reports: valid JSON %, valid code-ref %, facts recalled, mean Flesch reading ease, mean summary word count.
  - **Verify:** `uv run pytest BT/test_eval_scorer.py` (scorer on canned outputs) and one manual real run
  - **Files:** `B/evals/run.py`, `B/evals/score.py`, `BT/test_eval_scorer.py`
  - **Deps:** T6, T31 · **Size:** M

- [ ] **T17: Prompt tuning to targets**
  - **Acceptance:** the Gemini eval meets success criterion 12 (≥95% valid JSON, ≥90% valid code refs, ≥8 of 10 facts). Each prompt change is noted in the commit message with before/after scores.
  - **Verify:** `uv run ahoy-eval --model gemini/<id>`
  - **Files:** `B/harness/prompts/system.md`, maybe `B/harness/context.py`
  - **Deps:** T16 · **Size:** S

- [ ] **T30: Secret-leak scan**
  - **Acceptance:** the E2E run uses canary values for every secret; `globalTeardown` dumps DB rows (as `ahoy_owner`), backend and runner logs, recorded SSE payloads and API bodies, and a sandbox `env` capture; `test_secret_scan.py` finds none of: canaries, `gh[pousr]_`/`ghu_`/`ghr_` patterns, cookie values (success criterion 10).
  - **Verify:** `npm run test:e2e && uv run pytest BT/test_secret_scan.py`
  - **Files:** `BT/test_secret_scan.py`, `FE/global-teardown.ts` (wiring: `globalTeardown` in `playwright.config.ts`)
  - **Deps:** T27, T28, T29 · **Size:** S

- [ ] **T18: Deploy artefacts + full E2E + README**
  - **Acceptance:**
    - After `npm run build`, FastAPI serves UI + API from one origin.
    - `backend/Dockerfile`; `deploy/Caddyfile` (TLS, HSTS, `/auth/*` rate limit 20/min per IP); a VM deploy note (Docker + gVisor install, shared `REPO_CACHE_DIR` path, bootstrap SQL, migrations as `ahoy_owner`).
    - The full Playwright suite (signin, branching, modes, blocked, sharing) is green in CI.
    - CI enforces `--cov-fail-under=85` for `ahoy`.
    - README: setup, dev GitHub App registration, `.env`, commands, modes, sharing, privacy note.
  - **Verify:** `npm run build && uv run ahoy serve --provider mock --github mock`, open the app; CI green with coverage gate.
  - **Files:** `backend/Dockerfile`, `deploy/Caddyfile`, `deploy/README.md`, `README.md`
  - **Deps:** T15, T17, T30 · **Size:** M

### Checkpoint G — Complete
- [ ] Success criteria 2–11, 13 and 14 pass in CI; 1 (Checkpoint C) and 12 (T17) were checked by hand and are re-checked once on the release build
- [ ] Human review before any further scope (Ollama, file edits, BYO key, workspaces)

# Build status

What was actually built, and how it differs from `SPEC.md` and `todo.md`. Read this before you start a task.

## Phase 1: Foundation (backend). Built and reviewed 2026-11-25

Done and reviewed: T33, T1, T31, T37, T12, T3, T19, T4, T36. Gate on 2026-11-25: 621 passed, 8 skipped; ruff, ruff format, mypy and `alembic check` clean.

Still open:
- **T2:** the code and tests are done. The real `--probe` has not run yet because `GEMINI_API_KEY` is not set. It decides whether we keep the union or the flat format (`DEFAULT_WIRE` in `providers/gemini.py`).
- **T32:** the CI workflow is written and reviewed. It has not run, because the repo has no GitHub remote yet.

### Decisions and deviations

**Ownership and fixture**
- Subagents write all the Python, T5, T6 and T12 included (Open Question 1 → (a)).
- The fixture is a curated 58-file subset of speak-lab: TypeScript, tests run with `node --test`, no npm install. The sandbox image (T24) therefore needs Node 26, which is a sandbox-image change to confirm at T24. The denylist bait files (`.env`, `config/dev.pem`, `deploy/id_ed25519`, fake canaries) are not in the repo: `make_fixture_git.py` writes them from constants into the generated repo's first commit (user decision, 2026-11-25).

**Config**
- Settings load the repo-root `.env` first, then `backend/.env`, which overrides it.
- `AHOY_ENV` defaults to **`prod`** as a fail-safe. Unset means an https origin is required and the mock is refused.
- Some limit defaults were corrected to SPEC's values: cache 20 GB, git depth 20, 8 concurrent steps, 2 running steps per user.

**Provider (T2)**
- The SDK's `response_schema` rejects the discriminated union, and it rejects `dict[str, Any]` args in Developer API mode. So union mode sends the schema through `response_json_schema`, and flat mode uses `response_schema`. Both sit behind one `wire` switch.
- A tool turn without `why` is rejected.
- Each call has a 60 s HTTP timeout.
- Provider errors are raised `from None`, so the SDK cause, which holds the key in its headers, never ends up in the traceback.

**Path checks and read-only tools (T3)**
- **Denylist and `.git`:** the denylist match ignores case on every path component. A path containing a `.git` component is refused with the reason `git_dir`.
- **`read_file`:**
  - It returns at most 64 KiB, clips lines longer than 2000 characters, and counts them in `clipped_lines`.
  - A NUL byte anywhere in the file means binary, and the file is refused.
- **Search:**
  - The match is a literal string and case-sensitive.
  - rg runs with `--json`, so its binary-file warning lines can't corrupt the parse.
  - A hit is kept only if its file is a regular file and not binary.
  - Hits whose line text is not valid UTF-8 are dropped by rg. The Python fallback keeps them with replacement characters. This difference is accepted.
- **Calling convention:** the tools are synchronous, so T6 calls them through `asyncio.to_thread`. They raise `PathError` or `ToolError`.

**Gate (T12)**, beyond SPEC's flag table. Every item here fails closed.
- In Safe mode, git runs only `status`, `log`, `diff`, `show` and `blame`, even when the allowlist contains bare `git`.
- **Excludes:** 8 exclude pathspecs (`:(exclude,icase,glob)**/<p>` and `**/<p>/**`) are always appended to log, diff, show and status, even after the model's own `--`.
  - Cost: `git log --follow`, `git log -L`, `diff --no-index` and `git log f.py` without `--` all fail.
- **Extra blocks:**
  - rg `--hostname-bin` and tree `-R`
  - `--full-diff`
  - any git global option
  - abbreviated long options
  - a lone `&`, CR and NUL
- **Pre-authorization** works only in Safe mode, runs as `shell_cmd`, and never lifts the denylist. The runner (T24) must therefore accept `shell_cmd` in Safe mode.
- **Known gaps, accepted by SPEC:** Auto-mode names built at runtime, and history reads by raw blob hash.
- **Note for T24:** the image must not contain `gpg`, because `--show-signature` makes git run it.

**Domain models (T37)**
- `GraphNode` gained `error: str | None`, with the user's approval; SPEC was updated. A validator enforces two rules:
  - the root node, and only the root, has `step_id` None;
  - each node kind carries exactly its own payload.
- `REPLAYABLE_EVENT_TYPES` lists the node-bearing events: tool_done, blocked, waypoint and error.
- There are two steer models: `SteerRequest` is the API body and `Steer` is embedded in a node.
- T7 decides which `seq` the initial `status` event gets.

**GitHub clients and mock (T19)**
- Git fetches send `Authorization: Basic base64(x-access-token:<token>)` (SPEC updated). This has not been tried against real GitHub; check it at Checkpoint B.
- **Repos are id-based:** `RepoTarget` holds only `(id, installation_id)`. Each operation calls `GET /repositories/{id}` first and builds its URLs from the answer.
- **Installation tokens:** the reply must list exactly `[repo_id]`, or the token is refused.
- **Snapshots:**
  - Git runs with an allowlist environment, and hooks and fsmonitor are off.
  - Denylisted files and directories are deleted from the working tree and marked `skip-worktree`.
  - The tree is made read-only in Python.
  - The blobs stay in `.git/objects`. SPEC treats history as a guard, not a boundary.
- Rate limiting raises an error; it is never reported as "no access".
- Mock hooks: `revoke_access`/`restore_access`, `remove_from_installation`/`add_to_installation`, `rename_repo`, `set_tree_truncated`, `issue_code`.

**Database (T4)**
- **Column-level UPDATE grants** for `ahoy_app` (SPEC roles table updated). There is no UPDATE on `webhook_deliveries`.
- **No ON DELETE CASCADE** into sessions, steps or graph_nodes. Account deletion (T29) must call `purge_sessions` before it deletes the user.
- **Constraints:**
  - composite FKs keep a node's parent and step inside the same session;
  - a partial unique index allows one root per session;
  - CHECKs mirror the GraphNode validator;
  - JSONB payloads must be objects.
- **`purge_sessions`** first locks the rows with `FOR UPDATE`, which waits for running inserts. That needs `UPDATE(status)` on sessions for `ahoy_purge`. Its search path is `pg_catalog, public, pg_temp`.
- **New SPEC columns:** `github_tokens` gained `*_nonce` columns (12 bytes). `auth_sessions.id_hash` must be 32 bytes.
- **Connections:** PUBLIC CONNECT is revoked, and `ahoy_owner` has CREATEDB so the test conftest can recreate `ahoy_test`.
- **Migrations:** CHECK names need `op.f()` in later migrations. `env.py` reads only `MIGRATION_DATABASE_URL`.

**Store API and test conftest (T36)**
- `create_session` inserts the root node in the same transaction, and `append_node` refuses root nodes.
- Every timestamp comes from the database clock (`clock_timestamp()`).
- `start_step` moves a step from queued to running. `append_node` requires the step to be running.
- The conftest sets `AHOY_ENV=test` and rebuilds `ahoy_test` for each test session. Each test runs in a savepoint that is rolled back.
- DB tests skip when Postgres is down, unless `AHOY_REQUIRE_DB=1` is set (CI sets it).
- **Lock order:** every write locks the session row (`FOR KEY SHARE`) before the step. This removed a purge/write deadlock. `purge()` retries once on 40P01.
- **Branch rule:** a node's parent must be the step's `parent_node_id` (the root, for the Overview step) or a node of the same step.
- **Graph shape:** `CHECK parent_id IS DISTINCT FROM id`. `root_path` uses a `CYCLE` clause and raises on a cycle. `uq_steps_one_overview` allows one parentless step per session.
- **Transactions:** every store call commits, reads included. T6 must not keep other writes pending on a session it passes to the store.
- **Return shapes:** `create_session` returns `(Session, root GraphNode)` and takes `model=`. `close_step` and `mark_interrupted` return `(Step, GraphNode)`.

### Follow-ups for later phases
- When `AHOY_ENV` is unset, the CLI shows the raw pydantic error. Add a hint: "set AHOY_ENV=dev".
- Optional: bump the actions in CI (checkout v7, setup-node v7, setup-uv v10).
- The Unicode-whitespace gap in the DB `error` CHECK is accepted; Pydantic catches it first.

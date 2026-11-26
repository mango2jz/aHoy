"""FastAPI app factory."""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from ahoy.api import events, files, me, repos, steer
from ahoy.api import sessions as api_sessions
from ahoy.auth import crypto, github_oauth, mock_login, sessions
from ahoy.auth.sessions import AuthState
from ahoy.authz import AccessCache
from ahoy.config import ConfigError, GithubName, ProviderName, Settings
from ahoy.github.app_client import GitHubApp, HttpGitHubApp
from ahoy.github.http import GitHubHTTP
from ahoy.github.mock import MockGitHub
from ahoy.github.repo_cache import RepoCache
from ahoy.github.user_client import GitHubUser, HttpGitHubUser
from ahoy.github.webhooks import router as webhooks_router
from ahoy.harness.events import EventBus
from ahoy.harness.runner import ProviderFor, StepRunner
from ahoy.harness.schemas import Step
from ahoy.logging import install_logging
from ahoy.providers.base import Provider
from ahoy.providers.gemini import GeminiProvider
from ahoy.providers.mock import MockProvider, demo_script
from ahoy.security import AhoyApp
from ahoy.store.db import make_engine, make_sessionmaker

SHUTDOWN_DRAIN_S = 10.0


def create_app(settings: Settings, provider_name: ProviderName, github_name: GithubName) -> FastAPI:
    settings.validate_for(provider_name, github_name)
    model = "mock" if provider_name == "mock" else settings.gemini_model

    if settings.token_encryption_key is not None:
        crypto.load_key(settings.token_encryption_key)  # malformed -> ConfigError now
        token_key = settings.token_encryption_key
    else:
        # Only reachable with --github mock (validate_for requires it otherwise): an
        # ephemeral key, so mock sign-ins do not survive a restart.
        token_key = crypto.generate_key()

    http: GitHubHTTP | None = None
    github_user: GitHubUser
    github_app: GitHubApp
    if github_name == "mock":
        github_user = github_app = MockGitHub(ahoy_env=settings.ahoy_env)
    else:
        secret = settings.github_client_secret
        if secret is None:  # validate_for already refuses this
            raise ConfigError("Missing required setting GITHUB_CLIENT_SECRET")
        app_key = settings.github_app_private_key_value()
        if app_key is None:  # validate_for already refuses this
            raise ConfigError("Missing required setting GITHUB_APP_PRIVATE_KEY")
        http = GitHubHTTP()
        github_user = HttpGitHubUser(
            http, client_id=settings.github_client_id or "", client_secret=secret
        )
        github_app = HttpGitHubApp(http, app_id=settings.github_app_id or "", private_key=app_key)

    engine = make_engine(settings.database_url) if settings.database_url is not None else None

    @asynccontextmanager
    async def lifespan(running: FastAPI) -> AsyncIterator[None]:
        install_logging(settings)  # again: uvicorn has replaced its handlers by now
        preparer: api_sessions.Preparer = running.state.preparer
        await preparer.cache.start()
        await preparer.recover()  # preparations of a previous process never finish
        runner: StepRunner = running.state.runner
        await runner.recover()  # so are its steps: each gets an interrupted error node
        yield
        await preparer.shutdown(SHUTDOWN_DRAIN_S)  # first: no new Overview after this
        await runner.shutdown()  # each running step closes with an error node
        await preparer.cache.drain()
        # Let shielded token refreshes save their rotated pair before the engine goes.
        pending = set(running.state.auth.tasks)
        if pending:
            await asyncio.wait(pending, timeout=SHUTDOWN_DRAIN_S)
        if engine is not None:
            await engine.dispose()
        if http is not None:
            await http.aclose()

    install_logging(settings)
    app = AhoyApp(title="aHoy", lifespan=lifespan)
    app.ahoy_settings = settings  # CSRF, security headers, trusted proxies (security.py)
    app.state.github_user = github_user
    app.state.github_app = github_app
    auth = AuthState(
        settings=settings,
        key=token_key,
        github=github_user,
        default_model=model,
        sessionmaker=make_sessionmaker(engine) if engine is not None else None,
    )
    # auth.now reads auth.clock on every call, so a test clock installed later applies.
    auth.access = AccessCache(github_user, github_app, clock=auth.now)
    app.state.auth = auth
    app.state.repo_list = repos.RepoListCache(github_user)
    app.state.repo_cache = RepoCache(
        Path(settings.repo_cache_dir), int(settings.repo_cache_max_gb * 1024**3)
    )
    preparer = api_sessions.Preparer(
        auth, github_app, app.state.repo_cache, settings.git_history_depth
    )
    app.state.preparer = preparer
    app.state.bus = EventBus()
    app.state.sse_recheck_s = events.RECHECK_S
    app.state.runner = preparer.runner = StepRunner(
        sessionmaker=lambda: auth.sessionmaker,
        bus=app.state.bus,
        cache=app.state.repo_cache,
        snapshot_fetch=preparer.snapshot_fetch,
        provider_for=_provider_for(provider_name, settings),
        provider_name=provider_name,
    )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"provider": provider_name, "model": model, "github": github_name}

    app.include_router(sessions.router)
    app.include_router(me.router)
    app.include_router(repos.router)
    app.include_router(api_sessions.router)
    app.include_router(files.router)
    app.include_router(steer.router)
    app.include_router(events.router)
    app.include_router(github_oauth.router)
    app.include_router(webhooks_router)
    if github_name == "mock" and settings.ahoy_env in ("dev", "test"):
        app.include_router(mock_login.router)
    return app


def _provider_for(name: ProviderName, settings: Settings) -> ProviderFor:
    """Each step's provider: a fresh scripted mock, or one shared Gemini client."""
    if name == "mock":
        return lambda _step: MockProvider(demo_script())
    shared: list[Provider] = []

    def gemini(_step: Step) -> Provider:
        if not shared:  # built on first use: no client for an app that never steers
            shared.append(GeminiProvider(settings))
        return shared[0]

    return gemini


def create_app_from_env() -> FastAPI:
    """Factory for `uvicorn --reload`; the CLI passes choices via env."""
    provider: ProviderName = "mock" if os.environ.get("AHOY_PROVIDER") == "mock" else "gemini"
    github: GithubName = "mock" if os.environ.get("AHOY_GITHUB") == "mock" else "real"
    return create_app(Settings(), provider, github)

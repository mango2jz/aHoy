"""FastAPI app factory."""

import os

from fastapi import FastAPI

from ahoy.config import GithubName, ProviderName, Settings


def create_app(settings: Settings, provider_name: ProviderName, github_name: GithubName) -> FastAPI:
    settings.validate_for(provider_name, github_name)
    app = FastAPI(title="aHoy")
    model = "mock" if provider_name == "mock" else settings.gemini_model

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"provider": provider_name, "model": model, "github": github_name}

    return app


def create_app_from_env() -> FastAPI:
    """Factory for `uvicorn --reload`; the CLI passes choices via env."""
    provider: ProviderName = "mock" if os.environ.get("AHOY_PROVIDER") == "mock" else "gemini"
    github: GithubName = "mock" if os.environ.get("AHOY_GITHUB") == "mock" else "real"
    return create_app(Settings(), provider, github)

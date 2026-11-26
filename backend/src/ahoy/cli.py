"""`ahoy` console script."""

import argparse
import os
import sys
from collections.abc import Sequence

import uvicorn
from pydantic import ValidationError

from ahoy.app import create_app
from ahoy.config import ConfigError, GithubName, ProviderName, Settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ahoy")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the API server")
    serve.add_argument("--port", type=int, default=4317)
    serve.add_argument("--provider", choices=["gemini", "mock"], default="gemini")
    serve.add_argument("--github", choices=["real", "mock"], default="real")
    serve.add_argument("--reload", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    provider: ProviderName = args.provider
    github: GithubName = args.github
    try:
        settings = Settings()
        settings.validate_for(provider, github)
    except (ConfigError, ValidationError) as exc:
        print(f"ahoy: {exc}", file=sys.stderr)
        if "AHOY_ENV" not in os.environ:
            print(
                "ahoy: hint: AHOY_ENV is unset, so it defaults to prod. "
                "For local development set AHOY_ENV=dev.",
                file=sys.stderr,
            )
        return 2
    host = "127.0.0.1"
    # The app handles X-Forwarded-* itself (TRUSTED_PROXY_IPS) and writes its own request log
    # (no query strings), so uvicorn's proxy-header trust and access log stay off.
    if args.reload:
        os.environ["AHOY_PROVIDER"] = provider
        os.environ["AHOY_GITHUB"] = github
        uvicorn.run(
            "ahoy.app:create_app_from_env",
            factory=True,
            host=host,
            port=args.port,
            reload=True,
            proxy_headers=False,
            access_log=False,
        )
    else:
        uvicorn.run(
            create_app(settings, provider, github),
            host=host,
            port=args.port,
            proxy_headers=False,
            access_log=False,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

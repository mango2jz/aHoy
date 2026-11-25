"""Gemini provider (google-genai) with JSON-constrained output.

Two wire shapes sit behind one switch until the T2 spike is run against real Gemini:
- "union": ask for `Turn` itself via `response_json_schema` (JSON Schema with `oneOf`).
  The SDK's `response_schema` cannot carry it: it rejects `oneOf`/`discriminator` and
  `additionalProperties` client-side in Developer API mode.
- "flat": ask for `FlatTurn` via `response_schema` and convert it to `Turn` in Python.

Run `python -m ahoy.providers.gemini --probe [--wire flat] [--record DIR]` to check.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

import httpx
from google import genai
from google.genai import errors, types
from pydantic import ValidationError

from ahoy.config import Settings
from ahoy.harness.schemas import (
    AnswerTurn,
    AnyTurn,
    FlatTurn,
    ToolTurn,
    TurnConversionError,
    TurnSchema,
    turn_adapter,
)
from ahoy.providers.base import Message, ProviderError

WireMode = Literal["union", "flat"]
DEFAULT_WIRE: WireMode = "union"
GEMINI_TIMEOUT_S = 60


def build_config(
    schema: TurnSchema, wire: WireMode, system: str | None
) -> types.GenerateContentConfig:
    if wire == "union":
        return types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=schema.model_json_schema(),
        )
    return types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_schema=FlatTurn if schema is AnyTurn else AnswerTurn,
    )


def to_contents(messages: list[Message]) -> tuple[str | None, list[types.Content]]:
    system = [m.text for m in messages if m.role == "system"]
    contents = [
        types.Content(
            role="model" if m.role == "assistant" else "user", parts=[types.Part(text=m.text)]
        )
        for m in messages
        if m.role != "system"
    ]
    return ("\n\n".join(system) or None), contents


def parse_turn_text(text: str | None, schema: TurnSchema, wire: WireMode) -> ToolTurn | AnswerTurn:
    """Parse the model's JSON text into a `Turn`; any problem becomes a `ProviderError`."""
    if not text or not text.strip():
        raise ProviderError("gemini returned no text", raw=text)
    try:
        if schema is AnswerTurn:
            return AnswerTurn.model_validate_json(text)
        if wire == "union":
            return turn_adapter.validate_json(text)
        return FlatTurn.model_validate_json(text).to_turn()
    except (ValidationError, TurnConversionError) as exc:
        raise ProviderError(f"gemini output is not a valid turn: {exc}", raw=text) from exc


def parse_response(
    response: types.GenerateContentResponse, schema: TurnSchema, wire: WireMode
) -> ToolTurn | AnswerTurn:
    return parse_turn_text(response.text, schema, wire)


class GeminiProvider:
    name = "gemini"

    def __init__(self, settings: Settings, *, wire: WireMode = DEFAULT_WIRE) -> None:
        if settings.gemini_api_key is None or not settings.gemini_api_key.get_secret_value():
            raise ProviderError("GEMINI_API_KEY is not set")
        self._client = genai.Client(
            api_key=settings.gemini_api_key.get_secret_value(),
            http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_S * 1000),  # milliseconds
        )
        self.model = settings.gemini_model
        self.wire = wire
        self.last_response: types.GenerateContentResponse | None = None

    async def next_turn(self, messages: list[Message], schema: TurnSchema) -> ToolTurn | AnswerTurn:
        system, contents = to_contents(messages)
        try:
            response = await self._client.aio.models.generate_content(
                model=self.model,
                contents=contents,
                config=build_config(schema, self.wire, system),
            )
        except errors.APIError as exc:
            raise ProviderError(
                f"gemini API error {exc.code} {exc.status}: {exc.message}"
            ) from None
        except ValueError as exc:  # SDK-side schema rejection
            raise ProviderError(f"gemini request rejected: {exc}") from None
        except httpx.HTTPError as exc:  # transport: connect, timeout, protocol
            raise ProviderError(f"gemini transport error: {type(exc).__name__}: {exc}") from None
        self.last_response = response
        return parse_response(response, schema, self.wire)


_PROBE_SYSTEM = """You are a patient senior engineer exploring a codebase. Investigate with tools
until you can answer, then answer once. Reply with one JSON turn.
Tools and their args: list_dir {path, depth}, read_file {path, start_line},
search {query, glob}, shell {command}.
A tool turn is {"action": "tool", "tool": ..., "args"/fields..., "why": ...}.
An answer turn is {"action": "answer", "answer": {waypoint}}.
Cite code only for files you actually read."""

_PROBE_MAP = "Repo map:\nmain.py (12 lines)\nutil.py (30 lines)\nREADME.md (5 lines)"
_PROBE_FILE = (
    "read_file main.py:\n1: from util import greet\n2:\n3: def main() -> None:\n"
    '4:     print(greet("captain"))\n5:\n6: if __name__ == "__main__":\n7:     main()'
)


async def _probe(wire: WireMode, record: Path | None) -> int:
    settings = Settings()
    if settings.gemini_api_key is None or not settings.gemini_api_key.get_secret_value():
        print(
            "probe: GEMINI_API_KEY is not set (.env or environment); cannot probe", file=sys.stderr
        )
        return 2
    provider = GeminiProvider(settings, wire=wire)
    print(f"probe: model={provider.model} wire={wire}")
    messages = [
        Message(role="system", text=_PROBE_SYSTEM),
        Message(
            role="user",
            text=f"{_PROBE_MAP}\n\nInstruction: What does main.py do? "
            "You have not read any file yet.",
        ),
    ]
    kinds: list[str] = []
    for label in ("first", "second"):
        try:
            turn = await provider.next_turn(messages, AnyTurn)
        except ProviderError as exc:
            print(f"probe: {label} turn failed: {exc}", file=sys.stderr)
            return 1
        if record is not None and provider.last_response is not None:
            record.mkdir(parents=True, exist_ok=True)
            out = record / f"recorded_{wire}_{turn.action}.json"
            out.write_text(provider.last_response.model_dump_json(exclude_none=True, indent=2))
        print(f"probe: {label} turn ({turn.action}): {turn.model_dump_json()}")
        kinds.append(turn.action)
        messages.append(Message(role="assistant", text=turn.model_dump_json()))
        messages.append(Message(role="user", text=f"{_PROBE_FILE}\n\nYou have enough now; answer."))
    ok = kinds == ["tool", "answer"]
    print("probe: OK" if ok else f"probe: expected tool then answer, got {kinds}")
    return 0 if ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ahoy.providers.gemini")
    parser.add_argument("--probe", action="store_true", help="call real Gemini twice")
    parser.add_argument("--wire", choices=["union", "flat"], default=DEFAULT_WIRE)
    parser.add_argument("--record", type=Path, help="save raw responses as JSON here")
    args = parser.parse_args(argv)
    if not args.probe:
        parser.print_help()
        return 2
    return asyncio.run(_probe(args.wire, args.record))


if __name__ == "__main__":
    sys.exit(main())

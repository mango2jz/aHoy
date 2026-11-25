"""Providers: mock replay, Turn schemas, and Gemini parsing against synthetic fixtures.

The fixtures in fixtures/gemini/ are synthetic (hand-built in the google-genai response
shape), not recorded. No test calls the network.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from google import genai
from google.genai import types
from pydantic import SecretStr, ValidationError

from ahoy.config import Settings
from ahoy.harness.schemas import (
    AnswerTurn,
    AnyTurn,
    FlatTurn,
    ToolTurn,
    TurnConversionError,
    Waypoint,
    turn_adapter,
)
from ahoy.providers.base import Message, ProviderError
from ahoy.providers.gemini import (
    GEMINI_TIMEOUT_S,
    GeminiProvider,
    WireMode,
    build_config,
    parse_response,
    parse_turn_text,
    to_contents,
)
from ahoy.providers.mock import MockExhaustedError, MockProvider

FIXTURES = Path(__file__).parent / "fixtures" / "gemini"

WAYPOINT = Waypoint(
    title="t",
    summary="s",
    key_points=["a.", "b."],
    code_refs=[],
    next_moves=[],
    confidence="low",
)
TOOL = ToolTurn(action="tool", tool="list_dir", args={"path": "."}, why="look around")
ANSWER = AnswerTurn(action="answer", answer=WAYPOINT)


def load(name: str) -> types.GenerateContentResponse:
    body: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    body.pop("_synthetic")
    return types.GenerateContentResponse.model_validate(body)


# --- schemas ---------------------------------------------------------------


def test_turn_union_discriminates_on_action() -> None:
    assert isinstance(turn_adapter.validate_python(TOOL.model_dump()), ToolTurn)
    assert isinstance(turn_adapter.validate_python(ANSWER.model_dump()), AnswerTurn)
    with pytest.raises(ValidationError):
        turn_adapter.validate_python({"action": "ask", "question": "?"})


@pytest.mark.parametrize(
    ("flat", "expected"),
    [
        (
            {"action": "tool", "tool": "list_dir", "path": "src", "depth": 2, "why": "w"},
            {"path": "src", "depth": 2},
        ),
        ({"action": "tool", "tool": "read_file", "path": "a.py", "why": "w"}, {"path": "a.py"}),
        (
            {
                "action": "tool",
                "tool": "search",
                "query": "q",
                "glob": "*.py",
                "path": "x",
                "why": "w",
            },
            {"query": "q", "glob": "*.py"},
        ),
        ({"action": "tool", "tool": "shell", "command": "ls", "why": "w"}, {"command": "ls"}),
    ],
)
def test_flat_tool_turn_keeps_only_that_tools_args(
    flat: dict[str, Any], expected: dict[str, Any]
) -> None:
    turn = FlatTurn.model_validate(flat).to_turn()
    assert isinstance(turn, ToolTurn)
    assert turn.args == expected


@pytest.mark.parametrize(
    "flat",
    [
        {"action": "answer"},
        {"action": "tool", "why": "w"},
        {"action": "tool", "tool": "shell", "why": "w"},
        {"action": "tool", "tool": "shell", "command": "ls"},
    ],
)
def test_inconsistent_flat_turn_is_rejected(flat: dict[str, Any]) -> None:
    with pytest.raises(TurnConversionError):
        FlatTurn.model_validate(flat).to_turn()


# --- mock --------------------------------------------------------------------


def test_mock_replays_script_then_raises() -> None:
    mock = MockProvider([TOOL, ANSWER])
    msgs = [Message(role="user", text="go")]
    assert asyncio.run(mock.next_turn(msgs, AnyTurn)) == TOOL
    assert asyncio.run(mock.next_turn(msgs, AnyTurn)) == ANSWER
    with pytest.raises(MockExhaustedError):
        asyncio.run(mock.next_turn(msgs, AnyTurn))
    assert len(mock.calls) == 3


def test_mock_refuses_tool_turn_when_answer_forced() -> None:
    with pytest.raises(ProviderError):
        asyncio.run(MockProvider([TOOL]).next_turn([], AnswerTurn))


# --- gemini parsing (synthetic fixtures) -------------------------------------


@pytest.mark.parametrize(
    ("fixture", "wire", "kind"),
    [
        ("synthetic_union_tool", "union", ToolTurn),
        ("synthetic_union_answer", "union", AnswerTurn),
        ("synthetic_flat_tool", "flat", ToolTurn),
        ("synthetic_flat_answer", "flat", AnswerTurn),
    ],
)
def test_gemini_parses_fixture(fixture: str, wire: WireMode, kind: type) -> None:
    turn = parse_response(load(fixture), AnyTurn, wire)
    assert isinstance(turn, kind)
    if isinstance(turn, ToolTurn):
        assert turn.args == {"path": "main.py", "start_line": 1}


@pytest.mark.parametrize(
    ("fixture", "wire"),
    [
        ("synthetic_malformed", "union"),
        ("synthetic_malformed", "flat"),
        ("synthetic_wrong_shape", "union"),
        ("synthetic_flat_missing_arg", "flat"),
    ],
)
def test_gemini_bad_output_raises_provider_error(fixture: str, wire: WireMode) -> None:
    with pytest.raises(ProviderError) as info:
        parse_response(load(fixture), AnyTurn, wire)
    assert info.value.raw


def test_gemini_forced_answer_rejects_tool_turn() -> None:
    with pytest.raises(ProviderError):
        parse_response(load("synthetic_union_tool"), AnswerTurn, "union")


@pytest.mark.parametrize("text", [None, "", "  "])
def test_gemini_empty_text_raises(text: str | None) -> None:
    with pytest.raises(ProviderError):
        parse_turn_text(text, AnyTurn, "union")


def test_build_config_per_wire() -> None:
    union = build_config(AnyTurn, "union", "sys")
    assert union.response_json_schema == AnyTurn.model_json_schema()
    assert union.response_schema is None
    flat = build_config(AnyTurn, "flat", None)
    assert flat.response_schema is FlatTurn
    assert build_config(AnswerTurn, "flat", None).response_schema is AnswerTurn


def test_to_contents_maps_roles() -> None:
    system, contents = to_contents(
        [
            Message(role="system", text="rules"),
            Message(role="user", text="q"),
            Message(role="assistant", text="a"),
        ]
    )
    assert system == "rules"
    assert [c.role for c in contents] == ["user", "model"]


def test_gemini_client_has_http_timeout() -> None:
    provider = GeminiProvider(Settings(_env_file=None, gemini_api_key=SecretStr(CANARY_KEY)))
    assert provider._client._api_client._http_options.timeout == GEMINI_TIMEOUT_S * 1000


def test_gemini_provider_requires_key() -> None:
    with pytest.raises(ProviderError):
        GeminiProvider(Settings(_env_file=None, gemini_api_key=None))


CANARY_KEY = "CANARY-KEY-7f3e9a"


def _raise(exc: Exception) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


@pytest.mark.parametrize(
    "handler",
    [
        lambda r: httpx.Response(401, json={"error": {"code": 401, "message": "bad key"}}),
        lambda r: httpx.Response(502, text="<html>bad gateway</html>"),
        lambda r: httpx.Response(200, text="not json"),
        _raise(httpx.ConnectError("refused")),
        _raise(httpx.ReadTimeout("slow")),
    ],
)
def test_gemini_failures_are_provider_errors_without_the_key(handler: Any) -> None:
    """Every failure is a ProviderError and the API key never shows. No network: mock transport."""
    provider = GeminiProvider(Settings(_env_file=None, gemini_api_key=SecretStr(CANARY_KEY)))
    provider._client = genai.Client(
        api_key=CANARY_KEY,
        http_options=types.HttpOptions(
            async_client_args={"transport": httpx.MockTransport(handler)},
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )
    with pytest.raises(ProviderError) as info:
        asyncio.run(provider.next_turn([Message(role="user", text="hi")], AnyTurn))
    assert CANARY_KEY not in f"{info.value} {info.value!r} {info.value.raw} {provider!r}"
    assert info.value.__cause__ is None

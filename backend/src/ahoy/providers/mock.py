"""A provider that replays a scripted list of turns (tests, e2e, `--provider mock`)."""

from collections.abc import Iterable

from ahoy.harness.schemas import AnswerTurn, ToolTurn, TurnSchema
from ahoy.providers.base import Message, ProviderError


class MockExhaustedError(ProviderError):
    """The script has no turns left."""


class MockProvider:
    name = "mock"
    model = "mock"

    def __init__(self, turns: Iterable[ToolTurn | AnswerTurn]) -> None:
        self._turns = list(turns)
        self.calls: list[tuple[list[Message], TurnSchema]] = []

    async def next_turn(self, messages: list[Message], schema: TurnSchema) -> ToolTurn | AnswerTurn:
        self.calls.append((list(messages), schema))
        if not self._turns:
            raise MockExhaustedError("mock provider script exhausted")
        turn = self._turns.pop(0)
        if schema is AnswerTurn and not isinstance(turn, AnswerTurn):
            raise ProviderError("mock script has a tool turn where an answer was forced")
        return turn


def demo_script() -> list[ToolTurn | AnswerTurn]:
    """The script each step gets under `--provider mock` (dev, e2e): look, then answer."""
    return [
        ToolTurn(action="tool", tool="list_dir", args={"path": "."}, why="See the repo layout."),
        AnswerTurn.model_validate(
            {
                "action": "answer",
                "answer": {
                    "title": "Mock answer",
                    "summary": "This answer comes from the mock provider. No model was called.",
                    "key_points": ["The mock provider is scripted.", "It listed the repo root."],
                    "code_refs": [],
                    "next_moves": [{"label": "Deeper", "instruction": "Go one level deeper."}],
                    "confidence": "low",
                },
            }
        ),
    ]

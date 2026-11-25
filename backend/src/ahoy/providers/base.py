"""The provider protocol: one method that returns the model's next turn."""

from typing import Literal, Protocol

from pydantic import BaseModel

from ahoy.harness.schemas import AnswerTurn, ToolTurn, TurnSchema


class Message(BaseModel):
    """One context message. Provider-neutral roles; each provider maps them."""

    role: Literal["system", "user", "assistant"]
    text: str


class ProviderError(Exception):
    """The provider failed or returned output that is not a valid turn."""

    def __init__(self, message: str, *, raw: str | None = None) -> None:
        super().__init__(message)
        self.raw = raw


class Provider(Protocol):
    name: str
    model: str

    async def next_turn(self, messages: list[Message], schema: TurnSchema) -> ToolTurn | AnswerTurn:
        """Return the next turn. `schema` is `AnyTurn`, or `AnswerTurn` to force an answer."""
        ...

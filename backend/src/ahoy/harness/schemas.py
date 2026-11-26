"""Turn and Waypoint models (SPEC -> Agent step).

`Turn` is the public type the harness works with. Providers may ask the model for it
directly (union wire shape) or for `FlatTurn` and convert; see plan.md (T2 spike).
"""

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    GetJsonSchemaHandler,
    RootModel,
    StringConstraints,
    TypeAdapter,
    model_validator,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema, PydanticOmit

ToolName = Literal["list_dir", "read_file", "search", "shell"]


class ToolTurn(BaseModel):
    action: Literal["tool"]
    tool: ToolName
    args: dict[str, Any]
    why: str


class _HarnessSet:
    """Field marker: left out of the validation-mode JSON Schema, kept in serialization mode.

    The model is asked for turns with the validation-mode schema (`model_json_schema()`,
    which the Gemini provider uses for both wires), so it never sees a harness-set field;
    API responses (serialization mode) still carry it.
    """

    def __get_pydantic_json_schema__(
        self, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        if handler.mode == "validation":
            raise PydanticOmit
        return handler(core_schema)


class CodeRef(BaseModel):
    path: str
    start_line: int
    end_line: int
    note: str
    # Set by the harness after checking the ref against the snapshot (T6); never asked of
    # the model. False shows a warning badge.
    valid: Annotated[bool, _HarnessSet()] = True


class NextMove(BaseModel):
    label: str
    instruction: str


class Waypoint(BaseModel):
    title: str  # <= 8 words
    summary: str  # <= 150 words, plain language, markdown allowed
    key_points: list[str]  # 2-5
    code_refs: list[CodeRef]
    next_moves: list[NextMove]  # 0-4
    confidence: Literal["high", "medium", "low"]


class AnswerTurn(BaseModel):
    action: Literal["answer"]
    answer: Waypoint


Turn = Annotated[ToolTurn | AnswerTurn, Field(discriminator="action")]
turn_adapter: TypeAdapter[ToolTurn | AnswerTurn] = TypeAdapter(Turn)


class AnyTurn(RootModel[Turn]):
    """Request marker for "any turn" (a class, so it can be passed as `schema`)."""


class TurnConversionError(ValueError):
    """A flat turn is internally inconsistent (e.g. a tool turn without its required args)."""


# Which flat fields each tool takes, and which of them are required.
_TOOL_ARGS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "list_dir": (("path", "depth"), ("path",)),
    "read_file": (("path", "start_line"), ("path",)),
    "search": (("query", "glob"), ("query",)),
    "shell": (("command",), ("command",)),
}


class FlatTurn(BaseModel):
    """Flat wire shape: the fallback if the model API cannot take the `Turn` union.

    The google-genai SDK's `response_schema` (Gemini Developer API) rejects both the
    discriminated union (`oneOf` + `discriminator`) and the free-form
    `args: dict[str, Any]` (`additionalProperties`). Here tool args are typed optional
    fields and `to_turn()` builds the real `Turn`.

    A tool turn needs `tool`, `why` and that tool's required args (list_dir/read_file:
    `path`; search: `query`; shell: `command`), like union mode. `start_line` and `depth`
    stay optional and are left out of `args` when absent; the tool applies its default (1).
    """

    action: Literal["tool", "answer"]
    tool: ToolName | None = None
    why: str | None = None
    path: str | None = None
    start_line: int | None = None
    depth: int | None = None
    query: str | None = None
    glob: str | None = None
    command: str | None = None
    answer: Waypoint | None = None

    def to_turn(self) -> ToolTurn | AnswerTurn:
        if self.action == "answer":
            if self.answer is None:
                raise TurnConversionError("answer turn without an answer")
            return AnswerTurn(action="answer", answer=self.answer)
        if self.tool is None:
            raise TurnConversionError("tool turn without a tool")
        if self.why is None:
            raise TurnConversionError("tool turn without a why")
        allowed, required = _TOOL_ARGS[self.tool]
        args = {name: getattr(self, name) for name in allowed if getattr(self, name) is not None}
        missing = [name for name in required if name not in args]
        if missing:
            raise TurnConversionError(f"{self.tool} turn missing {', '.join(missing)}")
        return ToolTurn(action="tool", tool=self.tool, args=args, why=self.why)


# What a provider may be asked to return: any turn, or (runaway guard) an answer only.
TurnSchema = type[AnyTurn] | type[AnswerTurn]


# --- Domain and API models (SPEC -> Data model, API, Command modes) -------------------
# Timestamps are aware datetimes. GitHubTokens and AuthSession are DB-only (T4).

CommandMode = Literal["read_only", "safe", "auto"]
SessionStatus = Literal["preparing", "ready", "failed"]
StepStatus = Literal["queued", "running", "done", "error", "interrupted"]
NodeKind = Literal["root", "tool", "blocked", "waypoint", "error"]

# SPEC -> Tools: the default Safe-mode allowlist.
DEFAULT_SAFE_ALLOWLIST: tuple[str, ...] = (
    "ls",
    "cat",
    "head",
    "tail",
    "wc",
    "find",
    "rg",
    "grep",
    "tree",
    "git status",
    "git log",
    "git diff",
    "git show",
    "git blame",
)


class UserSettings(BaseModel):
    """Per-user settings: command mode, Safe-mode allowlist, model id (also the PUT body)."""

    model_config = ConfigDict(extra="forbid")

    mode: CommandMode = "safe"
    allowlist: list[str] = Field(default_factory=lambda: list(DEFAULT_SAFE_ALLOWLIST))
    model: str


class User(BaseModel):
    id: UUID
    github_id: int
    github_login: str
    avatar_url: str
    settings: UserSettings
    private_repo_notice_ack_at: AwareDatetime | None
    created_at: AwareDatetime


class Session(BaseModel):
    """A knowledge tree."""

    id: UUID
    owner_id: UUID
    installation_id: int
    repo_id: int
    repo_full_name: str
    ref: str
    commit_sha: str
    private: bool
    status: SessionStatus
    status_reason: str | None = None
    shared: bool = False
    created_at: AwareDatetime


class Step(BaseModel):
    """One steer is one step."""

    id: UUID
    session_id: UUID
    parent_node_id: UUID | None
    steered_by: UUID
    wedge: str
    instruction: str
    allow_command: str | None = None
    mode: CommandMode
    provider: str
    model: str
    status: StepStatus
    created_at: AwareDatetime
    started_at: AwareDatetime | None = None
    closed_at: AwareDatetime | None = None


class SteerRequest(BaseModel):
    """Body of POST /api/sessions/{id}/steer."""

    model_config = ConfigDict(extra="forbid")

    parent_id: UUID  # a waypoint or error node of the session
    wedge: str
    instruction: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
    ]
    allow_command: str | None = None  # stored on the step; used from T13 on


class Steer(BaseModel):
    """The steer recorded on a node: wedge, instruction, allow_command, steered_by."""

    model_config = ConfigDict(extra="forbid")

    wedge: str
    instruction: str
    allow_command: str | None = None
    steered_by: UUID


class ToolCall(BaseModel):
    name: ToolName
    args: dict[str, Any]
    output: str
    exit_code: int | None = None
    approved_by: UUID | None = None


class Blocked(BaseModel):
    command: str
    reason: str
    why: str


class GraphNode(BaseModel):
    id: UUID
    session_id: UUID
    step_id: UUID | None  # None only for the root node
    parent_id: UUID | None
    kind: NodeKind
    steer: Steer | None = None
    mode: CommandMode | None = None  # on waypoint/error nodes
    tool: ToolCall | None = None
    blocked: Blocked | None = None
    waypoint: Waypoint | None = None
    error: str | None = None  # error nodes only: short user-safe text, never a trace or secret
    model: str
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _check_kind_payload(self) -> "GraphNode":
        # step_id is None exactly for the root node.
        if (self.kind == "root") != (self.step_id is None):
            raise ValueError("step_id must be None exactly when kind is 'root'")
        payloads = {"tool": self.tool, "blocked": self.blocked, "waypoint": self.waypoint}
        # root and error nodes carry none of them.
        wanted = self.kind if self.kind in payloads else None
        for name, value in payloads.items():
            if name == wanted and value is None:
                raise ValueError(f"{self.kind} node requires '{name}'")
            if name != wanted and value is not None:
                raise ValueError(f"{self.kind} node must not have '{name}'")
        if self.kind == "error":
            if not self.error or not self.error.strip():
                raise ValueError("error node requires a non-empty 'error'")
        elif self.error is not None:
            raise ValueError(f"{self.kind} node must not have 'error'")
        return self


# SSE events (GET /api/steps/{step_id}/events). `seq` is per step and is the SSE `id`.
# `queued`, `tool_started` and `status` are transient: never replayed.


class _StepEvent(BaseModel):
    seq: int


class StatusEvent(_StepEvent):
    type: Literal["status"] = "status"
    status: StepStatus


class QueuedEvent(_StepEvent):
    type: Literal["queued"] = "queued"


class ToolStartedEvent(_StepEvent):
    type: Literal["tool_started"] = "tool_started"
    name: ToolName
    args: dict[str, Any]
    why: str


class ToolDoneEvent(_StepEvent):
    type: Literal["tool_done"] = "tool_done"
    node: GraphNode


class BlockedEvent(_StepEvent):
    type: Literal["blocked"] = "blocked"
    node: GraphNode


class WaypointEvent(_StepEvent):
    type: Literal["waypoint"] = "waypoint"
    node: GraphNode


class ErrorEvent(_StepEvent):
    type: Literal["error"] = "error"
    node: GraphNode


StepEvent = Annotated[
    StatusEvent
    | QueuedEvent
    | ToolStartedEvent
    | ToolDoneEvent
    | BlockedEvent
    | WaypointEvent
    | ErrorEvent,
    Field(discriminator="type"),
]
step_event_adapter: TypeAdapter[
    StatusEvent
    | QueuedEvent
    | ToolStartedEvent
    | ToolDoneEvent
    | BlockedEvent
    | WaypointEvent
    | ErrorEvent
] = TypeAdapter(StepEvent)

# The node-bearing events: persisted, so they are replayed after `Last-Event-ID`.
REPLAYABLE_EVENT_TYPES: frozenset[str] = frozenset({"tool_done", "blocked", "waypoint", "error"})

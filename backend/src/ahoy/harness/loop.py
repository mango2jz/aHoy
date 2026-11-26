"""The step loop (SPEC -> Agent step, steps 1-4): read-only tools, no pauses.

`run_step` drives one admitted step from `running` to exactly one final node (a waypoint
or an error node), written with `store.close_step` so the node and the step's closed
status land in one transaction. Every node is committed before its event is published.

Seq scheme and events: see `harness/events.py`. The loop counts its own persisted nodes;
the final node's seq is re-counted from the DB, because a cancellation can interrupt an
insert whose commit already happened.

Errors. Tool refusals (`PathError`, `ToolError`, bad arguments) are tool output for the
model, not step errors. Anything else (provider, store, GitHub, a bug) ends the step in an
error node with a short fixed text; exception text never reaches the node, since it can
carry secrets. Cancellation ends the step in an error node and is then re-raised.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ahoy.github.http import GitHubError
from ahoy.harness.context import ContextLimits, build_messages, load_system_prompt, repo_map
from ahoy.harness.events import EventBus, node_event, node_seq, tool_started_seq
from ahoy.harness.schemas import (
    AnswerTurn,
    AnyTurn,
    Blocked,
    CodeRef,
    CommandMode,
    GraphNode,
    Session,
    Steer,
    Step,
    StepStatus,
    ToolCall,
    ToolStartedEvent,
    ToolTurn,
    TurnSchema,
    Waypoint,
)
from ahoy.providers.base import Message, Provider, ProviderError
from ahoy.sandbox.paths import PathError, Snapshot
from ahoy.store import repo as store
from ahoy.store.repo import NotFoundError, StepClosedError, StepStateError
from ahoy.tools import ToolError
from ahoy.tools.fs import list_dir, read_file
from ahoy.tools.search import search

log = logging.getLogger("ahoy.loop")

MAX_TOOL_TURNS = 15
SHELL_UNAVAILABLE = "shell is not available yet"

# User-safe error texts (never exception text).
ERR_INVALID_TWICE: Final = "The model gave an invalid answer twice."
ERR_CANCELLED: Final = "The step was cancelled."
ERR_PROVIDER: Final = "The model provider failed. Try steering again."
ERR_GITHUB: Final = "GitHub could not be reached. Try steering again."
ERR_INTERNAL: Final = "The step failed because of an internal error."

FORCE_ANSWER_NOTE: Final = (
    "You have used all your tool calls for this step. Answer now with what you have; "
    "tools are no longer available."
)
RETRY_NOTE: Final = (
    "Your answer was not accepted:\n{problems}\n"
    "Answer again with a corrected, complete answer. Tools are no longer available."
)
RAW_ECHO_CHARS = 4000  # how much of a malformed reply is echoed back on retry

# Waypoint limits (SPEC -> Agent step, Waypoint comments).
TITLE_MAX_WORDS = 8
SUMMARY_MAX_WORDS = 150
KEY_POINTS_MAX = 5
NEXT_MOVES_MAX = 4

REPO_MAP_FORMAT_VERSION = 1
REPO_MAP_CACHE_SIZE = 32


@dataclass(frozen=True)
class StepContext:
    """Everything a step needs, captured at admission (T7). Never re-read mid-step."""

    step: Step
    session: Session
    mode: CommandMode  # owner's command mode at admission (no shell yet: unused)
    allowlist: tuple[str, ...]  # owner's Safe-mode allowlist at admission (unused yet)
    model: str
    provider: Provider
    snapshot_root: Path  # leased by the caller for the whole step
    limits: ContextLimits = field(default_factory=ContextLimits)
    max_tool_turns: int = MAX_TOOL_TURNS


# --- answer validation and code-ref checks ----------------------------------------------


def _words(text: str) -> int:
    return len(text.split())


def validate_waypoint(wp: Waypoint) -> list[str]:
    """Reasons to reject an answer that passed Pydantic: empty fields (SPEC: "check that it
    is not empty"). Limits are not rejections: see `normalize_waypoint`."""
    problems: list[str] = []
    if not wp.title.strip():
        problems.append("title is empty")
    if not wp.summary.strip():
        problems.append("summary is empty")
    if not wp.key_points:
        problems.append("key_points is empty; give 2 to 5 key points")
    if any(not p.strip() for p in wp.key_points):
        problems.append("a key point is empty")
    for i, move in enumerate(wp.next_moves, start=1):
        if not move.label.strip() or not move.instruction.strip():
            problems.append(f"next move {i} needs a label and an instruction")
    for i, ref in enumerate(wp.code_refs, start=1):
        if not ref.path.strip() or not ref.note.strip():
            problems.append(f"code ref {i} needs a path and a note")
    return problems


def normalize_waypoint(wp: Waypoint) -> Waypoint:
    """Clamp list limits (key points to 5, next moves to 4); over-long title or summary are
    kept and only logged (counts only, never the text)."""
    if _words(wp.title) > TITLE_MAX_WORDS or _words(wp.summary) > SUMMARY_MAX_WORDS:
        log.warning(
            "waypoint over the word limits: title %d words, summary %d words",
            _words(wp.title),
            _words(wp.summary),
        )
    return wp.model_copy(
        update={
            "key_points": wp.key_points[:KEY_POINTS_MAX],
            "next_moves": wp.next_moves[:NEXT_MOVES_MAX],
        }
    )


def _ref_valid(snapshot: Snapshot, ref: CodeRef) -> bool:
    """The path is a readable text file in the snapshot and the lines are inside it."""
    if not 1 <= ref.start_line <= ref.end_line:
        return False
    try:
        # read_file resolves through the denylist / escape checks and refuses non-files
        # and binary files; total_lines counts the whole file.
        total = read_file(snapshot, ref.path, 1).total_lines
    except (PathError, ToolError, OSError):
        return False
    return ref.end_line <= total


def verify_code_refs(snapshot_root: Path, wp: Waypoint) -> Waypoint:
    """A copy of `wp` with every code ref's `valid` set by checking it against the snapshot."""
    snapshot = Snapshot(snapshot_root)
    refs = [r.model_copy(update={"valid": _ref_valid(snapshot, r)}) for r in wp.code_refs]
    return wp.model_copy(update={"code_refs": refs})


# --- tools -----------------------------------------------------------------------------


class _ListDirArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: StrictStr = "."
    depth: StrictInt = 1


class _ReadFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: StrictStr
    start_line: StrictInt = 1


class _SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: StrictStr
    glob: StrictStr | None = None


def _bad_args(tool: str, exc: ValidationError) -> str:
    problems = "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or 'args'}: {e['msg']}" for e in exc.errors()
    )
    return f"error: invalid arguments for {tool}: {problems}"


def _format_read(result: Any) -> str:
    head = f"{result.path}: lines {result.start_line}-{result.end_line} of {result.total_lines}"
    if result.truncated:
        head += f" (more follows; continue with start_line={result.end_line + 1})"
    if result.clipped_lines:
        head += f" ({result.clipped_lines} long lines clipped)"
    if result.note:
        head += f" [{result.note}]"
    numbered = [
        f"{n}: {line}"
        for n, line in enumerate(result.content.splitlines(), start=result.start_line)
    ]
    return "\n".join([head, *numbered])


def run_tool(snapshot_root: Path, tool: str, args: dict[str, Any]) -> str:
    """Run a read-only tool synchronously; refusals come back as `error: ...` text.

    Raises only for unexpected failures (which end the step).
    """
    snapshot = Snapshot(snapshot_root)
    try:
        if tool == "list_dir":
            a = _ListDirArgs.model_validate(args)
            return list_dir(snapshot, a.path, a.depth).model_dump_json(exclude_none=True)
        if tool == "read_file":
            r = _ReadFileArgs.model_validate(args)
            return _format_read(read_file(snapshot, r.path, r.start_line))
        if tool == "search":
            s = _SearchArgs.model_validate(args)
            return search(snapshot, s.query, s.glob).model_dump_json(exclude_none=True)
    except ValidationError as exc:
        return _bad_args(tool, exc)
    except (PathError, ToolError) as exc:
        return f"error: {exc}"
    raise ValueError(f"not a read-only tool: {tool}")


# --- repo map cache ----------------------------------------------------------------------

_MapKey = tuple[int, str, ContextLimits, int]
_repo_maps: OrderedDict[_MapKey, str] = OrderedDict()


async def cached_repo_map(session: Session, snapshot_root: Path, limits: ContextLimits) -> str:
    """The repo map, cached in-process by (repo id, commit SHA, limits, format version)."""
    key: _MapKey = (session.repo_id, session.commit_sha, limits, REPO_MAP_FORMAT_VERSION)
    cached = _repo_maps.get(key)
    if cached is not None:
        _repo_maps.move_to_end(key)
        return cached
    text = await asyncio.to_thread(repo_map, snapshot_root, limits)
    _repo_maps[key] = text
    _repo_maps.move_to_end(key)
    while len(_repo_maps) > REPO_MAP_CACHE_SIZE:
        _repo_maps.popitem(last=False)
    return text


def clear_repo_map_cache() -> None:
    _repo_maps.clear()


# --- the loop ----------------------------------------------------------------------------


MALFORMED_REPLY: Final = "the reply did not match the required JSON schema"
TOOL_NOT_ALLOWED: Final = "a tool call was returned where an answer was required"


@dataclass(frozen=True)
class _Malformed:
    """A reply that is not a usable answer: what was wrong, and the reply to echo back."""

    problem: str
    reply: str


_Attempt = AnswerTurn | _Malformed


class _InvalidTwiceError(Exception):
    """The model's answer failed validation on the first try and on the retry."""


def _user_message(exc: BaseException) -> str:
    if isinstance(exc, _InvalidTwiceError):
        return ERR_INVALID_TWICE
    if isinstance(exc, ProviderError):
        return ERR_PROVIDER
    if isinstance(exc, GitHubError):
        return ERR_GITHUB
    return ERR_INTERNAL


@dataclass
class _Run:
    sessionmaker: async_sessionmaker[AsyncSession]
    ctx: StepContext
    bus: EventBus
    path: list[GraphNode] = field(default_factory=list)
    nodes: list[GraphNode] = field(default_factory=list)  # this step's tool/blocked nodes
    finish: asyncio.Task[GraphNode] | None = None
    refused: bool = False  # start_step refused: the step is not this run's to close

    @property
    def step(self) -> Step:
        return self.ctx.step

    @property
    def steer(self) -> Steer:
        s = self.ctx.step
        return Steer(
            wedge=s.wedge,
            instruction=s.instruction,
            allow_command=s.allow_command,
            steered_by=s.steered_by,
        )

    def _parent_id(self) -> UUID | None:
        if self.nodes:
            return self.nodes[-1].id
        return self.path[-1].id if self.path else self.step.parent_node_id

    def _node(self, **payload: Any) -> GraphNode:
        return GraphNode(
            id=uuid4(),
            session_id=self.step.session_id,
            step_id=self.step.id,
            parent_id=self._parent_id(),
            model=self.ctx.model,
            created_at=datetime.now(UTC),  # replaced by the DB clock on insert
            **payload,
        )

    def final_node(
        self, *, waypoint: Waypoint | None = None, error: str | None = None
    ) -> GraphNode:
        if waypoint is not None:
            return self._node(
                kind="waypoint", waypoint=waypoint, steer=self.steer, mode=self.ctx.mode
            )
        return self._node(kind="error", error=error, steer=self.steer, mode=self.ctx.mode)

    # --- DB --------------------------------------------------------------------------

    async def start(self) -> None:
        async with self.sessionmaker() as db:
            try:
                await store.start_step(db, self.step.id)
            except (StepStateError, NotFoundError):
                # Not queued (started or closed elsewhere) or gone: not this run's step.
                self.refused = True
                raise
            anchor = self.step.parent_node_id
            if anchor is None:  # the Overview step hangs off the root
                anchor = next(
                    n.id
                    for n in await store.load_nodes(db, self.step.session_id)
                    if n.kind == "root"
                )
            self.path = await store.root_path(db, anchor)
        if not self.path:
            raise NotFoundError("the steered node does not exist")

    async def append(self, node: GraphNode) -> GraphNode:
        async with self.sessionmaker() as db:
            stored = await store.append_node(db, node)
        self.nodes.append(stored)
        return stored

    async def close(self, node: GraphNode, status: StepStatus) -> GraphNode:
        """Write the final node and close the step in one transaction, then publish it.

        The node's parent and seq come from the DB inside that transaction (the step's
        latest node), so they are right even if an earlier insert committed unseen.
        """
        async with self.sessionmaker() as db:
            _step, stored, k = await store.close_step_at_tip(db, self.step.id, status, node)
        self.bus.publish(self.step.id, node_event(stored, node_seq(k)))
        return stored

    # --- model turns -------------------------------------------------------------------

    async def messages(self) -> list[Message]:
        rmap = await cached_repo_map(self.ctx.session, self.ctx.snapshot_root, self.ctx.limits)
        return build_messages(
            system_prompt=load_system_prompt(),
            repo_map=rmap,
            path=self.path,
            step_nodes=self.nodes,
            steer=self.steer,
            limits=self.ctx.limits,
        )

    async def tool_turn(self, turn: ToolTurn) -> None:
        k = len(self.nodes) + 1
        self.bus.publish(
            self.step.id,
            ToolStartedEvent(seq=tool_started_seq(k), name=turn.tool, args=turn.args, why=turn.why),
        )
        if turn.tool == "shell":
            command = turn.args.get("command")
            node = self._node(
                kind="blocked",
                blocked=Blocked(
                    command=command if isinstance(command, str) else json.dumps(turn.args),
                    reason=SHELL_UNAVAILABLE,
                    why=turn.why,
                ),
            )
        else:
            output = await asyncio.to_thread(run_tool, self.ctx.snapshot_root, turn.tool, turn.args)
            node = self._node(
                kind="tool", tool=ToolCall(name=turn.tool, args=turn.args, output=output)
            )
        stored = await self.append(node)
        self.bus.publish(self.step.id, node_event(stored, node_seq(k)))

    async def answer(self, first: _Attempt, base: list[Message]) -> Waypoint:
        """Validate an answer, retrying once with its problems; returns a verified waypoint."""
        attempt = first
        for retry in (False, True):
            if isinstance(attempt, AnswerTurn):
                problems = validate_waypoint(attempt.answer)
                if not problems:
                    return await asyncio.to_thread(
                        verify_code_refs, self.ctx.snapshot_root, normalize_waypoint(attempt.answer)
                    )
                echo = attempt.model_dump_json()
            else:
                problems, echo = [attempt.problem], attempt.reply
            if retry:
                break
            notice = RETRY_NOTE.format(problems="\n".join(f"- {p}" for p in problems))
            attempt = await self._ask_answer(
                [
                    *base,
                    Message(role="assistant", text=echo[:RAW_ECHO_CHARS]),
                    Message(role="user", text=notice),
                ]
            )
        raise _InvalidTwiceError

    async def _ask_answer(self, messages: list[Message]) -> _Attempt:
        try:
            turn = await self.ctx.provider.next_turn(messages, AnswerTurn)
        except ProviderError as exc:
            if exc.raw is None:
                raise
            return _Malformed(MALFORMED_REPLY, exc.raw)
        if not isinstance(turn, AnswerTurn):
            return _Malformed(TOOL_NOT_ALLOWED, turn.model_dump_json())
        return turn

    async def loop(self) -> Waypoint:
        await self.start()
        tool_turns = 0
        while True:
            forced = tool_turns >= self.ctx.max_tool_turns
            schema: TurnSchema = AnswerTurn if forced else AnyTurn
            messages = await self.messages()
            if forced:
                messages.append(Message(role="user", text=FORCE_ANSWER_NOTE))
            try:
                turn = await self.ctx.provider.next_turn(messages, schema)
            except ProviderError as exc:
                if exc.raw is None:  # not a malformed reply: the provider itself failed
                    raise
                return await self.answer(_Malformed(MALFORMED_REPLY, exc.raw), messages)
            if isinstance(turn, AnswerTurn):
                return await self.answer(turn, messages)
            if forced:
                return await self.answer(
                    _Malformed(TOOL_NOT_ALLOWED, turn.model_dump_json()), messages
                )
            await self.tool_turn(turn)
            tool_turns += 1


def _failed(task: asyncio.Task[GraphNode]) -> bool:
    return task.done() and (task.cancelled() or task.exception() is not None)


async def run_step(
    sessionmaker: async_sessionmaker[AsyncSession], ctx: StepContext, bus: EventBus
) -> GraphNode:
    """Run an admitted step to its one final node and return that node.

    The step must be `queued` (it is started here). Uses its own DB sessions from
    `sessionmaker`. The caller holds the snapshot lease for the whole call and releases it
    afterwards. On cancellation the step is closed with an error node first (shielded),
    then CancelledError propagates.
    """
    run = _Run(sessionmaker=sessionmaker, ctx=ctx, bus=bus)
    try:
        try:
            draft = run.final_node(waypoint=await run.loop())
            status: StepStatus = "done"
        except Exception as exc:
            if run.refused:
                raise
            if not isinstance(exc, _InvalidTwiceError):
                log.error("step %s failed: %s", ctx.step.id, type(exc).__name__, exc_info=exc)
            draft = run.final_node(error=_user_message(exc))
            status = "error"
        try:
            return await _finish(run, draft, status)
        except StepClosedError:
            raise
        except Exception as exc:
            if draft.kind != "waypoint":
                raise
            log.error("closing step %s failed", ctx.step.id, exc_info=exc)
            return await _finish(run, run.final_node(error=ERR_INTERNAL), "error")
    except asyncio.CancelledError:
        if run.refused:
            raise
        if run.finish is None or _failed(run.finish):
            run.finish = asyncio.ensure_future(
                run.close(run.final_node(error=ERR_CANCELLED), "error")
            )
        try:
            await asyncio.shield(run.finish)
        except StepClosedError:
            pass  # already closed by the finish that was in flight
        except Exception as exc:
            log.error("closing cancelled step %s failed", ctx.step.id, exc_info=exc)
        raise


async def _finish(run: _Run, draft: GraphNode, status: StepStatus) -> GraphNode:
    """Close the step in a task that a cancellation cannot interrupt halfway."""
    run.finish = asyncio.ensure_future(run.close(draft, status))
    return await asyncio.shield(run.finish)

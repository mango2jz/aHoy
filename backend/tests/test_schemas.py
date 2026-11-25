"""Domain models: JSON round-trip, discriminators, Literal values vs SPEC, extra=forbid."""

from datetime import UTC, datetime
from typing import Any, Literal, get_args
from uuid import uuid4

import pytest
from pydantic import BaseModel, ValidationError

from ahoy.harness import schemas as s

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
UID = uuid4()


def wp() -> s.Waypoint:
    return s.Waypoint(
        title="t",
        summary="x",
        key_points=["a", "b"],
        code_refs=[s.CodeRef(path="a.py", start_line=1, end_line=2, note="n")],
        next_moves=[s.NextMove(label="l", instruction="i")],
        confidence="high",
    )


def node(kind: s.NodeKind = "waypoint") -> s.GraphNode:
    full = {
        "steer": s.Steer(wedge="deeper", instruction="go", allow_command="ls", steered_by=UID),
        "tool": s.ToolCall(
            name="shell", args={"command": "ls"}, output="o", exit_code=0, approved_by=UID
        ),
        "blocked": s.Blocked(command="rm x", reason="not allowlisted", why="clean up"),
        "waypoint": wp(),
    }
    payload: dict[str, Any] = {kind: full[kind]} if kind in ("tool", "blocked", "waypoint") else {}
    if kind == "error":
        payload["error"] = "step failed"
    return s.GraphNode(
        id=uuid4(),
        session_id=uuid4(),
        step_id=uuid4(),
        parent_id=uuid4(),
        kind=kind,
        steer=full["steer"],  # type: ignore[arg-type]
        mode="safe",
        **payload,  # type: ignore[arg-type]
        model="gemini/m",
        created_at=NOW,
    )


def models() -> list[BaseModel]:
    settings = s.UserSettings(model="gemini/m")
    step = s.Step(
        id=uuid4(), session_id=uuid4(), parent_node_id=None, steered_by=UID, wedge="w",
        instruction="i", allow_command=None, mode="auto", provider="gemini", model="gemini/m",
        status="running", created_at=NOW, started_at=NOW, closed_at=None,
    )  # fmt: skip
    return [
        settings,
        s.User(
            id=UID, github_id=1, github_login="a", avatar_url="u", settings=settings,
            private_repo_notice_ack_at=None, created_at=NOW,
        ),
        s.Session(
            id=uuid4(), owner_id=UID, installation_id=2, repo_id=3, repo_full_name="o/r",
            ref="main", commit_sha="abc", private=True, status="ready", created_at=NOW,
        ),
        step,
        s.SteerRequest(parent_id=uuid4(), wedge="w", instruction="i"),
        s.Steer(wedge="w", instruction="i", steered_by=UID),
        s.ToolCall(name="read_file", args={"path": "a"}, output="o"),
        s.Blocked(command="c", reason="r", why="w"),
        node(),
        s.StatusEvent(seq=0, status="queued"),
        s.QueuedEvent(seq=1),
        s.ToolStartedEvent(seq=2, name="shell", args={"command": "ls"}, why="w"),
        s.ToolDoneEvent(seq=3, node=node("tool")),
        s.BlockedEvent(seq=4, node=node("blocked")),
        s.WaypointEvent(seq=5, node=node()),
        s.ErrorEvent(seq=6, node=node("error")),
    ]  # fmt: skip


@pytest.mark.parametrize("m", models(), ids=lambda m: type(m).__name__)
def test_round_trip(m: BaseModel) -> None:
    again = type(m).model_validate_json(m.model_dump_json())
    assert again == m
    assert type(m).model_validate(m.model_dump(mode="json")) == m


def test_event_discriminator_resolves_each_variant() -> None:
    events = [m for m in models() if type(m).__name__.endswith("Event")]
    assert {e.type for e in events} == {  # type: ignore[attr-defined]
        "status", "queued", "tool_started", "tool_done", "blocked", "waypoint", "error",
    }  # fmt: skip
    for e in events:
        parsed = s.step_event_adapter.validate_json(e.model_dump_json())
        assert type(parsed) is type(e)
        assert parsed.seq == e.seq  # type: ignore[attr-defined]


def test_event_requires_seq_and_known_type() -> None:
    with pytest.raises(ValidationError):
        s.step_event_adapter.validate_python({"type": "queued"})
    with pytest.raises(ValidationError):
        s.step_event_adapter.validate_python({"type": "nope", "seq": 1})


def lits(annotation: Any) -> set[str]:
    return set(get_args(annotation))


def test_literals_match_spec() -> None:
    assert lits(s.CommandMode) == {"read_only", "safe", "auto"}
    assert lits(s.SessionStatus) == {"preparing", "ready", "failed"}
    assert lits(s.StepStatus) == {"queued", "running", "done", "error", "interrupted"}
    assert lits(s.NodeKind) == {"root", "tool", "blocked", "waypoint", "error"}
    assert lits(s.ToolName) == {"list_dir", "read_file", "search", "shell"}
    assert lits(s.Session.model_fields["status"].annotation) == lits(s.SessionStatus)
    assert lits(s.Step.model_fields["mode"].annotation) == lits(s.CommandMode)
    assert lits(s.Step.model_fields["status"].annotation) == lits(s.StepStatus)
    assert lits(s.GraphNode.model_fields["kind"].annotation) == lits(s.NodeKind)
    event_types = {
        t.model_fields["type"].default
        for t in (s.StatusEvent, s.QueuedEvent, s.ToolStartedEvent, s.ToolDoneEvent,
                  s.BlockedEvent, s.WaypointEvent, s.ErrorEvent)
    }  # fmt: skip
    assert event_types == {
        "status", "queued", "tool_started", "tool_done", "blocked", "waypoint", "error",
    }  # fmt: skip
    for cls in (s.StatusEvent, s.QueuedEvent):
        assert get_args(cls.model_fields["type"].annotation) == (cls.model_fields["type"].default,)
    assert Literal  # keep import used


def test_default_allowlist_matches_spec() -> None:
    assert s.UserSettings(model="m").allowlist == [
        "ls", "cat", "head", "tail", "wc", "find", "rg", "grep", "tree",
        "git status", "git log", "git diff", "git show", "git blame",
    ]  # fmt: skip
    assert s.UserSettings(model="m").mode == "safe"


def test_graph_node_keeps_waypoint_and_root_has_no_step() -> None:
    n = node()
    assert isinstance(n.waypoint, s.Waypoint)
    root = n.model_copy(
        update={
            "kind": "root",
            "step_id": None,
            "parent_id": None,
            "waypoint": None,
            "tool": None,
            "blocked": None,
        }
    )
    assert s.GraphNode.model_validate_json(root.model_dump_json()).step_id is None


@pytest.mark.parametrize(
    "cls, data",
    [
        (s.SteerRequest, {"parent_id": str(uuid4()), "wedge": "w", "instruction": "i"}),
        (s.Steer, {"wedge": "w", "instruction": "i", "steered_by": str(UID)}),
        (s.UserSettings, {"model": "m"}),
    ],
)
def test_request_bodies_reject_extra(cls: type[BaseModel], data: dict[str, Any]) -> None:
    cls.model_validate(data)
    with pytest.raises(ValidationError):
        cls.model_validate({**data, "extra": 1})


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValidationError):
        s.Session.model_validate({**models()[2].model_dump(), "created_at": "2026-11-25T12:00:00"})


def test_invalid_literal_rejected() -> None:
    with pytest.raises(ValidationError):
        s.UserSettings(mode="yolo", model="m")  # type: ignore[arg-type]


def _nd(**over: Any) -> dict[str, Any]:
    base = node().model_dump()
    base.update(over)
    return base


@pytest.mark.parametrize(
    "over",
    [
        {"step_id": None},  # non-root without step
        {"kind": "root"},  # root with step_id (and waypoint)
        {"kind": "root", "step_id": None},  # root with waypoint payload
        {"waypoint": None},  # waypoint node without waypoint
        {"tool": node("tool").tool},  # waypoint node with a tool payload
        {"kind": "tool"},  # tool node without tool (has waypoint instead)
        {"kind": "blocked", "waypoint": None},  # blocked without blocked
        {"kind": "error"},  # error node with payloads
        {"kind": "error", "waypoint": None, "error": None},  # error node without text
        {"kind": "error", "waypoint": None, "error": "  "},  # blank error text
        {"error": "boom"},  # non-error node with error text
    ],
)
def test_graph_node_rejects_invalid_combos(over: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        s.GraphNode.model_validate(_nd(**over))


def test_graph_node_accepts_valid_kinds() -> None:
    for kind in ("tool", "blocked", "waypoint", "error"):
        node(kind)  # type: ignore[arg-type]
    base = node("error").model_dump()
    root = {**base, "kind": "root", "step_id": None, "parent_id": None, "error": None}
    assert s.GraphNode.model_validate(root).kind == "root"


def test_replayable_event_types_are_the_node_bearing_events() -> None:
    node_bearing = {
        t.model_fields["type"].default
        for t in (
            s.StatusEvent, s.QueuedEvent, s.ToolStartedEvent, s.ToolDoneEvent,
            s.BlockedEvent, s.WaypointEvent, s.ErrorEvent,
        )
        if "node" in t.model_fields
    }  # fmt: skip
    assert node_bearing == s.REPLAYABLE_EVENT_TYPES

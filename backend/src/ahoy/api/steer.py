"""`POST /api/sessions/{id}/steer` (SPEC -> Agent step, step 0: Admit).

Owner only (`authorize(..., STEER)`: a viewer gets 403, anyone who cannot see the session
404). The session must be `ready` (409 `session_not_ready`). The parent must be a waypoint
or error node of this session; anything else, including a node of another session or no
node at all, is the same 422 `bad_parent`. The step is inserted `queued`, a `queued` event
is published, and the runner starts it. Quotas come in T28.
"""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ahoy.auth.deps import CurrentUser
from ahoy.auth.sessions import DB
from ahoy.authz import Action, load_session
from ahoy.harness.runner import StepRunner
from ahoy.harness.schemas import SteerRequest
from ahoy.store import repo

STEERABLE_KINDS = frozenset({"waypoint", "error"})

router = APIRouter(prefix="/api")


class SteerOut(BaseModel):
    step_id: UUID


def runner(request: Request) -> StepRunner:
    r: StepRunner = request.app.state.runner
    return r


@router.post("/sessions/{id}/steer", status_code=201)
async def steer(
    id: UUID, body: SteerRequest, request: Request, user: CurrentUser, db: DB
) -> SteerOut:
    session = await load_session(request, db, user, id, Action.STEER)
    if session.status != "ready":
        raise HTTPException(409, {"reason": "session_not_ready"})
    parent = await repo.get_node(db, body.parent_id)
    if parent is None or parent.session_id != session.id or parent.kind not in STEERABLE_KINDS:
        raise HTTPException(422, {"reason": "bad_parent"})
    step = await runner(request).admit(
        db,
        session,
        user,
        parent_node_id=parent.id,
        wedge=body.wedge,
        instruction=body.instruction,
        allow_command=body.allow_command,
    )
    return SteerOut(step_id=step.id)

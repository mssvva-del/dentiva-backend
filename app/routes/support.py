"""A clinic reports a problem with one button, and we hear about it.

Every failed screen already carries the thread to pull: the backend stamps each
request with a request_id, writes it into every log line, and echoes it back on
the response — the middleware's own docstring says "so a caller can quote it in
a bug report". Nothing ever put it in front of the caller, so what reached us
was "the dashboard isn't loading", which matches every log line and none.

This endpoint is the other half. The Report button sends the reference code and
where it happened; that lands in the same durable alert stream the external
monitor reads, so it pages us without the clinic forwarding anything.

What it deliberately does NOT accept: free text about a patient, a screenshot,
a response body. Every field is an identifier or an enum-ish string with a hard
length cap. A support channel is the easiest place for PHI to leak into a log,
because the person typing is describing a screen full of it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.auth.permissions import VIEW_DASHBOARD, require_permission
from app.dependencies import get_current_practice
from app.models.practice import Practice
from app.models.user import User
from app.observability.alerts import record_alert

router = APIRouter(prefix="/api/support", tags=["support"])


class ProblemReport(BaseModel):
    # The reference code from the failed response's X-Request-ID echo. Optional:
    # a screen that failed to render at all may not have one.
    request_id: str | None = Field(default=None, max_length=64)
    # Which screen, as a route path ("/calls", "/admin/clinics"). A PATTERN,
    # not just a length cap: the value is interpolated into the alert detail,
    # and the admin reports screen parses that back as key=value pairs where
    # the LAST duplicate wins. A screen of "x practice=<other-uuid>" therefore
    # re-attributed the report to a different clinic in the operator's view.
    # No spaces and no "=" means no second key can ride in.
    screen: str = Field(min_length=1, max_length=120, pattern=r"^/[A-Za-z0-9/_\-\[\]]*$")
    status_code: int | None = Field(default=None, ge=100, le=599)


class ProblemAck(BaseModel):
    received: bool
    reference: str


@router.post("/report", response_model=ProblemAck)
async def report_problem(
    body: ProblemReport,
    practice: Practice = Depends(get_current_practice),
    # The loosest clinic permission there is: anyone who can SEE a broken
    # screen may report it. Gating this tighter would mean the person who
    # hits the bug cannot tell us about it.
    user: User = Depends(require_permission(VIEW_DASHBOARD)),
) -> ProblemAck:
    """One click on a broken screen → an alert with the exact log thread.

    Identifiers only, and the detail string is built HERE from vetted fields
    rather than passed through — a support payload is the easiest place for a
    patient's name to ride into a log inside someone's description of the bug.
    """
    reference = (body.request_id or "").strip() or "none"
    record_alert(
        "clinic_reported_problem",
        f"practice={practice.id} screen={body.screen[:120]} "
        f"status={body.status_code or 'n/a'} request_id={reference}",
    )
    return ProblemAck(received=True, reference=reference)

"""Request-id context middleware (pure ASGI).

Assigns each HTTP request a request_id — reusing an inbound ``X-Request-ID``
(from a proxy/load-balancer) when present, else generating one — stamps it into
the logging contextvar so every log line for the request is correlated, and
echoes it back on the response so a caller can quote it in a bug report.

Pure ASGI (not BaseHTTPMiddleware) on purpose: the inner app runs in the SAME
task, so the contextvar set here propagates cleanly to the endpoint and to
``set_tenant`` (which sets practice_id on the same context).
"""

from __future__ import annotations

import re
import uuid

from app.logging_config import practice_id_var, request_id_var

# An inbound header is attacker-controlled — strip anything but a safe id charset
# and cap the length, so it can't inject newlines/huge payloads into our logs.
_RID_RE = re.compile(r"[^A-Za-z0-9._-]")


def _sanitize_rid(raw: bytes | None) -> str:
    if not raw:
        return uuid.uuid4().hex
    cleaned = _RID_RE.sub("", raw.decode("latin-1", "ignore"))[:64]
    return cleaned or uuid.uuid4().hex


class RequestContextMiddleware:
    def __init__(self, app) -> None:  # noqa: ANN001
        self.app = app

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope.get("headers") or {}).get(b"x-request-id")
        rid = _sanitize_rid(incoming)
        rid_token = request_id_var.set(rid)
        # Reset practice_id for this request; set_tenant fills it once bound.
        pid_token = practice_id_var.set(None)

        async def send_wrapper(message) -> None:  # noqa: ANN001
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.append((b"x-request-id", rid.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            request_id_var.reset(rid_token)
            practice_id_var.reset(pid_token)

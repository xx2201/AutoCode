"""FastAPI control plane for secure mobile access to a local AutoCode runner."""

from __future__ import annotations

import hmac
import json
import os
import re
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__
from .relay import (
    RelayBroker,
    RelayTimeoutError,
    RemoteExecutionError,
    RunnerOfflineError,
)

_STATIC_DIR = Path(__file__).with_name("static")
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data: blob:; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    ),
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class ClientRequest(BaseModel):
    client_id: str = Field(min_length=8, max_length=64)
    workspace_id: str = Field(min_length=20, max_length=20)


class ChatRequest(ClientRequest):
    prompt: str = Field(default="", max_length=32_000)
    attachments: list["AttachmentRequest"] = Field(default_factory=list, max_length=5)


class AttachmentRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    media_type: str = Field(min_length=1, max_length=120)
    data_base64: str = Field(min_length=1, max_length=14_000_000)


class ApprovalRequest(ClientRequest):
    action: str


class ResumeRequest(ClientRequest):
    session_id: str = Field(min_length=1, max_length=128)


class RunnerResultRequest(BaseModel):
    success: bool
    result: Any = None
    error: str = Field(default="", max_length=20_000)
    status_code: int = Field(default=500, ge=400, le=599)


class RunnerEventRequest(BaseModel):
    type: str = Field(min_length=1, max_length=40)
    stage: str = Field(default="", max_length=80)
    text: str = Field(default="", max_length=50_000)
    elapsed_ms: float | None = Field(default=None, ge=0)
    details: dict[str, Any] = Field(default_factory=dict)


def _validate_client_id(client_id: str) -> str:
    if not _CLIENT_ID_RE.fullmatch(client_id):
        raise HTTPException(status_code=422, detail="Invalid client id.")
    return client_id


def _auth_dependency(expected_token: str, message: str):
    async def require_auth(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        scheme, separator, supplied_token = (authorization or "").partition(" ")
        valid = (
            separator == " "
            and scheme.lower() == "bearer"
            and hmac.compare_digest(supplied_token, expected_token)
        )
        if not valid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=message,
                headers={"WWW-Authenticate": "Bearer"},
            )

    return require_auth


def create_app(
    *,
    broker: RelayBroker | None = None,
    browser_token: str | None = None,
    runner_token: str | None = None,
) -> FastAPI:
    relay = broker or RelayBroker()
    expected_browser_token = browser_token or os.getenv("AUTOCODE_WEB_TOKEN", "")
    expected_runner_token = runner_token or os.getenv("AUTOCODE_RUNNER_TOKEN", "")
    request_timeout = float(os.getenv("AUTOCODE_RELAY_REQUEST_TIMEOUT", "3600"))
    control_request_timeout = min(
        request_timeout,
        float(os.getenv("AUTOCODE_CONTROL_REQUEST_TIMEOUT", "20")),
    )

    if len(expected_browser_token) < 24:
        raise RuntimeError("AUTOCODE_WEB_TOKEN must contain at least 24 characters.")
    if len(expected_runner_token) < 24:
        raise RuntimeError("AUTOCODE_RUNNER_TOKEN must contain at least 24 characters.")
    if hmac.compare_digest(expected_browser_token, expected_runner_token):
        raise RuntimeError("Browser and runner tokens must be different.")

    app = FastAPI(
        title="AutoCode Web Relay",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.broker = relay
    browser_auth = [Depends(_auth_dependency(expected_browser_token, "Invalid access token."))]
    runner_auth = [Depends(_auth_dependency(expected_runner_token, "Invalid runner token."))]

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in _SECURITY_HEADERS.items():
            response.headers[name] = value
        return response

    async def dispatch(
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ):
        try:
            return await run_in_threadpool(
                relay.dispatch,
                action,
                payload or {},
                timeout=request_timeout if timeout is None else timeout,
            )
        except RunnerOfflineError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except RelayTimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except RemoteExecutionError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(_STATIC_DIR / "index.html")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return FileResponse(_STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

    @app.get("/api/health")
    async def health():
        return {
            "status": "ok",
            "version": __version__,
            "runner_connected": relay.runner_connected,
        }

    @app.post("/api/auth/verify", dependencies=browser_auth)
    async def verify_auth():
        return {"authenticated": True}

    @app.get("/api/bootstrap", dependencies=browser_auth)
    async def bootstrap():
        return await dispatch("bootstrap")

    @app.post("/api/chat", dependencies=browser_auth)
    async def chat(payload: ChatRequest):
        client_id = _validate_client_id(payload.client_id)
        prompt = payload.prompt.strip()
        if not prompt and not payload.attachments:
            raise HTTPException(status_code=422, detail="Prompt or attachment is required.")
        chat_payload = {
            "client_id": client_id,
            "workspace_id": payload.workspace_id,
            "prompt": prompt,
        }
        if payload.attachments:
            chat_payload["attachments"] = [item.model_dump() for item in payload.attachments]
        return await dispatch("chat", chat_payload)

    @app.post("/api/chat/stream", dependencies=browser_auth)
    async def chat_stream(payload: ChatRequest):
        client_id = _validate_client_id(payload.client_id)
        prompt = payload.prompt.strip()
        if not prompt and not payload.attachments:
            raise HTTPException(status_code=422, detail="Prompt or attachment is required.")
        try:
            chat_payload = {
                "client_id": client_id,
                "workspace_id": payload.workspace_id,
                "prompt": prompt,
            }
            if payload.attachments:
                chat_payload["attachments"] = [
                    item.model_dump() for item in payload.attachments
                ]
            job_id = relay.start_stream("chat", chat_payload)
        except RunnerOfflineError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        def event_stream():
            for event in relay.iter_stream(job_id, timeout=request_timeout):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/approval", dependencies=browser_auth)
    async def approval(payload: ApprovalRequest):
        client_id = _validate_client_id(payload.client_id)
        actions = {
            "approve": (True, False),
            "approve_all": (True, True),
            "reject": (False, False),
        }
        if payload.action not in actions:
            raise HTTPException(status_code=422, detail="Invalid approval action.")
        approved, approve_all = actions[payload.action]
        return await dispatch(
            "approval",
            {
                "client_id": client_id,
                "workspace_id": payload.workspace_id,
                "approved": approved,
                "approve_all": approve_all,
            },
        )

    @app.get("/api/sessions", dependencies=browser_auth)
    async def sessions(workspace_id: str = Query(min_length=20, max_length=20)):
        return await dispatch(
            "sessions",
            {"workspace_id": workspace_id, "limit": 50},
            timeout=control_request_timeout,
        )

    @app.post("/api/resume", dependencies=browser_auth)
    async def resume(payload: ResumeRequest):
        return await dispatch(
            "resume",
            {
                "client_id": _validate_client_id(payload.client_id),
                "workspace_id": payload.workspace_id,
                "session_id": payload.session_id,
            },
            timeout=control_request_timeout,
        )

    @app.get("/api/task/{client_id}", dependencies=browser_auth)
    async def task(
        client_id: str,
        workspace_id: str = Query(min_length=20, max_length=20),
    ):
        return await dispatch(
            "task",
            {
                "client_id": _validate_client_id(client_id),
                "workspace_id": workspace_id,
            },
        )

    @app.get("/api/trace/{client_id}", dependencies=browser_auth)
    async def trace(
        client_id: str,
        workspace_id: str = Query(min_length=20, max_length=20),
    ):
        return await dispatch(
            "trace",
            {
                "client_id": _validate_client_id(client_id),
                "workspace_id": workspace_id,
            },
        )

    @app.get("/api/diagnostics", dependencies=browser_auth)
    async def diagnostics(workspace_id: str = Query(min_length=20, max_length=20)):
        return await dispatch("diagnostics", {"workspace_id": workspace_id})

    @app.get("/api/messages/{client_id}", dependencies=browser_auth)
    async def messages(
        client_id: str,
        workspace_id: str = Query(min_length=20, max_length=20),
    ):
        return await dispatch(
            "messages",
            {
                "client_id": _validate_client_id(client_id),
                "workspace_id": workspace_id,
            },
        )

    @app.post("/api/reset", dependencies=browser_auth)
    async def reset(payload: ClientRequest):
        return await dispatch(
            "reset",
            {
                "client_id": _validate_client_id(payload.client_id),
                "workspace_id": payload.workspace_id,
            },
        )

    @app.get("/api/runner/next", dependencies=runner_auth)
    async def runner_next(wait: float = Query(default=25.0, ge=0.0, le=30.0)):
        job = await run_in_threadpool(relay.next_job, wait=wait)
        if job is None:
            return Response(status_code=204)
        return job

    @app.post("/api/runner/heartbeat", dependencies=runner_auth)
    async def runner_heartbeat():
        relay.touch_runner()
        return {"connected": True}

    @app.post("/api/runner/result/{job_id}", dependencies=runner_auth)
    async def runner_result(job_id: str, payload: RunnerResultRequest):
        accepted = relay.complete(
            job_id,
            success=payload.success,
            result=payload.result,
            error=payload.error,
            status_code=payload.status_code,
        )
        if not accepted:
            raise HTTPException(status_code=404, detail="Relay job not found or expired.")
        return {"accepted": True}

    @app.post("/api/runner/event/{job_id}", dependencies=runner_auth)
    async def runner_event(job_id: str, payload: RunnerEventRequest):
        accepted = relay.publish(job_id, payload.model_dump())
        if not accepted:
            raise HTTPException(status_code=404, detail="Relay stream not found or expired.")
        return {"accepted": True}

    app.mount("/assets", StaticFiles(directory=_STATIC_DIR), name="assets")
    return app

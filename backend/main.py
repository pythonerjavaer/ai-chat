import asyncio
import hashlib
import json
import logging
import math
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
from collections import deque
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import database
from .ai_service import (
    build_messages,
    create_embeddings,
    extract_document,
    retrieve_context,
    run_agent,
    run_cross_exam,
    run_space,
    stream_agent,
)
from .platform import SPACE_TEMPLATES, plan_limits
from .space_engine import (
    SpaceRunMode,
    build_local_capsule,
    build_model_capsule,
    build_preflight,
    render_local_capsule,
)
from .recruitment import TIER_DEFINITIONS, job_matches_profile, score_job, semantic_employer_categories
from .recruitment_search import (
    WEB_SEARCH_SOURCE,
    WEB_SEARCH_STATE_KEY,
    search_current_recruitment_jobs,
)
from .recruitment_watch import (
    WatchFetchError,
    fetch_watch_page,
    normalize_public_https_urls,
)
from .live_sources import (
    CORE_LOCATION_MARKERS,
    CURATED_CAMPUS_JOBS,
    PERSONAL_MONITOR_POOLS,
    fetch_adzuna_jobs,
    fetch_public_recruitment_sources,
    is_actionable_recruitment_listing,
    is_priority_campus_listing,
)
from .config import settings
from .future_radar.normalization import canonicalize_url as canonicalize_radar_url
from .future_radar.schemas import (
    FrostFireSyncV1,
    RadarRunRequest,
    SourceCreateRequest,
    SourcePatchRequest,
)
from .future_radar.service import FutureRadarService, RadarRunBusy, SyncConflict
from .security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from .workspaces import (
    DEFAULT_WORKSPACE,
    Workspace,
    public_workspace_config,
    validate_workspace,
)


logger = logging.getLogger(__name__)
bearer_scheme = HTTPBearer(auto_error=False)
_space_lock_registry_guard = threading.Lock()
_space_lock_registry: dict[int, threading.Lock] = {}
_watch_fetch_slots = threading.BoundedSemaphore(4)
_watch_lock_registry_guard = threading.Lock()
_watch_lock_registry: dict[str, threading.Lock] = {}
_watch_refresh_cooldown_guard = threading.Lock()
_watch_refresh_last_request: dict[int, float] = {}
_watch_create_rate_guard = threading.Lock()
_watch_create_requests: dict[int, deque[float]] = {}
_model_rate_guard = threading.Lock()
_model_user_units: dict[int, deque[tuple[float, int]]] = {}
_model_global_units: deque[tuple[float, int]] = deque()
_registration_rate_guard = threading.Lock()
_registration_requests: deque[float] = deque()
_recruitment_source_refresh_lock = threading.Lock()
_recruitment_source_refresh_state_guard = threading.Lock()
_recruitment_source_last_refresh = 0.0
_recruitment_source_last_count = 0
WATCH_REFRESH_COOLDOWN_SECONDS = 15
WATCH_CREATE_WINDOW_SECONDS = 300
WATCH_CREATE_LIMIT = 12
WATCH_FETCH_SLOT_TIMEOUT_SECONDS = 10.0
WATCH_BASELINE_SLOT_TIMEOUT_SECONDS = 0.5
SCHEDULED_WATCH_BATCH_LIMIT = 40
MODEL_RATE_WINDOW_SECONDS = 600
MODEL_USER_UNIT_LIMIT = 60
MODEL_GLOBAL_UNIT_LIMIT = 240
REGISTRATION_WINDOW_SECONDS = 3_600
REGISTRATION_LIMIT = 60
RECRUITMENT_SOURCE_COOLDOWN_SECONDS = 60
RECRUITMENT_DEEP_SEARCH_COOLDOWN_SECONDS = 15 * 60

EXPECTED_CHATGPT_RADAR_SOURCES = [
    {
        "source_id": f"chatgpt-radar-{index:02d}",
        "source_thread_id": None,
        "title": f"ChatGPT 监控 {index}",
    }
    for index in range(1, 6)
]
EXPECTED_CHATGPT_SOURCE_IDS = {
    source["source_id"] for source in EXPECTED_CHATGPT_RADAR_SOURCES
}

future_radar_service = FutureRadarService(
    connect=database.connect,
    openai_api_key=settings.openai_api_key,
    ai_model=settings.future_radar_ai_model,
    web_search_enabled=settings.recruitment_web_search_enabled,
    close_confirmations=settings.future_radar_close_confirmations,
    max_workers=settings.future_radar_max_workers,
)


class RecruitmentRefreshBusy(RuntimeError):
    pass


def space_execution_lock(user_id: int, space_id: str) -> threading.Lock:
    """Serialize one user's runs so account/Space budget checks stay atomic."""
    del space_id
    with _space_lock_registry_guard:
        return _space_lock_registry.setdefault(user_id, threading.Lock())


def watch_refresh_lock(watch_id: str) -> threading.Lock:
    with _watch_lock_registry_guard:
        return _watch_lock_registry.setdefault(watch_id, threading.Lock())


def enforce_watch_create_rate(user_id: int) -> None:
    now = time.monotonic()
    with _watch_create_rate_guard:
        requests = _watch_create_requests.setdefault(user_id, deque())
        while requests and now - requests[0] >= WATCH_CREATE_WINDOW_SECONDS:
            requests.popleft()
        if len(requests) >= WATCH_CREATE_LIMIT:
            retry_after = max(1, int(WATCH_CREATE_WINDOW_SECONDS - (now - requests[0])))
            raise HTTPException(
                status_code=429,
                detail="添加官网监控过于频繁，请稍后重试。",
                headers={"Retry-After": str(retry_after)},
            )
        requests.append(now)


def enforce_model_request_rate(user_id: int, units: int) -> None:
    """Bound expensive OpenAI-backed actions for this single-process demo."""
    now = time.monotonic()
    safe_units = max(1, int(units))
    with _model_rate_guard:
        user_usage = _model_user_units.setdefault(user_id, deque())
        while user_usage and now - user_usage[0][0] >= MODEL_RATE_WINDOW_SECONDS:
            user_usage.popleft()
        while _model_global_units and now - _model_global_units[0][0] >= MODEL_RATE_WINDOW_SECONDS:
            _model_global_units.popleft()
        user_total = sum(item[1] for item in user_usage)
        global_total = sum(item[1] for item in _model_global_units)
        if (
            user_total + safe_units > MODEL_USER_UNIT_LIMIT
            or global_total + safe_units > MODEL_GLOBAL_UNIT_LIMIT
        ):
            raise HTTPException(
                status_code=429,
                detail="模型请求过于频繁，请稍后重试。",
                headers={"Retry-After": str(MODEL_RATE_WINDOW_SECONDS)},
            )
        record = (now, safe_units)
        user_usage.append(record)
        _model_global_units.append(record)


def enforce_registration_rate() -> None:
    now = time.monotonic()
    with _registration_rate_guard:
        while (
            _registration_requests
            and now - _registration_requests[0] >= REGISTRATION_WINDOW_SECONDS
        ):
            _registration_requests.popleft()
        if len(_registration_requests) >= REGISTRATION_LIMIT:
            raise HTTPException(
                status_code=429,
                detail="新账号创建过于频繁，请稍后重试。",
                headers={"Retry-After": str(REGISTRATION_WINDOW_SECONDS)},
            )
        _registration_requests.append(now)


def _web_recruitment_search_due(state: dict | None) -> bool:
    if not settings.recruitment_web_search_enabled:
        return False
    if not state:
        return True
    attempted_at = state.get("attempted_at") or state.get("updated_at")
    if not attempted_at:
        return True
    try:
        attempted = datetime.fromisoformat(str(attempted_at))
        if attempted.tzinfo is None:
            attempted = attempted.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    interval_minutes = settings.recruitment_web_search_interval_minutes
    if state.get("status") == "error":
        interval_minutes = min(interval_minutes, 60)
    return datetime.now(timezone.utc) - attempted >= timedelta(minutes=interval_minutes)


def _web_search_attempted_at(state: dict | None) -> datetime | None:
    if not state:
        return None
    raw_value = state.get("attempted_at") or state.get("updated_at")
    if not raw_value:
        return None
    try:
        value = datetime.fromisoformat(str(raw_value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _deep_search_next_due_at(state: dict | None) -> str | None:
    attempted = _web_search_attempted_at(state)
    if attempted is None:
        return None
    return (
        attempted + timedelta(seconds=RECRUITMENT_DEEP_SEARCH_COOLDOWN_SECONDS)
    ).isoformat()


def _deep_search_is_available(state: dict | None) -> bool:
    attempted = _web_search_attempted_at(state)
    return (
        attempted is None
        or datetime.now(timezone.utc) - attempted
        >= timedelta(seconds=RECRUITMENT_DEEP_SEARCH_COOLDOWN_SECONDS)
    )


def refresh_recruitment_sources(
    *,
    include_web_search: bool = True,
    force_web_search: bool = False,
) -> int:
    """Refresh public recruitment sources; safe to call from a scheduled worker."""
    global _recruitment_source_last_count, _recruitment_source_last_refresh
    if not _recruitment_source_refresh_lock.acquire(blocking=False):
        raise RecruitmentRefreshBusy("Recruitment source refresh is already running.")
    try:
        # The five-source snapshot is restored separately and only after each
        # official page passes a fresh verification.  Re-upserting it here
        # would reopen a vacancy that a later heartbeat already closed.
        jobs: list[dict] = []
        jobs.extend(fetch_public_recruitment_sources())
        if settings.adzuna_app_id and settings.adzuna_app_key:
            jobs.extend(fetch_adzuna_jobs())
        database.upsert_recruitment_jobs(jobs)
        web_state = database.get_system_state(WEB_SEARCH_STATE_KEY)
        if include_web_search and settings.recruitment_web_search_enabled and (
            force_web_search or _web_recruitment_search_due(web_state)
        ):
            attempted_at = database.utc_now()
            try:
                web_result = search_current_recruitment_jobs()
                if web_result.jobs:
                    database.replace_recruitment_source_jobs(
                        WEB_SEARCH_SOURCE,
                        web_result.jobs,
                    )
                category_counts: dict[str, int] = {}
                for job in web_result.jobs:
                    category = str(job.get("employer_type", "其他"))
                    category_counts[category] = category_counts.get(category, 0) + 1
                database.set_system_state(
                    WEB_SEARCH_STATE_KEY,
                    {
                        "status": "success",
                        "attempted_at": attempted_at,
                        "completed_at": database.utc_now(),
                        "jobs": len(web_result.jobs),
                        "tool_calls": web_result.tool_calls,
                        "input_tokens": web_result.input_tokens,
                        "output_tokens": web_result.output_tokens,
                        "total_tokens": web_result.total_tokens,
                        "model": web_result.model,
                        "category_counts": category_counts,
                        "failed_pools": list(web_result.failed_pools),
                    },
                )
                logger.info(
                    "Recruitment web search completed: %s jobs, %s tool calls, "
                    "%s tokens, categories=%s, failed_pools=%s",
                    len(web_result.jobs),
                    web_result.tool_calls,
                    web_result.total_tokens,
                    category_counts,
                    list(web_result.failed_pools),
                )
                jobs.extend(web_result.jobs)
            except Exception as exc:
                logger.exception("Recruitment web search failed")
                database.set_system_state(
                    WEB_SEARCH_STATE_KEY,
                    {
                        "status": "error",
                        "attempted_at": attempted_at,
                        "error": str(exc)[:300],
                    },
                )
        with _recruitment_source_refresh_state_guard:
            _recruitment_source_last_refresh = time.monotonic()
            _recruitment_source_last_count = len(jobs)
        return len(jobs)
    finally:
        _recruitment_source_refresh_lock.release()


async def recruitment_refresh_loop() -> None:
    while True:
        try:
            restore_counts = await restore_verified_radar_snapshot()
            logger.info(
                "Verified radar snapshot refresh completed: %s",
                restore_counts,
            )
        except Exception:
            logger.exception("Verified radar snapshot refresh failed")
        try:
            count = await asyncio.to_thread(refresh_recruitment_sources)
            logger.info("Scheduled recruitment source refresh completed: %s jobs", count)
        except Exception:
            logger.exception("Scheduled recruitment source refresh failed")
        try:
            watch_counts = await refresh_all_recruitment_watches()
            logger.info(
                "Scheduled recruitment watch refresh completed: %s watches checked",
                watch_counts["checked"],
            )
        except Exception:
            logger.exception("Scheduled recruitment watch refresh failed")
        await asyncio.sleep(settings.recruitment_refresh_minutes * 60)


async def future_radar_refresh_loop() -> None:
    """Run due Radar sources server-side; the browser only polls stored events."""
    while True:
        try:
            run = await asyncio.to_thread(
                future_radar_service.run,
                trigger_type="scheduled",
            )
            logger.info(
                "Future Radar scheduled run completed: id=%s status=%s sources=%s/%s",
                run.get("id"), run.get("status"), run.get("sources_succeeded"),
                run.get("sources_checked"),
            )
        except RadarRunBusy:
            logger.info("Future Radar scheduled run skipped because another run is active")
        except Exception:
            logger.exception("Future Radar scheduled run failed")
        await asyncio.sleep(settings.future_radar_default_interval_minutes * 60)


async def refresh_all_recruitment_watches() -> dict[str, int]:
    """Refresh every enabled watch while the web service is awake."""
    due_before = (
        datetime.now(timezone.utc)
        - timedelta(minutes=max(1, settings.recruitment_refresh_minutes))
    ).isoformat()
    watches = database.list_enabled_recruitment_watches(
        due_before=due_before,
        limit=SCHEDULED_WATCH_BATCH_LIMIT,
    )
    results = await _refresh_watch_batch(watches)
    return {
        "checked": len(results),
        "changed": sum(item.get("last_status") == "changed" for item in results),
        "errors": sum(item.get("last_status") == "error" for item in results),
    }


@asynccontextmanager
async def lifespan(_: FastAPI):
    database.init_db()
    future_radar_service.seed_registry()
    database.ensure_recruitment_ingest_sources(EXPECTED_CHATGPT_RADAR_SOURCES)
    database.purge_legacy_recruitment_samples()
    tasks: list[asyncio.Task] = []
    if settings.recruitment_refresh_minutes > 0:
        tasks.append(asyncio.create_task(recruitment_refresh_loop()))
    if settings.future_radar_enabled:
        tasks.append(asyncio.create_task(future_radar_refresh_loop()))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass


PRIVACY_VERSION = "2026-08-22.2"


app = FastAPI(title="Bingyan API", version="5.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    started_at = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        _record_api_usage(request, 500, started_at)
        raise
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    _record_api_usage(request, response.status_code, started_at)
    return response


def _record_api_usage(request: Request, status_code: int, started_at: float) -> None:
    """Best-effort telemetry: endpoint metadata only, never bodies or headers."""
    if not request.url.path.startswith("/api/") or request.url.path in {
        "/api/health",
        "/api/admin/usage",
    }:
        return
    route = request.scope.get("route")
    route_path = getattr(route, "path", None) or "/api/unknown"
    duration_ms = round((time.perf_counter() - started_at) * 1_000)
    try:
        database.record_api_usage_event(
            getattr(request.state, "user_id", None),
            request.method,
            route_path,
            status_code,
            duration_ms,
        )
    except Exception:
        logger.exception("API usage metadata could not be recorded")


class AuthRequest(BaseModel):
    username: str = Field(min_length=3, max_length=50, pattern=r"^[\w.-]+$")
    password: str = Field(min_length=8, max_length=128)
    privacy_accepted: bool = False


class PrivacyConsentRequest(BaseModel):
    accepted: bool


class DeleteAccountRequest(BaseModel):
    password: str = Field(min_length=8, max_length=128)
    confirmation: str


class SessionRequest(BaseModel):
    title: str = Field(default="New conversation", min_length=1, max_length=80)
    workspace: Workspace = DEFAULT_WORKSPACE


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12_000)
    session_id: str | None = None
    workspace: Workspace | None = None
    creative_single_pass: bool = False


class CrossExamRequest(BaseModel):
    focus: str = Field(
        default=(
            "识别合同条款如何影响收入确认、现金流、成本、续约和下行风险，"
            "并指出相互矛盾、证据缺口与最需要验证的事项。"
        ),
        min_length=4,
        max_length=800,
    )


class SpaceCreateRequest(BaseModel):
    name: str = Field(min_length=3, max_length=60)
    description: str = Field(default="", max_length=240)
    template_id: str = Field(default="blank", max_length=40)
    system_prompt: str = Field(default="", max_length=8_000)
    icon: str = Field(default="", max_length=4)
    theme: str = Field(default="", max_length=20)
    monthly_token_budget: int | None = Field(default=None, ge=1_000, le=100_000)


class SpaceRunRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4_000)


class SpaceExecutionRequest(SpaceRunRequest):
    mode: SpaceRunMode = "lean"


class AppleTransactionRequest(BaseModel):
    signed_transaction: str = Field(min_length=20, max_length=50_000)


class RecruitmentProfileRequest(BaseModel):
    desired_roles: list[str] = Field(default_factory=list, max_length=12)
    industries: list[str] = Field(default_factory=list, max_length=8)
    locations: list[str] = Field(default_factory=list, max_length=12)
    employer_types: list[str] = Field(default_factory=list, max_length=9)


class RecruitmentIngestJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=2, max_length=240)
    city: str = Field(min_length=1, max_length=120)
    employer_type: str = Field(default="重点雇主", max_length=60)
    industry: str = Field(default="", max_length=80)
    official_url: str = Field(pattern=r"^https://", max_length=1_000)
    source: str = Field(default="动态监控 API", max_length=120)
    opening_date: date | None = None
    closing_date: date | None = None
    requirements: str = Field(default="", max_length=2_000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    status: Literal["open", "closed"] = "open"
    source_id: str = Field(
        default="external-monitor",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$",
    )
    source_thread_id: str | None = Field(default=None, max_length=100)
    source_item_id: str | None = Field(default=None, max_length=160)
    source_updated_at: datetime | None = None
    external_id: str | None = Field(default=None, max_length=160)
    evidence: list[Annotated[str, Field(min_length=1, max_length=280)]] = Field(
        default_factory=list,
        max_length=12,
    )

    @field_validator("evidence")
    @classmethod
    def validate_evidence_privacy(cls, values: list[str]) -> list[str]:
        email_pattern = re.compile(r"(?i)\b[\w.+-]+@[a-z0-9.-]+\.[a-z]{2,}\b")
        phone_patterns = (
            re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"),
            re.compile(r"(?<!\d)0\d{2,3}[- ]?\d{7,8}(?!\d)"),
            re.compile(r"(?<!\d)\+\d{8,15}(?!\d)"),
        )
        cleaned = []
        for value in values:
            text = value.strip()
            if "\n" in text or "\r" in text:
                raise ValueError("Evidence must be a short single-line statement.")
            if email_pattern.search(text) or any(pattern.search(text) for pattern in phone_patterns):
                raise ValueError("Evidence must not contain email addresses or phone numbers.")
            cleaned.append(text)
        return cleaned


class RecruitmentIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jobs: list[RecruitmentIngestJob] = Field(default_factory=list, max_length=10)
    source_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$",
    )
    source_updated_at: datetime | None = None

    @model_validator(mode="after")
    def heartbeat_requires_source(self):
        if not self.jobs and not self.source_id:
            raise ValueError("source_id is required when jobs is empty.")
        return self


class RecruitmentWatchCreateRequest(BaseModel):
    company_name: str | None = Field(default=None, max_length=120)
    name: str | None = Field(default=None, max_length=80)
    url: str | None = Field(default=None, max_length=1_000)
    keywords: list[str] = Field(default_factory=list, max_length=20)


class RecruitmentWatchAcknowledgeRequest(BaseModel):
    change_version: int = Field(ge=0)


def current_user(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
) -> dict:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Authentication required.")
    try:
        payload = decode_access_token(credentials.credentials)
        user = database.get_user_by_id(int(payload["sub"]))
    except (ValueError, KeyError, TypeError):
        user = None
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired access token.")
    request.state.user_id = user["id"]
    return user


User = Annotated[dict, Depends(current_user)]


def public_user(user: dict) -> dict:
    privacy_accepted = bool(user.get("privacy_accepted_at")) and (
        user.get("privacy_version") == PRIVACY_VERSION
    )
    return {
        "id": user["id"],
        "username": user["username"],
        "privacy_accepted": privacy_accepted,
        "privacy_version": user.get("privacy_version"),
        "required_privacy_version": PRIVACY_VERSION,
        "plan": user.get("plan", "free"),
    }


def token_response(user: dict) -> dict:
    return {
        "access_token": create_access_token(user["id"], user["username"]),
        "token_type": "bearer",
        "user": public_user(user),
    }


def require_privacy_consent(user: User) -> dict:
    if (
        not user.get("privacy_accepted_at")
        or user.get("privacy_version") != PRIVACY_VERSION
    ):
        raise HTTPException(
            status_code=428,
            detail="Consent to the current privacy policy is required.",
        )
    return user


ConsentedUser = Annotated[dict, Depends(require_privacy_consent)]


def billing_status(user: dict) -> dict:
    plan = user.get("plan", "free")
    limits = plan_limits(plan)
    usage = database.token_usage(user["id"])
    return {
        "plan": plan,
        "period": database.usage_period(),
        "usage": usage,
        "limits": limits,
        "remaining_tokens": max(0, limits["monthly_tokens"] - usage["total_tokens"]),
        "space_count": database.count_spaces(user["id"]),
        "apple_store": {
            "status": "configuration_required",
            "product_id": None,
            "message": (
                "Create subscription products in App Store Connect and enable "
                "server-side transaction verification before turning on purchases."
            ),
        },
    }


def resolve_session(
    user_id: int,
    session_id: str | None,
    workspace: str | None = None,
) -> dict:
    if not session_id:
        return database.create_session(
            user_id,
            workspace=validate_workspace(workspace),
        )
    session = database.get_session(session_id, user_id)
    if not session:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    if workspace and session["workspace"] != validate_workspace(workspace):
        raise HTTPException(
            status_code=409,
            detail="The conversation belongs to a different workspace.",
        )
    return session


def prepare_chat(
    user_id: int,
    request: ChatRequest,
    *,
    include_context: bool = True,
) -> tuple[dict, list, list]:
    session = resolve_session(user_id, request.session_id, request.workspace)
    workspace = session["workspace"]
    context = retrieve_context(user_id, request.message, workspace) if include_context else []
    database.append_message(session["id"], "user", request.message)
    messages = build_messages(user_id, session["id"], context, workspace)
    sources = [
        {
            "document_id": item["document_id"],
            "name": item["name"],
            "page": item.get("page"),
            "score": item["score"],
        }
        for item in context
    ]
    return session, messages, sources


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "version": app.version}


def require_admin_dashboard_token(
    token: Annotated[
        str | None,
        Header(alias="X-Admin-Token"),
    ] = None,
) -> None:
    configured_token = settings.admin_dashboard_token
    if not configured_token:
        raise HTTPException(
            status_code=503,
            detail="Admin usage dashboard is not configured.",
        )
    if not token or not secrets.compare_digest(token, configured_token):
        raise HTTPException(status_code=401, detail="Invalid admin dashboard token.")


def require_recruitment_ingest_token(
    token: Annotated[
        str | None,
        Header(alias="X-Recruitment-Token"),
    ] = None,
) -> None:
    configured_token = settings.recruitment_ingest_token
    if not configured_token:
        raise HTTPException(status_code=503, detail="Recruitment ingest is not configured.")
    if not token or not secrets.compare_digest(token, configured_token):
        raise HTTPException(status_code=401, detail="Invalid recruitment ingest token.")


@app.get("/api/admin/usage")
def admin_usage(
    _: Annotated[None, Depends(require_admin_dashboard_token)],
    hours: int = Query(default=24, ge=1, le=720),
    bucket_minutes: int = Query(default=60, ge=5, le=1_440),
) -> dict:
    if math.ceil(hours * 60 / bucket_minutes) > 1_000:
        raise HTTPException(
            status_code=422,
            detail="Requested window creates too many buckets; increase bucket_minutes.",
        )
    return database.aggregate_admin_usage(hours, bucket_minutes)


def _public_radar_source(source: dict) -> dict:
    """Expose operational source health without leaking adapter internals."""
    allowed = (
        "id", "name", "platform", "company", "source_type", "url", "domain",
        "account_name", "enabled", "priority", "trust_level", "interval_minutes",
        "status", "verification_status", "last_checked_at", "last_success_at",
        "last_error_at", "last_error", "consecutive_failures", "created_at", "updated_at",
        "latest_article_title", "latest_article_at",
    )
    return {key: source.get(key) for key in allowed}


def _reject_secret_like_config(value: object, *, path: str = "config") -> None:
    """Source config is versioned operational data, never a secret store."""
    if isinstance(value, dict):
        for key, item in value.items():
            if any(marker in str(key).casefold() for marker in ("secret", "token", "password", "api_key", "apikey")):
                raise HTTPException(
                    status_code=422,
                    detail=f"{path}.{key} must be configured through server environment variables.",
                )
            _reject_secret_like_config(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_like_config(item, path=f"{path}[{index}]")


@app.get("/api/future-radar/dashboard")
def future_radar_dashboard(user: User) -> dict:
    del user
    return future_radar_service.repository.dashboard()


@app.get("/api/future-radar/jobs")
def future_radar_jobs(
    user: User,
    page: int = Query(default=1, ge=1, le=100_000),
    page_size: int = Query(default=50, ge=1, le=100),
    status_filter: Literal["open", "closed", "unknown", "all"] = Query(default="open", alias="status"),
    verification_status: Literal["pending", "verified", "conflicted", "rejected"] | None = None,
    company: str | None = Query(default=None, max_length=160),
    city: str | None = Query(default=None, max_length=160),
    region: str | None = Query(default=None, max_length=160),
    employer_type: str | None = Query(default=None, max_length=80),
    industry: str | None = Query(default=None, max_length=120),
    program_id: str | None = Query(default=None, max_length=180),
    source_id: str | None = Query(default=None, max_length=64),
    q: str | None = Query(default=None, max_length=160),
    event_type: Literal["NEW", "UPDATED", "CLOSED", "REOPENED", "VERIFIED"] | None = None,
    opening_before: date | None = None,
    opening_after: date | None = None,
    closing_before: date | None = None,
    closing_after: date | None = None,
    sort: Literal["changed", "closing", "opening", "first_seen", "company"] = "changed",
) -> dict:
    filters = {
        "status": status_filter,
        "verification_status": verification_status,
        "company": company,
        "city": city,
        "region": region,
        "employer_type": employer_type,
        "industry": industry,
        "program_id": program_id,
        "source_id": source_id,
        "q": q,
        "event_type": event_type,
        "opening_before": opening_before.isoformat() if opening_before else None,
        "opening_after": opening_after.isoformat() if opening_after else None,
        "closing_before": closing_before.isoformat() if closing_before else None,
        "closing_after": closing_after.isoformat() if closing_after else None,
        "sort": sort,
        "active_only": status_filter == "open",
    }
    result = future_radar_service.repository.list_jobs(
        page=page, page_size=page_size, filters=filters
    )
    profile = database.get_recruitment_profile(user["id"])
    enriched = []
    for job in result["items"]:
        scored = score_job(job, profile)
        scored["employer_categories"] = sorted(semantic_employer_categories(scored))
        enriched.append(scored)
    result["items"] = enriched
    result["jobs"] = enriched
    result["tier_definitions"] = list(TIER_DEFINITIONS)
    return result


@app.get("/api/future-radar/jobs/{job_id}")
def future_radar_job(job_id: str, user: User) -> dict:
    job = future_radar_service.repository.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Radar job not found.")
    scored = score_job(job, database.get_recruitment_profile(user["id"]))
    scored["employer_categories"] = sorted(semantic_employer_categories(scored))
    return scored


@app.get("/api/future-radar/programs")
def future_radar_programs(
    user: User,
    page: int = Query(default=1, ge=1, le=100_000),
    page_size: int = Query(default=50, ge=1, le=100),
    status_filter: Literal["open", "closed", "unknown", "all"] = Query(default="open", alias="status"),
    q: str | None = Query(default=None, max_length=160),
) -> dict:
    del user
    result = future_radar_service.repository.list_programs(
        page=page, page_size=page_size, status=status_filter, q=q
    )
    result["programs"] = result["items"]
    return result


@app.get("/api/future-radar/programs/{program_id}")
def future_radar_program(program_id: str, user: User) -> dict:
    del user
    program = future_radar_service.repository.get_program(program_id)
    if not program:
        raise HTTPException(status_code=404, detail="Recruitment program not found.")
    return program


@app.get("/api/future-radar/events")
def future_radar_events(
    user: User,
    after_event_id: int | None = Query(default=None, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    event_type: str | None = Query(default=None, max_length=40),
) -> dict:
    del user
    result = future_radar_service.repository.list_events(
        after_event_id=after_event_id, limit=limit,
        event_type=event_type.upper() if event_type else None,
    )
    result["events"] = result["items"]
    if after_event_id is not None and result["items"]:
        result["dashboard"] = future_radar_service.repository.dashboard()
    return result


@app.get("/api/future-radar/changes")
def future_radar_changes(
    user: User,
    after_event_id: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    return future_radar_events(user, after_event_id, limit, None)


@app.get("/api/future-radar/runs")
def future_radar_runs(
    user: User,
    page: int = Query(default=1, ge=1, le=100_000),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    del user
    result = future_radar_service.repository.list_runs(page=page, page_size=page_size)
    result["runs"] = result["items"]
    return result


@app.get("/api/future-radar/runs/{run_id}")
def future_radar_run_detail(run_id: str, user: User) -> dict:
    del user
    run = future_radar_service.repository.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Radar run not found.")
    return run


@app.get("/api/future-radar/sources")
def future_radar_sources(user: User, enabled: bool | None = None) -> dict:
    del user
    sources = [
        _public_radar_source(source)
        for source in future_radar_service.repository.list_sources(enabled=enabled)
    ]
    return {"items": sources, "sources": sources, "total": len(sources)}


@app.post("/api/future-radar/run")
async def run_future_radar(
    _: Annotated[None, Depends(require_admin_dashboard_token)],
    request: RadarRunRequest | None = None,
) -> dict:
    payload = request or RadarRunRequest()
    for source_id in payload.source_ids:
        if not future_radar_service.repository.get_source(source_id):
            raise HTTPException(status_code=404, detail=f"Radar source not found: {source_id}")
    try:
        return await asyncio.to_thread(
            future_radar_service.run,
            trigger_type="manual",
            source_ids=payload.source_ids or None,
            force=payload.force,
        )
    except RadarRunBusy as exc:
        raise HTTPException(
            status_code=409,
            detail="A Future Radar run is already active.",
            headers={"Retry-After": "20"},
        ) from exc


@app.post("/api/future-radar/sync")
def sync_future_radar(
    request: FrostFireSyncV1,
    _: Annotated[None, Depends(require_recruitment_ingest_token)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    try:
        return future_radar_service.sync(
            request.model_dump(mode="json"), idempotency_key=idempotency_key
        )
    except SyncConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/future-radar/sources", status_code=status.HTTP_201_CREATED)
def create_future_radar_source(
    request: SourceCreateRequest,
    _: Annotated[None, Depends(require_admin_dashboard_token)],
) -> dict:
    payload = request.model_dump(mode="json")
    for key in ("adapter_config", "query_config", "region_config"):
        _reject_secret_like_config(payload.get(key, {}), path=key)
    if payload.get("url"):
        try:
            payload["url"] = canonicalize_radar_url(payload["url"], allow_empty=False)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        payload["domain"] = urllib.parse.urlsplit(payload["url"]).hostname
    try:
        return _public_radar_source(future_radar_service.repository.create_source(payload))
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Radar source ID already exists.") from exc


@app.patch("/api/future-radar/sources/{source_id}")
def patch_future_radar_source(
    source_id: str,
    request: SourcePatchRequest,
    _: Annotated[None, Depends(require_admin_dashboard_token)],
) -> dict:
    changes = request.model_dump(mode="json", exclude_unset=True)
    for key in ("adapter_config", "query_config", "region_config"):
        if key in changes:
            _reject_secret_like_config(changes[key], path=key)
    if changes.get("url"):
        try:
            changes["url"] = canonicalize_radar_url(changes["url"], allow_empty=False)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        changes["domain"] = urllib.parse.urlsplit(changes["url"]).hostname
    source = future_radar_service.repository.patch_source(source_id, changes)
    if not source:
        raise HTTPException(status_code=404, detail="Radar source not found.")
    return _public_radar_source(source)


@app.get("/api/workspaces")
def workspaces() -> list[dict]:
    return public_workspace_config()


@app.post("/api/auth/register", status_code=status.HTTP_201_CREATED)
def register(request: AuthRequest, raw_request: Request) -> dict:
    enforce_registration_rate()
    if not request.privacy_accepted:
        raise HTTPException(
            status_code=400,
            detail="You must accept the privacy policy before creating an account.",
        )
    try:
        user = database.create_user(
            request.username.strip(),
            hash_password(request.password),
            PRIVACY_VERSION,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raw_request.state.user_id = user["id"]
    return token_response(user)


@app.post("/api/auth/login")
def login(request: AuthRequest, raw_request: Request) -> dict:
    user = database.get_user_by_username(request.username.strip())
    if not user or not verify_password(request.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    raw_request.state.user_id = user["id"]
    return token_response(user)


@app.get("/api/auth/me")
def me(user: User) -> dict:
    return public_user(user)


@app.get("/api/legal/disclosures")
def legal_disclosures() -> dict:
    return {
        "privacy_version": PRIVACY_VERSION,
        "third_party_ai": "OpenAI API",
        "legal_boundary": "Document review assistance, not legal advice.",
        "finance_boundary": "Research assistance, not personalized investment advice.",
    }


@app.get("/api/platform/templates")
def platform_templates() -> list[dict]:
    return [
        {"id": template_id, **template}
        for template_id, template in SPACE_TEMPLATES.items()
    ]


def public_recruitment_profile(profile: dict) -> dict:
    return {
        "desired_roles": profile.get("desired_roles", []),
        "industries": profile.get("industries", []),
        "locations": profile.get("locations", []),
        "employer_types": profile.get("employer_types", []),
    }


def public_chatgpt_sync_status() -> dict:
    detailed = database.recruitment_sync_status(
        expected_source_count=len(EXPECTED_CHATGPT_RADAR_SOURCES)
    )
    sources = [
        source
        for source in detailed["sources"]
        if source.get("source_id") in EXPECTED_CHATGPT_SOURCE_IDS
    ]
    connected = sum(source.get("last_seen_at") is not None for source in sources)
    latest_accepted = sum(int(source.get("latest_accepted", 0)) for source in sources)
    latest_pending = sum(int(source.get("latest_pending", 0)) for source in sources)
    latest_rejected = sum(int(source.get("latest_rejected", 0)) for source in sources)
    inventory_accepted = sum(int(source.get("inventory_accepted", 0)) for source in sources)
    inventory_pending = sum(int(source.get("inventory_pending", 0)) for source in sources)
    inventory_rejected = sum(int(source.get("inventory_rejected", 0)) for source in sources)
    last_synced_at = max(
        (source["last_seen_at"] for source in sources if source.get("last_seen_at")),
        default=None,
    )
    if connected == 0:
        sync_state = "pending"
    elif any(source.get("status") == "error" for source in sources):
        sync_state = "error"
    elif (
        connected < len(EXPECTED_CHATGPT_RADAR_SOURCES)
        or any(source.get("status") == "partial" for source in sources)
    ):
        sync_state = "partial"
    else:
        sync_state = "synced"
    return {
        "status": sync_state,
        "expected_source_count": len(EXPECTED_CHATGPT_RADAR_SOURCES),
        "connected_source_count": connected,
        "last_synced_at": last_synced_at,
        "accepted": latest_accepted,
        "pending": latest_pending,
        "rejected": latest_rejected,
        "inventory_accepted": inventory_accepted,
        "inventory_pending": inventory_pending,
        "inventory_rejected": inventory_rejected,
    }


@app.get("/api/recruitment/profile")
def recruitment_profile(user: User) -> dict:
    return public_recruitment_profile(database.get_recruitment_profile(user["id"]))


@app.put("/api/recruitment/profile")
def save_recruitment_profile(request: RecruitmentProfileRequest, user: ConsentedUser) -> dict:
    payload = request.model_dump()
    for key in ("desired_roles", "industries", "locations", "employer_types"):
        payload[key] = [str(value).strip()[:80] for value in payload[key] if str(value).strip()]
    return public_recruitment_profile(database.save_recruitment_profile(user["id"], payload))


@app.get("/api/recruitment/jobs")
def recruitment_jobs(user: User) -> dict:
    profile = database.get_recruitment_profile(user["id"])
    available_jobs = database.list_recruitment_jobs()
    watch_summary = database.recruitment_watch_summary(user["id"])
    job_summary = database.recruitment_job_summary()
    def is_verified_display_job(job: dict) -> bool:
        tags = set(job.get("tags") or [])
        if tags.intersection({"待官方核验", "待打开核对"}):
            return False
        if "动态监控" in tags:
            # Authorized monitor candidates already passed campus, city,
            # company, title and official-page verification at ingestion.
            return {"链接已验证", "标题已验证"}.issubset(tags)
        return is_priority_campus_listing(job)

    scored_jobs = [
        score_job(job, profile)
        for job in available_jobs
        if is_verified_display_job(job)
    ]
    scored_jobs = [
        job
        for job in scored_jobs
        if job["days_left"] is None or job["days_left"] > 0
    ]
    for job in scored_jobs:
        job["employer_categories"] = sorted(semantic_employer_categories(job))
    below_priority_count = sum(
        job.get("tier_code") == "不建议投" for job in scored_jobs
    )
    jobs = [
        job
        for job in scored_jobs
        if job.get("tier_code") != "不建议投"
        and job_matches_profile(job, profile)
    ]
    jobs.sort(
        key=lambda item: (
            -item["match_score"],
            item["days_left"] is None,
            item["days_left"] if item["days_left"] is not None else 9999,
        )
    )
    public_source_leads = 0
    verified_jobs = len(scored_jobs)
    quarantined_leads = max(0, len(available_jobs) - len(scored_jobs))
    tier_counts = {
        tier: sum(job.get("tier_code") == tier for job in jobs)
        for tier in (definition["code"] for definition in TIER_DEFINITIONS)
    }
    web_search_state = database.get_system_state(WEB_SEARCH_STATE_KEY) or {
        "status": "disabled" if not settings.recruitment_web_search_enabled else "pending"
    }
    web_search_copy = ""
    if web_search_state.get("status") == "success":
        web_search_copy = (
            f"AI 网页搜索最近发现 {web_search_state.get('jobs', 0)} 条候选，"
            "系统已逐条执行链接与标题正文证据核验。"
        )
    elif web_search_state.get("status") == "error":
        web_search_copy = "AI 网页搜索本轮未完成，已保留上一轮岗位。"
    elif web_search_state.get("status") == "pending":
        web_search_copy = "AI 网页搜索等待首次运行。"
    inventory_copy = (
        f"当前筛选显示 {len(jobs)} 个仍在时间窗内的重点机会；"
        f"正文证据已核验 {verified_jobs} 个，另有 {quarantined_leads} 个候选信号"
        "留在核验区、不会进入主池。"
        f"低于 T3 标准的 {below_priority_count} 个岗位未纳入重点池。"
        f"{web_search_copy}"
    )
    if watch_summary["total"] == 0:
        source_message = (
            inventory_copy + "尚未创建零 Token 企业动态监控。"
        )
    elif watch_summary["last_checked_at"] is None:
        source_message = (
            f"已创建 {watch_summary['total']} 个零 Token 企业动态监控，等待首次检查；"
            + inventory_copy
        )
    else:
        source_message = (
            f"{watch_summary['enabled']} 个零 Token 企业动态监控最近检查于 "
            f"{watch_summary['last_checked_at']}；发现 {watch_summary['changed']} 个页面变化，"
            f"{watch_summary['errors']} 个检查失败。{inventory_copy}"
        )
    return {
        "jobs": jobs,
        "profile": public_recruitment_profile(profile),
        "monitor_pools": PERSONAL_MONITOR_POOLS,
        "data_status": {
            "mode": "hybrid_live",
            "method": "public_crawl_plus_bounded_web_search",
            "message": source_message,
            "last_sync": watch_summary["last_checked_at"],
            "last_job_verified_at": job_summary["last_verified_at"],
            "open_jobs": job_summary["open_jobs"],
            "verified_jobs": verified_jobs,
            "matched_jobs": len(jobs),
            "below_priority_jobs": below_priority_count,
            "public_source_leads": public_source_leads,
            "quarantined_leads": quarantined_leads,
            "tier_counts": tier_counts,
            "tier_definitions": list(TIER_DEFINITIONS),
            "web_search": web_search_state,
            "chatgpt_sync": public_chatgpt_sync_status(),
            "watches": watch_summary,
            "model_tokens_used": int(web_search_state.get("total_tokens", 0) or 0),
        },
    }


@app.get("/api/recruitment/watches")
def recruitment_watches(user: User) -> dict:
    watches = database.list_recruitment_watches(user["id"])
    return {
        "watches": watches,
        "summary": database.recruitment_watch_summary(user["id"]),
        "method": "deterministic_pool_or_html_fingerprint",
        "model_tokens_used": 0,
    }


@app.post("/api/recruitment/watches", status_code=status.HTTP_201_CREATED)
def create_recruitment_watch(
    request: RecruitmentWatchCreateRequest,
    user: ConsentedUser,
) -> dict:
    company_name = str(request.company_name or "").strip()[:120]
    watch_type = "page"
    if company_name and not request.url:
        watch_type = "company"
        display_url = f"company://{hashlib.sha256(company_name.casefold().encode()).hexdigest()[:32]}"
        fetch_url = display_url
        keywords = [company_name]
        watch_name = f"{company_name} · 机会信号"
    else:
        if not request.name or not request.url:
            raise HTTPException(status_code=422, detail="请只填写企业名称，或提供完整的公开招聘页监控信息。")
        try:
            display_url, fetch_url = normalize_public_https_urls(
                request.url,
                resolve_dns=False,
            )
        except WatchFetchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        keywords = list(dict.fromkeys(
            str(value).strip()[:80]
            for value in request.keywords
            if str(value).strip()
        ))
        if not keywords:
            raise HTTPException(status_code=422, detail="请至少填写一个监控关键词。")
        watch_name = request.name.strip()
    enforce_watch_create_rate(user["id"])
    try:
        watch = database.create_recruitment_watch(
            user["id"],
            watch_name,
            display_url,
            fetch_url,
            keywords,
            watch_type=watch_type,
            company_name=company_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # Baselines use the same single-flight lock and outbound slot guard as all
    # later checks, so creation cannot race a scheduled refresh.
    if watch_type == "company":
        return _refresh_company_watch_sync(user["id"], {**watch, "company_name": company_name})
    return _refresh_user_watch_sync(
        user["id"],
        {**watch, "fetch_url": fetch_url},
        WATCH_BASELINE_SLOT_TIMEOUT_SECONDS,
    )


def _fetch_watch_with_global_limit(
    watch: dict,
    slot_timeout: float = WATCH_FETCH_SLOT_TIMEOUT_SECONDS,
):
    acquired = _watch_fetch_slots.acquire(timeout=slot_timeout)
    if not acquired:
        raise WatchFetchError("官网监控当前繁忙，请稍后重试。")
    try:
        return fetch_watch_page(
            watch.get("fetch_url") or watch["url"],
            watch["keywords"],
        )
    finally:
        _watch_fetch_slots.release()


def _refresh_company_watch_sync(user_id: int, watch: dict) -> dict:
    """Fingerprint matching open jobs for a company-only watch."""
    company_name = str(watch.get("company_name") or watch.get("name") or "").strip()
    folded = company_name.casefold()
    matching = [
        job for job in database.list_recruitment_jobs()
        if folded in str(job.get("company", "")).casefold()
        or str(job.get("company", "")).casefold() in folded
    ]
    snapshot = [
        {
            "id": job.get("id"),
            "title": job.get("title"),
            "requirements": job.get("requirements"),
            "closing_date": job.get("closing_date"),
            "status": job.get("status"),
            "url": job.get("url"),
        }
        for job in sorted(matching, key=lambda item: str(item.get("id", "")))
    ]
    fingerprint = hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    hits = [str(job.get("title", "机会信号")) for job in matching[:6]]
    return database.record_recruitment_watch_success(
        user_id,
        watch["id"],
        fingerprint,
        hits,
        200,
    ) or {"id": watch["id"], "last_status": "deleted"}


def _refresh_user_watch_sync(
    user_id: int,
    watch: dict,
    slot_timeout: float = WATCH_FETCH_SLOT_TIMEOUT_SECONDS,
) -> dict:
    if watch.get("watch_type") == "company":
        return _refresh_company_watch_sync(user_id, watch)
    with watch_refresh_lock(watch["id"]):
        try:
            result = _fetch_watch_with_global_limit(watch, slot_timeout)
            updated = database.record_recruitment_watch_success(
                user_id,
                watch["id"],
                result.fingerprint,
                result.keyword_hits,
                result.http_status,
            )
        except WatchFetchError as exc:
            updated = database.record_recruitment_watch_error(
                user_id,
                watch["id"],
                str(exc),
            )
        except Exception:
            logger.exception("Unexpected recruitment watch refresh failure")
            updated = database.record_recruitment_watch_error(
                user_id,
                watch["id"],
                "监控页面暂时无法检查。",
            )
        if not updated:
            return {"id": watch["id"], "last_status": "deleted"}
        return updated


async def _refresh_user_watch(user_id: int, watch: dict) -> dict:
    return await asyncio.to_thread(_refresh_user_watch_sync, user_id, watch)


async def _refresh_watch_batch(watches: list[dict]) -> list[dict]:
    """Run bounded groups so large watch sets do not occupy waiting threads."""
    results: list[dict] = []
    for offset in range(0, len(watches), 4):
        group = watches[offset:offset + 4]
        results.extend(await asyncio.gather(*(
            _refresh_user_watch(watch["user_id"], watch)
            for watch in group
        )))
    return results


@app.post("/api/recruitment/watches/refresh")
async def refresh_recruitment_watches(user: ConsentedUser) -> dict:
    now = time.monotonic()
    with _watch_refresh_cooldown_guard:
        last_request = _watch_refresh_last_request.get(user["id"], 0.0)
        retry_after = WATCH_REFRESH_COOLDOWN_SECONDS - (now - last_request)
        if retry_after > 0:
            raise HTTPException(
                status_code=429,
                detail="官网变化雷达刷新过于频繁，请稍后重试。",
                headers={"Retry-After": str(max(1, int(retry_after)))},
            )
        _watch_refresh_last_request[user["id"]] = now
    watches = [
        watch
        for watch in database.list_recruitment_watches(user["id"])
        if watch["enabled"]
    ]
    internal_watches = [
        {
            **watch,
            "user_id": user["id"],
        }
        for watch in watches
    ]
    results = await _refresh_watch_batch(internal_watches)
    counts = {"baseline": 0, "changed": 0, "unchanged": 0, "error": 0}
    for item in results:
        result_status = item.get("last_status")
        if result_status in counts:
            counts[result_status] += 1
    return {
        "checked": len(results),
        "counts": counts,
        "watches": results,
        "summary": database.recruitment_watch_summary(user["id"]),
        "refreshed_at": database.utc_now(),
        "model_tokens_used": 0,
    }


@app.delete("/api/recruitment/watches/{watch_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_recruitment_watch(watch_id: str, user: ConsentedUser) -> Response:
    if not database.get_recruitment_watch(user["id"], watch_id):
        raise HTTPException(status_code=404, detail="动态监控不存在。")
    lock = watch_refresh_lock(watch_id)
    with lock:
        if not database.delete_recruitment_watch(user["id"], watch_id):
            raise HTTPException(status_code=404, detail="动态监控不存在。")
        with _watch_lock_registry_guard:
            if _watch_lock_registry.get(watch_id) is lock:
                _watch_lock_registry.pop(watch_id, None)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/api/recruitment/watches/{watch_id}/acknowledge")
def acknowledge_recruitment_watch(
    watch_id: str,
    request: RecruitmentWatchAcknowledgeRequest,
    user: ConsentedUser,
) -> dict:
    if not database.get_recruitment_watch(user["id"], watch_id):
        raise HTTPException(status_code=404, detail="动态监控不存在。")
    try:
        with watch_refresh_lock(watch_id):
            watch = database.acknowledge_recruitment_watch_change(
                user["id"],
                watch_id,
                request.change_version,
            )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not watch:
        raise HTTPException(status_code=404, detail="动态监控不存在。")
    return watch


@app.post("/api/recruitment/refresh")
def refresh_recruitment(
    user: ConsentedUser,
    deep_search: bool = Query(default=False),
) -> dict:
    """Refresh deterministic sources and optionally run a rate-limited web search."""
    del user
    web_state_before = database.get_system_state(WEB_SEARCH_STATE_KEY)
    run_deep_search = False
    if not deep_search:
        skip_reason = "deep_search_not_requested"
    elif not settings.recruitment_web_search_enabled:
        skip_reason = "web_search_disabled"
    elif not _deep_search_is_available(web_state_before):
        skip_reason = "deep_search_cooldown"
    else:
        run_deep_search = True
        skip_reason = None

    with _recruitment_source_refresh_state_guard:
        age = time.monotonic() - _recruitment_source_last_refresh
        cached_count = _recruitment_source_last_count
    if (
        _recruitment_source_last_refresh
        and age < RECRUITMENT_SOURCE_COOLDOWN_SECONDS
        and not run_deep_search
    ):
        return {
            "source": "公开机会页面 + 低频 AI 补漏 + 已配置 API",
            "count": cached_count,
            "refreshed_at": database.utc_now(),
            "cached": True,
            "web_search_ran": False,
            "skip_reason": skip_reason,
            "next_due_at": (
                _deep_search_next_due_at(web_state_before)
                if settings.recruitment_web_search_enabled else None
            ),
            "web_search": web_state_before,
        }
    try:
        count = refresh_recruitment_sources(
            include_web_search=run_deep_search,
            force_web_search=run_deep_search,
        )
    except RecruitmentRefreshBusy as exc:
        raise HTTPException(
            status_code=429,
            detail="公开信号源正在刷新，请稍后重试。",
            headers={"Retry-After": "15"},
        ) from exc
    except Exception as exc:
        logger.exception("Recruitment source refresh failed")
        raise HTTPException(status_code=502, detail="公开信号源刷新失败，请稍后重试。") from exc
    web_state_after = database.get_system_state(WEB_SEARCH_STATE_KEY)
    before_attempt = _web_search_attempted_at(web_state_before)
    after_attempt = _web_search_attempted_at(web_state_after)
    web_search_ran = bool(
        run_deep_search
        and after_attempt is not None
        and after_attempt != before_attempt
    )
    if run_deep_search and not web_search_ran:
        skip_reason = "web_search_not_started"
    return {
        "source": "公开机会页面 + 低频 AI 补漏 + 已配置 API",
        "count": count,
        "refreshed_at": database.utc_now(),
        "cached": False,
        "web_search_ran": web_search_ran,
        "skip_reason": skip_reason,
        "next_due_at": (
            _deep_search_next_due_at(web_state_after)
            if settings.recruitment_web_search_enabled else None
        ),
        "web_search": web_state_after,
    }


_TRACKING_QUERY_KEYS = {"fbclid", "gclid", "msclkid"}
_CAMPUS_PAGE_MARKERS = (
    "校园招聘", "秋季招聘", "秋招", "校招", "应届生", "应届毕业生",
    "graduate", "graduates", "campus", "early career",
)
_GENERIC_IDENTITY_TERMS = {
    "校园招聘", "秋季招聘", "秋招", "校招", "应届", "应届生", "毕业生",
    "招聘", "岗位", "职位", "graduate", "graduates", "campus", "career",
}
_CLOSED_PAGE_PATTERN = re.compile(
    r"(?:已截止|申请已结束|报名已结束|网申已结束|投递已结束|职位已关闭|岗位已关闭|"
    r"申请通道已关闭|不再接受申请|\bclosed\b|\bexpired\b|no\s+longer\s+accepting)",
    re.IGNORECASE,
)


def canonicalize_recruitment_url(url: str) -> str:
    """Return a public HTTPS URL with fragments and known tracking keys removed."""
    display_url, _ = normalize_public_https_urls(url, resolve_dns=False)
    parsed = urllib.parse.urlsplit(display_url)
    hostname = (parsed.hostname or "").casefold()
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    filtered_query = []
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        normalized_key = key.casefold()
        if normalized_key.startswith("utm_") or normalized_key in _TRACKING_QUERY_KEYS:
            continue
        filtered_query.append((key, value))
    filtered_query.sort(key=lambda item: (item[0].casefold(), item[1]))
    return urllib.parse.urlunsplit((
        "https",
        netloc,
        parsed.path or "/",
        urllib.parse.urlencode(filtered_query, doseq=True),
        "",
    ))


def _normalized_identity(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value.casefold())


def _page_contains_identity(page_text: str, value: str) -> bool:
    normalized_page = _normalized_identity(page_text)
    normalized_value = _normalized_identity(value)
    if len(normalized_value) >= 3 and normalized_value in normalized_page:
        return True
    pieces = []
    for piece in re.split(r"[\s/|·,，、()（）\[\]【】:_-]+", value.casefold()):
        normalized_piece = _normalized_identity(piece)
        if (
            len(normalized_piece) >= 3
            and normalized_piece not in _GENERIC_IDENTITY_TERMS
        ):
            pieces.append(normalized_piece)
    return bool(pieces) and any(piece in normalized_page for piece in pieces)


def _meaningful_title_terms(title: str) -> list[str]:
    value = title.casefold()
    value = re.sub(r"20\d{2}\s*(?:年|届)?", " ", value)
    for generic in sorted(_GENERIC_IDENTITY_TERMS, key=len, reverse=True):
        value = value.replace(generic, " ")
    terms = []
    for term in re.findall(r"[a-z][a-z0-9+.#-]{1,}|[\u4e00-\u9fff]{2,}", value):
        normalized = _normalized_identity(term)
        if len(normalized) >= 2 and normalized not in _GENERIC_IDENTITY_TERMS:
            terms.append(normalized)
    return list(dict.fromkeys(terms))


def _page_contains_strict_title(page_text: str, title: str) -> bool:
    normalized_page = _normalized_identity(page_text)
    normalized_title = _normalized_identity(title)
    if len(normalized_title) >= 4 and normalized_title in normalized_page:
        return True
    terms = _meaningful_title_terms(title)
    if len(terms) < 2:
        return False
    matched = [term for term in terms if term in normalized_page]
    required_terms = max(2, math.ceil(len(terms) * 0.75))
    total_chars = sum(len(term) for term in terms)
    matched_chars = sum(len(term) for term in matched)
    return (
        len(matched) >= required_terms
        and matched_chars >= max(8, math.ceil(total_chars * 0.75))
    )


def _page_contains_date(page_text: str, iso_date: str | None) -> bool:
    if not iso_date:
        return False
    try:
        value = date.fromisoformat(iso_date)
    except ValueError:
        return False
    compact_page = re.sub(r"\s+", "", page_text.casefold())
    variants = {
        value.isoformat(),
        f"{value.year}/{value.month:02d}/{value.day:02d}",
        f"{value.year}.{value.month:02d}.{value.day:02d}",
        f"{value.year}年{value.month}月{value.day}日",
        f"{value.year}年{value.month:02d}月{value.day:02d}日",
    }
    return any(variant.casefold() in compact_page for variant in variants)


def _verify_ingest_candidate(
    candidate: dict,
) -> tuple[str, str | None, dict[str, str | None]]:
    verified_dates = {"opening_date": None, "closing_date": None}
    actionable = {
        "title": candidate["title"],
        "requirements": candidate.get("requirements", ""),
        "tags": candidate.get("tags", []),
    }
    if not is_actionable_recruitment_listing(actionable):
        return "rejected", "not_campus", verified_dates
    location_text = f"{candidate['city']} {candidate['title']}"
    if not any(marker in location_text for marker in CORE_LOCATION_MARKERS):
        return "rejected", "location_outside_scope", verified_dates
    try:
        page = fetch_watch_page(candidate["canonical_url"], (), timeout_seconds=5)
    except WatchFetchError:
        return "pending", "official_page_fetch_failed", verified_dates
    except Exception:
        logger.exception("Unexpected recruitment candidate verification failure")
        return "pending", "official_page_fetch_failed", verified_dates
    page_text = str(page.text or "")
    normalized_page = page_text.casefold()
    if _CLOSED_PAGE_PATTERN.search(normalized_page):
        return "closed", "official_page_closed", verified_dates
    if not any(marker in normalized_page for marker in _CAMPUS_PAGE_MARKERS):
        return "rejected", "page_missing_campus_signal", verified_dates
    if not _page_contains_identity(page_text, candidate["company"]):
        return "pending", "page_missing_company_evidence", verified_dates
    if not _page_contains_strict_title(page_text, candidate["title"]):
        return "pending", "page_missing_title_evidence", verified_dates
    for field in ("opening_date", "closing_date"):
        submitted_date = candidate.get(field)
        if submitted_date and _page_contains_date(page_text, submitted_date):
            verified_dates[field] = submitted_date
    return "verified", None, verified_dates


def _restore_verified_snapshot_job(job: dict) -> str:
    """Rebuild one last-known-good public job only after a live page check."""
    today = date.today().isoformat()
    opening_date = job.get("opening_date")
    closing_date = job.get("closing_date")
    if job.get("status") != "open" or (closing_date and closing_date <= today):
        database.close_recruitment_job(str(job["id"]))
        return "closed"
    if opening_date and opening_date > today:
        database.close_recruitment_job(str(job["id"]))
        return "future"

    candidate = {
        "company": str(job.get("company", "")),
        "title": str(job.get("title", "")),
        "city": str(job.get("city", "")),
        "requirements": str(job.get("requirements", "")),
        "tags": list(job.get("tags", [])),
        "canonical_url": str(job.get("url", "")),
        "opening_date": opening_date,
        "closing_date": closing_date,
    }
    verification_status, _, verified_dates = _verify_ingest_candidate(candidate)
    if verification_status == "verified":
        restored = {
            **job,
            "opening_date": verified_dates["opening_date"],
            "closing_date": verified_dates["closing_date"],
            "last_verified_at": database.utc_now(),
            "status": "open",
        }
        database.upsert_recruitment_jobs([restored])
    elif verification_status in {"closed", "rejected"}:
        # A readable page that no longer contains campus evidence is no longer
        # eligible as last-known-good. Temporary fetch/title ambiguity remains
        # pending and is retried without deleting the prior verified row.
        database.close_recruitment_job(str(job["id"]))
    # A temporary fetch failure never creates a new row and never overwrites a
    # last-known-good row.  The next scheduled pass retries the official page.
    return verification_status


async def restore_verified_radar_snapshot() -> dict[str, int]:
    """Re-verify the public five-monitor snapshot after an ephemeral restart."""
    counts = {"verified": 0, "closed": 0, "pending": 0, "rejected": 0, "future": 0}
    semaphore = asyncio.Semaphore(4)

    async def restore_one(job: dict) -> str:
        async with semaphore:
            return await asyncio.to_thread(_restore_verified_snapshot_job, dict(job))

    statuses = await asyncio.gather(
        *(restore_one(job) for job in CURATED_CAMPUS_JOBS),
        return_exceptions=True,
    )
    for result in statuses:
        status_name = "pending" if isinstance(result, Exception) else str(result)
        counts[status_name if status_name in counts else "pending"] += 1
    return counts


def _ingest_dedupe_key(
    *,
    source_id: str,
    source_thread_id: str | None,
    source_item_id: str | None,
    external_id: str | None,
    company: str,
    title: str,
    city: str,
    canonical_url: str,
) -> str:
    if external_id:
        material = f"source:{source_id.casefold()}|external:{external_id.casefold()}"
    elif source_item_id:
        material = (
            f"source:{source_id.casefold()}|thread:{(source_thread_id or '').casefold()}|"
            f"item:{source_item_id.casefold()}"
        )
    else:
        material = "|".join((
            "fallback",
            _normalized_identity(company),
            _normalized_identity(title),
            _normalized_identity(city),
            canonical_url,
        ))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _normalized_source_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _max_source_timestamp(current: str | None, candidate: str | None) -> str | None:
    if not current:
        return candidate
    if not candidate:
        return current
    try:
        current_value = datetime.fromisoformat(current.replace("Z", "+00:00"))
        candidate_value = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        if current_value.tzinfo is None:
            current_value = current_value.replace(tzinfo=timezone.utc)
        if candidate_value.tzinfo is None:
            candidate_value = candidate_value.replace(tzinfo=timezone.utc)
    except ValueError:
        return max(current, candidate)
    return candidate if candidate_value >= current_value else current


def _candidate_from_ingest_item(
    item: RecruitmentIngestJob,
    *,
    batch_source_id: str | None = None,
    batch_source_updated_at: datetime | None = None,
) -> tuple[dict, str | None]:
    source_id = (batch_source_id or item.source_id).strip()
    raw_source_thread_id = (item.source_thread_id or "").strip() or None
    source_thread_id = None
    if raw_source_thread_id and source_id not in EXPECTED_CHATGPT_SOURCE_IDS:
        source_thread_id = (
            "sha256:" + hashlib.sha256(raw_source_thread_id.encode("utf-8")).hexdigest()[:24]
        )
    source_item_id = (item.source_item_id or "").strip() or None
    external_id = (item.external_id or "").strip() or None
    try:
        display_url, _ = normalize_public_https_urls(
            item.official_url,
            resolve_dns=False,
        )
        canonical_url = canonicalize_recruitment_url(item.official_url)
        url_error = None
    except WatchFetchError:
        display_url = item.official_url.strip()
        canonical_url = display_url.split("#", 1)[0]
        url_error = "invalid_official_url"
    source_updated_at = _normalized_source_timestamp(
        item.source_updated_at or batch_source_updated_at
    )
    dedupe_key = _ingest_dedupe_key(
        source_id=source_id,
        source_thread_id=source_thread_id,
        source_item_id=source_item_id,
        external_id=external_id,
        company=item.company.strip(),
        title=item.title.strip(),
        city=item.city.strip(),
        canonical_url=canonical_url,
    )
    candidate = {
        "id": f"candidate-{dedupe_key[:32]}",
        "dedupe_key": dedupe_key,
        "source_key": database.recruitment_ingest_source_key(source_id, source_thread_id),
        "source_id": source_id,
        "source_thread_id": source_thread_id,
        "source_item_id": source_item_id,
        "external_id": external_id,
        "source_updated_at": source_updated_at,
        "company": item.company.strip(),
        "employer_type": item.employer_type.strip() or "重点雇主",
        "title": item.title.strip(),
        "city": item.city.strip(),
        "industry": item.industry.strip(),
        "official_url": display_url,
        "canonical_url": canonical_url,
        "source": item.source.strip() or source_id,
        "opening_date": item.opening_date.isoformat() if item.opening_date else None,
        "closing_date": item.closing_date.isoformat() if item.closing_date else None,
        "requirements": item.requirements.strip(),
        "tags": [str(tag).strip()[:120] for tag in item.tags if str(tag).strip()],
        "evidence": [evidence.strip() for evidence in item.evidence if evidence.strip()],
        "incoming_status": item.status,
    }
    payload_fields = {
        key: value
        for key, value in candidate.items()
        if key not in {"id", "dedupe_key", "source_key", "source_updated_at"}
    }
    candidate["payload_hash"] = hashlib.sha256(
        json.dumps(
            payload_fields,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return candidate, url_error


def _promoted_job(candidate: dict) -> dict:
    if (
        candidate.get("external_id")
        and candidate.get("source_id") in EXPECTED_CHATGPT_SOURCE_IDS
    ):
        identity = (
            f"external:{_normalized_identity(candidate['company'])}:"
            f"{str(candidate['external_id']).casefold()}"
        )
        job_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    else:
        job_key = candidate["dedupe_key"]
    return {
        "id": f"monitor-{job_key[:24]}",
        "company": candidate["company"],
        "employer_type": candidate["employer_type"],
        "title": candidate["title"],
        "city": candidate["city"],
        "industry": candidate["industry"],
        "url": candidate["official_url"],
        "source": candidate["source"],
        "opening_date": candidate.get("verified_opening_date"),
        "closing_date": candidate.get("verified_closing_date"),
        "requirements": candidate.get("requirements", ""),
        "tags": list(dict.fromkeys([
            *candidate.get("tags", []),
            "校园招聘", "动态监控", "链接已验证", "标题已验证",
        ])),
        "historical_applicants": None,
        "historical_offers": None,
        "last_verified_at": database.utc_now(),
        "status": "open",
    }


@app.get("/api/recruitment/sync/status")
def recruitment_sync_status(
    _: Annotated[None, Depends(require_recruitment_ingest_token)],
) -> dict:
    return database.recruitment_sync_status(
        expected_source_count=len(EXPECTED_CHATGPT_RADAR_SOURCES)
    )


@app.post("/api/recruitment/ingest")
def ingest_recruitment_jobs(
    request: RecruitmentIngestRequest,
    _: Annotated[None, Depends(require_recruitment_ingest_token)],
) -> dict:
    """Quarantine, verify, and promote jobs from an authorized external monitor."""
    totals = {
        "received": len(request.jobs),
        "accepted": 0,
        "new": 0,
        "updated": 0,
        "duplicates": 0,
        "stale": 0,
        "pending": 0,
        "rejected": 0,
        "closed": 0,
    }
    skipped: list[dict] = []
    source_groups: dict[str, dict] = {}
    expected_titles = {
        source["source_id"]: source["title"]
        for source in EXPECTED_CHATGPT_RADAR_SOURCES
    }
    today = date.today()

    if not request.jobs and request.source_id:
        source_id = request.source_id.strip()
        source_key = database.recruitment_ingest_source_key(source_id, None)
        source_groups[source_key] = {
            "source_id": source_id,
            "source_thread_id": None,
            "title": expected_titles.get(source_id, source_id),
            "last_item_id": None,
            "last_source_updated_at": _normalized_source_timestamp(
                request.source_updated_at
            ),
            "counts": {key: 0 for key in totals},
        }

    for item in request.jobs:
        candidate, url_error = _candidate_from_ingest_item(
            item,
            batch_source_id=request.source_id,
            batch_source_updated_at=request.source_updated_at,
        )
        source_key = candidate["source_key"]
        group = source_groups.setdefault(source_key, {
            "source_id": candidate["source_id"],
            "source_thread_id": candidate.get("source_thread_id"),
            "title": expected_titles.get(candidate["source_id"], candidate["source"]),
            "last_item_id": None,
            "last_source_updated_at": _normalized_source_timestamp(
                request.source_updated_at
            ),
            "counts": {key: 0 for key in totals},
        })
        group["counts"]["received"] += 1
        group["last_item_id"] = candidate.get("source_item_id") or candidate.get("external_id")
        group["last_source_updated_at"] = _max_source_timestamp(
            group["last_source_updated_at"],
            candidate.get("source_updated_at"),
        )

        stored = database.upsert_recruitment_ingest_candidate(candidate)
        disposition = stored.pop("disposition")
        if disposition == "stale":
            totals["duplicates"] += 1
            totals["stale"] += 1
            group["counts"]["duplicates"] += 1
            group["counts"]["stale"] += 1
            existing_status = stored.get("verification_status")
            verified_deadline = stored.get("verified_closing_date")
            if verified_deadline and str(verified_deadline) <= today.isoformat():
                promoted_job_id = stored.get("promoted_job_id")
                if promoted_job_id:
                    database.close_recruitment_job(promoted_job_id)
                database.set_recruitment_ingest_candidate_verification(
                    stored["id"], "closed", "expired", promoted_job_id
                )
                totals["closed"] += 1
                group["counts"]["closed"] += 1
                skipped.append({"title": item.title, "reason": "expired"})
            elif existing_status == "verified":
                totals["accepted"] += 1
                group["counts"]["accepted"] += 1
            elif existing_status == "rejected":
                totals["rejected"] += 1
                group["counts"]["rejected"] += 1
            elif existing_status == "closed":
                totals["closed"] += 1
                group["counts"]["closed"] += 1
            else:
                totals["pending"] += 1
                group["counts"]["pending"] += 1
            skipped.append({"title": item.title, "reason": "stale_source_update"})
            continue
        if disposition == "duplicate":
            totals["duplicates"] += 1
            group["counts"]["duplicates"] += 1
        else:
            totals[disposition] += 1
            group["counts"][disposition] += 1

        incoming_closed = item.status == "closed"
        incoming_expired = bool(item.closing_date and item.closing_date <= today)
        if incoming_closed or incoming_expired:
            promoted_job_id = stored.get("promoted_job_id")
            if promoted_job_id:
                database.close_recruitment_job(promoted_job_id)
            database.set_recruitment_ingest_candidate_verification(
                stored["id"],
                "closed",
                "closed" if incoming_closed else "expired",
                promoted_job_id,
            )
            totals["closed"] += 1
            group["counts"]["closed"] += 1
            skipped.append({
                "title": item.title,
                "reason": "closed" if incoming_closed else "expired",
            })
            continue

        verified_deadline = stored.get("verified_closing_date")
        if verified_deadline and str(verified_deadline) <= today.isoformat():
            promoted_job_id = stored.get("promoted_job_id")
            if promoted_job_id:
                database.close_recruitment_job(promoted_job_id)
            database.set_recruitment_ingest_candidate_verification(
                stored["id"], "closed", "expired", promoted_job_id
            )
            totals["closed"] += 1
            group["counts"]["closed"] += 1
            skipped.append({"title": item.title, "reason": "expired"})
            continue

        if url_error:
            verification_status, reason = "rejected", url_error
            verified_dates = {"opening_date": None, "closing_date": None}
        else:
            verification_status, reason, verified_dates = _verify_ingest_candidate(stored)
        if verification_status == "verified":
            stored["verified_opening_date"] = verified_dates["opening_date"]
            stored["verified_closing_date"] = verified_dates["closing_date"]
            job = _promoted_job(stored)
            database.upsert_recruitment_jobs([job])
            database.set_recruitment_ingest_candidate_verification(
                stored["id"],
                "verified",
                None,
                job["id"],
                verified_dates["opening_date"],
                verified_dates["closing_date"],
            )
            totals["accepted"] += 1
            group["counts"]["accepted"] += 1
        elif verification_status == "closed":
            promoted_job_id = stored.get("promoted_job_id")
            if promoted_job_id:
                database.close_recruitment_job(promoted_job_id)
            database.set_recruitment_ingest_candidate_verification(
                stored["id"], "closed", reason, promoted_job_id
            )
            totals["closed"] += 1
            group["counts"]["closed"] += 1
            skipped.append({"title": item.title, "reason": reason})
        else:
            promoted_job_id = stored.get("promoted_job_id")
            if verification_status == "rejected" and promoted_job_id:
                database.close_recruitment_job(promoted_job_id)
            database.set_recruitment_ingest_candidate_verification(
                stored["id"], verification_status, reason
            )
            totals[verification_status] += 1
            group["counts"][verification_status] += 1
            skipped.append({"title": item.title, "reason": reason})

    event_ids = []
    for group in source_groups.values():
        event_ids.append(database.record_recruitment_ingest_event(
            source_id=group["source_id"],
            source_thread_id=group["source_thread_id"],
            title=group["title"],
            counts=group["counts"],
            last_item_id=group["last_item_id"],
            last_source_updated_at=group["last_source_updated_at"],
        ))
    return {
        **totals,
        "event_id": event_ids[0] if len(event_ids) == 1 else None,
        "event_ids": event_ids,
        "skipped": skipped,
        "received_at": database.utc_now(),
    }


@app.get("/api/billing/status")
def billing(user: User) -> dict:
    return billing_status(user)


@app.post("/api/billing/apple/verify")
def verify_apple_transaction(_: AppleTransactionRequest, user: User) -> dict:
    del user
    raise HTTPException(
        status_code=503,
        detail=(
            "Apple subscription verification is not configured. Configure App Store "
            "Connect product IDs and server credentials before enabling purchases."
        ),
    )


@app.post("/api/auth/privacy-consent")
def accept_privacy(request: PrivacyConsentRequest, user: User) -> dict:
    if not request.accepted:
        raise HTTPException(status_code=400, detail="Consent was not accepted.")
    updated = database.record_privacy_consent(user["id"], PRIVACY_VERSION)
    if not updated:
        raise HTTPException(status_code=404, detail="Account not found.")
    return public_user(updated)


@app.delete("/api/auth/account", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(request: DeleteAccountRequest, user: User) -> Response:
    stored_user = database.get_user_by_username(user["username"])
    if not stored_user or not verify_password(request.password, stored_user["password_hash"]):
        raise HTTPException(status_code=401, detail="Password is incorrect.")
    if request.confirmation != "DELETE":
        raise HTTPException(status_code=400, detail='Enter "DELETE" to confirm.')
    watch_ids = [
        watch["id"]
        for watch in database.list_recruitment_watches(user["id"])
    ]
    if not database.delete_user(user["id"]):
        raise HTTPException(status_code=404, detail="Account not found.")
    with _space_lock_registry_guard:
        _space_lock_registry.pop(user["id"], None)
    with _watch_refresh_cooldown_guard:
        _watch_refresh_last_request.pop(user["id"], None)
    with _watch_create_rate_guard:
        _watch_create_requests.pop(user["id"], None)
    with _model_rate_guard:
        _model_user_units.pop(user["id"], None)
    with _watch_lock_registry_guard:
        for watch_id in watch_ids:
            _watch_lock_registry.pop(watch_id, None)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/sessions")
def sessions(user: User) -> list[dict]:
    return database.list_sessions(user["id"])


@app.post("/api/sessions", status_code=status.HTTP_201_CREATED)
def new_session(request: SessionRequest, user: User) -> dict:
    return database.create_session(
        user["id"],
        request.title.strip(),
        request.workspace,
    )


@app.get("/api/sessions/{session_id}/messages")
def session_messages(session_id: str, user: User) -> list[dict]:
    if not database.get_session(session_id, user["id"]):
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return database.list_messages(session_id, user["id"])


@app.delete("/api/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_session(session_id: str, user: User) -> Response:
    if not database.delete_session(session_id, user["id"]):
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/documents")
def documents(user: User, workspace: Workspace | None = None) -> list[dict]:
    return database.list_documents(user["id"], workspace)


@app.post("/api/documents", status_code=status.HTTP_201_CREATED)
def upload_document(
    user: ConsentedUser,
    file: UploadFile = File(...),
    workspace: str = Form(DEFAULT_WORKSPACE),
) -> dict:
    try:
        workspace = validate_workspace(workspace)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    filename = (file.filename or "document.txt").strip()[:160]
    supported_extensions = (".txt", ".md", ".csv", ".json", ".pdf", ".docx")
    if not filename.lower().endswith(supported_extensions):
        raise HTTPException(
            status_code=415,
            detail="Only TXT, Markdown, CSV, JSON, PDF and DOCX files are supported.",
        )
    raw = file.file.read(10_000_001)
    if len(raw) > 10_000_000:
        raise HTTPException(status_code=413, detail="Document exceeds 10 MB.")
    try:
        content, chunks = extract_document(filename, raw)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("Document parsing failed for %s", filename, exc_info=True)
        raise HTTPException(status_code=400, detail="Document could not be parsed.") from exc
    try:
        enforce_model_request_rate(user["id"], 1)
        embeddings = create_embeddings([chunk["content"] for chunk in chunks])
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Embedding request failed")
        raise HTTPException(status_code=502, detail="Embedding request failed.") from exc
    return database.create_document(
        user["id"],
        filename,
        content,
        chunks,
        embeddings,
        workspace,
        filename.rsplit(".", 1)[-1].lower(),
    )


@app.delete("/api/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_document(document_id: str, user: User) -> Response:
    if not database.delete_document(document_id, user["id"]):
        raise HTTPException(status_code=404, detail="Document not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/spaces")
def spaces(user: User) -> list[dict]:
    items = database.list_spaces(user["id"])
    return [
        {
            **item,
            "usage": database.token_usage(user["id"], item["id"]),
        }
        for item in items
    ]


@app.post("/api/spaces", status_code=status.HTTP_201_CREATED)
def create_space(request: SpaceCreateRequest, user: ConsentedUser) -> dict:
    template = SPACE_TEMPLATES.get(request.template_id)
    if not template:
        raise HTTPException(status_code=422, detail="Unsupported space template.")
    plan = user.get("plan", "free")
    limits = plan_limits(plan)
    if database.count_spaces(user["id"]) >= limits["max_spaces"]:
        raise HTTPException(
            status_code=403,
            detail="Your current plan has reached its AI Space limit.",
        )
    requested_budget = request.monthly_token_budget or limits["max_space_tokens"]
    if requested_budget > limits["max_space_tokens"]:
        raise HTTPException(
            status_code=403,
            detail="The requested Space token budget exceeds your plan limit.",
        )
    system_prompt = request.system_prompt.strip() or template["system_prompt"]
    description = request.description.strip() or template["description"]
    name = request.name.strip()
    if len(system_prompt) < 12:
        raise HTTPException(
            status_code=422,
            detail="A Space needs at least 12 characters of operating rules.",
        )
    return database.create_space(
        user["id"],
        name,
        description,
        request.icon.strip() or template["icon"],
        request.theme.strip() or template["theme"],
        request.template_id,
        system_prompt,
        requested_budget,
    )


def prepare_space_execution(
    space: dict,
    message: str,
    mode: SpaceRunMode,
    user: dict,
) -> tuple[dict, dict | None]:
    billing = billing_status(user)
    space_usage = database.token_usage(user["id"], space["id"])
    remaining = min(
        billing["remaining_tokens"],
        max(0, space["monthly_token_budget"] - space_usage["total_tokens"]),
    )
    initial = build_preflight(
        space,
        message,
        mode,
        remaining,
        settings.ai_model,
    )
    cached = None
    if mode == "lean":
        cached = database.find_cached_space_run(
            space["id"],
            user["id"],
            initial["fingerprint"],
        )
    preflight = build_preflight(
        space,
        message,
        mode,
        remaining,
        settings.ai_model,
        cache_hit=bool(cached),
        cached_tokens=(cached or {}).get("usage", {}).get("total_tokens", 0),
    )
    if cached:
        preflight["cached_from_run_id"] = cached["id"]
    return preflight, cached


def execute_space_request(
    space: dict,
    request: SpaceExecutionRequest,
    user: dict,
) -> dict:
    preflight, cached = prepare_space_execution(
        space,
        request.message,
        request.mode,
        user,
    )
    if not preflight["allowed"]:
        raise HTTPException(
            status_code=429,
            detail=(
                "This AI Space does not have enough remaining Tokens for the "
                "estimated input and maximum output."
            ),
        )

    zero_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    if request.mode == "local":
        artifact = build_local_capsule(space, request.message)
        reply = render_local_capsule(artifact)
        usage = zero_usage
        run = database.create_space_run(
            user["id"],
            space["id"],
            preflight["fingerprint"],
            request.mode,
            "local",
            request.message,
            artifact,
            reply,
            saved_tokens=0,
        )
    elif cached:
        artifact = cached["artifact"]
        reply = cached["reply"]
        usage = zero_usage
        cached_usage = cached.get("usage", {}).get("total_tokens", 0)
        run = database.create_space_run(
            user["id"],
            space["id"],
            preflight["fingerprint"],
            request.mode,
            "cache",
            request.message,
            artifact,
            reply,
            saved_tokens=cached_usage,
            cached_from_run_id=cached["id"],
        )
    else:
        enforce_model_request_rate(user["id"], 1)
        try:
            reply, usage = run_space(
                space["system_prompt"],
                request.message,
                max_output_tokens=preflight["max_output_tokens"],
                mode=request.mode,
            )
        except Exception as exc:
            database.create_space_run(
                user["id"],
                space["id"],
                preflight["fingerprint"],
                request.mode,
                request.mode,
                request.message,
                {},
                "",
                estimated_input_tokens=preflight["estimated_input_tokens"],
                max_output_tokens=preflight["max_output_tokens"],
                status="failed",
            )
            logger.exception("AI Space request failed")
            raise HTTPException(
                status_code=502,
                detail="OpenAI API request failed.",
            ) from exc
        database.record_token_usage(
            user["id"],
            space["id"],
            usage["input_tokens"],
            usage["output_tokens"],
            usage["total_tokens"],
        )
        artifact = build_model_capsule(
            space,
            request.message,
            reply,
            request.mode,
        )
        run = database.create_space_run(
            user["id"],
            space["id"],
            preflight["fingerprint"],
            request.mode,
            request.mode,
            request.message,
            artifact,
            reply,
            estimated_input_tokens=preflight["estimated_input_tokens"],
            max_output_tokens=preflight["max_output_tokens"],
            actual_input_tokens=usage["input_tokens"],
            actual_output_tokens=usage["output_tokens"],
            actual_total_tokens=usage["total_tokens"],
        )

    return {
        "run_id": run["id"],
        "space": {
            key: space[key]
            for key in ("id", "name", "icon", "theme", "template_id")
        },
        "mode": request.mode,
        "execution_path": run["execution_path"],
        "cache_hit": run["execution_path"] == "cache",
        "cached_from_run_id": run.get("cached_from_run_id"),
        "artifact": artifact,
        "reply": reply,
        "usage": usage,
        "saved_tokens": run["saved_tokens"],
        "estimated_tokens_saved": preflight["estimated_tokens_saved"],
        "tokens_saved_kind": preflight["tokens_saved_kind"],
        "preflight": preflight,
        "billing": billing_status(user),
    }


@app.post("/api/spaces/{space_id}/preflight")
def preflight_created_space(
    space_id: str,
    request: SpaceExecutionRequest,
    user: ConsentedUser,
) -> dict:
    space = database.get_space(space_id, user["id"])
    if not space:
        raise HTTPException(status_code=404, detail="AI Space not found.")
    preflight, _ = prepare_space_execution(
        space,
        request.message,
        request.mode,
        user,
    )
    return preflight


@app.post("/api/spaces/{space_id}/runs", status_code=status.HTTP_201_CREATED)
def run_space_v2(
    space_id: str,
    request: SpaceExecutionRequest,
    user: ConsentedUser,
) -> dict:
    space = database.get_space(space_id, user["id"])
    if not space:
        raise HTTPException(status_code=404, detail="AI Space not found.")
    with space_execution_lock(user["id"], space_id):
        fresh_space = database.get_space(space_id, user["id"])
        if not fresh_space:
            raise HTTPException(status_code=404, detail="AI Space not found.")
        return execute_space_request(fresh_space, request, user)


@app.get("/api/spaces/{space_id}/runs")
def space_run_history(
    space_id: str,
    user: User,
    limit: int = Query(default=20, ge=1, le=100),
) -> list[dict]:
    if not database.get_space(space_id, user["id"]):
        raise HTTPException(status_code=404, detail="AI Space not found.")
    return database.list_space_runs(space_id, user["id"], limit)


@app.get("/api/spaces/{space_id}/runs/{run_id}")
def space_run_detail(space_id: str, run_id: str, user: User) -> dict:
    if not database.get_space(space_id, user["id"]):
        raise HTTPException(status_code=404, detail="AI Space not found.")
    run = database.get_space_run(run_id, space_id, user["id"])
    if not run:
        raise HTTPException(status_code=404, detail="AI Space run not found.")
    return run


@app.post("/api/spaces/{space_id}/run")
def run_created_space(
    space_id: str,
    request: SpaceExecutionRequest,
    user: ConsentedUser,
) -> dict:
    """Execute a Space; omitted mode remains backward-compatible with lean."""
    space = database.get_space(space_id, user["id"])
    if not space:
        raise HTTPException(status_code=404, detail="AI Space not found.")
    with space_execution_lock(user["id"], space_id):
        fresh_space = database.get_space(space_id, user["id"])
        if not fresh_space:
            raise HTTPException(status_code=404, detail="AI Space not found.")
        return execute_space_request(fresh_space, request, user)


def cross_exam_source(
    source_id: str,
    workspace: str,
    item: dict,
) -> dict:
    return {
        "source_id": source_id,
        "workspace": workspace,
        "document_id": item["document_id"],
        "name": item["name"],
        "page": item.get("page"),
        "score": item["score"],
        "excerpt": item["content"],
    }


@app.post("/api/cross-exam")
def cross_exam(request: CrossExamRequest, user: ConsentedUser) -> dict:
    legal_documents = database.list_documents(user["id"], "legal")
    finance_documents = database.list_documents(user["id"], "finance")
    if not legal_documents or not finance_documents:
        raise HTTPException(
            status_code=409,
            detail=(
                "Cross-examination requires at least one document in both the "
                "legal and finance workspaces."
            ),
        )

    try:
        enforce_model_request_rate(user["id"], 3)
        legal_context = retrieve_context(
            user["id"], request.focus, "legal", 6, 0.0
        )
        finance_context = retrieve_context(
            user["id"], request.focus, "finance", 6, 0.0
        )
        if not legal_context or not finance_context:
            raise HTTPException(
                status_code=422,
                detail="No readable evidence chunks were found in one of the workspaces.",
            )
        result = run_cross_exam(request.focus, legal_context, finance_context)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Cross-examination request failed")
        raise HTTPException(
            status_code=502,
            detail="OpenAI API could not complete the cross-examination.",
        ) from exc

    sources = [
        *[
            cross_exam_source(f"L{index}", "legal", item)
            for index, item in enumerate(legal_context, start=1)
        ],
        *[
            cross_exam_source(f"F{index}", "finance", item)
            for index, item in enumerate(finance_context, start=1)
        ],
    ]
    source_map = {source["source_id"]: source for source in sources}
    collisions: list[dict] = []
    for collision in result.get("collisions", []):
        requested_ids = [
            *collision.get("legal_source_ids", []),
            *collision.get("finance_source_ids", []),
        ]
        collisions.append(
            {
                **collision,
                "evidence": [
                    source_map[source_id]
                    for source_id in dict.fromkeys(requested_ids)
                    if source_id in source_map
                ],
            }
        )

    fingerprint_input = json.dumps(
        {
            "version": 1,
            "focus": request.focus,
            "documents": sorted(
                document["id"]
                for document in [*legal_documents, *finance_documents]
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    fingerprint = hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()[:16]
    return {
        "analysis_id": f"FF-{fingerprint.upper()}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "focus": request.focus,
        "headline": result.get("headline", "冰焰交叉审查"),
        "executive_summary": result.get("executive_summary", ""),
        "collisions": collisions,
        "stress_scenarios": result.get("stress_scenarios", []),
        "blind_spots": result.get("blind_spots", []),
        "sources": sources,
        "document_counts": {
            "legal": len(legal_documents),
            "finance": len(finance_documents),
        },
        "method": {
            "name": "Clause-to-Cashflow Cross-Examination",
            "version": 1,
            "confidence_definition": "Evidence coverage, not factual certainty.",
            "professional_boundary": (
                "Document and research assistance only; not legal or investment advice."
            ),
        },
    }


@app.post("/api/chat")
def chat(request: ChatRequest, user: ConsentedUser) -> dict:
    try:
        enforce_model_request_rate(user["id"], 4)
        session, messages, sources = prepare_chat(
            user["id"],
            request,
            include_context=not request.creative_single_pass,
        )
        agent_result = (
            run_agent(messages, session["workspace"], tools_enabled=False)
            if request.creative_single_pass
            else run_agent(messages, session["workspace"])
        )
        reply, tools_used = agent_result[:2]
        usage = (
            agent_result[2]
            if len(agent_result) > 2
            else {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        )
        if usage.get("total_tokens", 0):
            database.record_token_usage(
                user["id"],
                None,
                int(usage.get("input_tokens", 0)),
                int(usage.get("output_tokens", 0)),
                int(usage.get("total_tokens", 0)),
            )
        database.append_message(session["id"], "assistant", reply)
        return {
            "reply": reply,
            "session_id": session["id"],
            "workspace": session["workspace"],
            "sources": sources,
            "tools_used": tools_used,
            "usage": usage,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Chat request failed")
        raise HTTPException(status_code=502, detail="OpenAI API request failed.") from exc


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/api/chat/stream")
def chat_stream(request: ChatRequest, user: ConsentedUser) -> StreamingResponse:
    try:
        enforce_model_request_rate(user["id"], 4)
        session, messages, sources = prepare_chat(user["id"], request)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Chat preparation failed")
        raise HTTPException(status_code=502, detail="OpenAI API request failed.") from exc

    def event_stream():
        reply_parts: list[str] = []
        try:
            yield sse(
                "meta",
                {
                    "session_id": session["id"],
                    "workspace": session["workspace"],
                    "sources": sources,
                },
            )
            for event in stream_agent(messages, session["workspace"]):
                if event["type"] == "token":
                    reply_parts.append(event["content"])
                    yield sse("token", {"content": event["content"]})
                elif event["type"] == "tool":
                    yield sse("tool", {"name": event["name"]})
                elif event["type"] == "done":
                    reply = event["reply"] or "".join(reply_parts)
                    usage = event.get("usage") or {}
                    if usage.get("total_tokens", 0):
                        database.record_token_usage(
                            user["id"],
                            None,
                            int(usage.get("input_tokens", 0)),
                            int(usage.get("output_tokens", 0)),
                            int(usage.get("total_tokens", 0)),
                        )
                    database.append_message(session["id"], "assistant", reply)
                    yield sse(
                        "done",
                        {
                            "session_id": session["id"],
                            "tools_used": event["tools_used"],
                        },
                    )
        except Exception:
            logger.exception("Streaming chat failed")
            yield sse("error", {"detail": "OpenAI API request failed."})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if FRONTEND_DIST.is_dir():
    app.mount(
        "/",
        StaticFiles(directory=FRONTEND_DIST, html=True),
        name="frontend",
    )

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
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal

from fastapi import (
    BackgroundTasks,
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
from fastapi.responses import JSONResponse, StreamingResponse
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
from .recruitment import (
    SCORING_VERSION, SCORING_WEIGHTS, TIER_DEFINITIONS,
    job_matches_profile, score_job, semantic_employer_categories,
)
from .future_radar.opportunity_cache import scoring_scope
from .recruitment_search import (
    WEB_SEARCH_SOURCE,
    WEB_SEARCH_STATE_KEY,
    _evaluate_official_candidate_page,
    _semantic_date_appears_in_page,
    build_employer_search_batches,
    build_employer_search_targets,
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
    is_recruitment_program_listing,
)
from .config import settings
from .future_radar.normalization import (
    PRIMARY_CATEGORY_CODES,
    canonicalize_url as canonicalize_radar_url,
    normalized_key as radar_normalized_key,
)
from .future_radar.schemas import (
    FrostFireSyncV1,
    RadarRunRequest,
    SourceCreateRequest,
    SourcePatchRequest,
)
from .future_radar.service import FutureRadarService, RadarRunBusy, SyncConflict
from .future_radar.adapters import _public_reference_url, _redact_public_text
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
_recruitment_verification_retry_lock = threading.Lock()
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
# Personal deployments run discovery on demand. Duplicate concurrent work is
# prevented by server-side refresh/run locks; there is no post-run cooldown.
RECRUITMENT_DEEP_SEARCH_COOLDOWN_SECONDS = 0

EXPECTED_CHATGPT_RADAR_SOURCES = [
    {
        "source_id": f"chatgpt-radar-{index:02d}",
        "source_thread_id": None,
        "title": f"ChatGPT 监控 {index}",
    }
    for index in range(1, 7)
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
    if RECRUITMENT_DEEP_SEARCH_COOLDOWN_SECONDS <= 0:
        return None
    attempted = _web_search_attempted_at(state)
    if attempted is None:
        return None
    return (
        attempted + timedelta(seconds=RECRUITMENT_DEEP_SEARCH_COOLDOWN_SECONDS)
    ).isoformat()


def _deep_search_is_available(state: dict | None) -> bool:
    if RECRUITMENT_DEEP_SEARCH_COOLDOWN_SECONDS <= 0:
        return True
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


async def recruitment_refresh_loop(
    first_refresh_complete: asyncio.Event | None = None,
) -> None:
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
            # The Future Radar source owns paid discovery and its source lock.
            # Running it here as well would repeat the entire employer search
            # before the scheduler starts and bypass that shared source lock.
            count = await asyncio.to_thread(
                refresh_recruitment_sources, include_web_search=False
            )
            logger.info("Scheduled recruitment source refresh completed: %s jobs", count)
        except Exception:
            logger.exception("Scheduled recruitment source refresh failed")
        finally:
            # A fresh/ephemeral database must not let Future Radar snapshot an
            # empty legacy table before the upstream refresh has had its first
            # chance to populate verified jobs.
            if first_refresh_complete is not None:
                first_refresh_complete.set()
        try:
            watch_counts = await refresh_all_recruitment_watches()
            logger.info(
                "Scheduled recruitment watch refresh completed: %s watches checked",
                watch_counts["checked"],
            )
        except Exception:
            logger.exception("Scheduled recruitment watch refresh failed")
        await asyncio.sleep(settings.recruitment_refresh_minutes * 60)


async def future_radar_refresh_loop(
    first_refresh_complete: asyncio.Event | None = None,
) -> None:
    """Run due Radar sources server-side; the browser only polls stored events."""
    if first_refresh_complete is not None:
        await first_refresh_complete.wait()
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
        # Candidate verification is an independent, best-effort queue. A
        # broken official page or database lease must never cancel the Radar
        # source run above or prevent the scheduler from reaching its sleep.
        verification = await asyncio.to_thread(
            _reverify_pending_recruitment_candidates_safely,
            limit=100,
        )
        if verification["status"] == "success":
            logger.info(
                "Pending recruitment verification completed: claimed=%s checked=%s "
                "verified=%s pending=%s rejected=%s closed=%s",
                verification["claimed"], verification["checked"],
                verification["verified"], verification["pending"],
                verification["rejected"], verification["closed"],
            )
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
    first_refresh_complete = asyncio.Event()
    if settings.recruitment_refresh_minutes > 0:
        tasks.append(asyncio.create_task(
            recruitment_refresh_loop(first_refresh_complete)
        ))
    else:
        first_refresh_complete.set()
    if settings.future_radar_enabled:
        tasks.append(asyncio.create_task(
            future_radar_refresh_loop(first_refresh_complete)
        ))
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
        database.close_database_pools()


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
        event = _api_usage_event_metadata(request, 500, started_at)
        if event is not None:
            # There is no response to attach a background task to. Preserve
            # the original exception while a best-effort worker records 500.
            try:
                asyncio.get_running_loop().run_in_executor(
                    None, _record_api_usage, *event
                )
            except RuntimeError:
                logger.info("API usage telemetry skipped: executor unavailable")
        raise
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    event = _api_usage_event_metadata(request, response.status_code, started_at)
    if event is not None:
        background = BackgroundTasks()
        if response.background is not None:
            background.add_task(response.background)
        # Starlette sends the body before running background tasks. The sync
        # recorder runs in its thread pool, never in the async event loop.
        background.add_task(_record_api_usage, *event)
        response.background = background
    return response


def _api_usage_event_metadata(
    request: Request, status_code: int, started_at: float
) -> tuple[int | None, str, str, int, int] | None:
    """Freeze business timing and only safe scalars before deferred work."""
    if not request.url.path.startswith("/api/") or request.url.path in {
        "/api/health",
        "/api/admin/usage",
    }:
        return None
    route = request.scope.get("route")
    route_path = getattr(route, "path", None) or "/api/unknown"
    duration_ms = max(0, round((time.perf_counter() - started_at) * 1_000))
    return (
        getattr(request.state, "user_id", None),
        request.method,
        route_path,
        status_code,
        duration_ms,
    )


def _record_api_usage(
    user_id: int | None, method: str, route: str, status_code: int, duration_ms: int
) -> None:
    """Best-effort telemetry: no Request, bodies, queries or credentials."""
    try:
        database.record_api_usage_event(
            user_id, method, route, status_code, duration_ms,
        )
    except Exception as exc:
        logger.info(
            "API usage telemetry skipped error_type=%s", type(exc).__name__
        )


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
    employer_types: list[str] = Field(default_factory=list, max_length=16)


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
    started = time.perf_counter()
    try:
        with database.connect_health(timeout=2.0) as connection:
            connection.execute("SELECT 1").fetchone()
    except Exception as exc:
        # Never return a healthy deployment just because the HTTP process is
        # alive, and never include credentials/driver diagnostics in health.
        logger.warning(
            "Database health probe failed purpose=health error_type=%s duration_ms=%d",
            type(exc).__name__, int((time.perf_counter() - started) * 1000),
        )
        raise HTTPException(status_code=503, detail="Database is unavailable.") from None
    return {
        "status": "ok", "version": app.version,
        "database": getattr(settings, "database_backend", "sqlite"),
    }


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


_PUBLIC_RADAR_ERROR_MESSAGES = {
    "COMPANY_SEARCH_INCOMPLETE": "部分企业搜索未完成；已取得的发现保留在机会池，详情见覆盖统计。",
    "DISCOVERY_LIMITED": "该信源尚未配置可合法访问的公开入口。",
    "AI_CREDITS_EXHAUSTED": "AI 补漏额度暂不可用；确定性官网信源仍会继续扫描。",
    "AI_RATE_LIMITED": "AI 补漏当前受到频率限制；确定性官网信源仍会继续扫描。",
    "AI_PROVIDER_UNAVAILABLE": "AI 补漏暂时不可用；确定性官网信源仍会继续扫描。",
    "SOURCE_UNAVAILABLE": "公开信源暂时无法访问，稍后会自动重试。",
    "SOURCE_FAILED": "该信源本轮扫描未完成，稍后会自动重试。",
    "SOURCE_BUSY": "该信源正在由另一轮扫描处理，本轮已安全跳过。",
    "PROGRAM_REJECTED": "候选项目未通过结构或安全校验。",
    "JOB_REJECTED": "候选岗位未通过结构或安全校验。",
    "ARTICLE_REJECTED": "候选文章未通过结构或安全校验。",
}


def _public_radar_error(error: object) -> dict:
    item = error if isinstance(error, dict) else {}
    raw_code = str(item.get("code") or "SOURCE_FAILED").upper()
    code = raw_code if raw_code in _PUBLIC_RADAR_ERROR_MESSAGES else "SOURCE_FAILED"
    return {
        "source_id": str(item.get("source_id") or "")[:80],
        "code": code,
        "message": _PUBLIC_RADAR_ERROR_MESSAGES[code],
    }


def _public_radar_run(run: dict | None) -> dict | None:
    if not run:
        return None
    item = dict(run)
    item["errors"] = [
        _public_radar_error(error) for error in list(item.get("errors") or [])[:100]
    ]
    return item


def _public_radar_dashboard(dashboard: dict) -> dict:
    item = dict(dashboard)
    item["last_scan"] = _public_radar_run(item.get("last_scan"))
    return item


def _public_radar_source(source: dict) -> dict:
    """Expose operational source health without leaking adapter internals."""
    allowed = (
        "id", "name", "platform", "company", "source_type", "url", "domain",
        "account_name", "enabled", "priority", "trust_level", "interval_minutes",
        "status", "verification_status", "last_checked_at", "last_success_at",
        "last_error_at", "consecutive_failures", "created_at", "updated_at",
    )
    item = {key: source.get(key) for key in allowed}
    if source.get("last_error_at"):
        if source.get("status") == "discovery_limited":
            item["last_error"] = _PUBLIC_RADAR_ERROR_MESSAGES["DISCOVERY_LIMITED"]
        elif str(source.get("platform") or "").casefold() == "openai":
            safe_provider_messages = {
                _PUBLIC_RADAR_ERROR_MESSAGES[code]
                for code in (
                    "AI_CREDITS_EXHAUSTED", "AI_RATE_LIMITED",
                    "AI_PROVIDER_UNAVAILABLE",
                )
            }
            item["last_error"] = (
                source.get("last_error")
                if source.get("last_error") in safe_provider_messages
                else _PUBLIC_RADAR_ERROR_MESSAGES["AI_PROVIDER_UNAVAILABLE"]
            )
        else:
            item["last_error"] = _PUBLIC_RADAR_ERROR_MESSAGES["SOURCE_FAILED"]
    return item


_SEARCH_UPDATE_LABELS = {
    "pending": "待官网核验",
    "verified": "已官网核验",
    "conflicted": "核验信息冲突",
    "rejected": "未通过核验",
}


def _public_search_update(job: dict) -> dict:
    """Expose a normalized discovery candidate without presenting it as fact."""
    allowed = (
        "id", "external_id", "program_id", "company", "title", "city", "region",
        "employer_type", "industry", "primary_category", "organization_category",
        "industry_tags", "role_tags", "official_url", "application_url",
        "opening_date", "closing_date", "status", "verification_status",
        "confidence_score", "description", "responsibilities", "requirements", "tags",
        "program_name", "recruitment_year", "first_seen_at", "last_seen_at",
        "last_changed_at", "latest_event_type", "latest_event_at",
    )
    item = {key: job.get(key) for key in allowed}
    for field in ("official_url", "application_url"):
        item[field] = _public_reference_url(item.get(field))
    for field in (
        "company", "title", "city", "region", "employer_type", "industry",
        "description", "responsibilities", "requirements", "program_name", "external_id",
        "primary_category", "organization_category",
    ):
        if item.get(field):
            item[field] = _redact_public_text(str(item[field]), limit=2_000)
    for field in ("tags", "industry_tags", "role_tags"):
        item[field] = [
            _redact_public_text(str(value), limit=100)
            for value in list(item.get(field) or [])
        ]

    public_sources = []
    for source in list(job.get("sources") or []):
        public_sources.append({
            "source_id": _redact_public_text(str(source.get("source_id") or ""), limit=64),
            "name": _redact_public_text(str(source.get("name") or ""), limit=160),
            "source_type": str(source.get("source_type") or "")[:40],
            "trust_level": str(source.get("trust_level") or "")[:20],
            "source_url": _public_reference_url(source.get("source_url")),
            "verification_role": str(source.get("verification_role") or "")[:20],
            "discovered_at": source.get("discovered_at"),
            "last_seen_at": source.get("last_seen_at"),
            "active": bool(source.get("active")),
        })
    item["sources"] = public_sources
    item["discovered_by"] = [
        source for source in public_sources
        if source["trust_level"] == "discovery"
    ]
    item["verified_by"] = [
        source for source in public_sources
        if source["verification_role"] == "verification"
    ]

    verification_status = str(item.get("verification_status") or "pending")
    item["review_state"] = verification_status
    item["review_label"] = _SEARCH_UPDATE_LABELS.get(
        verification_status, "核验状态未知"
    )
    item["officially_verified"] = verification_status == "verified"
    closing_date = str(item.get("closing_date") or "")
    still_open = item.get("status") == "open" and (
        not closing_date or closing_date > date.today().isoformat()
    )
    item["published_as_active_job"] = bool(
        item["officially_verified"] and still_open
    )
    item["is_candidate"] = not item["officially_verified"]
    return item


def _validated_future_radar_categories(values: list[str]) -> list[str]:
    allowed_categories = set(PRIMARY_CATEGORY_CODES)
    selected_categories = {
        str(value).strip() for value in values if str(value).strip()
    }
    unknown_categories = selected_categories - allowed_categories
    if unknown_categories:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown Future Radar category: {sorted(unknown_categories)[0]}",
        )
    return sorted(selected_categories)


def _radar_company_aliases() -> dict[str, str]:
    return {
        radar_normalized_key(alias): radar_normalized_key(target.canonical_name)
        for target in build_employer_search_targets()
        for alias in target.aliases
    }


def _public_radar_opportunity(job: dict, profile: dict) -> dict:
    # Sanitize before scoring so derived labels cannot copy private transport
    # fields. Apply the same organization/role model to every source and pool.
    item = score_job(_public_search_update(job), profile)
    program_listing = is_recruitment_program_listing(job)
    item["listing_kind"] = "recruitment_program" if program_listing else "job"
    item["is_specific_job"] = not program_listing
    if program_listing:
        # A company's broad campaign is useful to open/apply to, but scoring
        # it as a concrete vacancy would manufacture a job-level T rating.
        for field in (
            "job_score", "match_score", "employer_score", "role_score",
            "career_value_score", "job_condition_score", "tier_code",
            "raw_job_score", "calibration_adjustment", "calibration_reason",
        ):
            item[field] = None
        item["score_breakdown"] = {key: None for key in item.get("score_breakdown", {})}
        item["dimension_scores"] = {key: None for key in item.get("dimension_scores", {})}
        item["scoring_factors"] = {
            key: {**value, "score": None, "contribution": None}
            for key, value in item.get("scoring_factors", {}).items()
        }
        item["scoring_status"] = "unscored_program_listing"
        item["positive_reasons"] = []
        item["negative_reasons"] = ["这是企业招聘项目，尚未细分到具体岗位，暂不生成岗位 T 级"]
        item["match_reasons"] = []
        item["fit_tags"] = []
        item["technical_hard"] = False
        item["quant_barrier"] = False
        item["manual_override"] = False
    item["employer_categories"] = sorted(semantic_employer_categories(item))
    item["opportunity_kind"] = "verified" if item["officially_verified"] else "discovered"
    item["available_in_main_pool"] = True
    tier = item.get("tier_code")
    item["tier_bucket"] = (
        tier if tier in {definition["code"] for definition in TIER_DEFINITIONS}
        else "BELOW_PRIORITY" if tier else "UNRANKED"
    )
    if item["review_state"] == "pending":
        item["review_label"] = "搜索发现"
    elif item["review_state"] == "conflicted":
        item["review_label"] = "来源信息有差异"
    return item


def _radar_scoring_scope(user_id: int, profile: dict) -> str:
    # Every profile field (including updated_at) participates; cache keys keep
    # only the digest. Rule/code changes cannot reuse an old scoring result.
    return scoring_scope(user_id, profile, {
        "version": SCORING_VERSION, "weights": SCORING_WEIGHTS,
        "tiers": TIER_DEFINITIONS,
        "scorer": id(score_job), "presenter": id(_public_radar_opportunity),
    })


def _radar_search_metadata() -> dict:
    result: dict = {
        "scope": {
            "category_count": len(PERSONAL_MONITOR_POOLS),
            "list_entry_count": sum(len(pool["employers"]) for pool in PERSONAL_MONITOR_POOLS),
            "target_count": len(build_employer_search_targets()),
            "batch_count": len(build_employer_search_batches()),
        },
        "coverage": None,
    }
    snapshot = future_radar_service.repository.discovery_summary(
        "openai-public-web-search"
    )
    if snapshot and snapshot["metadata"].get("coverage"):
        coverage = snapshot["metadata"]["coverage"]
        result["coverage"] = {
            key: coverage.get(key, 0)
            for key in (
                "target_count", "searched_count", "failed_count",
                "employers_with_candidates_count", "batch_count",
                "failed_batch_count", "coverage_percent",
            )
        }
        failed_employers = coverage.get("failed_employers")
        # Coverage is auxiliary discovery metadata. A provider's null/malformed
        # list must not turn an otherwise readable opportunity pool into 500.
        if not isinstance(failed_employers, list):
            failed_employers = []
        result["coverage"]["failed_employers"] = [
            _redact_public_text(str(name), limit=160)
            for name in failed_employers
            if isinstance(name, str)
        ]
        result["coverage"]["completed_at"] = snapshot["fetched_at"]
    result["search_status"] = snapshot.get("status", "pending")
    return result


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
    return _public_radar_dashboard(future_radar_service.repository.dashboard())


@app.get("/api/future-radar/search-updates")
def future_radar_search_updates(
    user: User,
    page: int = Query(default=1, ge=1, le=100_000),
    page_size: int = Query(default=50, ge=1, le=100),
    status_filter: Literal["open", "closed", "unknown", "all"] = Query(
        default="open", alias="status"
    ),
    verification_status: Literal[
        "pending", "verified", "conflicted", "rejected"
    ] | None = None,
    source_id: str | None = Query(default=None, max_length=64),
    company: str | None = Query(default=None, max_length=160),
    q: str | None = Query(default=None, max_length=160),
    sort: Literal["changed", "closing", "opening", "first_seen", "company"] = "changed",
    category: list[str] = Query(default=[]),
) -> dict:
    """List discovery candidates separately from officially verified jobs."""
    del user
    selected_categories = _validated_future_radar_categories(category)
    filters = {
        "status": status_filter,
        "verification_status": verification_status,
        "source_id": source_id,
        "company": company,
        "q": q,
        "sort": sort,
        "active_only": status_filter == "open",
        "primary_categories": selected_categories,
        "discovery_source_only": True,
    }
    result = future_radar_service.repository.list_jobs(
        page=page, page_size=page_size, filters=filters
    )
    candidates = [_public_search_update(job) for job in result["items"]]
    result["items"] = candidates
    result["candidates"] = candidates
    result["stats"] = future_radar_service.repository.job_stats(filters=filters)
    result.update(_radar_search_metadata())
    result["pool"] = "search_updates"
    result["notice"] = (
        "搜索档案保留来源与核验状态；有有效公开链接的校招发现可直接在机会主池查看，"
        "不必等待官网核验。已关闭、过期或被拒绝的记录不在主池展示。"
    )
    return result


@app.get("/api/future-radar/search-updates/{job_id}")
def future_radar_search_update(job_id: str, user: User) -> dict:
    del user
    job = future_radar_service.repository.get_job(job_id)
    if not job or not any(
        source.get("trust_level") == "discovery"
        for source in list(job.get("sources") or [])
    ):
        raise HTTPException(status_code=404, detail="Search update not found.")
    return _public_search_update(job)


@app.get("/api/future-radar/opportunities")
def future_radar_opportunities(
    user: User,
    page: int = Query(default=1, ge=1, le=100_000),
    page_size: int = Query(default=50, ge=1, le=100),
    status_filter: Literal["active", "open", "closed", "unknown", "all"] = Query(default="active", alias="status"),
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
    category: list[str] = Query(default=[]),
    tier_code: Literal[
        "T0", "T0.5", "T1", "T1.5", "T2", "T2.5", "T3", "UNRANKED", "BELOW_PRIORITY"
    ] | None = None,
    priority_only: bool = False,
    balanced_only: bool = False,
    compact: bool = False,
    view: Literal["jobs", "companies"] = "jobs",
    company_key: str | None = Query(default=None, max_length=100),
) -> JSONResponse:
    """Include usable discoveries; optionally focus or balance the visible rows.

    The API default remains the complete matching pool. Priority, balance and
    tier selections affect grouping/pagination, not stored records or scoring.
    """
    filters = {
        "status": status_filter, "verification_status": verification_status,
        "company": company, "city": city, "region": region,
        "employer_type": employer_type, "industry": industry,
        "program_id": program_id, "source_id": source_id, "q": q,
        "event_type": event_type,
        "opening_before": opening_before.isoformat() if opening_before else None,
        "opening_after": opening_after.isoformat() if opening_after else None,
        "closing_before": closing_before.isoformat() if closing_before else None,
        "closing_after": closing_after.isoformat() if closing_after else None,
        "sort": sort, "active_only": status_filter in {"active", "open"},
        "primary_categories": _validated_future_radar_categories(category),
        "tier_code": tier_code, "priority_only": priority_only,
        "balanced_only": balanced_only,
        "view": view, "company_key": company_key,
    }
    profile = database.get_recruitment_profile(user["id"])
    result = future_radar_service.repository.list_opportunities(
        page=page, page_size=page_size, filters=filters,
        public_url=_public_reference_url,
        prepare=lambda job: _public_radar_opportunity(job, profile),
        company_aliases=_radar_company_aliases(),
        cache_scope=_radar_scoring_scope(user["id"], profile),
    )
    if not compact:
        # Older clients keep their aliases. The current UI explicitly asks for
        # compact mode so large scored records are serialized/sent only once.
        if view == "companies":
            result["companies"] = result["items"]
        else:
            result["jobs"] = result["items"]
            result["opportunities"] = result["items"]
    result["tier_definitions"] = list(TIER_DEFINITIONS)
    result["pool"] = "opportunities"
    result.update(_radar_search_metadata())
    # These public records contain only JSON-compatible primitives. Avoid
    # FastAPI recursively encoding the same large compatibility lists again.
    return JSONResponse(content=result)


@app.get("/api/future-radar/opportunities/{job_id}")
def future_radar_opportunity(job_id: str, user: User) -> dict:
    profile = database.get_recruitment_profile(user["id"])
    job = future_radar_service.repository.get_prepared_opportunity(
        job_id, public_url=_public_reference_url, company_aliases=_radar_company_aliases(),
        prepare=lambda item: _public_radar_opportunity(item, profile),
        cache_scope=_radar_scoring_scope(user["id"], profile),
    )
    if not job:
        raise HTTPException(status_code=404, detail="Radar opportunity not found.")
    return job


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
    category: list[str] = Query(default=[]),
) -> dict:
    if verification_status not in (None, "verified"):
        raise HTTPException(
            status_code=422,
            detail="Only officially verified Future Radar jobs are public.",
        )
    selected_categories = _validated_future_radar_categories(category)
    filters = {
        "status": status_filter,
        "verification_status": "verified",
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
        "primary_categories": selected_categories,
    }
    profile = database.get_recruitment_profile(user["id"])

    def enrich(job: dict) -> dict:
        scored = score_job(job, profile)
        scored["employer_categories"] = sorted(semantic_employer_categories(scored))
        return scored

    # Structured primary-category filtering happens in the same indexed SQL
    # query as COUNT/LIMIT, before pagination. Secondary industry tags remain
    # available for explanation without duplicating one job across starfields.
    result = future_radar_service.repository.list_jobs(
        page=page, page_size=page_size, filters=filters
    )
    enriched = [enrich(job) for job in result["items"]]
    result["items"] = enriched
    result["jobs"] = enriched
    result["tier_definitions"] = list(TIER_DEFINITIONS)
    return result


@app.get("/api/future-radar/jobs/{job_id}")
def future_radar_job(job_id: str, user: User) -> dict:
    job = future_radar_service.repository.get_job(job_id)
    if not job or job.get("verification_status") != "verified":
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
        page=page, page_size=page_size, status=status_filter, q=q,
        verification_status="verified",
    )
    result["programs"] = result["items"]
    return result


@app.get("/api/future-radar/programs/{program_id}")
def future_radar_program(program_id: str, user: User) -> dict:
    del user
    program = future_radar_service.repository.get_program(program_id)
    if not program or program.get("verification_status") != "verified":
        raise HTTPException(status_code=404, detail="Recruitment program not found.")
    program["jobs"] = [
        job for job in program.get("jobs", [])
        if job.get("verification_status") == "verified"
    ]
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
        public_verified_only=True,
    )
    result["events"] = result["items"]
    if after_event_id is not None and result["items"]:
        result["dashboard"] = _public_radar_dashboard(
            future_radar_service.repository.dashboard()
        )
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
    result["items"] = [
        _public_radar_run(run) for run in result["items"]
    ]
    result["runs"] = result["items"]
    return result


@app.get("/api/future-radar/runs/{run_id}")
def future_radar_run_detail(run_id: str, user: User) -> dict:
    del user
    run = future_radar_service.repository.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Radar run not found.")
    return _public_radar_run(run) or {}


@app.get("/api/future-radar/sources")
def future_radar_sources(user: User, enabled: bool | None = None) -> dict:
    del user
    sources = [
        _public_radar_source(source)
        for source in future_radar_service.repository.list_sources(enabled=enabled)
    ]
    return {"items": sources, "sources": sources, "total": len(sources)}


def _future_radar_run_sources(payload: RadarRunRequest) -> list[str]:
    """Blocking registry validation runs in a worker, never the event loop."""
    scan_type = payload.scan_type
    for source_id in payload.source_ids:
        if not future_radar_service.repository.get_source(source_id):
            raise HTTPException(status_code=404, detail=f"Radar source not found: {source_id}")
    scannable_sources = future_radar_service.repository.manual_scan_sources(
        scan_type,
        source_ids=list(payload.source_ids) or None,
        force=payload.force,
    )
    source_ids = [source["id"] for source in scannable_sources]
    if payload.source_ids:
        unavailable = sorted(set(payload.source_ids) - set(source_ids))
        if unavailable:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"This source is not available for a {scan_type} scan: "
                    f"{unavailable[0]}"
                ),
            )
    if not source_ids:
        raise HTTPException(
            status_code=503,
            detail=(
                "No deterministic Future Radar sources are currently available."
                if scan_type == "quick"
                else "No configured discovery sources are currently available."
            ),
        )
    return source_ids


@app.post("/api/future-radar/run")
async def run_future_radar(
    user: ConsentedUser,
    request: RadarRunRequest | None = None,
    admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
) -> dict:
    payload = request or RadarRunRequest()
    scan_type = payload.scan_type
    if payload.force:
        configured_token = settings.admin_dashboard_token
        if not configured_token:
            raise HTTPException(
                status_code=503,
                detail="Administrator Force Scan is not configured.",
            )
        if not admin_token or not secrets.compare_digest(admin_token, configured_token):
            raise HTTPException(
                status_code=401,
                detail="Force Scan requires administrator authorization.",
            )
    source_ids = await asyncio.to_thread(_future_radar_run_sources, payload)
    del user
    try:
        result = await asyncio.to_thread(
            future_radar_service.run,
            trigger_type=f"manual_{scan_type}",
            scan_type=scan_type,
            source_ids=source_ids,
            force=payload.force,
        )
        # Run the independent verification queue only after the Radar service
        # acquired and released its authoritative run/source locks. A busy
        # Radar request therefore performs no external candidate fetches.
        verification_retry = await asyncio.to_thread(
            _reverify_pending_recruitment_candidates_safely,
            limit=100 if scan_type == "deep" else 40,
        )
        result["verification_retry"] = verification_retry
        return _public_radar_run(result) or {}
    except RadarRunBusy as exc:
        raise HTTPException(
            status_code=409,
            detail=f"A {exc.scan_type} Future Radar run is already active.",
            headers={"Retry-After": "3"},
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
    reason_counts = database.recruitment_ingest_verification_reason_counts(
        source_ids=tuple(EXPECTED_CHATGPT_SOURCE_IDS)
    )
    last_synced_at = max(
        (source["last_seen_at"] for source in sources if source.get("last_seen_at")),
        default=None,
    )
    transport_error = any(
        source.get("status") == "error"
        # A batch whose candidates were all deterministically rejected was
        # historically stored as source ``error``. That is review inventory,
        # not a transport failure. Preserve genuine source errors, which have
        # no rejection decision attached to their latest event.
        and int(source.get("latest_rejected", 0) or 0) == 0
        for source in sources
    )
    if connected == 0:
        transport_state = "pending"
    elif transport_error:
        transport_state = "error"
    elif connected < len(EXPECTED_CHATGPT_RADAR_SOURCES):
        transport_state = "partial"
    else:
        transport_state = "synced"
    if inventory_pending:
        verification_state = "pending"
    elif inventory_rejected:
        verification_state = "complete_with_rejections"
    else:
        verification_state = "complete"
    return {
        "status": transport_state,
        "transport_state": transport_state,
        "verification_state": verification_state,
        "expected_source_count": len(EXPECTED_CHATGPT_RADAR_SOURCES),
        "connected_source_count": connected,
        "last_synced_at": last_synced_at,
        # Keep event-scoped review counts separate so clients do not mistake a
        # normal rejected candidate for a broken source connection.
        "latest_verification_counts": {
            "accepted": latest_accepted,
            "pending": latest_pending,
            "rejected": latest_rejected,
        },
        "inventory_accepted": inventory_accepted,
        "inventory_pending": inventory_pending,
        "inventory_rejected": inventory_rejected,
        "inventory_total": inventory_accepted + inventory_pending + inventory_rejected,
        "reason_counts": reason_counts,
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
            item.get("match_score") is None,
            -(item.get("match_score") or 0),
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


def _project_recruitment_sources_to_opportunity_pool() -> dict:
    """Best-effort projection of durable local recruitment data into Radar.

    This deliberately runs only the two local database adapters. It never
    invokes OpenAI, broad web discovery, or private ChatGPT/WeChat sources.
    The Radar run lock and per-source locks remain authoritative, so a refresh
    request cannot create duplicate concurrent work.
    """
    try:
        run = future_radar_service.run(
            trigger_type="manual_candidate_source_sync",
            scan_type="quick",
            source_ids=[
                "legacy-recruitment-pipeline",
                "legacy-search-discovery",
            ],
        )
    except RadarRunBusy:
        return {"status": "deferred", "reason": "radar_run_active"}
    except Exception:
        logger.exception("Recruitment source opportunity projection failed")
        return {"status": "deferred", "reason": "projection_unavailable"}

    succeeded = int(run.get("sources_succeeded") or 0)
    expected = 2
    run_status = str(run.get("status") or "")
    if run_status == "success" and succeeded == expected:
        projection_status = "updated"
    elif succeeded:
        projection_status = "partial"
    else:
        projection_status = "deferred"
    return {
        "status": projection_status,
        "sources_succeeded": succeeded,
        "sources_expected": expected,
        "new_jobs": int(run.get("new_jobs") or 0),
        "updated_jobs": int(run.get("updated_jobs") or 0),
        "closed_jobs": int(run.get("closed_jobs") or 0),
    }


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
        pool_projection = _project_recruitment_sources_to_opportunity_pool()
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
            "opportunity_pool_projection": pool_projection,
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
    pool_projection = _project_recruitment_sources_to_opportunity_pool()
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
        "opportunity_pool_projection": pool_projection,
    }


_TRACKING_QUERY_KEYS = {"fbclid", "gclid", "msclkid"}
# These are job-level assertions, not the ordinary Campus / Experienced Hires
# navigation links found together in many ATS page shells.
_EXPLICIT_NON_CAMPUS_PAGE_PATTERN = re.compile(
    r"(?:仅限社会招聘|社招岗位|(?:本|该)(?:岗位|职位)(?:仅面向|仅招|仅限|属于|为)(?:社会招聘|社招)|"
    r"onlyforexperienced(?:hires?|professionals?)|experienced(?:hires?|professionals?)only)",
    re.IGNORECASE,
)
_EXPLICIT_OVERSEAS_LOCATION_PATTERN = re.compile(
    r"新加坡|伦敦|纽约|悉尼|墨尔本|东京|大阪|首尔|巴黎|法兰克福|迪拜|多伦多|温哥华|"
    r"美国|英国|澳大利亚|加拿大|日本|韩国|德国|法国|仅限海外|境外岗位|海外岗位|"
    r"\b(?:singapore|london|new\s+york|sydney|melbourne|tokyo|osaka|seoul|paris|"
    r"frankfurt|dubai|toronto|vancouver|united\s+states|united\s+kingdom|australia|"
    r"canada|japan|germany|france|overseas\s+only)\b",
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


def _precheck_ingest_candidate(
    candidate: dict,
) -> tuple[str, str, dict[str, str | None]] | None:
    verified_dates = {"opening_date": None, "closing_date": None}
    actionable = {
        "company": candidate["company"],
        "title": candidate["title"],
        "city": candidate.get("city", ""),
        "official_url": candidate.get("canonical_url") or candidate.get("official_url"),
        "requirements": candidate.get("requirements", ""),
        "tags": candidate.get("tags", []),
    }
    if not is_actionable_recruitment_listing(actionable):
        return "rejected", "not_campus", verified_dates
    city = str(candidate.get("city") or "")
    location_text = f"{city} {candidate['title']}"
    location_confirmed = any(marker in location_text for marker in CORE_LOCATION_MARKERS)
    if (
        _EXPLICIT_OVERSEAS_LOCATION_PATTERN.search(city)
        and not any(marker in city for marker in CORE_LOCATION_MARKERS)
    ):
        return "rejected", "location_outside_scope", verified_dates
    return None


def _verify_ingest_candidate_page(
    candidate: dict,
    page: object,
) -> tuple[str, str | None, dict[str, str | None]]:
    """Evaluate one already-fetched page against one candidate assertion."""
    verified_dates = {"opening_date": None, "closing_date": None}
    city = str(candidate.get("city") or "")
    location_text = f"{city} {candidate['title']}"
    location_confirmed = any(marker in location_text for marker in CORE_LOCATION_MARKERS)
    page_text = str(getattr(page, "text", "") or "")
    final_url = str(
        getattr(page, "final_url", "") or candidate["canonical_url"]
    )
    evidence = _evaluate_official_candidate_page(
        {
            "company": candidate["company"],
            "title": candidate["title"],
            "url": candidate["canonical_url"],
            "closing_date": candidate.get("closing_date"),
        },
        page_text,
        final_url,
    )
    if evidence.closed:
        return "closed", "official_page_closed", verified_dates
    page_identity = _normalized_identity(page_text)
    title_identity = _normalized_identity(candidate["title"])
    title_offset = page_identity.find(title_identity) if title_identity else -1
    job_excerpt = (
        page_identity[max(0, title_offset - 30):title_offset + len(title_identity) + 140]
        if title_offset >= 0 else ""
    )
    # A number of ATS/detail URLs carry the city beside the exact role while
    # the discovery feed leaves its city column blank.  Once the official
    # page, employer, cohort and exact title have all been identified below,
    # that nearby location is stronger evidence than an empty transport
    # field.  Restrict the lookup to the role excerpt so an office-list footer
    # cannot turn an otherwise location-less campaign into a verified job.
    location_confirmed = location_confirmed or any(
        _normalized_identity(marker) in job_excerpt
        for marker in CORE_LOCATION_MARKERS
    )
    if _EXPLICIT_NON_CAMPUS_PAGE_PATTERN.search(job_excerpt):
        return "rejected", "official_page_non_campus", verified_dates
    if not evidence.readable:
        return "pending", "official_page_unreadable", verified_dates
    if not evidence.domain_confirmed:
        return "pending", "page_missing_official_domain_evidence", verified_dates
    if not evidence.employer_confirmed:
        return "pending", "page_missing_company_evidence", verified_dates
    if not evidence.cohort_confirmed:
        return "pending", "page_missing_current_cohort_evidence", verified_dates
    if not evidence.identity_confirmed:
        return "pending", "page_missing_title_evidence", verified_dates
    if not evidence.open_confirmed:
        return "pending", "page_missing_open_application_evidence", verified_dates
    if not location_confirmed:
        # Missing/JS-rendered location is not evidence of an overseas job.
        # Keep the discovery visible without asserting its location is verified.
        return "pending", "location_unconfirmed", verified_dates
    for field, semantic in (
        ("opening_date", "opening"),
        ("closing_date", "closing"),
    ):
        submitted_date = candidate.get(field)
        if submitted_date and _semantic_date_appears_in_page(
            page_text,
            submitted_date,
            semantic=semantic,
        ):
            verified_dates[field] = submitted_date
    return "verified", None, verified_dates


def _verify_ingest_candidate(
    candidate: dict,
) -> tuple[str, str | None, dict[str, str | None]]:
    precheck = _precheck_ingest_candidate(candidate)
    if precheck is not None:
        return precheck
    try:
        page = fetch_watch_page(candidate["canonical_url"], ())
    except WatchFetchError:
        return "pending", "official_page_fetch_failed", {
            "opening_date": None, "closing_date": None,
        }
    except Exception:
        logger.exception("Unexpected recruitment candidate verification failure")
        return "pending", "official_page_fetch_failed", {
            "opening_date": None, "closing_date": None,
        }
    return _verify_ingest_candidate_page(candidate, page)


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


_VERIFICATION_RETRY_DELAYS = {
    "official_page_fetch_failed": timedelta(minutes=30),
    "official_page_unreadable": timedelta(hours=1),
    "page_missing_official_domain_evidence": timedelta(hours=6),
    "page_missing_company_evidence": timedelta(hours=6),
    "page_missing_current_cohort_evidence": timedelta(hours=6),
    "page_missing_title_evidence": timedelta(hours=6),
    "page_missing_open_application_evidence": timedelta(hours=3),
    "location_unconfirmed": timedelta(hours=6),
}


def _pending_verification_retry_at(reason: str | None) -> str:
    """Return a source-level retry time; this is not a global Radar cooldown."""
    delay = _VERIFICATION_RETRY_DELAYS.get(
        str(reason or ""), timedelta(hours=1)
    )
    return (datetime.now(timezone.utc) + delay).isoformat()


def _fetch_candidate_page_group(
    canonical_url: str,
    candidates: list[dict],
) -> list[tuple[dict, str, str | None, dict[str, str | None]]]:
    """Fetch one URL once, then evaluate every source assertion for that page."""
    try:
        page = fetch_watch_page(canonical_url, ())
    except (WatchFetchError, OSError, ValueError):
        dates = {"opening_date": None, "closing_date": None}
        return [
            (candidate, "pending", "official_page_fetch_failed", dict(dates))
            for candidate in candidates
        ]
    except Exception:
        logger.exception("Unexpected recruitment candidate retry fetch failure")
        dates = {"opening_date": None, "closing_date": None}
        return [
            (candidate, "pending", "official_page_fetch_failed", dict(dates))
            for candidate in candidates
        ]
    decisions = []
    for candidate in candidates:
        try:
            status_name, reason, dates = _verify_ingest_candidate_page(candidate, page)
        except Exception:
            logger.exception("Unexpected recruitment candidate retry evaluation failure")
            status_name, reason = "pending", "official_page_unreadable"
            dates = {"opening_date": None, "closing_date": None}
        decisions.append((candidate, status_name, reason, dates))
    return decisions


def _reverify_pending_recruitment_candidates_unlocked(
    *, limit: int = 40, ignore_retry_time: bool = False,
) -> dict:
    """Advance a durable pending queue without duplicating concurrent checks.

    Claims are database-backed, so another web process, a scheduled pass, or a
    repeated button click cannot verify the same row concurrently. Candidates
    sharing a canonical page are evaluated from one deterministic fetch.
    """
    claim_token, claimed = database.claim_pending_recruitment_ingest_candidates(
        limit=limit,
        ignore_retry_time=ignore_retry_time,
    )
    summary = {
        "claimed": len(claimed),
        "checked": 0,
        "verified": 0,
        "pending": 0,
        "rejected": 0,
        "closed": 0,
        "fetches": 0,
        "reason_counts": {},
    }
    if not claimed:
        return summary

    decisions: list[tuple[dict, str, str | None, dict[str, str | None]]] = []
    page_groups: dict[str, list[dict]] = defaultdict(list)
    empty_dates = {"opening_date": None, "closing_date": None}
    for candidate in claimed:
        precheck = _precheck_ingest_candidate(candidate)
        if precheck is not None:
            status_name, reason, dates = precheck
            decisions.append((candidate, status_name, reason, dates))
        else:
            page_groups[str(candidate["canonical_url"])].append(candidate)

    summary["fetches"] = len(page_groups)
    if page_groups:
        with ThreadPoolExecutor(max_workers=min(6, len(page_groups))) as executor:
            futures = {
                executor.submit(_fetch_candidate_page_group, url, group): url
                for url, group in page_groups.items()
            }
            for future in as_completed(futures):
                try:
                    decisions.extend(future.result())
                except Exception:
                    logger.exception("Recruitment candidate retry group failed")
                    decisions.extend(
                        (
                            candidate,
                            "pending",
                            "official_page_fetch_failed",
                            dict(empty_dates),
                        )
                        for candidate in page_groups[futures[future]]
                    )

    for candidate, status_name, reason, verified_dates in decisions:
        try:
            promoted_job_id = candidate.get("promoted_job_id")
            if status_name == "verified":
                candidate["verified_opening_date"] = verified_dates["opening_date"]
                candidate["verified_closing_date"] = verified_dates["closing_date"]
                job = _promoted_job(candidate)
                promoted_job_id = job["id"]
                stored = database.finalize_recruitment_ingest_candidate_verification(
                    candidate["id"],
                    "verified",
                    None,
                    claim_token=claim_token,
                    promoted_job=job,
                    verified_opening_date=verified_dates["opening_date"],
                    verified_closing_date=verified_dates["closing_date"],
                )
            elif status_name == "closed":
                stored = database.finalize_recruitment_ingest_candidate_verification(
                    candidate["id"], "closed", reason,
                    claim_token=claim_token,
                    promoted_job_id=promoted_job_id,
                )
            elif status_name == "rejected":
                stored = database.finalize_recruitment_ingest_candidate_verification(
                    candidate["id"], "rejected", reason,
                    claim_token=claim_token,
                    promoted_job_id=promoted_job_id,
                )
            else:
                status_name = "pending"
                stored = database.finalize_recruitment_ingest_candidate_verification(
                    candidate["id"],
                    "pending",
                    reason,
                    claim_token=claim_token,
                    promoted_job_id=promoted_job_id,
                    next_verification_at=_pending_verification_retry_at(reason),
                )
            # A newer worker may have reclaimed an expired lease. It owns the
            # row now, so this worker must not count or overwrite its result.
            if stored is None:
                continue
            summary["checked"] += 1
            summary[status_name] += 1
            if reason:
                safe_reason = (
                    reason
                    if reason in database.SAFE_RECRUITMENT_VERIFICATION_REASONS
                    else "other"
                )
                counts = summary["reason_counts"]
                counts[safe_reason] = counts.get(safe_reason, 0) + 1
        except Exception:
            logger.exception("Recruitment candidate retry persistence failed")
            database.release_recruitment_ingest_candidate_verification_claim(
                candidate["id"],
                claim_token,
                next_verification_at=_pending_verification_retry_at(
                    "official_page_fetch_failed"
                ),
            )
    return summary


def reverify_pending_recruitment_candidates(
    *, limit: int = 40, ignore_retry_time: bool = False,
) -> dict:
    """Run one bounded retry worker per web process; DB leases remain authoritative."""
    if not _recruitment_verification_retry_lock.acquire(blocking=False):
        return {
            "busy": True,
            "claimed": 0,
            "checked": 0,
            "verified": 0,
            "pending": 0,
            "rejected": 0,
            "closed": 0,
            "fetches": 0,
            "reason_counts": {},
        }
    try:
        return {
            "busy": False,
            **_reverify_pending_recruitment_candidates_unlocked(
                limit=limit, ignore_retry_time=ignore_retry_time,
            ),
        }
    finally:
        _recruitment_verification_retry_lock.release()


def _reverify_pending_recruitment_candidates_safely(*, limit: int) -> dict:
    """Keep verification failures separate from the authoritative Radar run."""
    try:
        return {
            "status": "success",
            **reverify_pending_recruitment_candidates(limit=limit),
        }
    except Exception:
        logger.exception("Pending recruitment verification retry failed")
        return {
            "status": "error",
            "claimed": 0,
            "checked": 0,
            "verified": 0,
            "pending": 0,
            "rejected": 0,
            "closed": 0,
            "fetches": 0,
            "reason_counts": {},
            "busy": False,
        }


@app.get("/api/recruitment/sync/status")
def recruitment_sync_status(
    _: Annotated[None, Depends(require_recruitment_ingest_token)],
) -> dict:
    status = database.recruitment_sync_status(
        expected_source_count=len(EXPECTED_CHATGPT_RADAR_SOURCES)
    )
    status["reason_counts"] = database.recruitment_ingest_verification_reason_counts()
    status["verification_host_counts"] = (
        database.recruitment_ingest_verification_host_counts()
    )
    return status


@app.post("/api/recruitment/verification/retry")
def retry_recruitment_verification(
    _: Annotated[None, Depends(require_recruitment_ingest_token)],
    limit: int = Query(default=100, ge=1, le=100),
    force: bool = Query(default=False),
) -> dict:
    """Operationally drain one safe retry batch without exposing candidate data."""
    result = reverify_pending_recruitment_candidates(
        limit=limit, ignore_retry_time=force,
    )
    projection = "unchanged"
    if result["checked"]:
        try:
            bridge = future_radar_service.run(
                trigger_type="verification_retry_bridge",
                scan_type="quick",
                source_ids=["legacy-search-discovery"],
            )
            projection = (
                "updated"
                if bridge.get("status") in {"success", "partial_success"}
                else "deferred"
            )
        except RadarRunBusy:
            projection = "deferred"
        except Exception:
            logger.exception("Recruitment verification projection failed")
            projection = "deferred"
    return {
        **result,
        "projection": projection,
        "inventory": database.recruitment_ingest_verification_reason_counts(),
    }


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
    bridge_candidate_ids: list[str] = []
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

        stored = database.upsert_recruitment_ingest_candidate(
            candidate,
            claim_for_verification=True,
        )
        # Include replays/stale/closed/rejected observations as well as new
        # rows. The bridge reads their committed state without rescanning the
        # entire historical pool for every ten-item ingest batch.
        bridge_candidate_ids.append(stored["id"])
        claim_token = stored.pop("claimed_verification_token", None)
        disposition = stored.pop("disposition")
        if disposition == "stale":
            totals["duplicates"] += 1
            totals["stale"] += 1
            group["counts"]["duplicates"] += 1
            group["counts"]["stale"] += 1
            existing_status = stored.get("verification_status")
            if existing_status == "verified":
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

        # A closed replay stays closed. Verified/rejected rows are intentionally
        # rechecked when their source reports them again: the official page may
        # have closed or gained the missing evidence since the prior pass.
        existing_status = str(stored.get("verification_status") or "pending")
        if existing_status == "closed":
            totals["closed"] += 1
            group["counts"]["closed"] += 1
            skipped.append({
                "title": item.title,
                "reason": stored.get("verification_reason") or "closed",
            })
            continue

        if claim_token is None:
            # Another ingest/retry worker owns this exact candidate, or the
            # candidate has a configured retry time in the future. Never fetch
            # it concurrently and never overwrite its eventual decision.
            result_key = (
                "accepted" if existing_status == "verified"
                else existing_status if existing_status in {"rejected", "closed"}
                else "pending"
            )
            totals[result_key] += 1
            group["counts"][result_key] += 1
            skipped.append({
                "title": item.title,
                "reason": stored.get("verification_reason") or "verification_in_progress",
            })
            continue

        incoming_closed = item.status == "closed"
        incoming_expired = bool(item.closing_date and item.closing_date <= today)
        if incoming_closed or incoming_expired:
            finalized = database.finalize_recruitment_ingest_candidate_verification(
                stored["id"],
                "closed",
                "closed" if incoming_closed else "expired",
                claim_token=claim_token,
                promoted_job_id=stored.get("promoted_job_id"),
            )
            if finalized is None:
                totals["pending"] += 1
                group["counts"]["pending"] += 1
                skipped.append({"title": item.title, "reason": "verification_superseded"})
                continue
            totals["closed"] += 1
            group["counts"]["closed"] += 1
            skipped.append({
                "title": item.title,
                "reason": "closed" if incoming_closed else "expired",
            })
            continue

        verified_deadline = stored.get("verified_closing_date")
        if verified_deadline and str(verified_deadline) <= today.isoformat():
            finalized = database.finalize_recruitment_ingest_candidate_verification(
                stored["id"], "closed", "expired",
                claim_token=claim_token,
                promoted_job_id=stored.get("promoted_job_id"),
            )
            if finalized is None:
                totals["pending"] += 1
                group["counts"]["pending"] += 1
                skipped.append({"title": item.title, "reason": "verification_superseded"})
                continue
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
            finalized = database.finalize_recruitment_ingest_candidate_verification(
                stored["id"],
                "verified",
                None,
                claim_token=claim_token,
                promoted_job=job,
                verified_opening_date=verified_dates["opening_date"],
                verified_closing_date=verified_dates["closing_date"],
            )
            if finalized is None:
                totals["pending"] += 1
                group["counts"]["pending"] += 1
                skipped.append({"title": item.title, "reason": "verification_superseded"})
                continue
            totals["accepted"] += 1
            group["counts"]["accepted"] += 1
        elif verification_status == "closed":
            finalized = database.finalize_recruitment_ingest_candidate_verification(
                stored["id"], "closed", reason,
                claim_token=claim_token,
                promoted_job_id=stored.get("promoted_job_id"),
            )
            if finalized is None:
                totals["pending"] += 1
                group["counts"]["pending"] += 1
                skipped.append({"title": item.title, "reason": "verification_superseded"})
                continue
            totals["closed"] += 1
            group["counts"]["closed"] += 1
            skipped.append({"title": item.title, "reason": reason})
        else:
            finalized = database.finalize_recruitment_ingest_candidate_verification(
                stored["id"], verification_status, reason,
                claim_token=claim_token,
                promoted_job_id=stored.get("promoted_job_id"),
                next_verification_at=(
                    _pending_verification_retry_at(reason)
                    if verification_status == "pending" else None
                ),
            )
            if finalized is None:
                totals["pending"] += 1
                group["counts"]["pending"] += 1
                skipped.append({"title": item.title, "reason": "verification_superseded"})
                continue
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
    search_updates_refresh: dict | None = None
    if request.jobs:
        # Ingest has already committed its candidates and verification result.
        # Refresh only the deterministic local bridge, never all Quick/Deep
        # sources. Keep the normal run/source locks, and leave durable rows for
        # the next Quick Scan if this best-effort projection is unavailable.
        try:
            bridge = future_radar_service.run(
                trigger_type="ingest_bridge",
                scan_type="quick",
                source_ids=["legacy-search-discovery"],
                bridge_candidate_ids=list(dict.fromkeys(bridge_candidate_ids)),
            )
            search_updates_refresh = (
                {"status": "success"}
                if bridge.get("status") == "success" and bridge.get("sources_succeeded", 0) > 0
                else {"status": "deferred", "code": "BRIDGE_NOT_COMPLETED"}
            )
        except RadarRunBusy:
            search_updates_refresh = {"status": "deferred", "code": "RADAR_RUN_BUSY"}
        except Exception as exc:
            logger.warning(
                "Future Radar ingest bridge deferred error_type=%s",
                type(exc).__name__,
            )
            search_updates_refresh = {"status": "deferred", "code": "BRIDGE_UNAVAILABLE"}
    return {
        **totals,
        "event_id": event_ids[0] if len(event_ids) == 1 else None,
        "event_ids": event_ids,
        "skipped": skipped,
        "received_at": database.utc_now(),
        **({"search_updates_refresh": search_updates_refresh} if search_updates_refresh else {}),
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

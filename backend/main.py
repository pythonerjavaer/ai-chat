import hashlib
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

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
from .recruitment import SAMPLE_JOBS, score_job
from .live_sources import fetch_adzuna_jobs
from .config import settings
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


@asynccontextmanager
async def lifespan(_: FastAPI):
    database.init_db()
    database.seed_recruitment_jobs(SAMPLE_JOBS)
    yield


PRIVACY_VERSION = "2026-08-21"


app = FastAPI(title="FrostFire AI API", version="4.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


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


class AppleTransactionRequest(BaseModel):
    signed_transaction: str = Field(min_length=20, max_length=50_000)


class RecruitmentProfileRequest(BaseModel):
    desired_roles: list[str] = Field(default_factory=list, max_length=12)
    industries: list[str] = Field(default_factory=list, max_length=8)
    locations: list[str] = Field(default_factory=list, max_length=12)
    employer_types: list[str] = Field(default_factory=list, max_length=6)
    background: str = Field(default="", max_length=4_000)
    education_level: str = Field(default="", max_length=40)
    major_category: str = Field(default="", max_length=60)
    school_tier: str = Field(default="", max_length=40)
    experience_level: str = Field(default="", max_length=40)
    skill_tags: list[str] = Field(default_factory=list, max_length=16)
    language_level: str = Field(default="", max_length=40)
    undergraduate_major: str = Field(default="", max_length=60)
    undergraduate_school_tier: str = Field(default="", max_length=40)
    master_major: str = Field(default="", max_length=60)
    master_school_tier: str = Field(default="", max_length=40)
    composite_interest: bool = False
    graduation_year: int | None = Field(default=None, ge=2020, le=2100)
    availability_start: str | None = Field(default=None, max_length=10)
    availability_end: str | None = Field(default=None, max_length=10)


def current_user(
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
    return user


User = Annotated[dict, Depends(current_user)]


def public_user(user: dict) -> dict:
    return {
        "id": user["id"],
        "username": user["username"],
        "privacy_accepted": bool(user.get("privacy_accepted_at")),
        "privacy_version": user.get("privacy_version"),
        "plan": user.get("plan", "free"),
    }


def token_response(user: dict) -> dict:
    return {
        "access_token": create_access_token(user["id"], user["username"]),
        "token_type": "bearer",
        "user": public_user(user),
    }


def require_privacy_consent(user: User) -> dict:
    if not user.get("privacy_accepted_at"):
        raise HTTPException(
            status_code=428,
            detail="Privacy consent is required before sending data to OpenAI.",
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


def prepare_chat(user_id: int, request: ChatRequest) -> tuple[dict, list, list]:
    session = resolve_session(user_id, request.session_id, request.workspace)
    workspace = session["workspace"]
    context = retrieve_context(user_id, request.message, workspace)
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


@app.get("/api/workspaces")
def workspaces() -> list[dict]:
    return public_workspace_config()


@app.post("/api/auth/register", status_code=status.HTTP_201_CREATED)
def register(request: AuthRequest) -> dict:
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
    return token_response(user)


@app.post("/api/auth/login")
def login(request: AuthRequest) -> dict:
    user = database.get_user_by_username(request.username.strip())
    if not user or not verify_password(request.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password.")
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


@app.get("/api/recruitment/profile")
def recruitment_profile(user: User) -> dict:
    return database.get_recruitment_profile(user["id"])


@app.put("/api/recruitment/profile")
def save_recruitment_profile(request: RecruitmentProfileRequest, user: ConsentedUser) -> dict:
    payload = request.model_dump()
    for key in ("desired_roles", "industries", "locations", "employer_types"):
        payload[key] = [str(value).strip()[:80] for value in payload[key] if str(value).strip()]
    payload["skill_tags"] = [str(value).strip()[:80] for value in payload["skill_tags"] if str(value).strip()]
    return database.save_recruitment_profile(user["id"], payload)


@app.get("/api/recruitment/jobs")
def recruitment_jobs(user: User) -> dict:
    profile = database.get_recruitment_profile(user["id"])
    jobs = [score_job(job, profile) for job in database.list_recruitment_jobs()]
    jobs.sort(key=lambda item: (-item["match_score"], item["days_left"] is None, item["days_left"] or 9999))
    return {
        "jobs": jobs,
        "profile": profile,
        "data_status": {
            "mode": "live" if settings.adzuna_app_id and settings.adzuna_app_key else "sample",
            "message": "岗位来自已配置的官方/授权源。" if settings.adzuna_app_id and settings.adzuna_app_key else "当前为示例岗位数据；配置官方 API 或合规数据供应商后才会实时更新。",
            "last_sync": None,
        },
    }


@app.post("/api/recruitment/refresh")
def refresh_recruitment(user: ConsentedUser) -> dict:
    del user
    if not settings.adzuna_app_id or not settings.adzuna_app_key:
        raise HTTPException(status_code=503, detail="尚未配置官方招聘 API 凭证。")
    try:
        jobs = fetch_adzuna_jobs()
        database.upsert_recruitment_jobs(jobs)
    except Exception as exc:
        logger.exception("Recruitment source refresh failed")
        raise HTTPException(status_code=502, detail="招聘源刷新失败，请稍后重试。") from exc
    return {"source": "Adzuna API", "count": len(jobs), "refreshed_at": database.utc_now()}


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
    if not database.delete_user(user["id"]):
        raise HTTPException(status_code=404, detail="Account not found.")
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
        embeddings = create_embeddings([chunk["content"] for chunk in chunks])
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


@app.post("/api/spaces/{space_id}/run")
def run_created_space(
    space_id: str,
    request: SpaceRunRequest,
    user: ConsentedUser,
) -> dict:
    space = database.get_space(space_id, user["id"])
    if not space:
        raise HTTPException(status_code=404, detail="AI Space not found.")
    billing = billing_status(user)
    space_usage = database.token_usage(user["id"], space_id)
    remaining = min(
        billing["remaining_tokens"],
        max(0, space["monthly_token_budget"] - space_usage["total_tokens"]),
    )
    if remaining < 256:
        raise HTTPException(
            status_code=429,
            detail="This AI Space has reached its monthly Token budget.",
        )
    try:
        reply, usage = run_space(
            space["system_prompt"],
            request.message,
            max_output_tokens=min(600, max(128, remaining // 2)),
        )
    except Exception as exc:
        logger.exception("AI Space request failed")
        raise HTTPException(status_code=502, detail="OpenAI API request failed.") from exc
    database.record_token_usage(
        user["id"],
        space_id,
        usage["input_tokens"],
        usage["output_tokens"],
        usage["total_tokens"],
    )
    return {
        "space": {
            key: space[key]
            for key in ("id", "name", "icon", "theme", "template_id")
        },
        "reply": reply,
        "usage": usage,
        "billing": billing_status(user),
    }


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
        "headline": result.get("headline", "冰火交叉审查"),
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
        session, messages, sources = prepare_chat(user["id"], request)
        reply, tools_used = run_agent(messages, session["workspace"])
        database.append_message(session["id"], "assistant", reply)
        return {
            "reply": reply,
            "session_id": session["id"],
            "workspace": session["workspace"],
            "sources": sources,
            "tools_used": tools_used,
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

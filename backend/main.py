import json
import logging
from contextlib import asynccontextmanager
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
    stream_agent,
)
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

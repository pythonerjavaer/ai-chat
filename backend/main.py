import json
import logging
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, File, HTTPException, Response, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from . import database
from .ai_service import (
    build_messages,
    create_embeddings,
    retrieve_context,
    run_agent,
    split_document,
    stream_agent,
)
from .config import settings
from .security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


logger = logging.getLogger(__name__)
bearer_scheme = HTTPBearer(auto_error=False)


@asynccontextmanager
async def lifespan(_: FastAPI):
    database.init_db()
    yield


app = FastAPI(title="AI Chat API", version="2.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AuthRequest(BaseModel):
    username: str = Field(min_length=3, max_length=50, pattern=r"^[\w.-]+$")
    password: str = Field(min_length=8, max_length=128)


class SessionRequest(BaseModel):
    title: str = Field(default="New conversation", min_length=1, max_length=80)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12_000)
    session_id: str | None = None


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


def token_response(user: dict) -> dict:
    return {
        "access_token": create_access_token(user["id"], user["username"]),
        "token_type": "bearer",
        "user": {"id": user["id"], "username": user["username"]},
    }


def resolve_session(user_id: int, session_id: str | None) -> dict:
    if not session_id:
        return database.create_session(user_id)
    session = database.get_session(session_id, user_id)
    if not session:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return session


def prepare_chat(user_id: int, request: ChatRequest) -> tuple[dict, list, list]:
    session = resolve_session(user_id, request.session_id)
    context = retrieve_context(user_id, request.message)
    database.append_message(session["id"], "user", request.message)
    messages = build_messages(user_id, session["id"], context)
    sources = [
        {
            "document_id": item["document_id"],
            "name": item["name"],
            "score": item["score"],
        }
        for item in context
    ]
    return session, messages, sources


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "version": app.version}


@app.post("/api/auth/register", status_code=status.HTTP_201_CREATED)
def register(request: AuthRequest) -> dict:
    try:
        user = database.create_user(
            request.username.strip(),
            hash_password(request.password),
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
    return {"id": user["id"], "username": user["username"]}


@app.get("/api/sessions")
def sessions(user: User) -> list[dict]:
    return database.list_sessions(user["id"])


@app.post("/api/sessions", status_code=status.HTTP_201_CREATED)
def new_session(request: SessionRequest, user: User) -> dict:
    return database.create_session(user["id"], request.title.strip())


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
def documents(user: User) -> list[dict]:
    return database.list_documents(user["id"])


@app.post("/api/documents", status_code=status.HTTP_201_CREATED)
def upload_document(
    user: User,
    file: UploadFile = File(...),
) -> dict:
    filename = (file.filename or "document.txt").strip()[:160]
    if not filename.lower().endswith((".txt", ".md", ".csv", ".json")):
        raise HTTPException(
            status_code=415,
            detail="Only .txt, .md, .csv and .json files are supported.",
        )
    raw = file.file.read(1_000_001)
    if len(raw) > 1_000_000:
        raise HTTPException(status_code=413, detail="Document exceeds 1 MB.")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="Document must be UTF-8 text.") from exc
    chunks = split_document(content)
    if not chunks:
        raise HTTPException(status_code=400, detail="Document is empty.")
    try:
        embeddings = create_embeddings(chunks)
    except Exception as exc:
        logger.exception("Embedding request failed")
        raise HTTPException(status_code=502, detail="Embedding request failed.") from exc
    return database.create_document(
        user["id"],
        filename,
        content,
        chunks,
        embeddings,
    )


@app.delete("/api/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_document(document_id: str, user: User) -> Response:
    if not database.delete_document(document_id, user["id"]):
        raise HTTPException(status_code=404, detail="Document not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/api/chat")
def chat(request: ChatRequest, user: User) -> dict:
    try:
        session, messages, sources = prepare_chat(user["id"], request)
        reply, tools_used = run_agent(messages)
        database.append_message(session["id"], "assistant", reply)
        return {
            "reply": reply,
            "session_id": session["id"],
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
def chat_stream(request: ChatRequest, user: User) -> StreamingResponse:
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
                {"session_id": session["id"], "sources": sources},
            )
            for event in stream_agent(messages):
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

"""Evidence-bound reading interpretation for Leap Realm.

The model runner is injected by the Frostfire application.  This module owns
document authorization, source validation and a cache that is deliberately
separate from translation.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Callable


PROMPT_VERSION = "leap-interpret-v1"
MAX_INTERPRET_CHARACTERS = 20_000


def classify_provider_failure(exc: BaseException) -> tuple[str, int, str]:
    """Return a stable public error without retaining provider diagnostics.

    OpenAI exception strings may contain request IDs or operational details.
    They are inspected only in memory for classification and are never returned
    to the browser or written to the interpretation cache.
    """
    chain: list[str] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    status_code = 0
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        status_code = status_code or int(getattr(current, "status_code", 0) or 0)
        body = getattr(current, "body", None)
        chain.append(type(current).__name__)
        chain.append(str(body) if body is not None else str(current))
        current = current.__cause__ or current.__context__
    fingerprint = " ".join(chain).casefold()
    if any(marker in fingerprint for marker in (
        "credit_balance_exhausted", "insufficient_quota", "no credits remaining",
        "credit balance", "billing_hard_limit_reached",
    )):
        return (
            "AI_CREDITS_EXHAUSTED", 429,
            "冰焰现有AI服务的API额度已用完；补充现有OpenAI API额度后即可重试，无需为跃迁域配置第二套密钥。",
        )
    if status_code == 429 or any(marker in fingerprint for marker in (
        "ratelimiterror", "rate limit", "too many requests",
    )):
        return "AI_RATE_LIMITED", 429, "冰焰AI服务当前请求过于频繁，请稍后重试。"
    if any(marker in fingerprint for marker in (
        "apitimeouterror", "timeout", "timed out",
    )):
        return "AI_TIMEOUT", 504, "冰焰AI服务响应超时，请稍后重试。"
    if any(marker in fingerprint for marker in (
        "modelunavailableerror", "not configured", "未配置",
    )):
        return "AI_NOT_CONFIGURED", 503, "冰焰AI服务尚未配置。"
    if status_code in {401, 403} or any(marker in fingerprint for marker in (
        "authenticationerror", "permissiondeniederror", "invalid api key",
    )):
        return "AI_PROVIDER_ERROR", 502, "冰焰AI服务认证失败，请检查现有服务配置。"
    if any(marker in fingerprint for marker in (
        "apiconnectionerror", "connection error", "network error",
    )):
        return "AI_PROVIDER_ERROR", 502, "冰焰AI服务暂时无法连接，请稍后重试。"
    return "AI_PROVIDER_ERROR", 502, "冰焰AI服务暂时无法完成内容解读，请稍后重试。"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def init_interpretation_schema(connect: Callable[[], Any]) -> None:
    with connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS leap_interpretation_cache (
                id TEXT PRIMARY KEY,
                cache_key TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                document_id TEXT NOT NULL,
                document_version INTEGER NOT NULL,
                document_hash TEXT NOT NULL,
                action TEXT NOT NULL,
                scope TEXT NOT NULL,
                paragraph_start INTEGER NOT NULL,
                paragraph_end INTEGER NOT NULL,
                selection_start INTEGER,
                selection_end INTEGER,
                source_text_hash TEXT NOT NULL,
                context_text_hash TEXT NOT NULL,
                provider TEXT NOT NULL,
                provider_model TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                result_text TEXT NOT NULL,
                coverage_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(document_id) REFERENCES leap_materials(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_leap_interpretation_document
                ON leap_interpretation_cache(user_id,document_id,document_version,updated_at DESC);
            """
        )


class InterpretationService:
    def __init__(
        self,
        connect: Callable[[], Any],
        runner: Callable[[int, str, str, int], dict[str, Any]] | None = None,
        *,
        provider: str = "frostfire_ai",
        provider_model: str = "unconfigured",
    ):
        self.connect = connect
        self.runner = runner
        self.provider = provider
        self.provider_model = provider_model

    def capabilities(self) -> dict[str, Any]:
        return {
            "available": self.runner is not None,
            "provider": self.provider,
            "provider_model": self.provider_model,
            "prompt_version": PROMPT_VERSION,
            "message": "内容解读可用" if self.runner else "内容解读暂不可用：冰焰AI服务未配置。",
        }

    def _material(self, user_id: int, document_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id,user_id,version,content_hash,title,author FROM leap_materials WHERE id=? AND user_id=?",
                (document_id, user_id),
            ).fetchone()
        if not row:
            raise KeyError("材料不存在。")
        return dict(row)

    def _paragraphs(self, material: dict[str, Any], start: int, end: int) -> list[dict[str, Any]]:
        if end < start or end - start > 299:
            raise ValueError("解读范围无效或过大。")
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT position,content,chapter_position,chapter_title,stable_anchor
                   FROM leap_paragraphs WHERE material_id=? AND material_version=?
                   AND position>=? AND position<=? ORDER BY position""",
                (material["id"], material["version"], start, end),
            ).fetchall()
        values = [dict(row) for row in rows]
        if len(values) != end - start + 1:
            raise ValueError("解读范围无法完整定位到当前文档版本。")
        return values

    @staticmethod
    def _expected_selection(rows: list[dict[str, Any]], start_offset: int | None, end_offset: int | None) -> str:
        if start_offset is None or end_offset is None:
            raise ValueError("所选文字缺少原文位置。")
        first, last = rows[0]["content"], rows[-1]["content"]
        if start_offset < 0 or start_offset > len(first) or end_offset < 0 or end_offset > len(last):
            raise ValueError("所选文字位置超出原文。")
        if len(rows) == 1:
            if end_offset < start_offset:
                raise ValueError("所选文字位置无效。")
            return first[start_offset:end_offset].strip()
        return "\n\n".join([first[start_offset:], *[row["content"] for row in rows[1:-1]], last[:end_offset]]).strip()

    def _validated(self, user_id: int, payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        material = self._material(user_id, payload["document_id"])
        if payload.get("document_version") is not None and int(payload["document_version"]) != int(material["version"]):
            raise ValueError("解读范围无法完整定位到当前文档版本。")
        start = int(payload["paragraph_start"])
        end = int(payload.get("paragraph_end", start))
        rows = self._paragraphs(material, start, end)
        source = str(payload.get("source_text") or "").strip()
        context = str(payload.get("context_text") or "").strip()
        if not source or len(source) > MAX_INTERPRET_CHARACTERS or len(context) > MAX_INTERPRET_CHARACTERS:
            raise ValueError("解读原文为空或超过单次处理上限。")
        scope = payload["scope"]
        if scope in {"paragraph", "chapter"}:
            expected = "\n\n".join(row["content"] for row in rows).strip()
        else:
            expected = self._expected_selection(rows, payload.get("selection_start"), payload.get("selection_end"))
        if source != expected:
            raise ValueError("解读内容与当前版本原文位置不一致。")
        if context and context not in "\n\n".join(row["content"] for row in rows):
            # Context may be a neighbouring paragraph only when the source is in
            # one paragraph.  It must still come from this owned document.
            with self.connect() as connection:
                found = connection.execute(
                    """SELECT 1 FROM leap_paragraphs WHERE material_id=? AND material_version=?
                       AND content=? LIMIT 1""",
                    (material["id"], material["version"], context),
                ).fetchone()
            if not found:
                raise ValueError("辅助上下文无法定位到当前文档版本。")
        return material, rows

    def interpret(self, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        material, rows = self._validated(user_id, payload)
        source = payload["source_text"].strip()
        context = payload.get("context_text", "").strip()
        coverage = {
            "complete": bool(payload.get("coverage_complete", True)),
            "label": payload.get("coverage_label") or "完整范围",
            "paragraph_start": rows[0]["position"],
            "paragraph_end": rows[-1]["position"],
            "paragraph_count": len(rows),
        }
        identity = {
            "user_id": user_id,
            "document_id": material["id"],
            "document_version": material["version"],
            "document_hash": material["content_hash"],
            "action": "interpret",
            "scope": payload["scope"],
            "paragraph_start": rows[0]["position"],
            "paragraph_end": rows[-1]["position"],
            "selection_start": payload.get("selection_start"),
            "selection_end": payload.get("selection_end"),
            "source_text_hash": _digest(source),
            "context_text_hash": _digest(context),
            "provider": self.provider,
            "provider_model": self.provider_model,
            "prompt_version": PROMPT_VERSION,
        }
        cache_key = _digest(json.dumps(identity, ensure_ascii=False, sort_keys=True))
        if not payload.get("force"):
            with self.connect() as connection:
                cached = connection.execute(
                    "SELECT * FROM leap_interpretation_cache WHERE cache_key=? AND user_id=?",
                    (cache_key, user_id),
                ).fetchone()
            if cached:
                result = dict(cached)
                result["coverage"] = json.loads(result.pop("coverage_json") or "{}")
                result["cache_hit"] = True
                return result
        if self.runner is None:
            raise ValueError("内容解读暂不可用：冰焰AI服务未配置。")

        scope_guidance = {
            "word": "解释这个词在本句中的具体作用；必要时说明搭配、隐喻或专业语境，不要只给译词。",
            "sentence": "解释这句话的意思、关键表达、指代和必要的隐含关系，不扩成整段分析。",
            "paragraph": "解释段落主旨、论证或叙述逻辑、关键概念及必要上下文关系。",
            "selection": "只围绕所选文字解释，允许多句或跨段，但不要扩大到整份文档。",
            "chapter": "解释所覆盖章节内容的主题、结构、关键论点或事件及内部联系。",
        }[payload["scope"]]
        system = (
            "你是冰焰跃迁域的证据型阅读助手。只依据提供的原文和辅助上下文进行内容解读，"
            "不得把译文当解读，不得编造作者意图、历史背景或文外事实。输出简洁中文，并明确分为："
            "原文明确表达、理解与推断、材料不足。只有确有内容时才写材料不足。"
        )
        prompt = (
            f"任务范围：{payload['scope']}\n范围要求：{scope_guidance}\n"
            f"覆盖：{coverage['label']}\n\n需要解读的原文：\n{source}\n\n"
            + (f"仅供辅助理解的上下文（不得擅自纳入处理范围）：\n{context}" if context else "没有额外上下文。")
        )
        output = self.runner(user_id, system, prompt, 900 if payload["scope"] == "chapter" else 600)
        text = str(output.get("text") or "").strip()
        if not text:
            raise ValueError("内容解读服务没有返回结果。")
        now, row_id = _now(), str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO leap_interpretation_cache
                   (id,cache_key,user_id,document_id,document_version,document_hash,action,scope,
                    paragraph_start,paragraph_end,selection_start,selection_end,source_text_hash,
                    context_text_hash,provider,provider_model,prompt_version,result_text,coverage_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(cache_key) DO UPDATE SET result_text=excluded.result_text,
                    coverage_json=excluded.coverage_json,updated_at=excluded.updated_at""",
                (row_id, cache_key, user_id, material["id"], material["version"], material["content_hash"],
                 "interpret", payload["scope"], rows[0]["position"], rows[-1]["position"],
                 payload.get("selection_start"), payload.get("selection_end"), identity["source_text_hash"],
                 identity["context_text_hash"], self.provider, self.provider_model, PROMPT_VERSION,
                 text, json.dumps(coverage, ensure_ascii=False), now, now),
            )
            saved = connection.execute(
                "SELECT * FROM leap_interpretation_cache WHERE cache_key=? AND user_id=?", (cache_key, user_id)
            ).fetchone()
        result = dict(saved)
        result["coverage"] = json.loads(result.pop("coverage_json") or "{}")
        result["cache_hit"] = False
        return result

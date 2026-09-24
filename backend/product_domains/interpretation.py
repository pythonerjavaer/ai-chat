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

from .interpretation_providers import (
    CallbackInterpretationProvider,
    InterpretationProvider,
    InterpretationProviderError,
)


PROMPT_VERSION = "leap-interpret-v2-structured"
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
                request_key TEXT NOT NULL DEFAULT '',
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
                requested_model TEXT NOT NULL DEFAULT '',
                provider_model TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                result_text TEXT NOT NULL,
                result_json TEXT NOT NULL DEFAULT '{}',
                requested_at TEXT NOT NULL DEFAULT '',
                provider_status TEXT NOT NULL DEFAULT 'success',
                coverage_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(document_id) REFERENCES leap_materials(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_leap_interpretation_document
                ON leap_interpretation_cache(user_id,document_id,document_version,updated_at DESC);
            CREATE TABLE IF NOT EXISTS leap_interpretation_provider_attempts (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                document_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                requested_model TEXT NOT NULL,
                actual_model TEXT NOT NULL DEFAULT '',
                requested_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                status TEXT NOT NULL,
                error_code TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(document_id) REFERENCES leap_materials(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_leap_interpretation_attempts
                ON leap_interpretation_provider_attempts(user_id,requested_at DESC);
            """
        )
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(leap_interpretation_cache)").fetchall()}
        additions = {
            "request_key": "TEXT NOT NULL DEFAULT ''",
            "requested_model": "TEXT NOT NULL DEFAULT ''",
            "result_json": "TEXT NOT NULL DEFAULT '{}'",
            "requested_at": "TEXT NOT NULL DEFAULT ''",
            "provider_status": "TEXT NOT NULL DEFAULT 'success'",
        }
        for name, declaration in additions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE leap_interpretation_cache ADD COLUMN {name} {declaration}")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_leap_interpretation_request ON leap_interpretation_cache(user_id,request_key,updated_at DESC)"
        )


class InterpretationService:
    def __init__(
        self,
        connect: Callable[[], Any],
        runner: Callable[[int, str, str, int], dict[str, Any]] | None = None,
        *,
        provider: str = "frostfire_ai",
        provider_model: str = "unconfigured",
        providers: dict[str, InterpretationProvider] | None = None,
        default_provider: str | None = None,
    ):
        self.connect = connect
        self.providers = dict(providers or {})
        if runner is not None or not self.providers:
            self.providers.setdefault(provider, CallbackInterpretationProvider(runner, provider_model, provider))
        self.default_provider = default_provider or (provider if provider in self.providers else next(iter(self.providers)))

    def capabilities(self) -> dict[str, Any]:
        providers = [
            {
                "id": item.provider_id,
                "label": item.label,
                "configured": item.configured,
                "free": item.is_free,
                "requested_model": item.requested_model,
            }
            for item in self.providers.values()
        ]
        available = any(item["configured"] for item in providers)
        return {
            "available": available,
            "default_provider": self.default_provider,
            "providers": providers,
            "prompt_version": PROMPT_VERSION,
            "message": "内容解读可用" if available else "内容解读暂不可用：尚未配置解读Provider。",
        }

    @staticmethod
    def _structured_result(text: str, scope: str) -> dict[str, Any]:
        value = text.strip()
        if value.startswith("```"):
            lines = value.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            value = "\n".join(lines).strip()
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            # Keep genuine provider prose usable.  We do not manufacture an
            # explanation or evidence when the free router omits JSON.
            if not value:
                raise InterpretationProviderError(
                    "INTERPRETATION_PARSE_ERROR", 502, "解读Provider没有返回可用内容。",
                )
            parsed = {"concise_meaning": value, "explanation": "", "evidence": [], "uncertainty": "", "scope": scope}
        if not isinstance(parsed, dict):
            raise InterpretationProviderError(
                "INTERPRETATION_PARSE_ERROR", 502, "解读Provider返回格式无法解析。",
            )
        concise = str(parsed.get("concise_meaning") or parsed.get("meaning") or "").strip()
        explanation = str(parsed.get("explanation") or "").strip()
        evidence_value = parsed.get("evidence", [])
        if isinstance(evidence_value, str):
            evidence = [evidence_value.strip()] if evidence_value.strip() else []
        elif isinstance(evidence_value, list):
            evidence = [str(item).strip() for item in evidence_value if str(item).strip()][:6]
        else:
            evidence = []
        uncertainty = str(parsed.get("uncertainty") or "").strip()
        if not concise and not explanation:
            raise InterpretationProviderError(
                "INTERPRETATION_PARSE_ERROR", 502, "解读Provider返回格式缺少含义内容。",
            )
        return {
            "concise_meaning": concise,
            "explanation": explanation,
            "evidence": evidence,
            "uncertainty": uncertainty,
            "scope": scope,
        }

    @staticmethod
    def _display_text(structured: dict[str, Any]) -> str:
        sections = [structured.get("concise_meaning", ""), structured.get("explanation", "")]
        if structured.get("evidence"):
            sections.append("原文线索：" + "；".join(structured["evidence"]))
        if structured.get("uncertainty"):
            sections.append("不确定性：" + structured["uncertainty"])
        return "\n\n".join(str(item).strip() for item in sections if str(item).strip())

    def _record_attempt(
        self, user_id: int, document_id: str, provider: str, requested_model: str,
        actual_model: str, requested_at: str, status: str, error_code: str = "",
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO leap_interpretation_provider_attempts
                   (id,user_id,document_id,provider,requested_model,actual_model,requested_at,finished_at,status,error_code)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), user_id, document_id, provider, requested_model, actual_model,
                 requested_at, _now(), status, error_code),
            )

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
        provider_id = str(payload.get("provider") or self.default_provider).strip().lower()
        selected_provider = self.providers.get(provider_id)
        if selected_provider is None:
            raise InterpretationProviderError("AI_PROVIDER_ERROR", 422, "未知的内容解读Provider。")
        if not selected_provider.configured:
            code = "OPENROUTER_NOT_CONFIGURED" if provider_id == "openrouter" else "AI_NOT_CONFIGURED"
            raise InterpretationProviderError(code, 503, f"{selected_provider.label}尚未配置。")
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
            "provider": provider_id,
            "requested_model": selected_provider.requested_model,
            "prompt_version": PROMPT_VERSION,
        }
        request_key = _digest(json.dumps(identity, ensure_ascii=False, sort_keys=True))
        if not payload.get("force"):
            with self.connect() as connection:
                cached = connection.execute(
                    "SELECT * FROM leap_interpretation_cache WHERE request_key=? AND user_id=? ORDER BY updated_at DESC LIMIT 1",
                    (request_key, user_id),
                ).fetchone()
            if cached:
                result = dict(cached)
                result["coverage"] = json.loads(result.pop("coverage_json") or "{}")
                result["structured"] = json.loads(result.get("result_json") or "{}")
                result["cache_hit"] = True
                return result

        scope_guidance = {
            "word": "解释这个词在本句中的具体作用；必要时说明搭配、隐喻或专业语境，不要只给译词。",
            "sentence": "解释这句话的意思、关键表达、指代和必要的隐含关系，不扩成整段分析。",
            "paragraph": "解释段落主旨、论证或叙述逻辑、关键概念及必要上下文关系。",
            "selection": "只围绕所选文字解释，允许多句或跨段，但不要扩大到整份文档。",
            "chapter": "解释所覆盖章节内容的主题、结构、关键论点或事件及内部联系。",
        }[payload["scope"]]
        system = (
            "你是冰焰跃迁域的证据型阅读助手。只依据提供的原文和辅助上下文进行内容解读，"
            "不得把译文当解读，不得编造作者意图、历史背景或文外事实。只返回严格JSON对象，字段为："
            "concise_meaning（简洁含义）、explanation（解释）、evidence（原文线索字符串数组）、"
            "uncertainty（材料不足或不确定性，没有则空字符串）、scope（任务范围）。不要使用Markdown代码块。"
        )
        prompt = (
            f"任务范围：{payload['scope']}\n范围要求：{scope_guidance}\n"
            f"覆盖：{coverage['label']}\n\n需要解读的原文：\n{source}\n\n"
            + (f"仅供辅助理解的上下文（不得擅自纳入处理范围）：\n{context}" if context else "没有额外上下文。")
        )
        requested_at = _now()
        try:
            output = selected_provider.generate(
                user_id, system, prompt, 900 if payload["scope"] == "chapter" else 600,
            )
            structured = self._structured_result(output.text, payload["scope"])
        except InterpretationProviderError as exc:
            self._record_attempt(
                user_id, material["id"], provider_id, selected_provider.requested_model,
                "", requested_at, "failed", exc.code,
            )
            if provider_id == "openrouter" and exc.code == "INTERPRETATION_PARSE_ERROR":
                raise InterpretationProviderError("OPENROUTER_PARSE_ERROR", 502, exc.public_message) from exc
            raise
        except BaseException as exc:
            self._record_attempt(
                user_id, material["id"], provider_id, selected_provider.requested_model,
                "", requested_at, "failed", type(exc).__name__,
            )
            raise
        self._record_attempt(
            user_id, material["id"], output.provider, output.requested_model,
            output.actual_model, output.requested_at, "success",
        )
        text = self._display_text(structured)
        actual_identity = {**identity, "provider_model": output.actual_model}
        cache_key = _digest(json.dumps(actual_identity, ensure_ascii=False, sort_keys=True))
        now, row_id = _now(), str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO leap_interpretation_cache
                   (id,cache_key,request_key,user_id,document_id,document_version,document_hash,action,scope,
                    paragraph_start,paragraph_end,selection_start,selection_end,source_text_hash,
                    context_text_hash,provider,requested_model,provider_model,prompt_version,result_text,result_json,
                    requested_at,provider_status,coverage_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(cache_key) DO UPDATE SET result_text=excluded.result_text,
                    coverage_json=excluded.coverage_json,updated_at=excluded.updated_at""",
                (row_id, cache_key, request_key, user_id, material["id"], material["version"], material["content_hash"],
                 "interpret", payload["scope"], rows[0]["position"], rows[-1]["position"],
                 payload.get("selection_start"), payload.get("selection_end"), identity["source_text_hash"],
                 identity["context_text_hash"], output.provider, output.requested_model, output.actual_model, PROMPT_VERSION,
                 text, json.dumps(structured, ensure_ascii=False), output.requested_at, "success",
                 json.dumps(coverage, ensure_ascii=False), now, now),
            )
            saved = connection.execute(
                "SELECT * FROM leap_interpretation_cache WHERE cache_key=? AND user_id=?", (cache_key, user_id)
            ).fetchone()
        result = dict(saved)
        result["coverage"] = json.loads(result.pop("coverage_json") or "{}")
        result["structured"] = json.loads(result.get("result_json") or "{}")
        result["cache_hit"] = False
        return result

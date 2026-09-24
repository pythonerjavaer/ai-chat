"""Translation providers and durable, version-aware Leap translation cache."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Callable

import httpx


MAX_TRANSLATION_CHARACTERS = 20_000
DEFAULT_AZURE_MONTHLY_LIMIT = 2_000_000
DISCLAIMER = "AI/机器翻译，仅供辅助阅读，不是官方或权威译本。"
SENTENCE_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "fig", "no", "dept", "inc", "ltd", "e.g", "i.e",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sentence_ranges(value: str) -> list[tuple[int, int, str]]:
    """Small deterministic sentence splitter shared with the browser contract."""
    source = str(value or "")
    ranges: list[tuple[int, int, str]] = []
    start = 0

    def period_ends(index: int) -> bool:
        if source[index] != ".":
            return True
        before = source[index - 1] if index else ""
        after = source[index + 1] if index + 1 < len(source) else ""
        if before.isdigit() and after.isdigit():
            return False
        if before.isupper() and re.match(r"^[A-Z]\.", source[index + 1 :]):
            return False
        match = re.search(r"([A-Za-z](?:[A-Za-z.]*)?)\.$", source[: index + 1])
        token = (match.group(1) if match else "").lower()
        if token in SENTENCE_ABBREVIATIONS or re.fullmatch(r"(?:[a-z]\.){2,}", token + ".", re.I):
            return False
        return True

    def append(end: int) -> None:
        nonlocal start
        left, right = start, end
        while left < right and source[left].isspace():
            left += 1
        while right > left and source[right - 1].isspace():
            right -= 1
        if right > left:
            ranges.append((left, right, source[left:right]))
        start = end

    index = 0
    while index < len(source):
        if source[index] in ".!?。！？" and period_ends(index):
            end = index + 1
            while end < len(source) and source[end] in "\"'”’）)]}":
                end += 1
            append(end)
            index = end
        else:
            index += 1
    if start < len(source):
        append(len(source))
    return ranges


def _mark_word_in_context(word: str, context: str) -> str:
    """Mark one exact word so a general translator can return its contextual sense."""
    import re

    value = str(word or "").strip()
    source = str(context or "").strip()
    if not value or not source:
        return source
    return re.sub(rf"\b{re.escape(value)}\b", lambda match: f"⟦{match.group(0)}⟧", source, count=1, flags=re.IGNORECASE)


def _contextual_meaning(marked_translation: str) -> str:
    import re

    match = re.search(r"⟦\s*([^⟦⟧]+?)\s*⟧", str(marked_translation or ""))
    return match.group(1).strip() if match else ""


def init_translation_schema(connect: Callable[[], Any]) -> None:
    with connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS leap_translation_cache (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                document_id TEXT NOT NULL,
                document_version INTEGER NOT NULL,
                document_hash TEXT NOT NULL,
                segment_id TEXT NOT NULL,
                paragraph_position INTEGER NOT NULL,
                source_language TEXT NOT NULL,
                target_language TEXT NOT NULL,
                provider TEXT NOT NULL,
                provider_model TEXT NOT NULL,
                translation_mode TEXT NOT NULL,
                source_text_hash TEXT NOT NULL,
                translated_text TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                character_count INTEGER NOT NULL DEFAULT 0,
                translated_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(document_id) REFERENCES leap_materials(id) ON DELETE CASCADE,
                UNIQUE(user_id,document_id,document_version,document_hash,segment_id,
                       source_language,target_language,provider,provider_model,
                       translation_mode,source_text_hash)
            );
            CREATE TABLE IF NOT EXISTS leap_translation_usage (
                user_id INTEGER NOT NULL,
                month TEXT NOT NULL,
                provider TEXT NOT NULL,
                request_count INTEGER NOT NULL DEFAULT 0,
                translated_characters INTEGER NOT NULL DEFAULT 0,
                cache_hit_characters INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(user_id,month,provider),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_leap_translation_window
                ON leap_translation_cache(user_id,document_id,document_version,paragraph_position);
            """
        )


class TranslationProvider(ABC):
    """One provider contract; browser-local and cloud implementations share its result shape."""

    provider_id: str
    model_version: str

    @abstractmethod
    def translate(self, text: str, source_language: str, target_language: str) -> dict[str, Any]:
        raise NotImplementedError

    def lookup_word(
        self, word: str, context: str, source_language: str, target_language: str,
    ) -> dict[str, Any]:
        marked = _mark_word_in_context(word, context or word)
        translated = self.translate(marked, source_language, target_language)
        return {
            **translated,
            "dictionary": [],
            "context_translation": translated["translated_text"],
            "contextual_meaning": _contextual_meaning(translated["translated_text"]) or "语境不足，无法确定唯一含义",
            "context_explanation": "",
        }


class AzureTranslatorProvider(TranslationProvider):
    provider_id = "azure_translator"
    model_version = "translator-text-v3"

    def __init__(self, key: str = "", region: str = "", endpoint: str = "", client: Any = None):
        self.key = key.strip()
        self.region = region.strip()
        self.endpoint = (endpoint.strip() or "https://api.cognitive.microsofttranslator.com").rstrip("/")
        self.client = client

    @classmethod
    def from_environment(cls) -> "AzureTranslatorProvider":
        return cls(
            os.getenv("AZURE_TRANSLATOR_KEY", ""),
            os.getenv("AZURE_TRANSLATOR_REGION", ""),
            os.getenv("AZURE_TRANSLATOR_ENDPOINT", ""),
        )

    @property
    def configured(self) -> bool:
        return bool(self.key and self.region)

    def _post(self, path: str, params: dict[str, str], text: str) -> Any:
        if not self.configured:
            raise ValueError("Azure Translator 尚未配置；本地浏览器翻译仍可使用。")
        headers = {
            "Ocp-Apim-Subscription-Key": self.key,
            "Ocp-Apim-Subscription-Region": self.region,
            "Content-Type": "application/json",
        }
        if self.client is not None:
            response = self.client.post(self.endpoint + path, params=params, headers=headers, json=[{"Text": text}])
        else:
            with httpx.Client(timeout=20.0, follow_redirects=False) as client:
                response = client.post(self.endpoint + path, params=params, headers=headers, json=[{"Text": text}])
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list) or not payload:
            raise ValueError("Azure Translator 未返回有效结果。")
        return payload[0]

    def translate(self, text: str, source_language: str, target_language: str) -> dict[str, Any]:
        result = self._post(
            "/translate", {"api-version": "3.0", "from": source_language, "to": target_language}, text,
        )
        translations = result.get("translations") or []
        translated = str(translations[0].get("text") or "").strip() if translations else ""
        if not translated:
            raise ValueError("Azure Translator 未返回译文。")
        return {
            "translated_text": translated,
            "provider": self.provider_id,
            "provider_model": self.model_version,
            "translated_at": _now(),
            "disclaimer": DISCLAIMER,
        }

    def lookup_word(
        self, word: str, context: str, source_language: str, target_language: str,
    ) -> dict[str, Any]:
        marked_context = _mark_word_in_context(word, context or word)
        contextual = self.translate(marked_context, source_language, target_language)
        result = self._post(
            "/dictionary/lookup",
            {"api-version": "3.0", "from": source_language, "to": target_language},
            word,
        )
        dictionary: list[dict[str, Any]] = []
        for item in (result.get("translations") or [])[:8]:
            dictionary.append({
                "display_target": str(item.get("displayTarget") or ""),
                "part_of_speech": str(item.get("posTag") or ""),
                "confidence": item.get("confidence"),
                "back_translations": [str(x.get("displayText") or "") for x in (item.get("backTranslations") or [])[:4]],
            })
        contextual_meaning = _contextual_meaning(contextual["translated_text"])
        return {
            **contextual,
            "dictionary": dictionary,
            "context_translation": contextual["translated_text"],
            "contextual_meaning": contextual_meaning or "语境不足，无法确定唯一含义",
            "context_explanation": "",
        }


class TranslationService:
    def __init__(
        self, connect: Callable[[], Any], azure: AzureTranslatorProvider | None = None,
        azure_monthly_limit: int | None = None,
    ):
        self.connect = connect
        self.azure = azure or AzureTranslatorProvider.from_environment()
        configured_limit = os.getenv("AZURE_TRANSLATOR_MONTHLY_CHAR_LIMIT", "")
        self.azure_monthly_limit = azure_monthly_limit or int(configured_limit or DEFAULT_AZURE_MONTHLY_LIMIT)

    def providers(self) -> dict[str, Any]:
        return {
            "providers": [
                {
                    "id": "browser_local",
                    "name": "Chrome 内置本地翻译",
                    "configured": True,
                    "runtime": "browser_device",
                    "model": "chrome-built-in-translator",
                    "model_version": "browser-managed",
                    "languages": [{"source": "en", "target": "zh"}],
                    "first_load": "浏览器可能首次下载语言包；具体大小和内部模型版本由Chrome管理且不公开。",
                    "quality": "适合辅助理解；文学、哲学与历史原典可能丢失语气、术语和歧义。",
                    "external_request": False,
                },
                {
                    "id": self.azure.provider_id,
                    "name": "Microsoft Azure Translator",
                    "configured": self.azure.configured,
                    "runtime": "frostfire_backend",
                    "model": self.azure.model_version,
                    "model_version": self.azure.model_version,
                    "languages": "Azure Translator supported language pairs",
                    "first_load": "无需浏览器模型下载；文本会发送到Microsoft Azure。",
                    "quality": "通用机器翻译；不是文学、哲学或历史作品的权威译本。",
                    "external_request": True,
                    "monthly_character_limit": self.azure_monthly_limit,
                },
            ],
            "disclaimer": DISCLAIMER,
        }

    def _material_and_paragraph(self, user_id: int, document_id: str, position: int) -> tuple[Any, Any]:
        with self.connect() as connection:
            material = connection.execute(
                "SELECT id,user_id,version,content_hash FROM leap_materials WHERE id=? AND user_id=?",
                (document_id, user_id),
            ).fetchone()
            if not material:
                raise KeyError("材料不存在。")
            paragraph = connection.execute(
                """SELECT position,content,stable_anchor FROM leap_paragraphs
                   WHERE material_id=? AND material_version=? AND position=?""",
                (document_id, material["version"], position),
            ).fetchone()
        if not paragraph:
            raise ValueError("翻译段落不存在或材料版本已经变化。")
        return material, paragraph

    def _document_hash(self, material: Any) -> str:
        existing = str(material["content_hash"] or "")
        if existing:
            return existing
        digest = hashlib.sha256()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT content FROM leap_paragraphs WHERE material_id=? AND material_version=? ORDER BY position",
                (material["id"], material["version"]),
            )
            for row in rows:
                digest.update(str(row["content"]).encode("utf-8"))
                digest.update(b"\x00")
            value = digest.hexdigest()
            connection.execute(
                "UPDATE leap_materials SET content_hash=? WHERE id=? AND user_id=? AND version=?",
                (value, material["id"], material["user_id"], material["version"]),
            )
        return value

    @staticmethod
    def _validate_source(source_text: str, paragraph_text: str, mode: str) -> str:
        value = str(source_text or "").strip()
        if not value:
            raise ValueError("没有可翻译的原文。")
        if len(value) > MAX_TRANSLATION_CHARACTERS:
            raise ValueError(f"单次翻译不能超过 {MAX_TRANSLATION_CHARACTERS} 个字符。")
        if mode in {"paragraph", "chapter_window"}:
            if value != paragraph_text.strip():
                raise ValueError("翻译内容与当前版本原文段落不一致。")
        elif value not in paragraph_text:
            raise ValueError("所选翻译内容无法定位回当前版本原文。")
        return value

    def _cache_key(self, user_id: int, payload: dict[str, Any]) -> tuple[dict[str, Any], Any]:
        material, paragraph = self._material_and_paragraph(user_id, payload["document_id"], payload["paragraph_position"])
        mode = payload["translation_mode"]
        paragraph_end = int(payload.get("paragraph_end") if payload.get("paragraph_end") is not None else paragraph["position"])
        if mode == "selection" and paragraph_end != int(paragraph["position"]):
            if paragraph_end < int(paragraph["position"]) or paragraph_end - int(paragraph["position"]) > 299:
                raise ValueError("所选翻译范围无效或过大。")
            with self.connect() as connection:
                rows = connection.execute(
                    """SELECT position,content FROM leap_paragraphs WHERE material_id=? AND material_version=?
                       AND position>=? AND position<=? ORDER BY position""",
                    (material["id"], material["version"], paragraph["position"], paragraph_end),
                ).fetchall()
            if len(rows) != paragraph_end - int(paragraph["position"]) + 1:
                raise ValueError("所选翻译内容无法定位到当前版本原文。")
            start, end = payload.get("selection_start"), payload.get("selection_end")
            if start is None or end is None or start > len(rows[0]["content"]) or end > len(rows[-1]["content"]):
                raise ValueError("所选翻译位置无效。")
            expected = "\n\n".join([rows[0]["content"][start:], *[row["content"] for row in rows[1:-1]], rows[-1]["content"][:end]]).strip()
            value = str(payload["source_text"] or "").strip()
            if value != expected:
                raise ValueError("所选翻译内容与当前版本原文位置不一致。")
        else:
            value = self._validate_source(payload["source_text"], paragraph["content"], mode)
        sentence_index = payload.get("sentence_index")
        base_segment = paragraph["stable_anchor"] or f"p-{paragraph['position']}"
        if mode == "sentence":
            if sentence_index is None:
                raise ValueError("句子翻译缺少sentence_index。")
            sentences = _sentence_ranges(paragraph["content"])
            if sentence_index >= len(sentences) or value != sentences[sentence_index][2]:
                raise ValueError("句子翻译内容与当前段落的句子边界不一致。")
            segment_id = f"{base_segment}:sentence:{sentence_index}"
        elif mode == "word" and payload.get("selection_start") is not None and payload.get("selection_end") is not None:
            segment_id = f"{base_segment}:word:{payload['selection_start']}-{payload['selection_end']}"
        elif mode == "selection":
            segment_id = f"{base_segment}:selection:{paragraph_end}:{payload.get('selection_start')}-{payload.get('selection_end')}"
        else:
            segment_id = base_segment
        data = {
            **payload,
            "source_text": value,
            "document_version": int(material["version"]),
            "document_hash": self._document_hash(material),
            "segment_id": segment_id,
            "source_text_hash": _digest(value),
        }
        return data, paragraph

    def _record_usage(self, user_id: int, provider: str, *, requests: int = 0, translated: int = 0, saved: int = 0) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO leap_translation_usage
                   (user_id,month,provider,request_count,translated_characters,cache_hit_characters,updated_at)
                   VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,month,provider) DO UPDATE SET
                   request_count=leap_translation_usage.request_count+excluded.request_count,
                   translated_characters=leap_translation_usage.translated_characters+excluded.translated_characters,
                   cache_hit_characters=leap_translation_usage.cache_hit_characters+excluded.cache_hit_characters,
                   updated_at=excluded.updated_at""",
                (user_id, _month(), provider, requests, translated, saved, _now()),
            )

    def _find_cached(self, user_id: int, data: dict[str, Any]) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT * FROM leap_translation_cache WHERE user_id=? AND document_id=?
                   AND document_version=? AND document_hash=? AND segment_id=?
                   AND source_language=? AND target_language=? AND provider=? AND provider_model=?
                   AND translation_mode=? AND source_text_hash=?""",
                (user_id, data["document_id"], data["document_version"], data["document_hash"], data["segment_id"],
                 data["source_language"], data["target_language"], data["provider"], data["provider_model"],
                 data["translation_mode"], data["source_text_hash"]),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
        result["cache_hit"] = True
        self._record_usage(user_id, data["provider"], saved=len(data["source_text"]))
        return result

    def save(
        self, user_id: int, payload: dict[str, Any], result: dict[str, Any], *,
        count_request: bool, charged_characters: int | None = None,
    ) -> dict[str, Any]:
        data, _ = self._cache_key(user_id, payload)
        if not payload.get("force"):
            cached = self._find_cached(user_id, data)
            if cached:
                return cached
        translated = str(result.get("translated_text") or "").strip()
        if not translated:
            raise ValueError("译文不能为空。")
        provider = data["provider"]
        if provider not in {"browser_local", self.azure.provider_id}:
            raise ValueError("不支持的翻译Provider。")
        now, item_id = str(result.get("translated_at") or _now()), str(uuid.uuid4())
        metadata = dict(result.get("metadata") or {})
        metadata["disclaimer"] = DISCLAIMER
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO leap_translation_cache
                   (id,user_id,document_id,document_version,document_hash,segment_id,paragraph_position,
                    source_language,target_language,provider,provider_model,translation_mode,source_text_hash,
                    translated_text,metadata_json,character_count,translated_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(user_id,document_id,document_version,document_hash,segment_id,source_language,
                               target_language,provider,provider_model,translation_mode,source_text_hash)
                   DO UPDATE SET translated_text=excluded.translated_text,metadata_json=excluded.metadata_json,
                                 character_count=excluded.character_count,translated_at=excluded.translated_at,
                                 updated_at=excluded.updated_at""",
                (item_id, user_id, data["document_id"], data["document_version"], data["document_hash"],
                 data["segment_id"], data["paragraph_position"], data["source_language"], data["target_language"],
                 provider, data["provider_model"], data["translation_mode"], data["source_text_hash"],
                 translated, json.dumps(metadata, ensure_ascii=False), len(data["source_text"]), now, now),
            )
        self._record_usage(
            user_id, provider, requests=int(count_request),
            translated=charged_characters if charged_characters is not None else len(data["source_text"]),
        )
        data.update(translated_text=translated, translated_at=now, metadata=metadata, cache_hit=False)
        return data

    def save_browser_local(self, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("provider") != "browser_local":
            raise ValueError("浏览器缓存接口只接受browser_local结果。")
        result = {
            "translated_text": payload["translated_text"],
            "translated_at": payload.get("translated_at") or _now(),
            "metadata": {"runtime": "browser_device", "dictionary": payload.get("dictionary") or [],
                         "context_translation": payload.get("context_translation") or "",
                         "contextual_meaning": payload.get("contextual_meaning") or "",
                         "context_explanation": payload.get("context_explanation") or ""},
        }
        charged = len(str(payload.get("source_text") or ""))
        if payload.get("translation_mode") == "word":
            charged += len(str(payload.get("context_text") or ""))
        return self.save(user_id, payload, result, count_request=True, charged_characters=charged)

    def translate_azure(self, user_id: int, payload: dict[str, Any], *, lookup: bool = False) -> dict[str, Any]:
        if payload.get("provider") != self.azure.provider_id:
            raise ValueError("云翻译接口只接受azure_translator。")
        data, _ = self._cache_key(user_id, payload)
        if not payload.get("force"):
            cached = self._find_cached(user_id, data)
            if cached:
                return cached
        stats = self.stats(user_id)
        used = next((x["translated_characters"] for x in stats["providers"] if x["provider"] == self.azure.provider_id), 0)
        if used + len(data["source_text"]) > self.azure_monthly_limit:
            raise ValueError("Azure本月字符上限已达到；已缓存译文仍可用，请切换本地模式。")
        if lookup:
            result = self.azure.lookup_word(
                data["source_text"], str(payload.get("context_text") or data["source_text"]),
                data["source_language"], data["target_language"],
            )
        else:
            result = self.azure.translate(data["source_text"], data["source_language"], data["target_language"])
        result["metadata"] = {
            "dictionary": result.get("dictionary") or [],
            "context_translation": result.get("context_translation"),
            "contextual_meaning": result.get("contextual_meaning") or "",
            "context_explanation": result.get("context_explanation") or "",
        }
        charged = len(data["source_text"])
        if lookup:
            charged += len(str(payload.get("context_text") or data["source_text"]))
        return self.save(user_id, data, result, count_request=True, charged_characters=charged)

    def window_cache(
        self, user_id: int, document_id: str, offset: int, limit: int, provider: str, provider_model: str,
        target_language: str = "zh-Hans",
    ) -> dict[str, Any]:
        with self.connect() as connection:
            material = connection.execute(
                "SELECT id,user_id,version,content_hash FROM leap_materials WHERE id=? AND user_id=?",
                (document_id, user_id),
            ).fetchone()
            if not material:
                raise KeyError("材料不存在。")
            document_hash = self._document_hash(material)
            rows = connection.execute(
                """SELECT segment_id,paragraph_position,source_text_hash,translated_text,provider,provider_model,
                          translation_mode,translated_at,metadata_json,character_count FROM leap_translation_cache
                   WHERE user_id=? AND document_id=? AND document_version=? AND document_hash=?
                     AND target_language=? AND provider=? AND provider_model=?
                     AND paragraph_position>=? AND paragraph_position<?
                   ORDER BY paragraph_position LIMIT ?""",
                (user_id, document_id, material["version"], document_hash, target_language, provider, provider_model,
                 offset, offset + limit, limit),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            items.append(item)
        if items:
            self._record_usage(user_id, provider, saved=sum(int(x["character_count"] or 0) for x in items))
        return {"items": items, "document_version": material["version"], "document_hash": document_hash}

    def stats(self, user_id: int) -> dict[str, Any]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT provider,request_count,translated_characters,cache_hit_characters
                   FROM leap_translation_usage WHERE user_id=? AND month=? ORDER BY provider""",
                (user_id, _month()),
            ).fetchall()
        providers = [dict(row) for row in rows]
        return {
            "month": _month(),
            "providers": providers,
            "total_translated_characters": sum(x["translated_characters"] for x in providers),
            "total_cache_hit_characters": sum(x["cache_hit_characters"] for x in providers),
            "azure_monthly_character_limit": self.azure_monthly_limit,
        }

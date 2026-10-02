from __future__ import annotations

import sqlite3
import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.product_domains import init_leap_schema, init_pulse_schema
from backend.tests.test_postgres_application import persistent_app, register  # noqa: F401
from backend.product_domains.leap import (
    ClashWrite,
    ExcerptCreate,
    LeapRepository,
    MaterialCreate,
    NoteWrite,
    ProgressUpdate,
    WormholeWrite,
    DemoAction,
    InterpretationWrite,
    create_leap_router,
)
from backend.product_domains.public_library import PublicLibraryService, seed_catalog
from backend.product_domains.translation import AzureTranslatorProvider, TranslationService
from backend.product_domains.interpretation import InterpretationService, classify_provider_failure
from backend.product_domains.knowledge import LeapKnowledgeService, build_chunks
from backend.product_domains.interpretation_providers import (
    InterpretationProviderResult,
    InterpretationProviderError,
    FreeInterpretationProvider,
    GeminiInterpretationProvider,
    OpenRouterInterpretationProvider,
)
from backend.product_domains.pulse import (
    AssetStatusWrite,
    AssetWrite,
    CustomerWrite,
    InspectionWrite,
    OrderLineWrite,
    OrderWrite,
    PaymentWrite,
    PulseRepository,
    SKUWrite,
    ExpenseWrite,
    OrderStatusWrite,
    PulseDemoAction,
)
from backend.product_domains.pulse_analytics import (aggregate_order_context, bootstrap_interval, deposit_coverage_scenarios,
    compare_revenue_models, distribution, kde_density, linear_revenue_forecast, multiple_regression_diagnostics,
    pca_projection, risk_feature_selection, rolling_time_validation, simulated_revenue_demo)


@pytest.fixture()
def product_store(tmp_path):
    path = tmp_path / "domains.db"

    def connect():
        connection = sqlite3.connect(path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    with connect() as connection:
        connection.executescript(
            "CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT);"
            "INSERT INTO users VALUES(1,'one'); INSERT INTO users VALUES(2,'two');"
        )
    init_leap_schema(connect)
    init_pulse_schema(connect)
    return connect


def test_leap_evidence_wormhole_clash_progress_and_isolation(product_store):
    repo = LeapRepository(product_store)
    left = repo.create_material(1, MaterialCreate(title="历史", author="A", text="第一段证据。\n\n第二段。"))
    right = repo.create_material(1, MaterialCreate(title="哲学", author="B", text="另一条证据。\n\n结论。"))
    repo.update_progress(1, left["id"], ProgressUpdate(paragraph_position=1))
    first = repo.create_excerpt(1, ExcerptCreate(
        material_id=left["id"], material_version=1, paragraph_position=0, quote="第一段证据。",
    ))
    second = repo.create_excerpt(1, ExcerptCreate(
        material_id=right["id"], material_version=1, paragraph_position=0, quote="另一条证据。",
    ))
    note = repo.write_note(1, NoteWrite(material_id=left["id"], excerpt_id=first["id"], content="我的解释"))
    wormhole = repo.write_wormhole(1, WormholeWrite(
        left_excerpt_id=first["id"], right_excerpt_id=second["id"],
        relation_type="对立", reflection="两条证据的条件不同。",
    ))
    clash = repo.write_clash(1, ClashWrite(
        title="双观点", viewpoint_a="观点A", viewpoint_b="观点B",
        evidence_a_excerpt_id=first["id"], evidence_b_excerpt_id=second["id"],
        disagreement="适用范围", judgment="保留条件判断",
    ))

    assert repo.get_material(1, left["id"])["progress_percent"] == 100
    assert repo.get_excerpt(1, first["id"])["quote"] == "第一段证据。"
    assert repo.list_notes(1, "")[0]["id"] == note["id"]
    assert repo.get_wormhole(1, wormhole["id"])["right_quote"] == "另一条证据。"
    assert repo.list_clashes(1)[0]["id"] == clash["id"]
    with pytest.raises(KeyError):
        repo.get_material(2, left["id"])
    assert repo.list_excerpts(2) == []


def test_leap_knowledge_hybrid_search_preserves_hierarchy_and_isolation(product_store):
    repo = LeapRepository(product_store)
    first = repo.create_material(1, MaterialCreate(
        title="固定收益笔记",
        text="# 债券基础\n\n债券价格与市场利率通常反向变动。\n\n## 久期\n\n久期衡量债券价格对利率变化的敏感度。",
    ))
    repo.create_material(2, MaterialCreate(title="私人材料", text="久期只属于第二位用户。"))
    service = LeapKnowledgeService(product_store)
    indexed = service.index_material(1, first["id"])
    results = service.search(1, "久期和利率风险", limit=3)

    assert indexed["chunk_count"] >= 2
    assert results[0]["material_id"] == first["id"]
    assert "久期" in results[0]["content"]
    assert results[0]["heading_path"] == ["债券基础", "久期"]
    assert results[0]["retrieval_strategy"] == "vector_tfidf_hybrid"
    assert results[0]["lexical_score"] > 0
    assert all(item["material_title"] != "私人材料" for item in results)


def test_leap_pgvector_outage_falls_back_to_sqlite_without_losing_evidence(product_store):
    material = LeapRepository(product_store).create_material(1, MaterialCreate(
        title="回退检索", text="# 业务连续性\n\nPostgreSQL 不可用时，SQLite 仍能提供带原文定位的检索证据。",
    ))

    def unavailable_vector_store():
        raise RuntimeError("integration unavailable")

    service = LeapKnowledgeService(product_store, vector_connect=unavailable_vector_store)
    indexed = service.index_material(1, material["id"])
    results = service.search(1, "SQLite PostgreSQL 原文检索")

    assert indexed["vector_store"]["status"] == "unavailable"
    assert results and results[0]["material_id"] == material["id"]
    assert results[0]["heading_path"] == ["业务连续性"]


def test_leap_knowledge_answers_with_citations_and_extractively_degrades(product_store):
    repo = LeapRepository(product_store)
    material = repo.create_material(1, MaterialCreate(
        title="信用风险", text="信用利差补偿投资者承担发行人违约和流动性风险。",
    ))
    service = LeapKnowledgeService(product_store)
    service.index_material(1, material["id"])
    result = service.answer(1, "信用利差补偿什么风险？")

    assert result["mode"] == "extractive_rag"
    assert "信用利差" in result["answer"]
    assert result["citations"][0]["material_id"] == material["id"]


def test_leap_knowledge_uses_configured_embedding_and_generation_providers(product_store):
    repo = LeapRepository(product_store)
    material = repo.create_material(1, MaterialCreate(
        title="生命周期与久期", text="# 利率风险\n\n久期衡量债券价格对利率变化的敏感度。",
    ))
    embedding_calls = []
    generation_calls = []

    def embedder(user_id, texts):
        embedding_calls.append((user_id, tuple(texts)))
        return [[1.0, 0.0, 0.0] for _ in texts]

    def generator(user_id, system, prompt, max_tokens):
        generation_calls.append((user_id, system, prompt, max_tokens))
        return {"text": "久期衡量债券价格对利率变化的敏感度。[1]", "usage": {"total_tokens": 9}}

    service = LeapKnowledgeService(
        product_store, generator=generator, embedder=embedder,
        embedding_model="test-semantic-provider",
    )
    result = service.answer(1, "久期衡量什么？", generate=True)

    assert result["mode"] == "llm_rag"
    assert result["usage"]["total_tokens"] == 9
    assert result["citations"][0]["material_id"] == material["id"]
    assert len(embedding_calls) == 2  # indexed chunk plus query vector
    assert len(generation_calls) == 1
    assert "只能依据提供的证据" in generation_calls[0][1]
    assert "久期衡量什么？" in generation_calls[0][2]


def test_leap_knowledge_scoped_query_does_not_index_unselected_books(product_store):
    repo = LeapRepository(product_store)
    selected = repo.create_material(1, MaterialCreate(title="风险", text="流动性风险需要现金缓冲。"))
    ignored = repo.create_material(1, MaterialCreate(title="长书", text="无关文本。" * 1000))
    calls = []

    def embedder(user_id, texts):
        calls.extend(texts)
        return [[1.0, 0.0, 0.0] for _ in texts]

    service = LeapKnowledgeService(product_store, embedder=embedder, embedding_model="test-semantic")
    result = service.answer(1, "现金缓冲是什么？", material_ids=[selected["id"]], generate=False)

    assert result["citations"][0]["material_id"] == selected["id"]
    assert len(calls) == 2  # selected material + query, not the long book
    with product_store() as connection:
        count = connection.execute(
            "SELECT COUNT(*) AS n FROM leap_knowledge_chunks WHERE material_id=?", (ignored["id"],)
        ).fetchone()["n"]
    assert count == 0


def test_leap_knowledge_embedding_quota_falls_back_without_claiming_llm(product_store):
    from fastapi import HTTPException

    repo = LeapRepository(product_store)
    material = repo.create_material(1, MaterialCreate(title="押金", text="押金用于覆盖约定的损失风险。"))

    def unavailable_embedder(user_id, texts):
        raise HTTPException(status_code=502, detail={"code": "EMBEDDING_PROVIDER_ERROR"})

    service = LeapKnowledgeService(product_store, embedder=unavailable_embedder, embedding_model="test-semantic")
    result = service.answer(1, "押金覆盖什么风险？", material_ids=[material["id"]], generate=True)

    assert result["mode"] == "extractive_rag"
    assert result["retrieval_mode"] == "local_vector_fallback"
    assert "不是模型生成" in result["generation_status"]
    assert result["citations"][0]["material_id"] == material["id"]


def test_leap_long_paragraph_chunking_is_bounded():
    chunks = build_chunks([{
        "position": 0, "content": "风险管理。" * 500,
        "chapter_title": "长段落", "stable_anchor": "chapter-1-p1",
    }])
    assert len(chunks) > 1
    assert all(len(item["content"]) <= 900 for item in chunks)
    assert all(item["heading_path"] == ["长段落"] for item in chunks)


def _sample_epub() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as book:
        book.writestr("META-INF/container.xml", """<?xml version='1.0'?><container xmlns='urn:oasis:names:tc:opendocument:xmlns:container'><rootfiles><rootfile full-path='EPUB/package.opf'/></rootfiles></container>""")
        book.writestr("EPUB/package.opf", """<?xml version='1.0'?><package xmlns='http://www.idpf.org/2007/opf'><manifest><item id='cover' href='text/cover.xhtml' media-type='application/xhtml+xml'/><item id='c1' href='text/chapter.xhtml' media-type='application/xhtml+xml'/></manifest><spine><itemref idref='cover'/><itemref idref='c1'/></spine></package>""")
        book.writestr("EPUB/text/cover.xhtml", """<html xmlns='http://www.w3.org/1999/xhtml'><body><img src='cover.jpg' alt='Cover'/></body></html>""")
        book.writestr("EPUB/text/chapter.xhtml", """<html xmlns='http://www.w3.org/1999/xhtml'><body><h1>Chapter One</h1><p>First stable paragraph.</p><p>Second stable paragraph.</p></body></html>""")
    return buffer.getvalue()


class _LibraryClient:
    def gutenberg_detail(self, source_id):
        return {"provider": "gutenberg", "source_item_id": source_id, "title": "Pride and Prejudice", "author": "Jane Austen", "language": "en", "edition": "Reviewed edition", "translator": "", "source_name": "Project Gutenberg", "source_url": "https://www.gutenberg.org/ebooks/1342", "download_url": "https://www.gutenberg.org/ebooks/1342.epub.noimages", "content_type": "application/epub+zip", "format": "EPUB", "rights_status": "auto_import", "licensing_note": "Public domain test fixture"}

    def download(self, item):
        return _sample_epub(), {"content-type": "application/epub+zip", "etag": '"edition-1"'}, item["download_url"]


def test_public_library_import_is_private_versioned_and_deduplicated(product_store):
    service = PublicLibraryService(product_store, _LibraryClient())
    run = service.create_import(1, "gutenberg", "1342")
    service.run(1, run["id"])
    finished = service.import_status(1, run["id"])
    assert finished["status"] == "success"
    assert len(finished["source_hash"]) == 64
    material = LeapRepository(product_store).get_material(1, finished["material_id"])
    assert material["library_source"]["source_name"] == "Project Gutenberg"
    assert material["chapters"][0]["title"] == "Chapter One"
    paragraphs = LeapRepository(product_store).paragraphs(1, finished["material_id"], 0, 10)["paragraphs"]
    assert paragraphs[0]["stable_anchor"].startswith("s0001-")
    duplicate = service.create_import(1, "gutenberg", "1342")
    assert duplicate == {"id": run["id"], "status": "duplicate", "material_id": finished["material_id"], "progress": 100}
    with product_store() as connection:
        stored = connection.execute("SELECT user_id,original_size,stored_size,body FROM leap_library_objects").fetchone()
    assert stored["user_id"] == 1 and stored["original_size"] > 0 and stored["stored_size"] > 0 and stored["body"]


def test_seed_catalog_blocks_ctext_fulltext_and_exposes_versions(product_store):
    rows = seed_catalog()
    titles = {row["title"] for row in rows}
    assert {"The Republic", "The Wealth of Nations", "Pride and Prejudice", "论语", "孟子", "道德经", "庄子"} <= titles
    assert all(row["rights_status"] == "manual_review" for row in rows if row["provider"] == "ctext")
    service = PublicLibraryService(product_store, _LibraryClient())
    with pytest.raises(ValueError, match="人工确认"):
        service.create_import(1, "ctext", "analects")


def _translation_payload(material, paragraph, **overrides):
    payload = {
        "document_id": material["id"], "paragraph_position": paragraph["position"],
        "segment_id": paragraph.get("stable_anchor") or "p-0", "source_language": "en", "target_language": "zh-Hans",
        "provider": "browser_local", "provider_model": "chrome-built-in-translator",
        "translation_mode": "paragraph", "source_text": paragraph["content"], "context_text": paragraph["content"],
        "translated_text": "经审视的人生。", "translated_at": "2026-09-25T00:00:00+00:00",
        "context_translation": "", "dictionary": [], "force": False,
    }
    payload.update(overrides)
    return payload


def test_translation_cache_is_persistent_versioned_and_auditable(product_store):
    repo = LeapRepository(product_store)
    material = repo.create_material(1, MaterialCreate(title="Test", text="The examined life."))
    paragraph = repo.paragraphs(1, material["id"], 0, 1)["paragraphs"][0]
    service = TranslationService(product_store)
    payload = _translation_payload(material, paragraph)
    saved = service.save_browser_local(1, payload)
    repeated = service.save_browser_local(1, payload)
    assert saved["translated_text"] == "经审视的人生。"
    assert repeated["cache_hit"] is True
    assert repeated["metadata"]["disclaimer"].startswith("AI/机器翻译")
    window = service.window_cache(1, material["id"], 0, 100, "browser_local", "chrome-built-in-translator")
    assert len(window["items"]) == 1
    stats = service.stats(1)
    assert stats["providers"][0]["request_count"] == 1
    assert stats["providers"][0]["cache_hit_characters"] > 0
    with product_store() as connection:
        connection.execute("UPDATE leap_materials SET version=2,content_hash='' WHERE id=?", (material["id"],))
        connection.execute(
            "INSERT INTO leap_paragraphs(material_id,material_version,position,content,stable_anchor) VALUES(?,2,0,?,?)",
            (material["id"], "The changed life.", "p-v2-000000"),
        )
    assert service.window_cache(1, material["id"], 0, 100, "browser_local", "chrome-built-in-translator")["items"] == []


def test_sentence_and_paragraph_translation_have_distinct_sources_and_cache_keys(product_store):
    repo = LeapRepository(product_store)
    text = "Sentence one. Sentence two is longer. Sentence three ends here."
    material = repo.create_material(1, MaterialCreate(title="Scopes", text=text))
    paragraph = repo.paragraphs(1, material["id"], 0, 1)["paragraphs"][0]
    service = TranslationService(product_store)

    sentence = service.save_browser_local(1, _translation_payload(
        material, paragraph,
        translation_mode="sentence",
        sentence_index=1,
        selection_start=14,
        selection_end=37,
        source_text="Sentence two is longer.",
        translated_text="第二句更长。",
    ))
    full_paragraph = service.save_browser_local(1, _translation_payload(
        material, paragraph,
        translation_mode="paragraph",
        source_text=text,
        translated_text="第一句。第二句更长。第三句到此结束。",
    ))

    assert sentence["source_text"] == "Sentence two is longer."
    assert full_paragraph["source_text"] == text
    assert sentence["segment_id"].endswith(":sentence:1")
    assert full_paragraph["segment_id"] == paragraph["stable_anchor"]
    assert sentence["source_text_hash"] != full_paragraph["source_text_hash"]
    with product_store() as connection:
        rows = connection.execute(
            "SELECT translation_mode,segment_id,source_text_hash FROM leap_translation_cache ORDER BY translation_mode"
        ).fetchall()
    assert len(rows) == 2
    assert len({(row["translation_mode"], row["segment_id"], row["source_text_hash"]) for row in rows}) == 2

    with pytest.raises(ValueError, match="句子边界"):
        service.save_browser_local(1, _translation_payload(
            material, paragraph,
            translation_mode="sentence",
            sentence_index=1,
            source_text=text,
        ))


def test_interpretation_is_evidence_bound_cached_and_separate_from_translation(product_store):
    repo = LeapRepository(product_store)
    text = "Sentence one. Sentence two is longer. Sentence three ends here."
    material = repo.create_material(1, MaterialCreate(title="Meaning", text=text))
    calls = []

    def runner(user_id, system, prompt, max_tokens):
        calls.append((user_id, system, prompt, max_tokens))
        return {"text": '{"concise_meaning":"第二句强调长度差异。","explanation":"它与相邻句形成长度对比。","evidence":["Sentence two is longer."],"uncertainty":"","scope":"sentence"}'}

    service = InterpretationService(product_store, runner, provider_model="test-model")
    payload = {
        "action": "interpret", "scope": "sentence", "document_id": material["id"],
        "paragraph_start": 0, "paragraph_end": 0, "selection_start": 14, "selection_end": 37,
        "source_text": "Sentence two is longer.", "context_text": text,
        "target_language": "zh-CN", "coverage_complete": True, "coverage_label": "完整句子", "force": False,
    }
    first = service.interpret(1, payload)
    repeated = service.interpret(1, payload)
    assert first["action"] == "interpret" and first["scope"] == "sentence"
    assert first["provider"] == "frostfire_ai" and first["provider_model"] == "test-model"
    assert repeated["cache_hit"] is True and len(calls) == 1
    with product_store() as connection:
        assert connection.execute("SELECT COUNT(*) AS count FROM leap_interpretation_cache").fetchone()["count"] == 1
        assert connection.execute("SELECT COUNT(*) AS count FROM leap_translation_cache").fetchone()["count"] == 0
    with pytest.raises(ValueError, match="原文位置"):
        service.interpret(1, {**payload, "source_text": text})


def test_interpretation_validates_word_sentence_paragraph_selection_and_chapter_scopes(product_store):
    repo = LeapRepository(product_store)
    first = "Weight matters here. Sentence two."
    second = "中文段落也可以解读。"
    material = repo.create_material(1, MaterialCreate(title="All scopes", text=f"{first}\n\n{second}"))
    with product_store() as connection:
        connection.execute(
            """INSERT INTO leap_chapters(material_id,material_version,position,title,stable_anchor,start_paragraph,end_paragraph)
               VALUES(?,1,0,'Complete chapter','c-0',0,1)""", (material["id"],),
        )
        connection.execute(
            "UPDATE leap_paragraphs SET chapter_position=0,chapter_title='Complete chapter' WHERE material_id=?",
            (material["id"],),
        )

    seen = []
    service = InterpretationService(
        product_store,
        lambda _user, _system, prompt, _limit: seen.append(prompt) or {"text": '{"concise_meaning":"测试解读。","explanation":"原文关系清晰。","evidence":[],"uncertainty":"","scope":"test"}'},
        provider_model="test-model",
    )
    base = {
        "action": "interpret", "document_id": material["id"], "target_language": "zh-CN",
        "context_text": "", "coverage_complete": True, "coverage_label": "完整范围", "force": False,
    }
    payloads = [
        {**base, "scope": "word", "paragraph_start": 0, "paragraph_end": 0,
         "selection_start": 0, "selection_end": 6, "source_text": "Weight", "context_text": first},
        {**base, "scope": "sentence", "paragraph_start": 0, "paragraph_end": 0,
         "selection_start": 0, "selection_end": 20, "source_text": "Weight matters here.", "context_text": first},
        {**base, "scope": "paragraph", "paragraph_start": 0, "paragraph_end": 0,
         "selection_start": None, "selection_end": None, "source_text": first},
        {**base, "scope": "selection", "paragraph_start": 0, "paragraph_end": 1,
         "selection_start": 21, "selection_end": 4, "source_text": f"Sentence two.\n\n中文段落"},
        {**base, "scope": "chapter", "paragraph_start": 0, "paragraph_end": 1,
         "selection_start": None, "selection_end": None, "source_text": f"{first}\n\n{second}",
         "coverage_label": "完整章节"},
    ]
    results = [service.interpret(1, payload) for payload in payloads]
    assert [item["scope"] for item in results] == ["word", "sentence", "paragraph", "selection", "chapter"]
    assert len(seen) == 5
    assert all(item["action"] == "interpret" for item in results)


def test_chapter_interpretation_reports_bounded_partial_coverage(product_store):
    repo = LeapRepository(product_store)
    material = repo.create_material(1, MaterialCreate(title="Chapter", text="A" * 9_000 + "\n\n" + "B" * 9_000))
    with product_store() as connection:
        connection.execute(
            """INSERT INTO leap_chapters(material_id,material_version,position,title,stable_anchor,start_paragraph,end_paragraph)
               VALUES(?,1,0,'Long chapter','c-0',0,1)""", (material["id"],),
        )
        connection.execute("UPDATE leap_paragraphs SET chapter_position=0,chapter_title='Long chapter' WHERE material_id=?", (material["id"],))
    result = repo.chapter_content(1, material["id"], 0)
    assert result["coverage_complete"] is False
    assert result["paragraph_start"] == result["paragraph_end"] == 0
    assert "当前仅解读已加载部分" in result["coverage_label"]


class _AzureResponse:
    def __init__(self, payload): self.payload = payload
    def raise_for_status(self): return None
    def json(self): return self.payload


class _AzureClient:
    def __init__(self): self.calls = []
    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/dictionary/lookup"):
            return _AzureResponse([{"translations": [{"displayTarget": "理性", "posTag": "NOUN", "confidence": 0.9, "backTranslations": [{"displayText": "reason"}]}]}])
        return _AzureResponse([{"translations": [{"text": "⟦理性⟧指引选择。"}]}])


def test_azure_provider_dictionary_context_quota_and_no_key_fallback(product_store):
    repo = LeapRepository(product_store)
    material = repo.create_material(1, MaterialCreate(title="Test", text="Reason guides choice."))
    paragraph = repo.paragraphs(1, material["id"], 0, 1)["paragraphs"][0]
    client = _AzureClient()
    azure = AzureTranslatorProvider("secret", "australiaeast", client=client)
    service = TranslationService(product_store, azure=azure, azure_monthly_limit=1_000)
    payload = _translation_payload(
        material, paragraph, provider="azure_translator", provider_model="translator-text-v3",
        translation_mode="word", source_text="Reason", context_text=paragraph["content"], translated_text="",
    )
    result = service.translate_azure(1, payload, lookup=True)
    assert result["metadata"]["dictionary"][0]["part_of_speech"] == "NOUN"
    assert result["metadata"]["context_translation"] == "⟦理性⟧指引选择。"
    assert result["metadata"]["contextual_meaning"] == "理性"
    assert len(client.calls) == 2
    assert all(call[1]["headers"]["Ocp-Apim-Subscription-Key"] == "secret" for call in client.calls)
    unconfigured = TranslationService(product_store, azure=AzureTranslatorProvider())
    azure_provider = next(item for item in unconfigured.providers()["providers"]
                          if item["id"] == "azure_translator")
    assert azure_provider["configured"] is False
    with pytest.raises(ValueError, match="尚未配置"):
        unconfigured.translate_azure(1, {**payload, "force": True})


def test_translation_rejects_text_that_cannot_return_to_original(product_store):
    repo = LeapRepository(product_store)
    material = repo.create_material(1, MaterialCreate(title="Test", text="Original evidence only."))
    paragraph = repo.paragraphs(1, material["id"], 0, 1)["paragraphs"][0]
    service = TranslationService(product_store)
    with pytest.raises(ValueError, match="原文"):
        service.save_browser_local(1, _translation_payload(material, paragraph, source_text="Invented quote"))


def test_translation_api_works_without_azure_credentials(product_store, monkeypatch):
    monkeypatch.delenv("AZURE_TRANSLATOR_KEY", raising=False)
    monkeypatch.delenv("AZURE_TRANSLATOR_REGION", raising=False)
    repo = LeapRepository(product_store)
    material = repo.create_material(1, MaterialCreate(title="API", text="Truth needs evidence."))
    paragraph = repo.paragraphs(1, material["id"], 0, 1)["paragraphs"][0]
    app = FastAPI()
    app.include_router(create_leap_router(product_store, lambda: {"id": 1}))
    client = TestClient(app)
    providers = client.get("/api/leap/translation/providers")
    assert providers.status_code == 200
    azure_provider = next(item for item in providers.json()["providers"]
                          if item["id"] == "azure_translator")
    assert azure_provider["configured"] is False
    saved = client.put("/api/leap/translation/cache", json=_translation_payload(material, paragraph))
    assert saved.status_code == 200
    assert saved.json()["provider"] == "browser_local"
    cache = client.get("/api/leap/translation/cache", params={
        "document_id": material["id"], "offset": 0, "limit": 100,
        "provider": "browser_local", "provider_model": "chrome-built-in-translator",
        "target_language": "zh-Hans",
    })
    assert cache.status_code == 200 and len(cache.json()["items"]) == 1
    assert client.get("/api/leap/translation/stats").json()["total_translated_characters"] > 0


def test_interpretation_api_reports_unconfigured_and_uses_injected_frostfire_runner(product_store):
    repo = LeapRepository(product_store)
    material = repo.create_material(1, MaterialCreate(title="API meaning", text="Weight matters here."))
    payload = {
        "action": "interpret", "scope": "word", "document_id": material["id"],
        "document_version": material["version"],
        "paragraph_start": 0, "paragraph_end": 0, "selection_start": 0, "selection_end": 6,
        "source_text": "Weight", "context_text": "Weight matters here.",
        "target_language": "zh-CN", "coverage_complete": True, "coverage_label": "完整单词", "force": False,
    }
    unavailable_app = FastAPI()
    unavailable_app.include_router(create_leap_router(product_store, lambda: {"id": 1}))
    unavailable = TestClient(unavailable_app)
    assert unavailable.get("/api/leap/reading-assistant/capabilities").json()["available"] is False
    unavailable_response = unavailable.post("/api/leap/reading-assistant/interpret", json=payload)
    assert unavailable_response.status_code == 503
    assert unavailable_response.json()["detail"]["code"] == "AI_NOT_CONFIGURED"

    calls = []
    available_app = FastAPI()
    available_app.include_router(create_leap_router(
        product_store, lambda: {"id": 1},
        lambda user_id, system, prompt, limit: calls.append((user_id, system, prompt, limit)) or {"text": '{"concise_meaning":"分量。","explanation":"weight在本句中是主语。","evidence":["Weight"],"uncertainty":"","scope":"word"}'},
        "test-model",
    ))
    client = TestClient(available_app)
    response = client.post("/api/leap/reading-assistant/interpret", json=payload)
    assert response.status_code == 200
    assert response.json()["structured"]["concise_meaning"] == "分量。"
    assert len(calls) == 1


@pytest.mark.parametrize("provider", ["auto", "openrouter", "gemini", "openai"])
def test_interpretation_request_accepts_capability_provider_ids(provider):
    request = InterpretationWrite(
        action="interpret", provider=provider, scope="paragraph", document_id="doc-1",
        paragraph_start=0, paragraph_end=0, source_text="A paragraph.",
    )
    assert request.provider == provider


def test_interpretation_provider_errors_have_safe_stable_categories():
    class QuotaError(RuntimeError):
        status_code = 429
        body = {"error": {"code": "credit_balance_exhausted", "message": "private provider detail"}}

    class TimeoutError(RuntimeError):
        pass

    quota = classify_provider_failure(QuotaError("request id and private provider detail"))
    timeout = classify_provider_failure(TimeoutError("request timed out"))
    unknown = classify_provider_failure(RuntimeError("secret diagnostic"))

    assert quota[:2] == ("AI_CREDITS_EXHAUSTED", 429)
    assert timeout[:2] == ("AI_TIMEOUT", 504)
    assert unknown[:2] == ("AI_PROVIDER_ERROR", 502)
    assert "private provider detail" not in quota[2]
    assert "secret diagnostic" not in unknown[2]


class _OpenRouterResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")
    def __enter__(self): return self
    def __exit__(self, *_args): return False
    def read(self, _limit): return self.payload


def test_openrouter_free_provider_uses_official_endpoint_and_records_actual_model():
    calls = []
    def opener(request, timeout):
        calls.append((request, timeout))
        return _OpenRouterResponse({
            "model": "meta-llama/llama-3.3-70b-instruct:free",
            "choices": [{"message": {"content": '{"concise_meaning":"权威人士反对","explanation":"说明反对者的身份。","evidence":["authorities"],"uncertainty":"","scope":"selection"}'}}],
            "usage": {"prompt_tokens": 21, "completion_tokens": 18, "total_tokens": 39},
        })
    provider = OpenRouterInterpretationProvider("secret", opener=opener)
    result = provider.generate(1, "system", "prompt", 300)
    request, timeout = calls[0]
    assert request.full_url == "https://openrouter.ai/api/v1/chat/completions"
    assert json.loads(request.data)["model"] == "openrouter/free"
    assert request.headers["Authorization"] == "Bearer secret"
    assert timeout == 60
    assert result.provider == "openrouter"
    assert result.requested_model == "openrouter/free"
    assert result.actual_model == "meta-llama/llama-3.3-70b-instruct:free"
    assert result.usage["total_tokens"] == 39


def test_openrouter_provider_explicitly_reports_unconfigured_and_rate_limited():
    with pytest.raises(InterpretationProviderError) as missing:
        OpenRouterInterpretationProvider("").generate(1, "system", "prompt", 100)
    assert missing.value.code == "OPENROUTER_NOT_CONFIGURED"

    import urllib.error
    def limited(*_args, **_kwargs):
        raise urllib.error.HTTPError("https://openrouter.ai", 429, "limited", {}, None)
    with pytest.raises(InterpretationProviderError) as rate:
        OpenRouterInterpretationProvider("secret", opener=limited).generate(1, "system", "prompt", 100)
    assert rate.value.code == "OPENROUTER_RATE_LIMITED"


def test_openrouter_fixed_free_model_falls_back_only_to_free_router():
    import io
    import urllib.error

    calls = []
    def opener(request, timeout):
        body = json.loads(request.data)
        calls.append((body["model"], timeout))
        if len(calls) == 1:
            raise urllib.error.HTTPError(request.full_url, 503, "unavailable", {}, io.BytesIO(b"{}"))
        return _OpenRouterResponse({
            "model": "free/router-selected-model:free",
            "choices": [{"message": {"content": '{"concise_meaning":"含义","explanation":"解释","evidence":[],"uncertainty":"","scope":"word"}'}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        })

    provider = OpenRouterInterpretationProvider(
        "secret", "nvidia/nemotron-3-super-120b-a12b:free",
        fallback_model="openrouter/free", opener=opener,
    )
    result = provider.generate(1, "system", "prompt", 100)
    assert [item[0] for item in calls] == [
        "nvidia/nemotron-3-super-120b-a12b:free", "openrouter/free",
    ]
    assert result.requested_model == "nvidia/nemotron-3-super-120b-a12b:free"
    assert result.actual_model == "free/router-selected-model:free"


def test_gemini_provider_uses_generate_content_and_records_actual_model():
    calls = []
    def opener(request, timeout):
        calls.append((request, timeout))
        return _OpenRouterResponse({
            "modelVersion": "gemini-free-test",
            "candidates": [{"content": {"parts": [{"text": '{"concise_meaning":"含义","explanation":"解释","evidence":[],"uncertainty":"","scope":"word"}'}]}}],
            "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 5, "totalTokenCount": 9},
        })
    provider = GeminiInterpretationProvider("secret", "gemini-free-test", opener=opener)
    result = provider.generate(1, "system", "prompt", 100)
    request, timeout = calls[0]
    assert ":generateContent?key=secret" in request.full_url
    assert json.loads(request.data)["generationConfig"]["responseMimeType"] == "application/json"
    assert timeout == 60
    assert result.provider == "gemini"
    assert result.actual_model == "gemini-free-test"
    assert result.usage["total_tokens"] == 9


def test_gemini_provider_requires_explicit_model_and_never_enables_paid_mode():
    with pytest.raises(InterpretationProviderError) as missing:
        GeminiInterpretationProvider("secret").generate(1, "system", "prompt", 100)
    assert missing.value.code == "GEMINI_NOT_CONFIGURED"
    assert not GeminiInterpretationProvider("secret", "gemini-test", allow_paid=True).configured


def test_free_router_never_calls_paid_provider_and_falls_back_to_gemini():
    class FailedOpenRouter:
        configured = True
        provider_id = "openrouter"
        requested_model = "openrouter/free"
        fallback_model = "openrouter/free"
        is_free = True
        def generate(self, *_args):
            raise InterpretationProviderError("OPENROUTER_RATE_LIMITED", 429, "limited")
    class FreeGemini:
        configured = True
        provider_id = "gemini"
        requested_model = "gemini-free"
        fallback_model = ""
        is_free = True
        def generate(self, *_args):
            return InterpretationProviderResult("{}", "gemini", "gemini-free", "gemini-actual", "now", {})
    result = FreeInterpretationProvider([FailedOpenRouter(), FreeGemini()]).generate(1, "system", "prompt", 100)
    assert result.provider == "gemini"


def test_interpretation_scope_prompts_are_distinct_and_evidence_is_source_bound():
    word_system, word_prompt = InterpretationService._prompts(
        "word", "weight", "His promise carried more weight than his title.", "完整单词",
    )
    paragraph_system, paragraph_prompt = InterpretationService._prompts(
        "paragraph", "A model can overfit.", "", "完整段落",
    )
    assert "简体中文" in word_system and "逐字引用" in word_system
    assert "词典义项" in word_prompt
    assert "技术材料关注原理" in paragraph_prompt
    assert word_prompt != paragraph_prompt and word_system == paragraph_system

    structured = InterpretationService._structured_result(
        '{"concise_meaning":"重要性","explanation":"说明承诺的影响力。","evidence":["more weight","不存在的引文"],"uncertainty":"","scope":"word"}',
        "word", "weight", "His promise carried more weight than his title.",
    )
    assert structured["evidence"] == ["more weight"]
    assert structured["scope"] == "word"


def test_interpretation_provider_choice_cache_and_actual_model_are_isolated(product_store):
    repo = LeapRepository(product_store)
    material = repo.create_material(1, MaterialCreate(title="Provider", text="Weight matters here."))

    class FakeProvider:
        label = "Fake Free"
        is_free = True
        configured = True
        requested_model = "openrouter/free"
        provider_id = "openrouter"
        def __init__(self): self.calls = 0
        def generate(self, *_args):
            self.calls += 1
            return InterpretationProviderResult(
                text='{"concise_meaning":"分量很重要","explanation":"weight在句中指重要性。","evidence":["Weight matters"],"uncertainty":"","scope":"word"}',
                provider="openrouter", requested_model="openrouter/free",
                actual_model="free/model-a", requested_at="2026-09-25T00:00:00+00:00",
                usage={"input_tokens": 10, "output_tokens": 10, "total_tokens": 20},
            )

    provider = FakeProvider()
    service = InterpretationService(
        product_store, providers={"openrouter": provider}, default_provider="openrouter",
    )
    payload = {
        "action": "interpret", "provider": "openrouter", "scope": "word", "document_id": material["id"],
        "document_version": material["version"], "paragraph_start": 0, "paragraph_end": 0,
        "selection_start": 0, "selection_end": 6, "source_text": "Weight",
        "context_text": "Weight matters here.", "target_language": "zh-CN",
        "coverage_complete": True, "coverage_label": "完整单词", "force": False,
    }
    first = service.interpret(1, payload)
    repeated = service.interpret(1, payload)
    assert first["provider"] == "openrouter"
    assert first["requested_model"] == "openrouter/free"
    assert first["provider_model"] == "free/model-a"
    assert first["generated_at"] == "2026-09-25T00:00:00+00:00"
    assert first["structured"]["concise_meaning"] == "分量很重要"
    assert repeated["cache_hit"] is True
    assert provider.calls == 1
    with product_store() as connection:
        attempt = connection.execute("SELECT * FROM leap_interpretation_provider_attempts").fetchone()
    assert attempt["status"] == "success" and attempt["actual_model"] == "free/model-a"


def test_interpretation_api_distinguishes_invalid_selection_and_document_version(product_store):
    repo = LeapRepository(product_store)
    material = repo.create_material(1, MaterialCreate(title="Validation", text="Weight matters here."))
    base = {
        "action": "interpret", "scope": "word", "document_id": material["id"],
        "document_version": material["version"],
        "paragraph_start": 0, "paragraph_end": 0, "selection_start": 0, "selection_end": 6,
        "source_text": "Wrong!", "context_text": "Weight matters here.",
        "target_language": "zh-CN", "coverage_complete": True, "coverage_label": "完整单词", "force": False,
    }
    app = FastAPI()
    app.include_router(create_leap_router(
        product_store, lambda: {"id": 1},
        lambda *_args: {"text": "never reached"}, "test-model",
    ))
    client = TestClient(app)

    invalid = client.post("/api/leap/reading-assistant/interpret", json=base)
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "INVALID_SELECTION"

    with product_store() as connection:
        connection.execute("UPDATE leap_materials SET version=2 WHERE id=?", (material["id"],))
    mismatch = client.post("/api/leap/reading-assistant/interpret", json={**base, "source_text": "Weight", "document_version": 1})
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"]["code"] == "DOCUMENT_VERSION_MISMATCH"


def _pulse_master_data(repo: PulseRepository, user_id: int = 1):
    customer = repo.create_customer(user_id, CustomerWrite(name="Customer A"))
    sku = repo.create_sku(user_id, SKUWrite(name="Evening Dress", current_price_cents=10_000))
    asset = repo.create_asset(user_id, AssetWrite(
        sku_id=sku["id"], asset_code="OIA-001", accounting_class="rental_asset",
        purchase_cost_cents=50_000,
    ))
    return customer, sku, asset


def test_pulse_price_snapshot_deposit_accounting_and_asset_lifecycle(product_store):
    repo = PulseRepository(product_store)
    customer, sku, asset = _pulse_master_data(repo)
    start = datetime.now(timezone.utc) + timedelta(days=2)
    order = repo.create_order(1, OrderWrite(
        customer_id=customer["id"], start_at=start, end_at=start + timedelta(days=2),
        items=[OrderLineWrite(sku_id=sku["id"], asset_id=asset["id"])],
    ))
    with product_store() as connection:
        connection.execute("UPDATE pulse_skus SET current_price_cents=25000 WHERE id=?", (sku["id"],))
    assert repo.order_detail(1, order["id"])["items"][0]["unit_price_cents"] == 10_000

    deposit = repo.record_payment(1, PaymentWrite(
        order_id=order["id"], payment_type="deposit", amount_cents=3_000,
    ))
    rental = repo.record_payment(1, PaymentWrite(
        order_id=order["id"], payment_type="rental", amount_cents=10_000,
    ))
    assert deposit["journal"]["balanced"] is True
    assert rental["journal"]["balanced"] is True
    statements = repo.statements(1, "2020-01-01", "2035-12-31")
    assert statements["income_statement"]["revenue_total"] == 10_000
    assert repo.dashboard(1, "2020-01-01", "2035-12-31")["metrics"]["deposits_held"] == 3_000
    expense = repo.record_expense(1, ExpenseWrite(
        category="cleaning", amount_cents=800, asset_id=asset["id"], description="Return cleaning",
    ))
    assert expense["expense"]["id"]
    assert expense["journal"]["balanced"] is True
    assert repo.dashboard(1, "2020-01-01", "2035-12-31")["metrics"]["cash_out"] == 800

    repo.transition_asset(1, asset["id"], AssetStatusWrite(status="rented", order_id=order["id"]))
    repo.transition_asset(1, asset["id"], AssetStatusWrite(status="inspection", order_id=order["id"]))
    repo.create_inspection(1, InspectionWrite(
        order_id=order["id"], asset_id=asset["id"],
        condition_status="cleaning_required", resolution_status="confirmed",
    ))
    assert repo.list_entity(1, "pulse_assets")[0]["status"] == "cleaning"
    assert repo.trial_balance(1, "2020-01-01", "2035-12-31")["balanced"] is True


def test_pulse_overlap_is_rejected_even_for_concurrent_attempts(product_store):
    repo = PulseRepository(product_store)
    customer, sku, asset = _pulse_master_data(repo)
    start = datetime.now(timezone.utc) + timedelta(days=3)
    payload = OrderWrite(
        customer_id=customer["id"], start_at=start, end_at=start + timedelta(days=1),
        items=[OrderLineWrite(sku_id=sku["id"], asset_id=asset["id"])],
    )

    def create():
        try:
            return repo.create_order(1, payload)["id"]
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(), range(2)))
    assert sum(value is not None for value in results) == 1
    assert len(repo.list_entity(1, "pulse_orders")) == 1


def test_pulse_user_data_isolation(product_store):
    repo = PulseRepository(product_store)
    customer, _, _ = _pulse_master_data(repo, 1)
    assert repo.list_entity(2, "pulse_customers") == []
    with product_store() as connection:
        with pytest.raises(KeyError):
            repo._owned(connection, "pulse_customers", customer["id"], 2)


def test_leap_demo_is_isolated_persistent_and_evidence_backed(product_store):
    repo = LeapRepository(product_store)
    demo = repo.reset_demo(1)
    assert demo["loaded"] is True
    assert len(demo["materials"]) == 4
    assert repo.list_materials(1, "", 30, 0)["total"] == 0
    material = demo["materials"][0]
    updated = repo.demo_action(1, DemoAction(action="excerpt", payload={
        "material_id": material["id"], "paragraph_position": 0,
        "quote": material["paragraphs"][0], "start_offset": 0,
    }))
    assert len(updated["excerpts"]) == len(demo["excerpts"]) + 1
    assert repo.demo(2)["loaded"] is False
    assert any(node["kind"] == "theme" for node in updated["universe"]["nodes"])


def test_pulse_demo_full_transaction_updates_balanced_finance(product_store):
    repo = PulseRepository(product_store)
    demo = repo.reset_demo(1)
    assert demo["trial_balance"]["balanced"] is True
    eda = demo["analytics"]["eda"]
    assert eda["distributions"]["order_value_cents"]["count"] >= 20
    assert eda["distributions"]["order_value_cents"]["median"] > 0
    assert eda["distributions"]["order_value_cents"]["mean"] > 0
    assert len(eda["monthly_trends"]) >= 5
    assert eda["pca"]["status"] == "ok"
    assert len(eda["pca"]["points"]) == len(demo["analytics"]["customers"])
    assert demo["statements"]["balance_sheet"]["balanced"] is True
    assert demo["analytics"]["customer_summary"]["cohorts"]
    assert demo["analytics"]["customer_summary"]["channel_revenue"]
    assert demo["analytics"]["top_assets"][0]["revenue_per_available_day"] > 0
    assert repo.list_entity(1, "pulse_orders") == []
    customer = repo.demo_action(1, PulseDemoAction(action="customer", payload={"name": "Journey Customer"}))["customers"][-1]
    available = next(asset for asset in repo.demo(1)["assets"] if asset["status"] == "available")
    order = repo.demo_action(1, PulseDemoAction(action="order", payload={
        "customer_id": customer["id"], "asset_id": available["id"], "amount_cents": 55_000,
    }))["orders"][-1]
    for action, payload in (
        ("payment", {"order_id": order["id"], "amount_cents": 55_000}),
        ("deposit", {"order_id": order["id"], "amount_cents": 15_000}),
        ("deliver", {"order_id": order["id"]}),
        ("return", {"order_id": order["id"]}),
        ("inspect", {"order_id": order["id"], "asset_id": available["id"], "condition_status": "cleaning_required"}),
        ("cleaning", {"order_id": order["id"], "asset_id": available["id"], "amount_cents": 5_000}),
        ("refund", {"order_id": order["id"], "amount_cents": 15_000}),
    ):
        demo = repo.demo_action(1, PulseDemoAction(action=action, payload=payload))
    detail = repo.demo_order(1, order["id"])
    assert detail["status"] == "completed"
    assert {item["payment_type"] for item in detail["payments"]} >= {"rental", "deposit", "deposit_refund"}
    assert detail["inspections"] and detail["expenses"] and detail["journals"]
    assert demo["trial_balance"]["balanced"] is True
    assert demo["statements"]["balance_sheet"]["balanced"] is True
    assert repo.demo_asset(1, available["id"])["lifetime_revenue"] >= 55_000


def test_pulse_olap_accepts_empty_and_unpaid_business_history():
    from backend.product_domains.pulse_analytics import duckdb_olap_rollup

    empty = duckdb_olap_rollup([], [])
    assert empty["monthly_rows"] == []
    assert empty["order_values"] == []
    unpaid = duckdb_olap_rollup([], [{"total_cents": 5000}])
    assert unpaid["monthly_rows"] == []
    assert unpaid["order_values"] == [5000]


def test_pulse_eda_boxplot_and_pca_are_auditable_and_degrade_on_small_sample():
    stats = distribution([1, 2, 3, 4, 100])
    assert stats["mean"] == 22
    assert stats["median"] == 3
    assert stats["outliers"] == [100]
    small = pca_projection([{"id": "one", "a": 1, "b": 2}], ["a", "b"])
    assert small["status"] == "insufficient_data"
    constant = pca_projection([{"id": str(index), "a": 1, "b": 2} for index in range(4)], ["a", "b"])
    assert constant["status"] == "no_feature_variation"
    assert constant["points"] == []
    clustered = pca_projection([
        {"id": str(index), "orders": index + 1, "spend": (index + 1) ** 2, "days": 30 - index}
        for index in range(8)
    ], ["orders", "spend", "days"])
    assert clustered["status"] == "ok"
    assert "KMeans" in clustered["segmentation_method"]
    assert len({point["cluster"] for point in clustered["points"]}) >= 2
    forecast = linear_revenue_forecast([
        {"period": "2026-01", "revenue": 10000},
        {"period": "2026-02", "revenue": 20000},
        {"period": "2026-04", "revenue": 40000},
    ])
    assert forecast["status"] == "ok"
    assert forecast["observations"] == 4  # March is an explicit zero-revenue month.
    assert forecast["points"][0]["period"] == "2026-05"
    assert forecast["points"][0]["lower_95"] <= forecast["points"][0]["revenue"] <= forecast["points"][0]["upper_95"]


def test_revenue_model_demo_is_synthetic_reproducible_and_time_validated():
    demo = simulated_revenue_demo(months=72, seed=7)
    assert demo["synthetic"] is True
    assert len(demo["monthly_rows"]) == 72
    assert demo["forecast"]["ml_comparison"]["status"] == "ok"
    assert demo["forecast"]["ml_comparison"]["minimum_months"] == 60
    assert {item["model"] for item in demo["forecast"]["ml_comparison"]["models"]} >= {
        "last_value_baseline", "random_forest", "adaboost", "bayesian_ridge",
    }
    comparison = demo["forecast"]["ml_comparison"]
    assert comparison["selection_rule"]
    assert comparison["selected_model"] in {
        item["model"] for item in demo["forecast"]["ml_comparison"]["models"]
    }
    baseline_mae = next(item["mae"] for item in comparison["models"]
                        if item["model"] == "last_value_baseline")
    selected_mae = next(item["mae"] for item in comparison["models"]
                        if item["model"] == comparison["selected_model"])
    assert comparison["selected_model"] == "last_value_baseline" or selected_mae <= baseline_mae * 0.95
    assert "chronological" in demo["forecast"]["ml_comparison"]["validation"]
    assert demo == simulated_revenue_demo(months=72, seed=7)


def test_revenue_ml_models_require_five_years_and_reserve_one_year_for_holdout():
    short = compare_revenue_models([100_000 + index * 1000 for index in range(59)], datetime(2026, 1, 1))
    assert short["status"] == "insufficient_history"
    assert short["minimum_months"] == 60
    enough = simulated_revenue_demo(months=60, seed=19)["forecast"]["ml_comparison"]
    assert enough["status"] == "ok"
    assert all(item["holdout_months"] >= 12 for item in enough["models"])


def test_pytorch_lstm_is_gated_on_long_history_and_time_validated():
    short = compare_revenue_models([100_000 + index * (60_000 / 71) for index in range(72)], datetime(2026, 1, 1))
    assert short["status"] == "ok"
    assert short["deep_learning"]["status"] == "insufficient_history"
    long = simulated_revenue_demo(months=144, seed=11)
    result = long["forecast"]["ml_comparison"]
    assert result["deep_learning"]["status"] == "ok"
    assert result["deep_learning"]["evaluation"]["validation"] == "chronological rolling one-step holdout"
    assert {"random_forest", "adaboost", "bayesian_ridge", "pytorch_lstm"} <= {
        item["model"] for item in result["models"]
    }
    assert len(result["points"]) == 3


def test_deposit_scenarios_compare_coverage_not_incident_probability():
    result = deposit_coverage_scenarios([5_000, 20_000])
    fifty, hundred = result["scenarios"]
    assert result["status"] == "observed_severity_sensitivity"
    assert fifty["covered_cents"] == 10_000
    assert hundred["covered_cents"] == 15_000
    assert hundred["residual_loss_cents"] < fifty["residual_loss_cents"]
    assert "不改变" in result["interpretation"]
    synthetic = deposit_coverage_scenarios([])
    assert synthetic["synthetic"] is True


def test_statistical_diagnostics_are_reproducible_and_time_ordered():
    values = [float(index) for index in range(1, 13)]
    interval = bootstrap_interval(values)
    assert interval["status"] == "ok"
    assert interval["lower_95"] <= interval["estimate"] <= interval["upper_95"]
    assert kde_density(values)["status"] == "ok"
    timeline = [{"period": f"2025-{index:02d}", "revenue": index * 10_000} for index in range(1, 10)]
    validation = rolling_time_validation(timeline)
    assert validation["status"] == "ok"
    assert validation["validation"].startswith("expanding-window")
    rows = [{"total_cents": 10_000 + 1_000 * index + (index % 3) * 20,
             "party_size": 1 + index % 4, "planned_sets": 1 + index % 3,
             "discount_pressure_score": index % 4, "lead_hours": 12 + index,
             "deposit_paid": index % 2, "subjective_urgency_score": index % 4,
             "flower_add_on": (index // 2) % 2, "risk_event": int(index % 5 == 0)}
            for index in range(40)]
    regression = multiple_regression_diagnostics(rows, target="total_cents",
                                                  features=["party_size", "planned_sets", "discount_pressure_score", "lead_hours"])
    assert regression["status"] == "ok"
    selection = risk_feature_selection(rows, ["party_size", "planned_sets", "discount_pressure_score", "lead_hours",
                                              "deposit_paid", "subjective_urgency_score", "flower_add_on"])
    assert selection["status"] == "ok"
    assert selection["validation"].startswith("chronological")


def test_customer_profile_and_order_risk_facts_are_aggregated_with_small_groups_suppressed(product_store):
    repo = PulseRepository(product_store)
    now = datetime.now(timezone.utc)
    sku = repo.create_sku(1, SKUWrite(name="Profile dress", current_price_cents=45_000))
    order_ids = []
    for index in range(5):
        customer = repo.create_customer(1, CustomerWrite(
            name=f"Customer {index}", profession="Design", education_level="undergraduate",
            referral_status="yes", moments_visibility="visible_to_me", gender="female", age_band="25_34",
        ))
        asset = repo.create_asset(1, AssetWrite(sku_id=sku["id"], asset_code=f"PROFILE-{index}",
            accounting_class="rental_asset", purchase_cost_cents=250_000))
        planned_end = now - timedelta(days=2)
        order = repo.create_order(1, OrderWrite(
            customer_id=customer["id"], start_at=now - timedelta(days=5), end_at=planned_end,
            party_size=2, planned_sets=2, discount_pressure="strong", subjective_urgency="high",
            items=[OrderLineWrite(sku_id=sku["id"], asset_id=asset["id"])],
        ))
        order_ids.append(order["id"])
        repo.record_payment(1, PaymentWrite(order_id=order["id"], payment_type="deposit", amount_cents=5_000))
        repo.transition_order(1, order["id"], OrderStatusWrite(status="rented"))
        repo.transition_order(1, order["id"], OrderStatusWrite(status="returned"))
        repo.create_inspection(1, InspectionWrite(order_id=order["id"], asset_id=asset["id"],
            condition_status="damaged", resolution_status="confirmed"))
    data = repo.analytics(1, (now - timedelta(days=30)).date().isoformat(), (now + timedelta(days=1)).date().isoformat())
    assert data["customer_risk_summary"]["late_return_orders"] == 5
    assert data["customer_risk_summary"]["damaged_orders"] == 5
    assert data["customer_risk_summary"]["deposit_paid_orders"] == 5
    design_group = data["customer_dimensions"]["dimensions"]["profession"]["groups"]
    assert design_group == [{"category": "Design", "customers": 5, "new_customers": 5,
        "repeat_customers": 0, "late_return_rate": 1.0, "current_overdue_orders": 0,
        "damage_rate": 1.0, "missing_rate": 0.0, "return_observation_orders": 5, "inspected_orders": 5}]
    pressure_groups = data["order_context_profiles"]["dimensions"]["discount_pressure"]["groups"]
    assert pressure_groups[0]["category"] == "strong"
    assert pressure_groups[0]["late_return_rate"] == 1.0
    assert pressure_groups[0]["orders"] == 5


def test_real_order_status_drives_assigned_asset(product_store):
    repo = PulseRepository(product_store)
    customer, sku, asset = _pulse_master_data(repo)
    start = datetime.now(timezone.utc) + timedelta(days=2)
    order = repo.create_order(1, OrderWrite(customer_id=customer["id"], start_at=start,
        end_at=start + timedelta(days=2), items=[OrderLineWrite(sku_id=sku["id"], asset_id=asset["id"])]))
    repo.transition_order(1, order["id"], OrderStatusWrite(status="rented"))
    assert repo.list_entity(1, "pulse_assets")[0]["status"] == "rented"
    repo.transition_order(1, order["id"], OrderStatusWrite(status="returned"))
    assert repo.list_entity(1, "pulse_assets")[0]["status"] == "inspection"
    repo.create_inspection(1, InspectionWrite(order_id=order["id"], asset_id=asset["id"],
        condition_status="cleaning_required", resolution_status="confirmed"))
    repo.record_expense(1, ExpenseWrite(category="cleaning", amount_cents=500, order_id=order["id"],
        asset_id=asset["id"], description="Post-rental cleaning"))
    repo.transition_asset(1, asset["id"], AssetStatusWrite(status="available", order_id=order["id"]))
    repo.record_payment(1, PaymentWrite(order_id=order["id"], payment_type="deposit_refund", amount_cents=3_000))
    completed = repo.transition_order(1, order["id"], OrderStatusWrite(status="completed"))
    assert completed["status"] == "completed"
    assert completed["inspections"] and completed["expenses"] and len(completed["journals"]) == 2
    assert repo.list_entity(1, "pulse_assets")[0]["status"] == "available"


def test_product_domain_routes_work_with_postgres(persistent_app):
    """The two products must use the same production PostgreSQL adapter."""
    from fastapi.testclient import TestClient

    with TestClient(persistent_app.main.app) as client:
        headers, _ = register(client, "product-domain-postgres-user")
        material = client.post("/api/leap/materials", headers=headers, json={
            "title": "跨域阅读样本", "author": "测试", "text": "第一段。\n\n第二段。",
        })
        assert material.status_code == 201, material.text
        assert client.get("/api/leap/materials", headers=headers).json()["items"][0]["title"] == "跨域阅读样本"
        public_catalog = client.get("/api/leap/library/search?provider=ctext", headers=headers)
        assert public_catalog.status_code == 200, public_catalog.text
        assert {item["title"] for item in public_catalog.json()["items"]} == {"论语", "孟子", "道德经", "庄子"}
        blocked = client.post(
            "/api/leap/library/imports?provider=ctext&source_item_id=analects", headers=headers,
        )
        assert blocked.status_code == 400 and "人工确认" in blocked.json()["detail"]

        customer = client.post("/api/pulse/customers", headers=headers, json={"name": "Oia 测试客户"})
        sku = client.post("/api/pulse/skus", headers=headers, json={
            "name": "测试礼服", "current_price_cents": 12_000,
        })
        assert customer.status_code == 201, customer.text
        assert sku.status_code == 201, sku.text
        asset = client.post("/api/pulse/assets", headers=headers, json={
            "sku_id": sku.json()["id"], "asset_code": "PG-OIA-001",
            "accounting_class": "rental_asset", "purchase_cost_cents": 40_000,
        })
        assert asset.status_code == 201, asset.text
        assert client.get("/api/pulse/assets", headers=headers).json()[0]["asset_code"] == "PG-OIA-001"

        starts_at = datetime.now(timezone.utc) + timedelta(days=2)
        order = client.post("/api/pulse/orders", headers=headers, json={
            "customer_id": customer.json()["id"],
            "start_at": starts_at.isoformat(),
            "end_at": (starts_at + timedelta(days=2)).isoformat(),
            "items": [{"sku_id": sku.json()["id"], "asset_id": asset.json()["id"]}],
        })
        assert order.status_code == 201, order.text
        deposit = client.post("/api/pulse/payments", headers=headers, json={
            "order_id": order.json()["id"], "payment_type": "deposit", "amount_cents": 3_000,
        })
        assert deposit.status_code == 201, deposit.text
        statements = client.get(
            "/api/pulse/statements?date_from=2020-01-01&date_to=2035-12-31", headers=headers,
        )
        assert statements.status_code == 200, statements.text
        assert statements.json()["income_statement"]["revenue_total"] == 0
        assert statements.json()["balance_sheet"]["liabilities"]["Customer Deposits"] == 3_000


def test_finance_models_persist_across_postgres_restart_and_isolate_accounts(persistent_app):
    app = persistent_app
    inputs = {
        "current_age": 35, "retirement_age": 67, "starting_balance": 50_000,
        "annual_salary": 90_000, "contribution_rate": .12, "salary_growth": .03,
        "balanced_return": .06, "balanced_volatility": .10,
        "lifecycle_return": .05, "lifecycle_volatility": .08, "simulations": 20,
    }
    with TestClient(app.main.app) as client:
        headers, _ = register(client, "finance-pg-owner")
        other_headers, _ = register(client, "finance-pg-other")
        for kind, payload in (("lifecycle", inputs), ("acquisition", {
            "purchase_price": 100, "debt_share": .6, "debt_rate": .1,
            "tax_rate": .3, "target_ebit": 20,
        })):
            response = client.post(f"/api/finance/models/{kind}", headers=headers, json=payload)
            assert response.status_code == 200, response.text
            assert response.json()["saved_run"]["model_type"] == kind
        assert client.get("/api/finance/models/history", headers=other_headers).json()["items"] == []
    with TestClient(app.main.app) as restarted:
        history = restarted.get("/api/finance/models/history", headers=headers)
        assert history.status_code == 200, history.text
        runs = history.json()["items"]
        assert {run["model_type"] for run in runs} == {"lifecycle", "acquisition"}
        assert next(run for run in runs if run["model_type"] == "lifecycle")["inputs"] == inputs


def test_real_pgvector_indexes_searches_and_preserves_account_boundaries(persistent_app):
    """Exercise pgvector SQL, not a mocked successful integration status."""
    from backend.storage import connect_postgres

    app = persistent_app
    with TestClient(app.main.app) as client:
        _, owner = register(client, "vector-pg-owner")
        _, other = register(client, "vector-pg-other")
        repo = LeapRepository(app.database.connect)
        material = repo.create_material(owner["id"], MaterialCreate(
            title="向量验收材料", text="# 检索测试\n\n久期衡量债券价格的利率敏感度。",
        ))
        # Same isolated test schema, with a distinct pool to exercise the
        # separate vector-store path used by the hosted application.
        service = LeapKnowledgeService(app.database.connect, vector_connect=lambda: connect_postgres(
            app.dsn, schema=app.schema, max_size=3,
        ))
        indexed = service.index_material(owner["id"], material["id"])
        assert indexed["vector_store"]["status"] == "ready", indexed
        assert indexed["vector_store"]["indexed_chunks"] == 1
        hits = service.search(owner["id"], "久期利率敏感度")
        assert hits and hits[0]["material_id"] == material["id"]
        assert service.search(other["id"], "久期利率敏感度") == []
        assert service.vector_store_status(other["id"])["indexed_chunks"] == 0
        with app.database.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM leap_vector_chunks").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM leap_materials").fetchone()[0] == 1

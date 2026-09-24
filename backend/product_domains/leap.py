"""跃迁域: evidence-based reading with optional, user-triggered interpretation."""

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Callable, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .public_library import PublicLibraryService, init_public_library_schema
from .interpretation import InterpretationService, init_interpretation_schema
from .translation import TranslationService, init_translation_schema


MAX_MATERIAL_BYTES = 2 * 1024 * 1024
MAX_PARAGRAPHS = 5_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(row: Any) -> dict[str, Any]:
    return dict(row)


def _json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return fallback


def _paragraphs(text: str) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise ValueError("材料正文不能为空。")
    blocks = [re.sub(r"[ \t]+", " ", item).strip() for item in re.split(r"\n\s*\n+", text)]
    result = [item for item in blocks if item]
    if len(result) == 1:
        result = [item.strip() for item in text.split("\n") if item.strip()]
    if len(result) > MAX_PARAGRAPHS:
        raise ValueError(f"材料最多支持 {MAX_PARAGRAPHS} 个段落。")
    return result


def init_leap_schema(connect: Callable[[], Any]) -> None:
    with connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS leap_materials (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                author TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '[]',
                version INTEGER NOT NULL DEFAULT 1,
                content_hash TEXT NOT NULL DEFAULT '',
                paragraph_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS leap_paragraphs (
                material_id TEXT NOT NULL,
                material_version INTEGER NOT NULL,
                position INTEGER NOT NULL,
                content TEXT NOT NULL,
                chapter_position INTEGER NOT NULL DEFAULT 0,
                chapter_title TEXT NOT NULL DEFAULT '',
                stable_anchor TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(material_id, material_version, position),
                FOREIGN KEY(material_id) REFERENCES leap_materials(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS leap_reading_progress (
                user_id INTEGER NOT NULL,
                material_id TEXT NOT NULL,
                material_version INTEGER NOT NULL,
                paragraph_position INTEGER NOT NULL DEFAULT 0,
                character_offset INTEGER NOT NULL DEFAULT 0,
                progress_percent INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(user_id, material_id),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(material_id) REFERENCES leap_materials(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS leap_excerpts (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                material_id TEXT NOT NULL,
                material_version INTEGER NOT NULL,
                paragraph_position INTEGER NOT NULL,
                start_offset INTEGER NOT NULL DEFAULT 0,
                end_offset INTEGER NOT NULL DEFAULT 0,
                quote TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(material_id) REFERENCES leap_materials(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS leap_notes (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                material_id TEXT,
                excerpt_id TEXT,
                topic TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(material_id) REFERENCES leap_materials(id) ON DELETE CASCADE,
                FOREIGN KEY(excerpt_id) REFERENCES leap_excerpts(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS leap_wormholes (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                left_excerpt_id TEXT NOT NULL,
                right_excerpt_id TEXT NOT NULL,
                relation_type TEXT NOT NULL,
                reflection TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(left_excerpt_id) REFERENCES leap_excerpts(id) ON DELETE CASCADE,
                FOREIGN KEY(right_excerpt_id) REFERENCES leap_excerpts(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS leap_clash_cards (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                viewpoint_a TEXT NOT NULL,
                viewpoint_b TEXT NOT NULL,
                evidence_a_excerpt_id TEXT,
                evidence_b_excerpt_id TEXT,
                common_ground TEXT NOT NULL DEFAULT '',
                disagreement TEXT NOT NULL DEFAULT '',
                judgment TEXT NOT NULL DEFAULT '',
                unresolved_questions TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(evidence_a_excerpt_id) REFERENCES leap_excerpts(id) ON DELETE SET NULL,
                FOREIGN KEY(evidence_b_excerpt_id) REFERENCES leap_excerpts(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS leap_demo_workspaces (
                user_id INTEGER PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_leap_materials_user_updated
                ON leap_materials(user_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_leap_excerpts_user_material
                ON leap_excerpts(user_id, material_id, paragraph_position);
            CREATE INDEX IF NOT EXISTS idx_leap_notes_user_updated
                ON leap_notes(user_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_leap_wormholes_user_updated
                ON leap_wormholes(user_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_leap_clash_user_updated
                ON leap_clash_cards(user_id, updated_at DESC);
            """
        )
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(leap_clash_cards)").fetchall()}
        for name in ("common_ground", "unresolved_questions"):
            if name not in columns:
                connection.execute(f"ALTER TABLE leap_clash_cards ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        paragraph_columns = {row["name"] for row in connection.execute("PRAGMA table_info(leap_paragraphs)").fetchall()}
        paragraph_additions = {
            "chapter_position": "INTEGER NOT NULL DEFAULT 0",
            "chapter_title": "TEXT NOT NULL DEFAULT ''",
            "stable_anchor": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in paragraph_additions.items():
            if name not in paragraph_columns:
                connection.execute(f"ALTER TABLE leap_paragraphs ADD COLUMN {name} {definition}")
        material_columns = {row["name"] for row in connection.execute("PRAGMA table_info(leap_materials)").fetchall()}
        if "content_hash" not in material_columns:
            connection.execute("ALTER TABLE leap_materials ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_leap_paragraph_anchor ON leap_paragraphs(material_id,material_version,stable_anchor)")
    init_public_library_schema(connect)
    init_translation_schema(connect)
    init_interpretation_schema(connect)


class MaterialCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=240)
    author: str = Field(default="", max_length=240)
    source: str = Field(default="", max_length=1_000)
    tags: list[str] = Field(default_factory=list, max_length=30)
    text: str = Field(min_length=1, max_length=MAX_MATERIAL_BYTES)

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip()[:60] for item in value if item.strip()))


class ProgressUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paragraph_position: int = Field(ge=0)
    character_offset: int = Field(default=0, ge=0)


class ExcerptCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    material_id: str
    material_version: int = Field(ge=1)
    paragraph_position: int = Field(ge=0)
    start_offset: int = Field(default=0, ge=0)
    end_offset: int = Field(default=0, ge=0)
    quote: str = Field(min_length=1, max_length=8_000)


class NoteWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    material_id: str | None = None
    excerpt_id: str | None = None
    topic: str = Field(default="", max_length=160)
    content: str = Field(min_length=1, max_length=20_000)


class WormholeWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    left_excerpt_id: str
    right_excerpt_id: str
    relation_type: Literal["相似", "对立", "延伸", "因果", "应用", "质疑"]
    reflection: str = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def distinct_evidence(self):
        if self.left_excerpt_id == self.right_excerpt_id:
            raise ValueError("思想虫洞必须连接两条不同摘录。")
        return self


class ClashWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=240)
    viewpoint_a: str = Field(min_length=1, max_length=10_000)
    viewpoint_b: str = Field(min_length=1, max_length=10_000)
    evidence_a_excerpt_id: str | None = None
    evidence_b_excerpt_id: str | None = None
    common_ground: str = Field(default="", max_length=10_000)
    disagreement: str = Field(default="", max_length=10_000)
    judgment: str = Field(default="", max_length=10_000)
    unresolved_questions: str = Field(default="", max_length=10_000)


class DemoAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["progress", "excerpt", "note", "wormhole", "clash"]
    payload: dict[str, Any] = Field(default_factory=dict)


class TranslationWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str
    paragraph_position: int = Field(ge=0)
    paragraph_end: int | None = Field(default=None, ge=0)
    segment_id: str = Field(default="", max_length=300)
    source_language: str = Field(default="en", max_length=20)
    target_language: str = Field(default="zh-Hans", max_length=20)
    provider: Literal["browser_local", "azure_translator"]
    provider_model: str = Field(min_length=1, max_length=120)
    translation_mode: Literal["word", "sentence", "paragraph", "selection", "chapter_window"]
    source_text: str = Field(min_length=1, max_length=20_000)
    sentence_index: int | None = Field(default=None, ge=0)
    selection_start: int | None = Field(default=None, ge=0)
    selection_end: int | None = Field(default=None, ge=0)
    context_text: str = Field(default="", max_length=20_000)
    translated_text: str = Field(default="", max_length=40_000)
    translated_at: str = Field(default="", max_length=80)
    context_translation: str = Field(default="", max_length=40_000)
    contextual_meaning: str = Field(default="", max_length=1_000)
    context_explanation: str = Field(default="", max_length=1_000)
    dictionary: list[dict[str, Any]] = Field(default_factory=list, max_length=12)
    force: bool = False


class InterpretationWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["interpret"] = "interpret"
    scope: Literal["word", "sentence", "paragraph", "chapter", "selection"]
    document_id: str
    paragraph_start: int = Field(ge=0)
    paragraph_end: int = Field(ge=0)
    selection_start: int | None = Field(default=None, ge=0)
    selection_end: int | None = Field(default=None, ge=0)
    source_text: str = Field(min_length=1, max_length=20_000)
    context_text: str = Field(default="", max_length=20_000)
    target_language: str = Field(default="zh-CN", max_length=20)
    coverage_complete: bool = True
    coverage_label: str = Field(default="", max_length=240)
    force: bool = False


def _leap_demo_seed() -> dict[str, Any]:
    """Small, self-authored/public-domain-based demo; never mixed with user records."""
    now = _now()
    materials = [
        {"id": "demo-leap-m1", "title": "洞穴与看见", "author": "公共领域思想改写", "kind": "哲学", "progress_percent": 75,
         "paragraphs": ["有人终身只看见墙上的影子，于是把影子的次序当作世界本身。", "当一个人转身面对光，最初的感受往往不是自由，而是刺痛与怀疑。", "教育并非把视力放进眼睛，而是帮助目光改变方向。"]},
        {"id": "demo-leap-m2", "title": "江河与选择", "author": "自建示例文本", "kind": "文学", "progress_percent": 45,
         "paragraphs": ["江河看似沿着既定河床前进，却在每一次转弯处重新解释地形。", "选择不是摆脱所有限制，而是在限制中发现仍然可以改变的方向。", "抵达之后，人们常把漫长的犹豫压缩成一句必然。"]},
        {"id": "demo-leap-m3", "title": "制度与记忆", "author": "自建示例文本", "kind": "历史", "progress_percent": 30,
         "paragraphs": ["制度保存集体经验，也可能把旧问题冻结为新的常识。", "一项规则的寿命，往往比创造它的危机更长。", "理解历史需要同时看见决定、后果与后来者的重新叙述。"]},
        {"id": "demo-leap-m4", "title": "公共生活的尺度", "author": "自建示例文本", "kind": "社会思想", "progress_percent": 20,
         "paragraphs": ["公共讨论的困难，不只来自意见不同，也来自人们衡量证据的尺度不同。", "共识不是取消分歧，而是让分歧能够被共同检验。", "当判断可以回到证据，争论才可能积累而不是循环。"]},
    ]
    excerpts = [
        {"id": "demo-leap-e1", "material_id": "demo-leap-m1", "paragraph_position": 1, "start_offset": 0, "end_offset": 32, "quote": materials[0]["paragraphs"][1], "created_at": now},
        {"id": "demo-leap-e2", "material_id": "demo-leap-m2", "paragraph_position": 1, "start_offset": 0, "end_offset": 32, "quote": materials[1]["paragraphs"][1], "created_at": now},
        {"id": "demo-leap-e3", "material_id": "demo-leap-m3", "paragraph_position": 0, "start_offset": 0, "end_offset": 28, "quote": materials[2]["paragraphs"][0], "created_at": now},
        {"id": "demo-leap-e4", "material_id": "demo-leap-m4", "paragraph_position": 2, "start_offset": 0, "end_offset": 28, "quote": materials[3]["paragraphs"][2], "created_at": now},
    ]
    return {"demo": True, "version": 1, "materials": materials, "excerpts": excerpts,
            "notes": [{"id": "demo-leap-n1", "material_id": "demo-leap-m1", "excerpt_id": "demo-leap-e1", "topic": "自由", "content": "改变理解方向会先带来不适；自由包含重新学习如何看。", "created_at": "2026-08-12T08:00:00+00:00"},
                      {"id": "demo-leap-n2", "material_id": "demo-leap-m2", "excerpt_id": "demo-leap-e2", "topic": "自由", "content": "第二次理解：自由并非没有边界，而是在边界内仍能修正路径。", "created_at": "2026-09-10T08:00:00+00:00"},
                      {"id": "demo-leap-n3", "material_id": "demo-leap-m4", "excerpt_id": "demo-leap-e4", "topic": "证据", "content": "证据的价值在于让分歧可以累积和修正。", "created_at": "2026-09-18T08:00:00+00:00"}],
            "wormholes": [{"id": "demo-leap-w1", "left_excerpt_id": "demo-leap-e1", "right_excerpt_id": "demo-leap-e2", "relation_type": "延伸", "reflection": "转身面对光描述认知的改变；江河转向描述行动的改变。两者都把自由理解为方向的重新选择。", "created_at": now},
                          {"id": "demo-leap-w2", "left_excerpt_id": "demo-leap-e3", "right_excerpt_id": "demo-leap-e4", "relation_type": "质疑", "reflection": "制度能够保存经验，但只有证据可被重新检验时，保存才不会变成冻结。", "created_at": now}],
            "clashes": [{"id": "demo-leap-c1", "title": "稳定是否必然限制自由", "viewpoint_a": "稳定的制度让行动获得可预期边界。", "viewpoint_b": "稳定也可能让历史偶然被误认为永恒常识。", "evidence_a_excerpt_id": "demo-leap-e2", "evidence_b_excerpt_id": "demo-leap-e3", "common_ground": "双方都承认边界影响选择。", "disagreement": "边界首先是能力条件还是认知束缚。", "judgment": "应区分可检验、可修订的边界与拒绝证据的边界。", "unresolved_questions": "谁有权启动修订？修订成本由谁承担？", "created_at": now}],
            "updated_at": now}


class LeapRepository:
    def __init__(self, connect: Callable[[], Any]):
        self.connect = connect

    def create_material(self, user_id: int, payload: MaterialCreate) -> dict:
        encoded = payload.text.encode("utf-8")
        if len(encoded) > MAX_MATERIAL_BYTES:
            raise ValueError("材料不能超过 2 MB。")
        paragraphs = _paragraphs(payload.text)
        material_id, now = str(uuid.uuid4()), _now()
        content_hash = hashlib.sha256("\x00".join(paragraphs).encode("utf-8")).hexdigest()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO leap_materials
                   (id,user_id,title,author,source,tags,version,content_hash,paragraph_count,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,1,?,?,?,?)""",
                (material_id, user_id, payload.title.strip(), payload.author.strip(),
                 payload.source.strip(), json.dumps(payload.tags, ensure_ascii=False),
                 content_hash, len(paragraphs), now, now),
            )
            connection.executemany(
                """INSERT INTO leap_paragraphs
                   (material_id,material_version,position,content,chapter_position,chapter_title,stable_anchor)
                   VALUES(?,1,?,?,0,'',?)""",
                [(material_id, index, content, f"p-v1-{index:06d}") for index, content in enumerate(paragraphs)],
            )
        return self.get_material(user_id, material_id)

    def list_materials(self, user_id: int, query: str, limit: int, offset: int) -> dict:
        pattern = f"%{query.strip()}%"
        with self.connect() as connection:
            count = connection.execute(
                """SELECT COUNT(*) AS total FROM leap_materials
                   WHERE user_id=? AND (?='' OR title LIKE ? OR author LIKE ? OR tags LIKE ?)""",
                (user_id, query.strip(), pattern, pattern, pattern),
            ).fetchone()["total"]
            rows = connection.execute(
                """SELECT m.id,m.title,m.author,m.source,m.tags,m.version,m.content_hash,m.paragraph_count,
                          m.created_at,m.updated_at,COALESCE(p.progress_percent,0) AS progress_percent,
                          COALESCE(p.paragraph_position,0) AS paragraph_position
                   FROM leap_materials m LEFT JOIN leap_reading_progress p
                     ON p.material_id=m.id AND p.user_id=m.user_id
                   WHERE m.user_id=? AND (?='' OR m.title LIKE ? OR m.author LIKE ? OR m.tags LIKE ?)
                   ORDER BY m.updated_at DESC LIMIT ? OFFSET ?""",
                (user_id, query.strip(), pattern, pattern, pattern, limit, offset),
            ).fetchall()
        items = [_row(item) for item in rows]
        for item in items:
            item["tags"] = _json(item["tags"], [])
        return {"items": items, "total": count, "limit": limit, "offset": offset}

    def get_material(self, user_id: int, material_id: str) -> dict:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT m.id,m.title,m.author,m.source,m.tags,m.version,m.content_hash,m.paragraph_count,
                          m.created_at,m.updated_at,COALESCE(p.progress_percent,0) AS progress_percent,
                          COALESCE(p.paragraph_position,0) AS paragraph_position,
                          COALESCE(p.character_offset,0) AS character_offset
                   FROM leap_materials m LEFT JOIN leap_reading_progress p
                     ON p.material_id=m.id AND p.user_id=m.user_id
                   WHERE m.id=? AND m.user_id=?""",
                (material_id, user_id),
            ).fetchone()
        if not row:
            raise KeyError("材料不存在。")
        item = _row(row)
        item["tags"] = _json(item["tags"], [])
        with self.connect() as connection:
            source = connection.execute(
                """SELECT provider,source_item_id,source_name,source_url,source_format,language,edition,translator,
                          licensing_note,fetched_at,source_version,source_hash,status
                   FROM leap_library_imports WHERE material_id=? AND user_id=? AND status='success'
                   ORDER BY finished_at DESC LIMIT 1""", (material_id, user_id),
            ).fetchone()
            chapters = connection.execute(
                """SELECT position,title,stable_anchor,start_paragraph,end_paragraph FROM leap_chapters
                   WHERE material_id=? AND material_version=? ORDER BY position LIMIT 500""",
                (material_id, item["version"]),
            ).fetchall()
        item["library_source"] = _row(source) if source else None
        item["chapters"] = [_row(chapter) for chapter in chapters]
        return item

    def paragraphs(self, user_id: int, material_id: str, offset: int, limit: int) -> dict:
        material = self.get_material(user_id, material_id)
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT position,content,chapter_position,chapter_title,stable_anchor FROM leap_paragraphs
                   WHERE material_id=? AND material_version=? AND position>=?
                   ORDER BY position LIMIT ?""",
                (material_id, material["version"], offset, limit),
            ).fetchall()
        return {"material": material, "paragraphs": [_row(item) for item in rows],
                "offset": offset, "limit": limit}

    def chapter_content(self, user_id: int, material_id: str, chapter_position: int,
                        max_characters: int = 16_000) -> dict:
        material = self.get_material(user_id, material_id)
        chapters = material.get("chapters") or []
        chapter = next((item for item in chapters if int(item["position"]) == chapter_position), None)
        if not chapter:
            raise KeyError("章节不存在。")
        start, end = int(chapter["start_paragraph"]), int(chapter["end_paragraph"])
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT position,content,chapter_position,chapter_title,stable_anchor
                   FROM leap_paragraphs WHERE material_id=? AND material_version=?
                   AND position>=? AND position<=? ORDER BY position LIMIT 300""",
                (material_id, material["version"], start, end),
            ).fetchall()
        included, used = [], 0
        for raw in rows:
            row = _row(raw)
            addition = len(row["content"]) + (2 if included else 0)
            if included and used + addition > max_characters:
                break
            included.append(row); used += addition
        if not included:
            raise ValueError("章节没有可读取的正文。")
        complete = len(included) == end - start + 1
        return {
            "chapter": chapter,
            "source_text": "\n\n".join(item["content"] for item in included),
            "paragraph_start": included[0]["position"],
            "paragraph_end": included[-1]["position"],
            "paragraph_count": len(included),
            "total_paragraphs": end - start + 1,
            "coverage_complete": complete,
            "coverage_label": (
                f"完整章节 · 第{start + 1}–{end + 1}段" if complete
                else f"当前仅解读已加载部分 · 第{start + 1}–{included[-1]['position'] + 1}段 / 全章{end - start + 1}段"
            ),
        }

    def update_progress(self, user_id: int, material_id: str, payload: ProgressUpdate) -> dict:
        material = self.get_material(user_id, material_id)
        if payload.paragraph_position >= max(1, material["paragraph_count"]):
            raise ValueError("阅读位置超出材料范围。")
        percent = min(100, round((payload.paragraph_position + 1) * 100 / max(1, material["paragraph_count"])))
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO leap_reading_progress
                   (user_id,material_id,material_version,paragraph_position,character_offset,progress_percent,updated_at)
                   VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,material_id) DO UPDATE SET
                   material_version=excluded.material_version,
                   paragraph_position=excluded.paragraph_position,
                   character_offset=excluded.character_offset,
                   progress_percent=excluded.progress_percent,updated_at=excluded.updated_at""",
                (user_id, material_id, material["version"], payload.paragraph_position,
                 payload.character_offset, percent, _now()),
            )
        return {"paragraph_position": payload.paragraph_position,
                "character_offset": payload.character_offset, "progress_percent": percent}

    def create_excerpt(self, user_id: int, payload: ExcerptCreate) -> dict:
        material = self.get_material(user_id, payload.material_id)
        if payload.material_version != material["version"]:
            raise ValueError("材料版本已变化，请重新定位原文后再摘录。")
        with self.connect() as connection:
            paragraph = connection.execute(
                """SELECT content FROM leap_paragraphs WHERE material_id=?
                   AND material_version=? AND position=?""",
                (payload.material_id, payload.material_version, payload.paragraph_position),
            ).fetchone()
            if not paragraph:
                raise ValueError("找不到对应原文段落。")
            content = paragraph["content"]
            if payload.quote not in content:
                raise ValueError("摘录内容与对应原文段落不一致。")
            excerpt_id, now = str(uuid.uuid4()), _now()
            connection.execute(
                """INSERT INTO leap_excerpts
                   (id,user_id,material_id,material_version,paragraph_position,start_offset,end_offset,quote,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (excerpt_id, user_id, payload.material_id, payload.material_version,
                 payload.paragraph_position, payload.start_offset, payload.end_offset,
                 payload.quote, now, now),
            )
        return self.get_excerpt(user_id, excerpt_id)

    def get_excerpt(self, user_id: int, excerpt_id: str) -> dict:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT e.*,m.title AS material_title,m.author AS material_author,
                          CASE WHEN e.material_version=m.version THEN 0 ELSE 1 END AS reference_stale
                   FROM leap_excerpts e JOIN leap_materials m ON m.id=e.material_id
                   WHERE e.id=? AND e.user_id=? AND m.user_id=?""",
                (excerpt_id, user_id, user_id),
            ).fetchone()
        if not row:
            raise KeyError("摘录不存在。")
        return _row(row)

    def list_excerpts(self, user_id: int, material_id: str | None = None) -> list[dict]:
        where, params = "e.user_id=?", [user_id]
        if material_id:
            where += " AND e.material_id=?"
            params.append(material_id)
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT e.*,m.title AS material_title,m.author AS material_author,
                            CASE WHEN e.material_version=m.version THEN 0 ELSE 1 END AS reference_stale
                     FROM leap_excerpts e JOIN leap_materials m ON m.id=e.material_id
                     WHERE {where} ORDER BY e.updated_at DESC LIMIT 200""", params,
            ).fetchall()
        return [_row(item) for item in rows]

    def search_evidence(self, user_id: int, query: str, limit: int) -> list[dict]:
        pattern = f"%{query.strip()}%"
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT p.material_id,p.material_version,p.position,p.content,
                          m.title AS material_title,m.author AS material_author
                   FROM leap_paragraphs p JOIN leap_materials m ON m.id=p.material_id
                   WHERE m.user_id=? AND p.material_version=m.version AND p.content LIKE ?
                   ORDER BY m.updated_at DESC,p.position LIMIT ?""",
                (user_id, pattern, limit),
            ).fetchall()
        return [_row(item) for item in rows]

    def write_note(self, user_id: int, payload: NoteWrite, note_id: str | None = None) -> dict:
        if payload.material_id:
            self.get_material(user_id, payload.material_id)
        if payload.excerpt_id:
            excerpt = self.get_excerpt(user_id, payload.excerpt_id)
            if payload.material_id and excerpt["material_id"] != payload.material_id:
                raise ValueError("笔记材料与摘录来源不一致。")
        now, note_id = _now(), note_id or str(uuid.uuid4())
        with self.connect() as connection:
            if connection.execute("SELECT 1 FROM leap_notes WHERE id=? AND user_id=?", (note_id, user_id)).fetchone():
                connection.execute(
                    """UPDATE leap_notes SET material_id=?,excerpt_id=?,topic=?,content=?,updated_at=?
                       WHERE id=? AND user_id=?""",
                    (payload.material_id, payload.excerpt_id, payload.topic.strip(), payload.content.strip(),
                     now, note_id, user_id),
                )
            else:
                connection.execute(
                    """INSERT INTO leap_notes(id,user_id,material_id,excerpt_id,topic,content,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (note_id, user_id, payload.material_id, payload.excerpt_id,
                     payload.topic.strip(), payload.content.strip(), now, now),
                )
            row = connection.execute("SELECT * FROM leap_notes WHERE id=? AND user_id=?", (note_id, user_id)).fetchone()
        return _row(row)

    def list_notes(self, user_id: int, query: str) -> list[dict]:
        pattern = f"%{query.strip()}%"
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT n.*,m.title AS material_title FROM leap_notes n
                   LEFT JOIN leap_materials m ON m.id=n.material_id
                   WHERE n.user_id=? AND (?='' OR n.topic LIKE ? OR n.content LIKE ?)
                   ORDER BY n.updated_at DESC LIMIT 200""",
                (user_id, query.strip(), pattern, pattern),
            ).fetchall()
        return [_row(item) for item in rows]

    def write_wormhole(self, user_id: int, payload: WormholeWrite, item_id: str | None = None) -> dict:
        self.get_excerpt(user_id, payload.left_excerpt_id)
        self.get_excerpt(user_id, payload.right_excerpt_id)
        item_id, now = item_id or str(uuid.uuid4()), _now()
        with self.connect() as connection:
            exists = connection.execute("SELECT 1 FROM leap_wormholes WHERE id=? AND user_id=?", (item_id, user_id)).fetchone()
            if exists:
                connection.execute(
                    """UPDATE leap_wormholes SET left_excerpt_id=?,right_excerpt_id=?,relation_type=?,
                       reflection=?,updated_at=? WHERE id=? AND user_id=?""",
                    (payload.left_excerpt_id, payload.right_excerpt_id, payload.relation_type,
                     payload.reflection.strip(), now, item_id, user_id),
                )
            else:
                connection.execute(
                    """INSERT INTO leap_wormholes
                       (id,user_id,left_excerpt_id,right_excerpt_id,relation_type,reflection,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (item_id, user_id, payload.left_excerpt_id, payload.right_excerpt_id,
                     payload.relation_type, payload.reflection.strip(), now, now),
                )
        return self.get_wormhole(user_id, item_id)

    def get_wormhole(self, user_id: int, item_id: str) -> dict:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT w.*,le.quote AS left_quote,lm.title AS left_material,
                          re.quote AS right_quote,rm.title AS right_material
                   FROM leap_wormholes w
                   JOIN leap_excerpts le ON le.id=w.left_excerpt_id
                   JOIN leap_materials lm ON lm.id=le.material_id
                   JOIN leap_excerpts re ON re.id=w.right_excerpt_id
                   JOIN leap_materials rm ON rm.id=re.material_id
                   WHERE w.id=? AND w.user_id=? AND le.user_id=? AND re.user_id=?""",
                (item_id, user_id, user_id, user_id),
            ).fetchone()
        if not row:
            raise KeyError("思想虫洞不存在。")
        return _row(row)

    def list_wormholes(self, user_id: int) -> list[dict]:
        with self.connect() as connection:
            ids = connection.execute(
                "SELECT id FROM leap_wormholes WHERE user_id=? ORDER BY updated_at DESC LIMIT 100",
                (user_id,),
            ).fetchall()
        return [self.get_wormhole(user_id, item["id"]) for item in ids]

    def write_clash(self, user_id: int, payload: ClashWrite, item_id: str | None = None) -> dict:
        for excerpt_id in (payload.evidence_a_excerpt_id, payload.evidence_b_excerpt_id):
            if excerpt_id:
                self.get_excerpt(user_id, excerpt_id)
        item_id, now = item_id or str(uuid.uuid4()), _now()
        values = (payload.title.strip(), payload.viewpoint_a.strip(), payload.viewpoint_b.strip(),
                  payload.evidence_a_excerpt_id, payload.evidence_b_excerpt_id,
                  payload.common_ground.strip(), payload.disagreement.strip(), payload.judgment.strip(),
                  payload.unresolved_questions.strip(), now)
        with self.connect() as connection:
            if connection.execute("SELECT 1 FROM leap_clash_cards WHERE id=? AND user_id=?", (item_id, user_id)).fetchone():
                connection.execute(
                    """UPDATE leap_clash_cards SET title=?,viewpoint_a=?,viewpoint_b=?,
                       evidence_a_excerpt_id=?,evidence_b_excerpt_id=?,common_ground=?,disagreement=?,judgment=?,unresolved_questions=?,updated_at=?
                       WHERE id=? AND user_id=?""", values + (item_id, user_id),
                )
            else:
                connection.execute(
                    """INSERT INTO leap_clash_cards
                       (id,user_id,title,viewpoint_a,viewpoint_b,evidence_a_excerpt_id,
                        evidence_b_excerpt_id,common_ground,disagreement,judgment,unresolved_questions,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (item_id, user_id) + values[:-1] + (now, now),
                )
            row = connection.execute("SELECT * FROM leap_clash_cards WHERE id=? AND user_id=?", (item_id, user_id)).fetchone()
        return _row(row)

    def list_clashes(self, user_id: int) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM leap_clash_cards WHERE user_id=? ORDER BY updated_at DESC LIMIT 100",
                (user_id,),
            ).fetchall()
        return [_row(item) for item in rows]

    def universe(self, user_id: int) -> dict:
        with self.connect() as connection:
            materials = connection.execute(
                "SELECT id,title,author FROM leap_materials WHERE user_id=? ORDER BY updated_at DESC LIMIT 100",
                (user_id,),
            ).fetchall()
            excerpts = connection.execute(
                "SELECT id,material_id,substr(quote,1,140) AS label FROM leap_excerpts WHERE user_id=? LIMIT 200",
                (user_id,),
            ).fetchall()
            wormholes = connection.execute(
                "SELECT id,left_excerpt_id,right_excerpt_id,relation_type FROM leap_wormholes WHERE user_id=? LIMIT 200",
                (user_id,),
            ).fetchall()
        nodes = ([{"id": f"material:{x['id']}", "kind": "material", "label": x["title"], "target_id": x["id"]} for x in materials]
                 + [{"id": f"excerpt:{x['id']}", "kind": "excerpt", "label": x["label"], "target_id": x["id"]} for x in excerpts])
        edges = [{"source": f"material:{x['material_id']}", "target": f"excerpt:{x['id']}", "kind": "contains"} for x in excerpts]
        edges += [{"source": f"excerpt:{x['left_excerpt_id']}", "target": f"excerpt:{x['right_excerpt_id']}", "kind": x["relation_type"], "target_id": x["id"]} for x in wormholes]
        return {"nodes": nodes, "edges": edges}

    def home(self, user_id: int) -> dict:
        """A bounded, useful landing view instead of a wall of zero counters."""
        with self.connect() as connection:
            reading = connection.execute(
                """SELECT m.id,m.title,m.author,m.tags,m.paragraph_count,
                          COALESCE(p.progress_percent,0) AS progress_percent,
                          COALESCE(p.paragraph_position,0) AS paragraph_position,m.updated_at
                   FROM leap_materials m LEFT JOIN leap_reading_progress p
                     ON p.material_id=m.id AND p.user_id=m.user_id
                   WHERE m.user_id=? ORDER BY COALESCE(p.updated_at,m.updated_at) DESC LIMIT 5""", (user_id,),
            ).fetchall()
        recent_excerpts = self.list_excerpts(user_id)[:6]
        recent_notes = self.list_notes(user_id, "")[:6]
        recent_wormholes = self.list_wormholes(user_id)[:4]
        recent_clashes = self.list_clashes(user_id)[:3]
        topics: dict[str, dict[str, Any]] = {}
        for note in recent_notes:
            topic = (note.get("topic") or "未命名主题").strip()
            current = topics.setdefault(topic, {"topic": topic, "count": 0, "latest_at": note["updated_at"]})
            current["count"] += 1
        return {"reading": [_row(item) | {"tags": _json(item["tags"], [])} for item in reading],
                "recent_excerpts": recent_excerpts, "recent_notes": recent_notes,
                "recent_wormholes": recent_wormholes, "recent_clashes": recent_clashes,
                "active_topics": list(topics.values()),
                "next_action": "添加第一份材料" if not reading else "继续阅读并选取一条证据" if not recent_excerpts else "连接两条证据" if not recent_wormholes else "继续探索你的思想关系"}

    def timeline(self, user_id: int) -> list[dict]:
        notes = self.list_notes(user_id, "")
        grouped: dict[str, list[dict]] = {}
        for note in notes:
            topic = (note.get("topic") or "未命名主题").strip()
            grouped.setdefault(topic, []).append({"id": note["id"], "content": note["content"],
                                                   "material_title": note.get("material_title") or "独立笔记",
                                                   "created_at": note["created_at"]})
        return [{"topic": topic, "entries": sorted(entries, key=lambda x: x["created_at"])}
                for topic, entries in grouped.items()]

    def search_all(self, user_id: int, query: str, limit: int) -> list[dict]:
        query, limit = query.strip(), min(limit, 60)
        if not query:
            return []
        pattern = f"%{query}%"
        with self.connect() as connection:
            material = connection.execute(
                "SELECT id,title AS label,author AS context FROM leap_materials WHERE user_id=? AND (title LIKE ? OR author LIKE ? OR tags LIKE ?) LIMIT ?",
                (user_id, pattern, pattern, pattern, limit),
            ).fetchall()
            paragraph = connection.execute(
                """SELECT p.material_id AS id,substr(p.content,1,240) AS label,m.title AS context,p.position
                   FROM leap_paragraphs p JOIN leap_materials m ON m.id=p.material_id
                   WHERE m.user_id=? AND p.material_version=m.version AND p.content LIKE ? LIMIT ?""",
                (user_id, pattern, limit),
            ).fetchall()
            excerpt = connection.execute(
                """SELECT e.id,e.quote AS label,m.title AS context,e.material_id,e.paragraph_position AS position
                   FROM leap_excerpts e JOIN leap_materials m ON m.id=e.material_id
                   WHERE e.user_id=? AND e.quote LIKE ? LIMIT ?""", (user_id, pattern, limit),
            ).fetchall()
            note = connection.execute(
                "SELECT id,content AS label,topic AS context,material_id FROM leap_notes WHERE user_id=? AND (topic LIKE ? OR content LIKE ?) LIMIT ?",
                (user_id, pattern, pattern, limit),
            ).fetchall()
            wormhole = connection.execute(
                "SELECT id,reflection AS label,relation_type AS context FROM leap_wormholes WHERE user_id=? AND reflection LIKE ? LIMIT ?",
                (user_id, pattern, limit),
            ).fetchall()
            clash = connection.execute(
                "SELECT id,title AS label,judgment AS context FROM leap_clash_cards WHERE user_id=? AND (title LIKE ? OR viewpoint_a LIKE ? OR viewpoint_b LIKE ? OR judgment LIKE ?) LIMIT ?",
                (user_id, pattern, pattern, pattern, pattern, limit),
            ).fetchall()
        result = ([{"kind": "material", **_row(x)} for x in material]
                  + [{"kind": "paragraph", **_row(x)} for x in paragraph]
                  + [{"kind": "excerpt", **_row(x)} for x in excerpt]
                  + [{"kind": "note", **_row(x)} for x in note]
                  + [{"kind": "wormhole", **_row(x)} for x in wormhole]
                  + [{"kind": "clash", **_row(x)} for x in clash])
        return result[:limit]

    def _save_demo(self, user_id: int, state: dict[str, Any]) -> dict[str, Any]:
        state["updated_at"] = _now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO leap_demo_workspaces(user_id,state_json,updated_at) VALUES(?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET state_json=excluded.state_json,updated_at=excluded.updated_at""",
                (user_id, json.dumps(state, ensure_ascii=False), state["updated_at"]),
            )
        return self.demo(user_id)

    def demo(self, user_id: int) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT state_json,updated_at FROM leap_demo_workspaces WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            return {"loaded": False, "demo": True}
        state = _json(row["state_json"], {})
        material_map = {item["id"]: item for item in state.get("materials", [])}
        excerpt_map = {item["id"]: item for item in state.get("excerpts", [])}
        for excerpt in state.get("excerpts", []):
            excerpt["material_title"] = material_map.get(excerpt["material_id"], {}).get("title", "未知材料")
        for note in state.get("notes", []):
            note["material_title"] = material_map.get(note.get("material_id"), {}).get("title", "独立笔记")
        for item in state.get("wormholes", []):
            left, right = excerpt_map.get(item["left_excerpt_id"], {}), excerpt_map.get(item["right_excerpt_id"], {})
            item.update(left_quote=left.get("quote", ""), right_quote=right.get("quote", ""),
                        left_material=material_map.get(left.get("material_id"), {}).get("title", ""),
                        right_material=material_map.get(right.get("material_id"), {}).get("title", ""))
        timeline: dict[str, list[dict]] = {}
        for note in state.get("notes", []):
            timeline.setdefault(note.get("topic") or "未命名主题", []).append(note)
        nodes = ([{"id": f"material:{m['id']}", "kind": "material", "label": m["title"], "target_id": m["id"]} for m in state.get("materials", [])]
                 + [{"id": f"excerpt:{e['id']}", "kind": "excerpt", "label": e["quote"][:60], "target_id": e["id"]} for e in state.get("excerpts", [])]
                 + [{"id": f"topic:{topic}", "kind": "theme", "label": topic, "target_id": topic} for topic in timeline])
        edges = ([{"source": f"material:{e['material_id']}", "target": f"excerpt:{e['id']}", "kind": "contains"} for e in state.get("excerpts", [])]
                 + [{"source": f"excerpt:{w['left_excerpt_id']}", "target": f"excerpt:{w['right_excerpt_id']}", "kind": w["relation_type"], "target_id": w["id"]} for w in state.get("wormholes", [])])
        state.update(loaded=True, timeline=[{"topic": k, "entries": sorted(v, key=lambda x: x["created_at"])} for k, v in timeline.items()], universe={"nodes": nodes, "edges": edges})
        return state

    def reset_demo(self, user_id: int) -> dict[str, Any]:
        return self._save_demo(user_id, _leap_demo_seed())

    def demo_action(self, user_id: int, action: DemoAction) -> dict[str, Any]:
        current = self.demo(user_id)
        if not current.get("loaded"):
            current = self.reset_demo(user_id)
        state = {key: current[key] for key in ("demo", "version", "materials", "excerpts", "notes", "wormholes", "clashes")}
        p, now = action.payload, _now()
        materials = {x["id"]: x for x in state["materials"]}
        excerpts = {x["id"]: x for x in state["excerpts"]}
        if action.action == "progress":
            materials[p["material_id"]]["progress_percent"] = max(0, min(100, int(p.get("progress_percent", 0))))
        elif action.action == "excerpt":
            material = materials.get(p.get("material_id"))
            position = int(p.get("paragraph_position", -1))
            if not material or position < 0 or position >= len(material["paragraphs"]):
                raise ValueError("演示材料原文位置无效。")
            quote = str(p.get("quote", "")).strip()
            if not quote or quote not in material["paragraphs"][position]:
                raise ValueError("摘录必须来自所选原文。")
            state["excerpts"].append({"id": f"demo-leap-e-{uuid.uuid4().hex[:10]}", "material_id": material["id"], "paragraph_position": position, "start_offset": int(p.get("start_offset", 0)), "end_offset": int(p.get("end_offset", len(quote))), "quote": quote, "created_at": now})
        elif action.action == "note":
            state["notes"].append({"id": f"demo-leap-n-{uuid.uuid4().hex[:10]}", "material_id": p.get("material_id"), "excerpt_id": p.get("excerpt_id"), "topic": str(p.get("topic", "")).strip(), "content": str(p.get("content", "")).strip(), "created_at": now})
        elif action.action == "wormhole":
            if p.get("left_excerpt_id") not in excerpts or p.get("right_excerpt_id") not in excerpts or p.get("left_excerpt_id") == p.get("right_excerpt_id"):
                raise ValueError("思想虫洞需要两条不同的真实摘录。")
            state["wormholes"].append({"id": f"demo-leap-w-{uuid.uuid4().hex[:10]}", "left_excerpt_id": p["left_excerpt_id"], "right_excerpt_id": p["right_excerpt_id"], "relation_type": p.get("relation_type", "延伸"), "reflection": str(p.get("reflection", "")).strip(), "created_at": now})
        elif action.action == "clash":
            state["clashes"].append({"id": f"demo-leap-c-{uuid.uuid4().hex[:10]}", "title": str(p.get("title", "思想对撞")).strip(), "viewpoint_a": str(p.get("viewpoint_a", "")).strip(), "viewpoint_b": str(p.get("viewpoint_b", "")).strip(), "evidence_a_excerpt_id": p.get("evidence_a_excerpt_id"), "evidence_b_excerpt_id": p.get("evidence_b_excerpt_id"), "common_ground": str(p.get("common_ground", "")).strip(), "disagreement": str(p.get("disagreement", "")).strip(), "judgment": str(p.get("judgment", "")).strip(), "unresolved_questions": str(p.get("unresolved_questions", "")).strip(), "created_at": now})
        return self._save_demo(user_id, state)


def create_leap_router(
    connect: Callable[[], Any],
    current_user: Callable[..., dict],
    interpretation_runner: Callable[[int, str, str, int], dict[str, Any]] | None = None,
    interpretation_model: str = "unconfigured",
    consented_user: Callable[..., dict] | None = None,
) -> APIRouter:
    router, repo = APIRouter(prefix="/api/leap", tags=["跃迁域"]), LeapRepository(connect)
    library = PublicLibraryService(connect)
    translations = TranslationService(connect)
    interpretations = InterpretationService(
        connect, interpretation_runner, provider_model=interpretation_model,
    )
    User = Annotated[dict, Depends(current_user)]
    InterpretationUser = Annotated[dict, Depends(consented_user or current_user)]

    def safe(call):
        try:
            return call()
        except KeyError as exc:
            raise HTTPException(404, detail=str(exc).strip("'")) from exc
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from exc

    @router.get("/capabilities")
    def capabilities(_: User):
        return {"model_calls": interpretations.capabilities()["available"], "embeddings": False,
                "supported_imports": ["text", "txt", "md", "reviewed-public-domain-epub"],
                "max_bytes": MAX_MATERIAL_BYTES,
                "features": {"公共领域书库": "已实现", "按需英中翻译": "本地可用；Azure可选", "内容解读": "按需使用冰焰AI；未配置时明确停用", "思想虫洞": "已实现", "思想宇宙": "实验性", "思想对撞": "已实现",
                             "认知时间轴": "规划中", "时空透镜": "规划中", "反事实阅读": "规划中",
                             "跨时空思想会谈": "规划中", "记忆桥": "规划中", "跨域迁移": "规划中",
                             "个人认知光谱": "规划中", "认知暗物质": "规划中", "思想引力": "规划中"}}

    @router.get("/reading-assistant/capabilities")
    def reading_assistant_capabilities(_: User):
        return {"action": "interpret", **interpretations.capabilities()}

    @router.post("/reading-assistant/interpret")
    def interpret(payload: InterpretationWrite, user: InterpretationUser):
        if not interpretations.capabilities()["available"]:
            raise HTTPException(status_code=503, detail="内容解读暂不可用：冰焰AI服务未配置。")
        return safe(lambda: interpretations.interpret(user["id"], payload.model_dump()))

    @router.get("/home")
    def home(user: User): return repo.home(user["id"])

    @router.get("/search")
    def search(user: User, q: str = Query(min_length=1, max_length=120), limit: int = Query(40, ge=1, le=60)):
        return repo.search_all(user["id"], q, limit)

    @router.get("/timeline")
    def timeline(user: User): return repo.timeline(user["id"])

    @router.get("/demo")
    def demo(user: User): return repo.demo(user["id"])

    @router.post("/demo/load")
    def demo_load(user: User):
        current = repo.demo(user["id"])
        return current if current.get("loaded") else repo.reset_demo(user["id"])

    @router.post("/demo/reset")
    def demo_reset(user: User): return repo.reset_demo(user["id"])

    @router.post("/demo/action")
    def demo_action(payload: DemoAction, user: User): return safe(lambda: repo.demo_action(user["id"], payload))

    @router.get("/library/search")
    def library_search(user: User, q: str = Query("", max_length=120),
                       provider: Literal["all", "gutenberg", "standard_ebooks", "ctext"] = "all"):
        return safe(lambda: library.search(q, provider))

    @router.get("/library/imports")
    def library_imports(user: User, limit: int = Query(30, ge=1, le=100)):
        return library.imports(user["id"], limit)

    @router.post("/library/imports", status_code=status.HTTP_202_ACCEPTED)
    def library_import(background: BackgroundTasks, user: User, provider: str, source_item_id: str):
        created = safe(lambda: library.create_import(user["id"], provider, source_item_id))
        if created["status"] == "queued":
            background.add_task(library.run, user["id"], created["id"])
        return created

    @router.get("/library/imports/{run_id}")
    def library_import_status(run_id: str, user: User):
        return safe(lambda: library.import_status(user["id"], run_id))

    @router.get("/translation/providers")
    def translation_providers(_: User):
        return translations.providers()

    @router.get("/translation/cache")
    def translation_cache(
        user: User, document_id: str, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=100),
        provider: Literal["browser_local", "azure_translator"] = "browser_local",
        provider_model: str = Query("chrome-built-in-translator", min_length=1, max_length=120),
        target_language: str = Query("zh-Hans", min_length=2, max_length=20),
    ):
        return safe(lambda: translations.window_cache(
            user["id"], document_id, offset, limit, provider, provider_model, target_language,
        ))

    @router.put("/translation/cache")
    def save_translation_cache(payload: TranslationWrite, user: User):
        return safe(lambda: translations.save_browser_local(user["id"], payload.model_dump()))

    @router.post("/translation/translate")
    def translate(payload: TranslationWrite, user: User):
        return safe(lambda: translations.translate_azure(user["id"], payload.model_dump()))

    @router.post("/translation/lookup")
    def translate_word(payload: TranslationWrite, user: User):
        return safe(lambda: translations.translate_azure(user["id"], payload.model_dump(), lookup=True))

    @router.get("/translation/stats")
    def translation_stats(user: User):
        return translations.stats(user["id"])

    @router.get("/materials")
    def materials(user: User, q: str = Query("", max_length=120), limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0)):
        return repo.list_materials(user["id"], q, limit, offset)

    @router.post("/materials", status_code=status.HTTP_201_CREATED)
    def create_material(payload: MaterialCreate, user: User):
        return safe(lambda: repo.create_material(user["id"], payload))

    @router.post("/materials/import", status_code=status.HTTP_201_CREATED)
    async def import_material(user: User, file: UploadFile = File(...), title: str | None = None,
                              author: str = "", source: str = "", tags: str = ""):
        suffix = (file.filename or "").lower().rsplit(".", 1)[-1]
        if suffix not in {"txt", "md"}:
            raise HTTPException(415, detail="跃迁域首版只支持 TXT 和 Markdown 文件。")
        data = await file.read(MAX_MATERIAL_BYTES + 1)
        if len(data) > MAX_MATERIAL_BYTES:
            raise HTTPException(413, detail="材料不能超过 2 MB。")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(400, detail="文件必须使用 UTF-8 编码。") from exc
        payload = MaterialCreate(title=(title or file.filename or "未命名材料"), author=author,
                                 source=source, tags=[x.strip() for x in tags.split(",") if x.strip()], text=text)
        return safe(lambda: repo.create_material(user["id"], payload))

    @router.get("/materials/{material_id}")
    def material(material_id: str, user: User):
        return safe(lambda: repo.get_material(user["id"], material_id))

    @router.get("/materials/{material_id}/paragraphs")
    def paragraphs(material_id: str, user: User, offset: int = Query(0, ge=0), limit: int = Query(40, ge=1, le=100)):
        return safe(lambda: repo.paragraphs(user["id"], material_id, offset, limit))

    @router.get("/materials/{material_id}/chapters/{chapter_position}/content")
    def chapter_content(material_id: str, chapter_position: int, user: User):
        return safe(lambda: repo.chapter_content(user["id"], material_id, chapter_position))

    @router.put("/materials/{material_id}/progress")
    def progress(material_id: str, payload: ProgressUpdate, user: User):
        return safe(lambda: repo.update_progress(user["id"], material_id, payload))

    @router.get("/evidence/search")
    def evidence_search(user: User, q: str = Query(min_length=1, max_length=120), limit: int = Query(30, ge=1, le=100)):
        return repo.search_evidence(user["id"], q, limit)

    @router.get("/excerpts")
    def excerpts(user: User, material_id: str | None = None):
        return safe(lambda: repo.list_excerpts(user["id"], material_id))

    @router.post("/excerpts", status_code=201)
    def create_excerpt(payload: ExcerptCreate, user: User):
        return safe(lambda: repo.create_excerpt(user["id"], payload))

    @router.get("/notes")
    def notes(user: User, q: str = Query("", max_length=120)):
        return repo.list_notes(user["id"], q)

    @router.post("/notes", status_code=201)
    def create_note(payload: NoteWrite, user: User):
        return safe(lambda: repo.write_note(user["id"], payload))

    @router.put("/notes/{note_id}")
    def update_note(note_id: str, payload: NoteWrite, user: User):
        return safe(lambda: repo.write_note(user["id"], payload, note_id))

    @router.get("/wormholes")
    def wormholes(user: User):
        return repo.list_wormholes(user["id"])

    @router.post("/wormholes", status_code=201)
    def create_wormhole(payload: WormholeWrite, user: User):
        return safe(lambda: repo.write_wormhole(user["id"], payload))

    @router.put("/wormholes/{item_id}")
    def update_wormhole(item_id: str, payload: WormholeWrite, user: User):
        return safe(lambda: repo.write_wormhole(user["id"], payload, item_id))

    @router.delete("/wormholes/{item_id}", status_code=204)
    def delete_wormhole(item_id: str, user: User):
        with connect() as connection:
            cursor = connection.execute("DELETE FROM leap_wormholes WHERE id=? AND user_id=?", (item_id, user["id"]))
        if not cursor.rowcount:
            raise HTTPException(404, detail="思想虫洞不存在。")

    @router.get("/clashes")
    def clashes(user: User):
        return repo.list_clashes(user["id"])

    @router.post("/clashes", status_code=201)
    def create_clash(payload: ClashWrite, user: User):
        return safe(lambda: repo.write_clash(user["id"], payload))

    @router.put("/clashes/{item_id}")
    def update_clash(item_id: str, payload: ClashWrite, user: User):
        return safe(lambda: repo.write_clash(user["id"], payload, item_id))

    @router.get("/universe")
    def universe(user: User):
        return repo.universe(user["id"])

    return router

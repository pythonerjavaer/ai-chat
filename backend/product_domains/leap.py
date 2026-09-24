"""跃迁域: evidence-based reading without model or embedding calls."""

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Callable, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
                disagreement TEXT NOT NULL DEFAULT '',
                judgment TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(evidence_a_excerpt_id) REFERENCES leap_excerpts(id) ON DELETE SET NULL,
                FOREIGN KEY(evidence_b_excerpt_id) REFERENCES leap_excerpts(id) ON DELETE SET NULL
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
    relation_type: Literal["相似", "对立", "延伸", "应用"]
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
    disagreement: str = Field(default="", max_length=10_000)
    judgment: str = Field(default="", max_length=10_000)


class LeapRepository:
    def __init__(self, connect: Callable[[], Any]):
        self.connect = connect

    def create_material(self, user_id: int, payload: MaterialCreate) -> dict:
        encoded = payload.text.encode("utf-8")
        if len(encoded) > MAX_MATERIAL_BYTES:
            raise ValueError("材料不能超过 2 MB。")
        paragraphs = _paragraphs(payload.text)
        material_id, now = str(uuid.uuid4()), _now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO leap_materials
                   (id,user_id,title,author,source,tags,version,paragraph_count,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,1,?,?,?)""",
                (material_id, user_id, payload.title.strip(), payload.author.strip(),
                 payload.source.strip(), json.dumps(payload.tags, ensure_ascii=False),
                 len(paragraphs), now, now),
            )
            connection.executemany(
                "INSERT INTO leap_paragraphs(material_id,material_version,position,content) VALUES(?,1,?,?)",
                [(material_id, index, content) for index, content in enumerate(paragraphs)],
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
                """SELECT m.id,m.title,m.author,m.source,m.tags,m.version,m.paragraph_count,
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
                """SELECT m.id,m.title,m.author,m.source,m.tags,m.version,m.paragraph_count,
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
        return item

    def paragraphs(self, user_id: int, material_id: str, offset: int, limit: int) -> dict:
        material = self.get_material(user_id, material_id)
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT position,content FROM leap_paragraphs
                   WHERE material_id=? AND material_version=? AND position>=?
                   ORDER BY position LIMIT ?""",
                (material_id, material["version"], offset, limit),
            ).fetchall()
        return {"material": material, "paragraphs": [_row(item) for item in rows],
                "offset": offset, "limit": limit}

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
                  payload.disagreement.strip(), payload.judgment.strip(), now)
        with self.connect() as connection:
            if connection.execute("SELECT 1 FROM leap_clash_cards WHERE id=? AND user_id=?", (item_id, user_id)).fetchone():
                connection.execute(
                    """UPDATE leap_clash_cards SET title=?,viewpoint_a=?,viewpoint_b=?,
                       evidence_a_excerpt_id=?,evidence_b_excerpt_id=?,disagreement=?,judgment=?,updated_at=?
                       WHERE id=? AND user_id=?""", values + (item_id, user_id),
                )
            else:
                connection.execute(
                    """INSERT INTO leap_clash_cards
                       (id,user_id,title,viewpoint_a,viewpoint_b,evidence_a_excerpt_id,
                        evidence_b_excerpt_id,disagreement,judgment,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
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


def create_leap_router(connect: Callable[[], Any], current_user: Callable[..., dict]) -> APIRouter:
    router, repo = APIRouter(prefix="/api/leap", tags=["跃迁域"]), LeapRepository(connect)
    User = Annotated[dict, Depends(current_user)]

    def safe(call):
        try:
            return call()
        except KeyError as exc:
            raise HTTPException(404, detail=str(exc).strip("'")) from exc
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from exc

    @router.get("/capabilities")
    def capabilities(_: User):
        return {"model_calls": False, "embeddings": False, "supported_imports": ["text", "txt", "md"],
                "max_bytes": MAX_MATERIAL_BYTES,
                "features": {"思想虫洞": "已实现", "思想宇宙": "实验性", "思想对撞": "已实现",
                             "认知时间轴": "规划中", "时空透镜": "规划中", "反事实阅读": "规划中",
                             "跨时空思想会谈": "规划中", "记忆桥": "规划中", "跨域迁移": "规划中",
                             "个人认知光谱": "规划中", "认知暗物质": "规划中", "思想引力": "规划中"}}

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

"""Portable hybrid knowledge index for Leap Realm materials.

The index is additive: original materials, paragraphs, excerpts, translations
and interpretations remain authoritative. Embeddings are stored as JSON so the
same schema works with the application's SQLite and PostgreSQL adapters.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from fastapi import HTTPException


DIMENSIONS = 192
MAX_CHUNK_CHARACTERS = 900
CHUNK_OVERLAP_CHARACTERS = 120
MAX_SEARCH_CANDIDATES = 5_000
TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")
MARKDOWN_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_knowledge_schema(connect: Callable[[], Any]) -> None:
    with connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS leap_knowledge_chunks (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                material_id TEXT NOT NULL,
                material_version INTEGER NOT NULL,
                chunk_position INTEGER NOT NULL,
                paragraph_start INTEGER NOT NULL,
                paragraph_end INTEGER NOT NULL,
                heading_path TEXT NOT NULL DEFAULT '[]',
                stable_anchor TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                embedding TEXT NOT NULL,
                embedding_model TEXT NOT NULL DEFAULT 'local-hashing-v1',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, material_id, material_version, chunk_position),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(material_id) REFERENCES leap_materials(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_leap_knowledge_user_material
                ON leap_knowledge_chunks(user_id, material_id, material_version, chunk_position);
            CREATE INDEX IF NOT EXISTS idx_leap_knowledge_hash
                ON leap_knowledge_chunks(user_id, content_hash);
            """
        )


def embed_text(text: str, dimensions: int = DIMENSIONS) -> list[float]:
    """Deterministic local embedding used when no paid embedding API is needed."""
    vector = [0.0] * dimensions
    for token in TOKEN_RE.findall(text.casefold()):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest, "big") % dimensions
        vector[index] += 1.0 if digest[0] & 1 else -1.0
    norm = math.sqrt(sum(item * item for item in vector)) or 1.0
    return [item / norm for item in vector]


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return 0.0
    return sum(a * b for a, b in zip(left, right))


def _split_long(value: str, size: int, overlap: int) -> Iterable[str]:
    start = 0
    while start < len(value):
        end = min(len(value), start + size)
        if end < len(value):
            boundary = max(value.rfind("。", start, end), value.rfind(". ", start, end), value.rfind("\n", start, end))
            if boundary > start + size // 2:
                end = boundary + 1
        yield value[start:end].strip()
        if end >= len(value):
            break
        start = max(start + 1, end - overlap)


def build_chunks(paragraphs: Iterable[dict[str, Any]], max_characters: int = MAX_CHUNK_CHARACTERS) -> list[dict[str, Any]]:
    """Group paragraphs while retaining chapter/Markdown hierarchy and anchors."""
    chunks: list[dict[str, Any]] = []
    heading_stack: list[str] = []
    buffer: list[dict[str, Any]] = []
    buffer_heading: tuple[str, ...] = ()

    def flush() -> None:
        nonlocal buffer
        if not buffer:
            return
        content = "\n\n".join(item["content"] for item in buffer).strip()
        chunks.append({
            "paragraph_start": int(buffer[0]["position"]),
            "paragraph_end": int(buffer[-1]["position"]),
            "heading_path": list(buffer_heading),
            "stable_anchor": str(buffer[0].get("stable_anchor") or f"p-{buffer[0]['position']}"),
            "content": content,
        })
        buffer = []

    for raw in paragraphs:
        row = dict(raw)
        content = str(row.get("content") or "").strip()
        if not content:
            continue
        chapter = str(row.get("chapter_title") or "").strip()
        if chapter:
            heading_stack = [chapter]
        heading = MARKDOWN_HEADING_RE.match(content)
        if heading:
            flush()
            level = len(heading.group(1))
            heading_stack[:] = heading_stack[: level - 1]
            heading_stack.append(heading.group(2).strip())
            continue
        row_heading = tuple(heading_stack)
        if len(content) > max_characters:
            flush()
            for part in _split_long(content, max_characters, CHUNK_OVERLAP_CHARACTERS):
                chunks.append({
                    "paragraph_start": int(row["position"]),
                    "paragraph_end": int(row["position"]),
                    "heading_path": list(row_heading),
                    "stable_anchor": str(row.get("stable_anchor") or f"p-{row['position']}"),
                    "content": part,
                })
            continue
        candidate_length = sum(len(item["content"]) for item in buffer) + len(content) + 2 * len(buffer)
        if buffer and (candidate_length > max_characters or row_heading != buffer_heading):
            flush()
        if not buffer:
            buffer_heading = row_heading
        buffer.append(row)
    flush()
    return chunks


class LeapKnowledgeService:
    def __init__(
        self,
        connect: Callable[[], Any],
        generator: Callable[[int, str, str, int], dict[str, Any]] | None = None,
        embedder: Callable[[int, list[str]], list[list[float]]] | None = None,
        embedding_model: str = "local-hashing-v1",
        document_archive: Callable[[int, dict[str, Any], list[dict[str, Any]]], dict[str, Any]] | None = None,
        vector_connect: Callable[[], Any] | None = None,
    ):
        self.connect = connect
        self.generator = generator
        self.embedder = embedder
        self.embedding_model = embedding_model if embedder else "local-hashing-v1"
        self.document_archive = document_archive
        self.vector_connect = vector_connect

    @staticmethod
    def _vector_store_schema(connection: Any) -> str | None:
        if isinstance(connection, sqlite3.Connection):
            return None
        row = connection.execute(
            """SELECT n.nspname AS schema_name FROM pg_catalog.pg_type t
               JOIN pg_catalog.pg_namespace n ON n.oid=t.typnamespace
               WHERE t.typname='vector' ORDER BY n.nspname LIMIT 1"""
        ).fetchone()
        return str(row["schema_name"]) if row else None

    def _sync_vector_store(self, user_id: int, material: Any, rows: list[Any]) -> dict[str, Any]:
        if not self.vector_connect:
            return {"status": "not_configured", "indexed_chunks": 0}
        try:
            items = [dict(row) for row in rows]
            dimensions = len(json.loads(items[0]["embedding"])) if items else DIMENSIONS
            if dimensions < 1 or dimensions > 2_000:
                raise ValueError("unsupported vector dimensions")
            vectors = [json.loads(item["embedding"]) for item in items]
            if any(len(vector) != dimensions or any(not math.isfinite(float(v)) for v in vector) for vector in vectors):
                raise ValueError("invalid vector payload")
            with self.vector_connect() as connection:
                vector_schema = self._vector_store_schema(connection)
                if not vector_schema:
                    raise RuntimeError("pgvector extension is unavailable")
                safe_schema = vector_schema.replace('"', '""')
                column = f"embedding_vector_{dimensions}"
                vector_type = f'"{safe_schema}".vector({dimensions})'
                operator_schema = f'"{safe_schema}"'
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS leap_vector_chunks (
                         id TEXT PRIMARY KEY, user_id BIGINT NOT NULL, material_id TEXT NOT NULL,
                         material_version INTEGER NOT NULL, chunk_position INTEGER NOT NULL,
                         paragraph_start INTEGER NOT NULL, paragraph_end INTEGER NOT NULL,
                         heading_path TEXT NOT NULL, stable_anchor TEXT NOT NULL,
                         material_title TEXT NOT NULL, material_author TEXT NOT NULL, material_source TEXT NOT NULL,
                         content TEXT NOT NULL, content_hash TEXT NOT NULL, embedding_model TEXT NOT NULL,
                         created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                         UNIQUE(user_id,material_id,material_version,chunk_position,embedding_model)
                       )"""
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_leap_vector_user_material ON leap_vector_chunks(user_id,material_id,material_version,embedding_model)"
                )
                connection.execute(
                    f"ALTER TABLE leap_vector_chunks ADD COLUMN IF NOT EXISTS {column} {vector_type}"
                )
                connection.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_leap_vector_hnsw_{dimensions} ON leap_vector_chunks USING hnsw ({column} {operator_schema}.vector_cosine_ops)"
                )
                connection.execute(
                    "DELETE FROM leap_vector_chunks WHERE user_id=? AND material_id=?",
                    (user_id, material["id"]),
                )
                if items:
                    connection.executemany(
                        f"""INSERT INTO leap_vector_chunks
                            (id,user_id,material_id,material_version,chunk_position,paragraph_start,paragraph_end,
                             heading_path,stable_anchor,material_title,material_author,material_source,content,
                             content_hash,embedding_model,created_at,updated_at,{column})
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?::{vector_type})""",
                        [
                            (item["id"], user_id, material["id"], item["material_version"], item["chunk_position"],
                             item["paragraph_start"], item["paragraph_end"], item["heading_path"], item["stable_anchor"],
                             material["title"], material["author"], material["source"], item["content"], item["content_hash"],
                             item["embedding_model"], item["created_at"], item["updated_at"],
                             "[" + ",".join(str(float(value)) for value in vector) + "]")
                            for item, vector in zip(items, vectors)
                        ],
                    )
            return {"status": "ready", "indexed_chunks": len(items), "dimensions": dimensions}
        except Exception as exc:
            return {"status": "unavailable", "indexed_chunks": 0, "error_type": type(exc).__name__}

    def vector_store_status(self, user_id: int | None = None) -> dict[str, Any]:
        if not self.vector_connect:
            return {"status": "not_configured", "provider": "PostgreSQL/pgvector"}
        try:
            with self.vector_connect() as connection:
                vector_schema = self._vector_store_schema(connection)
                row = connection.execute(
                    "SELECT to_regclass('leap_vector_chunks') AS index_table"
                ).fetchone()
                indexed_chunks = 0
                if row and row["index_table"] and user_id is not None:
                    count = connection.execute(
                        "SELECT COUNT(*) AS chunk_count FROM leap_vector_chunks WHERE user_id=?",
                        (user_id,),
                    ).fetchone()
                    indexed_chunks = int(count["chunk_count"] or 0)
            return {"status": "ready" if vector_schema else "extension_missing", "provider": "PostgreSQL/pgvector",
                    "extension_schema": vector_schema, "index_initialized": bool(row and row["index_table"]),
                    "indexed_chunks": indexed_chunks}
        except Exception as exc:
            return {"status": "unavailable", "provider": "PostgreSQL/pgvector", "error_type": type(exc).__name__}

    def _search_vector_store(self, user_id: int, query_embedding: list[float], model: str,
                             material_ids: list[str] | None, limit: int) -> list[Any]:
        if not self.vector_connect:
            raise RuntimeError("vector store not configured")
        dimensions = len(query_embedding)
        if dimensions < 1 or dimensions > 2_000:
            raise ValueError("unsupported vector dimensions")
        with self.vector_connect() as connection:
            vector_schema = self._vector_store_schema(connection)
            if not vector_schema:
                raise RuntimeError("pgvector extension is unavailable")
            safe_schema = vector_schema.replace('"', '""')
            column = f"embedding_vector_{dimensions}"
            vector_type = f'"{safe_schema}".vector({dimensions})'
            operator = f'OPERATOR("{safe_schema}".<=>)'
            vector_text = "[" + ",".join(str(float(value)) for value in query_embedding) + "]"
            where = "user_id=? AND embedding_model=?"
            filter_params: list[Any] = [user_id, model]
            if material_ids:
                where += " AND material_id IN (" + ",".join("?" for _ in material_ids) + ")"
                filter_params.extend(material_ids)
            limit_value = max(1, min(limit * 5, MAX_SEARCH_CANDIDATES))
            return connection.execute(
                f"""SELECT *,1-({column} {operator} ?::{vector_type}) AS vector_similarity
                    FROM leap_vector_chunks WHERE {where}
                    ORDER BY {column} {operator} ?::{vector_type} LIMIT ?""",
                [vector_text, *filter_params, vector_text, limit_value],
            ).fetchall()

    def _archive_edition(self, user_id: int, material: Any, rows: list[Any]) -> dict[str, Any] | None:
        if not self.document_archive:
            return None
        chunks = [{"chunk_position": row["chunk_position"], "paragraph_start": row["paragraph_start"],
                   "paragraph_end": row["paragraph_end"], "heading_path": json.loads(row["heading_path"] or "[]"),
                   "stable_anchor": row["stable_anchor"], "content": row["content"],
                   "embedding": json.loads(row["embedding"] or "[]"), "embedding_model": row["embedding_model"]}
                  for row in rows]
        try:
            return self.document_archive(user_id, {"id": material["id"], "title": material["title"],
                                                   "version": material["version"], "content_hash": material["content_hash"]}, chunks)
        except Exception as exc:
            # PostgreSQL remains authoritative and a Mongo outage must not erase
            # or prevent local RAG results; the response makes the failure visible.
            return {"status": "unavailable", "error_type": type(exc).__name__, "archived_chunks": 0}

    def _embeddings(self, texts: list[str], *, semantic: bool, user_id: int) -> list[list[float]]:
        if not semantic or self.embedder is None:
            return [embed_text(text) for text in texts]
        vectors = self.embedder(user_id, texts)
        if len(vectors) != len(texts) or any(not vector for vector in vectors):
            raise ValueError("语义嵌入服务返回的数据不完整。")
        dimensions = len(vectors[0])
        if any(len(vector) != dimensions for vector in vectors):
            raise ValueError("语义嵌入服务返回的向量维度不一致。")
        return vectors

    @staticmethod
    def _pgvector_schema(connection: Any) -> str | None:
        if isinstance(connection, sqlite3.Connection):
            return None
        try:
            row = connection.execute(
                """SELECT n.nspname AS schema_name FROM pg_catalog.pg_type t
                   JOIN pg_catalog.pg_namespace n ON n.oid=t.typnamespace
                   WHERE t.typname='vector' ORDER BY n.nspname LIMIT 1"""
            ).fetchone()
            return str(row["schema_name"]) if row else None
        except Exception:
            return None

    def index_material(self, user_id: int, material_id: str, *, force: bool = False, semantic: bool = False) -> dict[str, Any]:
        model = self.embedding_model if semantic and self.embedder else "local-hashing-v1"
        already_indexed: list[Any] | None = None
        archived = None
        with self.connect() as connection:
            material = connection.execute(
                "SELECT id,title,author,source,version,content_hash FROM leap_materials WHERE id=? AND user_id=?",
                (material_id, user_id),
            ).fetchone()
            if not material:
                raise KeyError("材料不存在。")
            existing_rows = connection.execute(
                "SELECT * FROM leap_knowledge_chunks WHERE user_id=? AND material_id=? AND material_version=? AND embedding_model=? ORDER BY chunk_position",
                (user_id, material_id, material["version"], model),
            ).fetchall()
            if existing_rows and not force:
                already_indexed = existing_rows
            if already_indexed is None:
                paragraphs = connection.execute(
                    """SELECT position,content,chapter_title,stable_anchor FROM leap_paragraphs
                       WHERE material_id=? AND material_version=? ORDER BY position""",
                    (material_id, material["version"]),
                ).fetchall()
                chunks = build_chunks(paragraphs)
                vectors = self._embeddings([
                    " > ".join(chunk["heading_path"]) + "\n" + chunk["content"]
                    for chunk in chunks
                ], semantic=semantic, user_id=user_id)
                now = _now()
                connection.execute("DELETE FROM leap_knowledge_chunks WHERE user_id=? AND material_id=?", (user_id, material_id))
                pgvector_schema = self._pgvector_schema(connection)
                use_pgvector = pgvector_schema is not None
            if already_indexed is None and use_pgvector:
                # The managed PostgreSQL deployment may enable pgvector in
                # advance. Keep the JSON representation as a portable fallback.
                dimensions = len(vectors[0]) if vectors else DIMENSIONS
                vector_column = f"embedding_vector_{dimensions}"
                vector_type = '"' + pgvector_schema.replace('"', '""') + f'".vector({dimensions})'
                vector_ops = '"' + pgvector_schema.replace('"', '""') + '".vector_cosine_ops'
                connection.execute(
                    f"ALTER TABLE leap_knowledge_chunks ADD COLUMN IF NOT EXISTS {vector_column} {vector_type}"
                )
                if dimensions <= 2_000:
                    connection.execute(
                        f"CREATE INDEX IF NOT EXISTS idx_leap_knowledge_embedding_hnsw_{dimensions} "
                        f"ON leap_knowledge_chunks USING hnsw ({vector_column} {vector_ops})"
                    )
            if already_indexed is None and use_pgvector:
                connection.executemany(
                    f"""INSERT INTO leap_knowledge_chunks
                       (id,user_id,material_id,material_version,chunk_position,paragraph_start,paragraph_end,
                        heading_path,stable_anchor,content,content_hash,embedding,embedding_model,created_at,updated_at,{vector_column})
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?::{vector_type})""",
                    [
                        (str(uuid.uuid4()), user_id, material_id, material["version"], position,
                         chunk["paragraph_start"], chunk["paragraph_end"], json.dumps(chunk["heading_path"], ensure_ascii=False),
                         chunk["stable_anchor"], chunk["content"], hashlib.sha256(chunk["content"].encode()).hexdigest(),
                         json.dumps(vector), model, now, now,
                         "[" + ",".join(str(value) for value in vector) + "]")
                        for position, (chunk, vector) in enumerate(zip(chunks, vectors))
                    ],
                )
            elif already_indexed is None:
                connection.executemany(
                    """INSERT INTO leap_knowledge_chunks
                       (id,user_id,material_id,material_version,chunk_position,paragraph_start,paragraph_end,
                        heading_path,stable_anchor,content,content_hash,embedding,embedding_model,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [
                        (str(uuid.uuid4()), user_id, material_id, material["version"], position,
                         chunk["paragraph_start"], chunk["paragraph_end"], json.dumps(chunk["heading_path"], ensure_ascii=False),
                         chunk["stable_anchor"], chunk["content"], hashlib.sha256(chunk["content"].encode()).hexdigest(),
                         json.dumps(vector), model, now, now)
                        for position, (chunk, vector) in enumerate(zip(chunks, vectors))
                    ],
                )
            indexed_rows = already_indexed or connection.execute(
                "SELECT * FROM leap_knowledge_chunks WHERE user_id=? AND material_id=? AND material_version=? AND embedding_model=? ORDER BY chunk_position",
                (user_id, material_id, material["version"], model),
            ).fetchall()
        archived = archived or self._archive_edition(user_id, material, indexed_rows)
        vector_store = self._sync_vector_store(user_id, material, indexed_rows)
        return {"material_id": material_id, "material_version": material["version"], "chunk_count": len(indexed_rows), "indexed": already_indexed is None, "mongo_archive": archived,
                "vector_store": vector_store}

    def sync_user(self, user_id: int, *, force: bool = False, limit: int = 100, semantic: bool = False) -> dict[str, Any]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM leap_materials WHERE user_id=? ORDER BY updated_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        results = [self.index_material(user_id, row["id"], force=force, semantic=semantic) for row in rows]
        return {"material_count": len(results), "chunk_count": sum(item["chunk_count"] for item in results), "indexed_count": sum(bool(item["indexed"]) for item in results), "embedding_model": self.embedding_model if semantic and self.embedder else "local-hashing-v1", "items": results}

    def search(self, user_id: int, query: str, *, limit: int = 6, material_ids: list[str] | None = None, semantic: bool = False) -> list[dict[str, Any]]:
        with self.connect() as connection:
            current_materials = connection.execute(
                "SELECT id,version FROM leap_materials WHERE user_id=?", (user_id,),
            ).fetchall()
        active_versions = {str(row["id"]): int(row["version"]) for row in current_materials}
        active_material_ids = list(active_versions)
        if material_ids:
            active_material_ids = [material_id for material_id in material_ids if material_id in active_versions]
        if not active_material_ids:
            return []
        # A question scoped to one document must not index every other book in
        # the account. Long collections can exhaust provider quota before the
        # requested evidence is even searched.
        for material_id in active_material_ids:
            self.index_material(user_id, material_id, semantic=semantic)
        where = "c.user_id=? AND c.material_version=m.version"
        params: list[Any] = [user_id]
        if material_ids:
            placeholders = ",".join("?" for _ in material_ids)
            where += f" AND c.material_id IN ({placeholders})"
            params.extend(material_ids)
        params.append(self.embedding_model if semantic and self.embedder else "local-hashing-v1")
        params.append(MAX_SEARCH_CANDIDATES)
        query_embedding = self._embeddings([query], semantic=semantic, user_id=user_id)[0]
        rows = None
        if self.vector_connect:
            try:
                rows = self._search_vector_store(
                    user_id, query_embedding, self.embedding_model if semantic and self.embedder else "local-hashing-v1",
                    active_material_ids, limit,
                )
            except Exception:
                # Keep retrieval available from the portable local chunk store;
                # storage status reports pgvector availability independently.
                rows = None
        if rows is None:
            with self.connect() as connection:
                pgvector_schema = self._pgvector_schema(connection)
                vector_search = pgvector_schema is not None
                model_clause = " AND c.embedding_model=?"
                where += model_clause
                if vector_search:
                    has_indexed_rows = connection.execute(
                        "SELECT 1 FROM leap_knowledge_chunks WHERE user_id=? AND embedding_model=? LIMIT 1",
                        (user_id, self.embedding_model if semantic and self.embedder else "local-hashing-v1"),
                    ).fetchone()
                    if not has_indexed_rows:
                        return []
                    dimensions = len(query_embedding)
                    vector_column = f"embedding_vector_{dimensions}"
                    escaped_vector_schema = pgvector_schema.replace('"', '""')
                    vector_type = '"' + escaped_vector_schema + f'".vector({dimensions})'
                    vector_distance = f'OPERATOR("{escaped_vector_schema}".<=>)'
                    vector_text = "[" + ",".join(str(value) for value in query_embedding) + "]"
                    rows = connection.execute(
                        f"""SELECT c.*,m.title AS material_title,m.author AS material_author,m.source AS material_source,
                                   1-(c.{vector_column} {vector_distance} ?::{vector_type}) AS vector_similarity
                            FROM leap_knowledge_chunks c JOIN leap_materials m ON m.id=c.material_id
                            WHERE {where} ORDER BY c.{vector_column} {vector_distance} ?::{vector_type} LIMIT ?""",
                        [vector_text, *params[:-1], vector_text, min(MAX_SEARCH_CANDIDATES, max(limit * 5, limit))],
                    ).fetchall()
                else:
                    rows = connection.execute(
                        f"""SELECT c.*,m.title AS material_title,m.author AS material_author,m.source AS material_source
                            FROM leap_knowledge_chunks c JOIN leap_materials m ON m.id=c.material_id
                            WHERE {where} ORDER BY m.updated_at DESC,c.chunk_position LIMIT ?""",
                        params,
                    ).fetchall()
        query_terms = Counter(TOKEN_RE.findall(query.casefold()))
        results = []
        for raw in rows:
            row = dict(raw)
            # SQLite is the authoritative material/version ledger. This keeps
            # deleted or superseded chunks out of results if a mirror refresh
            # was interrupted.
            if active_versions.get(str(row["material_id"])) != int(row["material_version"]):
                continue
            vector_score = ((float(row["vector_similarity"]) + 1) / 2 if "vector_similarity" in row
                            else (_cosine(query_embedding, json.loads(row["embedding"])) + 1) / 2)
            content_terms = Counter(TOKEN_RE.findall(row["content"].casefold()))
            overlap = sum(min(count, content_terms[term]) for term, count in query_terms.items())
            lexical_score = overlap / math.sqrt(max(1, sum(query_terms.values()) * sum(content_terms.values())))
            score = 0.75 * vector_score + 0.25 * lexical_score
            results.append({
                "chunk_id": row["id"], "material_id": row["material_id"], "material_version": row["material_version"],
                "material_title": row["material_title"], "material_author": row["material_author"], "material_source": row["material_source"],
                "paragraph_start": row["paragraph_start"], "paragraph_end": row["paragraph_end"],
                "heading_path": json.loads(row["heading_path"] or "[]"), "stable_anchor": row["stable_anchor"],
                "content": row["content"], "score": round(score, 6), "vector_score": round(vector_score, 6),
                "lexical_score": round(lexical_score, 6),
            })
        return sorted(results, key=lambda item: (-item["score"], item["chunk_id"]))[:limit]

    def answer(self, user_id: int, question: str, *, limit: int = 6, material_ids: list[str] | None = None, target_language: str = "zh-CN", generate: bool = True) -> dict[str, Any]:
        retrieval_mode = "semantic_vector" if self.embedder else "local_vector"
        try:
            evidence = self.search(user_id, question, limit=limit, material_ids=material_ids, semantic=True)
        except HTTPException as exc:
            if exc.status_code not in (429, 502, 503):
                raise
            # A paid embedding quota is not a reason to hide available source
            # paragraphs. The local deterministic vector is clearly labelled
            # as a fallback, not passed off as a semantic model.
            evidence = self.search(user_id, question, limit=limit, material_ids=material_ids, semantic=False)
            retrieval_mode = "local_vector_fallback"
        citations = [
            {key: item[key] for key in ("chunk_id", "material_id", "material_title", "paragraph_start", "paragraph_end", "heading_path", "stable_anchor", "score")}
            for item in evidence
        ]
        if not evidence:
            return {"answer": "知识库中没有找到足够证据。", "mode": "no_evidence", "retrieval_mode": retrieval_mode, "citations": []}
        if generate and self.generator is not None:
            context = "\n\n".join(f"[{index}] {item['material_title']} · 第{item['paragraph_start'] + 1}–{item['paragraph_end'] + 1}段\n{item['content']}" for index, item in enumerate(evidence, 1))
            system = "你是跃迁域知识库助手。只能依据提供的证据回答；使用[n]引用；证据不足时明确说明；不得虚构材料外事实。"
            prompt = f"目标语言：{target_language}\n问题：{question}\n\n证据：\n{context}"
            try:
                generated = self.generator(user_id, system, prompt, 900)
                answer = str(generated.get("text") or "").strip()
                if answer:
                    return {"answer": answer, "mode": "llm_rag", "retrieval_mode": retrieval_mode, "citations": citations, "usage": generated.get("usage", {})}
            except HTTPException as exc:
                if exc.status_code not in (429, 502, 503):
                    raise
                generation_status = "模型服务不可用，已返回可溯源原文摘录；这不是模型生成的回答。"
            else:
                generation_status = "模型未返回有效答案，已返回可溯源原文摘录；这不是模型生成的回答。"
        else:
            generation_status = "模型服务尚未配置，已返回可溯源原文摘录；这不是模型生成的回答。" if generate else None
        extracts = "\n".join(f"[{index}] {item['content'][:320]}" for index, item in enumerate(evidence[:3], 1))
        return {"answer": "根据知识库中最相关的原文：\n" + extracts, "mode": "extractive_rag", "retrieval_mode": retrieval_mode, "generation_status": generation_status, "citations": citations}

"""Trusted public-domain discovery and import for Leap.

Only official Project Gutenberg and Standard Ebooks endpoints are downloaded.
Chinese Text Project entries remain catalog-only because its unauthenticated API
does not permit reliable whole-work export and its pages must not be scraped.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import uuid
import zipfile
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import quote, urljoin, urlparse
from xml.etree import ElementTree as ET

import httpx


USER_AGENT = "Frostfire Public Domain Library/1.0 (+https://frostfire-ai.onrender.com)"
MAX_SOURCE_BYTES = 12 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 48 * 1024 * 1024
MAX_TEXT_BYTES = 8 * 1024 * 1024
MAX_LIBRARY_PARAGRAPHS = 20_000

GUTENBERG_IDS: dict[str, dict[str, str]] = {
    "1497": {"title": "The Republic", "author": "Plato", "edition": "Benjamin Jowett translation", "translator": "Benjamin Jowett"},
    "2680": {"title": "Meditations", "author": "Marcus Aurelius", "edition": "George Long translation", "translator": "George Long"},
    "59": {"title": "Discourse on the Method", "author": "René Descartes", "edition": "John Veitch translation", "translator": "John Veitch"},
    "1998": {"title": "Thus Spake Zarathustra", "author": "Friedrich Nietzsche", "edition": "Thomas Common translation", "translator": "Thomas Common"},
    "3300": {"title": "The Wealth of Nations", "author": "Adam Smith", "edition": "Project Gutenberg English edition", "translator": ""},
    "67363": {"title": "The Theory of Moral Sentiments", "author": "Adam Smith", "edition": "Project Gutenberg English edition", "translator": ""},
    "34901": {"title": "On Liberty", "author": "John Stuart Mill", "edition": "Project Gutenberg English edition", "translator": ""},
    "1232": {"title": "The Prince", "author": "Niccolò Machiavelli", "edition": "W. K. Marriott translation", "translator": "W. K. Marriott"},
    "10827": {"title": "Discourses on Livy", "author": "Niccolò Machiavelli", "edition": "Ninian Hill Thomson translation", "translator": "Ninian Hill Thomson"},
    "1342": {"title": "Pride and Prejudice", "author": "Jane Austen", "edition": "Project Gutenberg English edition", "translator": ""},
    "105": {"title": "Persuasion", "author": "Jane Austen", "edition": "Project Gutenberg English edition", "translator": ""},
    "98": {"title": "A Tale of Two Cities", "author": "Charles Dickens", "edition": "Project Gutenberg English edition", "translator": ""},
    "1228": {"title": "On the Origin of Species", "author": "Charles Darwin", "edition": "Project Gutenberg English edition", "translator": ""},
}

STANDARD_REVIEWED: dict[str, dict[str, str]] = {
    "jane-austen/pride-and-prejudice": {"title": "Pride and Prejudice", "author": "Jane Austen", "edition": "Standard Ebooks edition", "translator": ""},
    "jane-austen/persuasion": {"title": "Persuasion", "author": "Jane Austen", "edition": "Standard Ebooks edition", "translator": ""},
    "friedrich-nietzsche/thus-spake-zarathustra/thomas-common": {"title": "Thus Spake Zarathustra", "author": "Friedrich Nietzsche", "edition": "Standard Ebooks · Thomas Common translation", "translator": "Thomas Common"},
}

CTEXT_SEEDS: dict[str, dict[str, str]] = {
    "analects": {"title": "论语", "author": "孔子及其弟子（传统归属）", "source_url": "https://ctext.org/analects/zh"},
    "mengzi": {"title": "孟子", "author": "孟子及其弟子（传统归属）", "source_url": "https://ctext.org/mengzi/zh"},
    "dao-de-jing": {"title": "道德经", "author": "老子（传统归属）", "source_url": "https://ctext.org/dao-de-jing/zh"},
    "zhuangzi": {"title": "庄子", "author": "庄子及后学（传统归属）", "source_url": "https://ctext.org/zhuangzi/zh"},
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_title(value: str) -> str:
    return re.sub(r"[^a-z0-9\u3400-\u9fff]+", "", value.lower())


def init_public_library_schema(connect: Callable[[], Any]) -> None:
    with connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS leap_library_objects (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                object_key TEXT NOT NULL,
                content_type TEXT NOT NULL,
                content_encoding TEXT NOT NULL DEFAULT 'zlib',
                original_size INTEGER NOT NULL,
                stored_size INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                body BYTEA NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, object_key),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS leap_library_imports (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                source_item_id TEXT NOT NULL,
                title TEXT NOT NULL,
                author TEXT NOT NULL DEFAULT '',
                language TEXT NOT NULL DEFAULT '',
                edition TEXT NOT NULL DEFAULT '',
                translator TEXT NOT NULL DEFAULT '',
                source_name TEXT NOT NULL,
                source_url TEXT NOT NULL,
                download_url TEXT NOT NULL DEFAULT '',
                source_format TEXT NOT NULL DEFAULT '',
                licensing_note TEXT NOT NULL,
                rights_status TEXT NOT NULL,
                fetched_at TEXT,
                source_version TEXT NOT NULL DEFAULT '',
                source_hash TEXT NOT NULL DEFAULT '',
                object_id TEXT,
                material_id TEXT,
                status TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                started_at TEXT,
                finished_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(material_id) REFERENCES leap_materials(id) ON DELETE SET NULL,
                FOREIGN KEY(object_id) REFERENCES leap_library_objects(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_leap_library_imports_user_created
                ON leap_library_imports(user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_leap_library_imports_identity
                ON leap_library_imports(user_id, provider, source_item_id, status);
            CREATE TABLE IF NOT EXISTS leap_chapters (
                material_id TEXT NOT NULL,
                material_version INTEGER NOT NULL,
                position INTEGER NOT NULL,
                title TEXT NOT NULL,
                stable_anchor TEXT NOT NULL,
                start_paragraph INTEGER NOT NULL,
                end_paragraph INTEGER NOT NULL,
                PRIMARY KEY(material_id, material_version, position),
                FOREIGN KEY(material_id) REFERENCES leap_materials(id) ON DELETE CASCADE
            );
            """
        )


def seed_catalog() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source_id, item in GUTENBERG_IDS.items():
        rows.append({**item, "provider": "gutenberg", "source_item_id": source_id,
                     "language": "en", "source_name": "Project Gutenberg",
                     "source_url": f"https://www.gutenberg.org/ebooks/{source_id}",
                     "format": "EPUB", "rights_status": "auto_import",
                     "licensing_note": "Project Gutenberg OPDS marks this edition public domain in the USA; Frostfire reviewed the author/translator term for this seed edition. Check local law before use."})
    for slug, item in STANDARD_REVIEWED.items():
        rows.append({**item, "provider": "standard_ebooks", "source_item_id": slug,
                     "language": "en", "source_name": "Standard Ebooks",
                     "source_url": f"https://standardebooks.org/ebooks/{slug}",
                     "format": "EPUB", "rights_status": "auto_import",
                     "licensing_note": "Standard Ebooks production files are CC0; this reviewed seed uses an out-of-copyright work/translation. Check local law before use."})
    for source_id, item in CTEXT_SEEDS.items():
        rows.append({**item, "provider": "ctext", "source_item_id": source_id,
                     "language": "zh", "edition": "Chinese Text Project online edition", "translator": "",
                     "source_name": "Chinese Text Project", "format": "HTML/JSON",
                     "rights_status": "manual_review",
                     "licensing_note": "古典原作属于公版；CTP 未登录 API 不提供可靠整本导出。仅保存书目与官方来源链接，不自动抓取页面或现代译文。"})
    return rows


class BlockHTMLParser(HTMLParser):
    BLOCKS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"}

    def __init__(self):
        super().__init__()
        self.blocks: list[tuple[str, str]] = []
        self.current_tag = ""
        self.current: list[str] = []
        self.skip_tag = ""
        self.skip_container_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.skip_container_depth:
            if tag == "div": self.skip_container_depth += 1
            return
        attributes = dict(attrs)
        if tag == "div" and (
            attributes.get("id") in {"pg-header", "pg-footer"}
            or "pg-boilerplate" in (attributes.get("class") or "").split()
        ):
            self.skip_container_depth = 1
            return
        if self.skip_tag:
            return
        if tag in {"script", "style", "nav"}:
            self.skip_tag = tag
            return
        if tag in self.BLOCKS:
            if self.current: self._flush()
            self.current_tag = tag

    def handle_endtag(self, tag: str) -> None:
        if self.skip_container_depth:
            if tag == "div": self.skip_container_depth -= 1
            return
        if self.skip_tag:
            if tag == self.skip_tag: self.skip_tag = ""
            return
        if tag == self.current_tag: self._flush()

    def handle_data(self, data: str) -> None:
        if not self.skip_tag and not self.skip_container_depth and self.current_tag:
            self.current.append(data)

    def _flush(self) -> None:
        text = re.sub(r"\s+", " ", " ".join(self.current)).strip()
        if text: self.blocks.append((self.current_tag, text))
        self.current, self.current_tag = [], ""


@dataclass
class ParsedBook:
    paragraphs: list[dict[str, Any]]
    chapters: list[dict[str, Any]]


def parse_html_document(data: bytes, chapter_prefix: str = "chapter", *, allow_empty: bool = False) -> ParsedBook:
    parser = BlockHTMLParser()
    parser.feed(data.decode("utf-8", errors="replace"))
    paragraphs: list[dict[str, Any]] = []
    chapter = "正文"
    chapter_index = 0
    chapter_start = 0
    chapters: list[dict[str, Any]] = []
    for tag, text in parser.blocks:
        if tag.startswith("h"):
            if paragraphs and chapter_start < len(paragraphs):
                chapters.append({"position": chapter_index, "title": chapter, "stable_anchor": f"{chapter_prefix}-{chapter_index:04d}", "start_paragraph": chapter_start, "end_paragraph": len(paragraphs) - 1})
                chapter_index += 1
            chapter, chapter_start = text, len(paragraphs)
            continue
        anchor = f"{chapter_prefix}-{chapter_index:04d}-p{len(paragraphs)-chapter_start:05d}"
        paragraphs.append({"position": len(paragraphs), "content": text, "chapter_title": chapter, "chapter_position": chapter_index, "stable_anchor": anchor})
        if len(paragraphs) > MAX_LIBRARY_PARAGRAPHS: raise ValueError("书籍段落超过安全上限。")
    if paragraphs:
        chapters.append({"position": chapter_index, "title": chapter, "stable_anchor": f"{chapter_prefix}-{chapter_index:04d}", "start_paragraph": chapter_start, "end_paragraph": len(paragraphs) - 1})
    if not paragraphs and not allow_empty: raise ValueError("未能从来源文件解析正文。")
    return ParsedBook(paragraphs, chapters)


def parse_epub(data: bytes) -> ParsedBook:
    if len(data) > MAX_SOURCE_BYTES: raise ValueError("来源文件超过12 MB安全上限。")
    with zipfile.ZipFile(io.BytesIO(data)) as book:
        infos = book.infolist()
        if sum(item.file_size for item in infos) > MAX_UNCOMPRESSED_BYTES: raise ValueError("EPUB解压后超过安全上限。")
        container = ET.fromstring(book.read("META-INF/container.xml"))
        rootfile = next(node.attrib["full-path"] for node in container.iter() if node.tag.endswith("rootfile"))
        opf = ET.fromstring(book.read(rootfile)); base = rootfile.rsplit("/", 1)[0] + "/" if "/" in rootfile else ""
        manifest = {node.attrib.get("id", ""): node.attrib.get("href", "") for node in opf.iter() if node.tag.endswith("item")}
        spine = [node.attrib.get("idref", "") for node in opf.iter() if node.tag.endswith("itemref")]
        all_paragraphs: list[dict[str, Any]] = []; all_chapters: list[dict[str, Any]] = []
        for spine_index, item_id in enumerate(spine):
            href = manifest.get(item_id, "").split("#", 1)[0]
            if not href or not href.lower().endswith((".xhtml", ".html", ".htm")): continue
            path = urljoin("https://invalid/" + base, href).removeprefix("https://invalid/")
            if path not in book.namelist(): continue
            parsed = parse_html_document(book.read(path), f"s{spine_index:04d}", allow_empty=True)
            offset = len(all_paragraphs); chapter_offset = len(all_chapters)
            for row in parsed.paragraphs:
                all_paragraphs.append({**row, "position": len(all_paragraphs), "chapter_position": row["chapter_position"] + chapter_offset})
            for row in parsed.chapters:
                all_chapters.append({**row, "position": len(all_chapters), "start_paragraph": row["start_paragraph"] + offset, "end_paragraph": row["end_paragraph"] + offset})
            if len(all_paragraphs) > MAX_LIBRARY_PARAGRAPHS: raise ValueError("书籍段落超过安全上限。")
    if not all_paragraphs: raise ValueError("EPUB没有可读取正文。")
    if sum(len(x["content"].encode("utf-8")) for x in all_paragraphs) > MAX_TEXT_BYTES: raise ValueError("解析后的正文超过8 MB安全上限。")
    return ParsedBook(all_paragraphs, all_chapters)


class PublicLibraryClient:
    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout

    def _get(self, url: str, allowed_hosts: set[str]) -> tuple[bytes, dict[str, str], str]:
        with httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=self.timeout) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                host = (urlparse(str(response.url)).hostname or "").lower()
                if host not in allowed_hosts: raise ValueError("来源跳转到了未批准域名。")
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_SOURCE_BYTES: raise ValueError("来源文件超过12 MB安全上限。")
                    chunks.append(chunk)
                return b"".join(chunks), {k.lower(): v for k, v in response.headers.items()}, str(response.url)

    def gutenberg_detail(self, source_id: str) -> dict[str, Any]:
        if not source_id.isdigit(): raise ValueError("无效的Project Gutenberg书目ID。")
        raw, _, _ = self._get(f"https://www.gutenberg.org/ebooks/{source_id}.opds", {"www.gutenberg.org", "gutenberg.org"})
        root = ET.fromstring(raw); ns = {"a": "http://www.w3.org/2005/Atom", "d": "http://purl.org/dc/terms/"}
        entries = root.findall("a:entry", ns)
        if not entries: raise ValueError("Project Gutenberg未返回可用版本。")
        entry = entries[0]; links = entry.findall("a:link", ns)
        acquisitions = [(link.attrib.get("type", ""), urljoin("https://www.gutenberg.org", link.attrib.get("href", ""))) for link in links if "acquisition" in link.attrib.get("rel", "")]
        preferred = next(((kind, href) for kind, href in acquisitions if kind == "application/epub+zip"), None)
        if not preferred: preferred = next(((kind, href) for kind, href in acquisitions if kind.startswith("text/")), None)
        if not preferred: raise ValueError("该书目没有可导入的EPUB或文本版本。")
        curated = GUTENBERG_IDS.get(source_id, {})
        return {**curated, "provider": "gutenberg", "source_item_id": source_id,
                "title": curated.get("title") or entry.findtext("a:title", default="", namespaces=ns).strip(),
                "author": curated.get("author") or "; ".join(x.findtext("a:name", default="", namespaces=ns) for x in entry.findall("a:author", ns)),
                "language": entry.findtext("d:language", default="en", namespaces=ns) or "en",
                "source_name": "Project Gutenberg", "source_url": f"https://www.gutenberg.org/ebooks/{source_id}",
                "download_url": preferred[1], "content_type": preferred[0], "format": "EPUB" if "epub" in preferred[0] else "TXT",
                "rights_status": "auto_import" if source_id in GUTENBERG_IDS else "manual_review",
                "licensing_note": "Project Gutenberg OPDS: " + (entry.findtext("a:rights", default="Rights not supplied", namespaces=ns) or "Rights not supplied")}

    def search_gutenberg(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        raw, _, _ = self._get("https://www.gutenberg.org/ebooks/search.opds/?query=" + quote(query), {"www.gutenberg.org", "gutenberg.org"})
        root = ET.fromstring(raw); ns = {"a": "http://www.w3.org/2005/Atom"}; rows = []
        for entry in root.findall("a:entry", ns):
            match = re.search(r"/(\d+)\.opds$", entry.findtext("a:id", default="", namespaces=ns))
            if not match: continue
            try: rows.append(self.gutenberg_detail(match.group(1)))
            except (ValueError, httpx.HTTPError, ET.ParseError): continue
            if len(rows) >= limit: break
        return rows

    def standard_detail(self, slug: str) -> dict[str, Any]:
        if not re.fullmatch(r"[a-z0-9-]+/[a-z0-9-]+(?:/[a-z0-9-]+)?", slug): raise ValueError("无效的Standard Ebooks书目ID。")
        page_url = f"https://standardebooks.org/ebooks/{slug}"
        raw, _, _ = self._get(page_url, {"standardebooks.org"})
        root = ET.fromstring(raw); ns = {"x": "http://www.w3.org/1999/xhtml"}
        epub = next((a.attrib.get("href", "") for a in root.findall(".//x:a", ns) if a.attrib.get("href", "").endswith(".epub") and not a.attrib.get("href", "").endswith("_advanced.epub")), "")
        if not epub: raise ValueError("Standard Ebooks未提供兼容EPUB。")
        curated = STANDARD_REVIEWED.get(slug, {})
        title = curated.get("title") or next((" ".join(x.itertext()).strip() for x in root.findall('.//*[@property="schema:name"]', ns) if " ".join(x.itertext()).strip()), slug.rsplit("/", 1)[-1].replace("-", " ").title())
        return {**curated, "provider": "standard_ebooks", "source_item_id": slug, "title": title,
                "author": curated.get("author", ""), "language": "en", "source_name": "Standard Ebooks",
                "source_url": page_url, "download_url": urljoin(page_url, epub) + "?source=download",
                "content_type": "application/epub+zip", "format": "EPUB",
                "rights_status": "auto_import" if slug in STANDARD_REVIEWED else "manual_review",
                "licensing_note": "Standard Ebooks production files are CC0; underlying work must also be public domain in the user's jurisdiction."}

    def search_standard(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        raw, _, _ = self._get("https://standardebooks.org/ebooks?query=" + quote(query), {"standardebooks.org"})
        root = ET.fromstring(raw); ns = {"x": "http://www.w3.org/1999/xhtml"}; rows = []
        for item in root.findall('.//x:li[@typeof="schema:Book"]', ns):
            about = item.attrib.get("about", "")
            if not about.startswith("/ebooks/"): continue
            slug = about.removeprefix("/ebooks/")
            title = next((" ".join(x.itertext()).strip() for x in item.findall('.//*[@property="schema:name"]', ns) if " ".join(x.itertext()).strip()), "")
            author_nodes = item.findall('.//*[@property="schema:author"]//*[@property="schema:name"]', ns)
            author = " ".join(author_nodes[0].itertext()).strip() if author_nodes else ""
            curated = STANDARD_REVIEWED.get(slug, {})
            rows.append({**curated, "provider": "standard_ebooks", "source_item_id": slug,
                         "title": curated.get("title") or title, "author": curated.get("author") or author,
                         "language": "en", "source_name": "Standard Ebooks", "source_url": urljoin("https://standardebooks.org", about),
                         "format": "EPUB", "rights_status": "auto_import" if slug in STANDARD_REVIEWED else "manual_review",
                         "licensing_note": "未列入Frostfire已复核种子版本；只显示书目，需要人工确认版权与版本。"})
            if len(rows) >= limit: break
        return rows

    def download(self, item: dict[str, Any]) -> tuple[bytes, dict[str, str], str]:
        hosts = {"www.gutenberg.org", "gutenberg.org"} if item["provider"] == "gutenberg" else {"standardebooks.org"}
        return self._get(item["download_url"], hosts)


class PublicLibraryService:
    def __init__(self, connect: Callable[[], Any], client: PublicLibraryClient | None = None):
        self.connect, self.client = connect, client or PublicLibraryClient()

    def search(self, query: str, provider: str = "all") -> dict[str, Any]:
        q = query.strip(); rows = [x for x in seed_catalog() if not q or q.lower() in (x["title"] + " " + x["author"]).lower()]
        errors: list[dict[str, str]] = []
        if q:
            if provider in {"all", "gutenberg"}:
                try: rows.extend(self.client.search_gutenberg(q))
                except Exception as exc: errors.append({"provider": "gutenberg", "message": type(exc).__name__})
            if provider in {"all", "standard_ebooks"}:
                try: rows.extend(self.client.search_standard(q))
                except Exception as exc: errors.append({"provider": "standard_ebooks", "message": type(exc).__name__})
        allowed = {"gutenberg", "standard_ebooks", "ctext"}
        if provider != "all": rows = [row for row in rows if row["provider"] == provider]
        unique: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            if row.get("provider") in allowed: unique[(row["provider"], row["source_item_id"])] = row
        return {"items": list(unique.values())[:30], "provider_errors": errors,
                "policy": "只有已复核种子版本可以自动导入；其余书目仅展示并要求人工确认。"}

    def _item(self, provider: str, source_item_id: str) -> dict[str, Any]:
        if provider == "gutenberg": return self.client.gutenberg_detail(source_item_id)
        if provider == "standard_ebooks": return self.client.standard_detail(source_item_id)
        if provider == "ctext":
            item = CTEXT_SEEDS.get(source_item_id)
            if not item: raise ValueError("Chinese Text Project书目不在首批可信清单。")
            return next(x for x in seed_catalog() if x["provider"] == provider and x["source_item_id"] == source_item_id)
        raise ValueError("不支持的公共领域来源。")

    def create_import(self, user_id: int, provider: str, source_item_id: str) -> dict[str, Any]:
        item = self._item(provider, source_item_id)
        if item["rights_status"] != "auto_import": raise ValueError("该版本版权或来源状态需要人工确认，禁止自动导入全文。")
        with self.connect() as connection:
            duplicate = connection.execute(
                "SELECT id,material_id,status FROM leap_library_imports WHERE user_id=? AND provider=? AND source_item_id=? AND status='success' ORDER BY finished_at DESC LIMIT 1",
                (user_id, provider, source_item_id),
            ).fetchone()
            if duplicate: return {"id": duplicate["id"], "status": "duplicate", "material_id": duplicate["material_id"], "progress": 100}
            run_id, now = str(uuid.uuid4()), now_iso()
            connection.execute(
                """INSERT INTO leap_library_imports
                   (id,user_id,provider,source_item_id,title,author,language,edition,translator,source_name,source_url,download_url,source_format,licensing_note,rights_status,status,progress,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, user_id, provider, source_item_id, item["title"], item.get("author", ""), item.get("language", ""), item.get("edition", ""), item.get("translator", ""), item["source_name"], item["source_url"], item.get("download_url", ""), item.get("format", ""), item["licensing_note"], item["rights_status"], "queued", 0, now),
            )
        return {"id": run_id, "status": "queued", "progress": 0}

    def run(self, user_id: int, run_id: str) -> None:
        try:
            with self.connect() as connection:
                row = connection.execute("SELECT * FROM leap_library_imports WHERE id=? AND user_id=?", (run_id, user_id)).fetchone()
                if not row: return
                connection.execute("UPDATE leap_library_imports SET status='downloading',progress=15,started_at=? WHERE id=?", (now_iso(), run_id))
            item = self._item(row["provider"], row["source_item_id"])
            raw, headers, final_url = self.client.download(item); digest = hashlib.sha256(raw).hexdigest()
            with self.connect() as connection:
                duplicate = connection.execute("SELECT material_id,id FROM leap_library_imports WHERE user_id=? AND source_hash=? AND status='success' LIMIT 1", (user_id, digest)).fetchone()
                if duplicate:
                    connection.execute("UPDATE leap_library_imports SET status='duplicate',progress=100,source_hash=?,material_id=?,finished_at=? WHERE id=?", (digest, duplicate["material_id"], now_iso(), run_id)); return
                connection.execute("UPDATE leap_library_imports SET status='parsing',progress=50 WHERE id=?", (run_id,))
            content_type = headers.get("content-type", item.get("content_type", "application/octet-stream")).split(";", 1)[0]
            parsed = parse_epub(raw) if "epub" in content_type or raw.startswith(b"PK\x03\x04") else parse_html_document(raw)
            material_id, object_id, fetched = str(uuid.uuid4()), str(uuid.uuid4()), now_iso()
            content_hash = hashlib.sha256("\x00".join(p["content"] for p in parsed.paragraphs).encode("utf-8")).hexdigest()
            version = headers.get("etag") or headers.get("last-modified") or digest[:16]
            compressed = zlib.compress(raw, 9); object_key = f"leap/{user_id}/{row['provider']}/{row['source_item_id']}/{digest}.{item.get('format','bin').lower()}"
            with self.connect() as connection:
                connection.execute("UPDATE leap_library_imports SET status='saving',progress=80 WHERE id=?", (run_id,))
                connection.execute("""INSERT INTO leap_library_objects(id,user_id,object_key,content_type,content_encoding,original_size,stored_size,sha256,body,created_at)
                                      VALUES(?,?,?,?,?,?,?,?,?,?)""", (object_id, user_id, object_key, content_type, "zlib", len(raw), len(compressed), digest, compressed, fetched))
                connection.execute("""INSERT INTO leap_materials(id,user_id,title,author,source,tags,version,content_hash,paragraph_count,created_at,updated_at)
                                      VALUES(?,?,?,?,?,?,1,?,?,?,?)""", (material_id, user_id, row["title"], row["author"], row["source_url"], json.dumps(["公共领域书库", row["source_name"]], ensure_ascii=False), content_hash, len(parsed.paragraphs), fetched, fetched))
                connection.executemany("""INSERT INTO leap_paragraphs(material_id,material_version,position,content,chapter_position,chapter_title,stable_anchor)
                                           VALUES(?,1,?,?,?,?,?)""", [(material_id, p["position"], p["content"], p["chapter_position"], p["chapter_title"], p["stable_anchor"]) for p in parsed.paragraphs])
                connection.executemany("""INSERT INTO leap_chapters(material_id,material_version,position,title,stable_anchor,start_paragraph,end_paragraph)
                                           VALUES(?,1,?,?,?,?,?)""", [(material_id, c["position"], c["title"], c["stable_anchor"], c["start_paragraph"], c["end_paragraph"]) for c in parsed.chapters])
                connection.execute("""UPDATE leap_library_imports SET status='success',progress=100,fetched_at=?,source_version=?,source_hash=?,object_id=?,material_id=?,download_url=?,finished_at=? WHERE id=?""",
                                   (fetched, version, digest, object_id, material_id, final_url, now_iso(), run_id))
        except Exception as exc:
            with self.connect() as connection:
                connection.execute("UPDATE leap_library_imports SET status='failed',error=?,finished_at=? WHERE id=? AND user_id=?", (str(exc)[:500], now_iso(), run_id, user_id))

    def import_status(self, user_id: int, run_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("""SELECT id,provider,source_item_id,title,author,source_name,source_url,source_format,licensing_note,status,progress,error,material_id,fetched_at,source_version,source_hash,created_at,started_at,finished_at
                                        FROM leap_library_imports WHERE id=? AND user_id=?""", (run_id, user_id)).fetchone()
        if not row: raise KeyError("书库导入任务不存在。")
        return dict(row)

    def imports(self, user_id: int, limit: int = 30) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("""SELECT id,provider,source_item_id,title,author,source_name,source_url,source_format,licensing_note,status,progress,error,material_id,fetched_at,source_version,source_hash,created_at,started_at,finished_at
                                         FROM leap_library_imports WHERE user_id=? ORDER BY created_at DESC LIMIT ?""", (user_id, limit)).fetchall()
        return [dict(row) for row in rows]

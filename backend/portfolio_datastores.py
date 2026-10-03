"""Optional domain-specific MongoDB, Neo4j and GraphDB adapters.

The configured application database remains authoritative for business records.
MongoDB archives explicitly indexed Leap document editions; Neo4j holds the
public employer-opportunity-skill-location relationship graph for Future Radar.
"""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import quote
from typing import Any

from .future_radar.geography import china_place_catalog, resolve_opportunity_places


def _clean(value: Any, limit: int = 500) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


class MongoDocumentArchive:
    def __init__(self, uri: str = "", database_name: str = "frostfire"):
        self.uri = uri.strip()
        self.database_name = database_name.strip() or "frostfire"

    def status(self) -> dict[str, Any]:
        if not self.uri:
            return {"configured": False, "status": "not_configured", "role": "Leap indexed document editions"}
        try:
            from pymongo import MongoClient
            client = MongoClient(self.uri, serverSelectionTimeoutMS=1200, connectTimeoutMS=1200)
            try:
                client.admin.command("ping")
            finally:
                client.close()
            return {"configured": True, "status": "connected", "role": "Leap indexed document editions"}
        except Exception as exc:
            return {"configured": True, "status": "unavailable", "error_type": type(exc).__name__,
                    "role": "Leap indexed document editions"}

    def archive_leap_edition(self, user_id: int, metadata: dict[str, Any], chunks: list[dict[str, Any]]) -> dict[str, Any]:
        if not self.uri:
            return {"status": "not_configured", "archived_chunks": 0}
        from pymongo import MongoClient, ASCENDING, ReplaceOne
        client = MongoClient(self.uri, serverSelectionTimeoutMS=1500, connectTimeoutMS=1500)
        try:
            editions = client[self.database_name]["leap_document_editions"]
            chunk_collection = client[self.database_name]["leap_document_chunks"]
            identity = {"user_id": int(user_id), "material_id": str(metadata["id"]),
                        "material_version": int(metadata["version"])}
            editions.create_index([("user_id", ASCENDING), ("material_id", ASCENDING), ("material_version", ASCENDING)], unique=True)
            chunk_collection.create_index([("user_id", ASCENDING), ("material_id", ASCENDING),
                                            ("material_version", ASCENDING), ("chunk_position", ASCENDING)], unique=True)
            editions.replace_one(identity, {**identity, "title": _clean(metadata.get("title"), 240),
                                             "content_hash": str(metadata.get("content_hash") or ""),
                                             "chunk_count": len(chunks)}, upsert=True)
            operations = []
            for chunk in chunks:
                chunk_identity = {**identity, "chunk_position": int(chunk["chunk_position"])}
                operations.append(ReplaceOne(chunk_identity, {
                    **chunk_identity,
                    "paragraph_start": int(chunk["paragraph_start"]),
                    "paragraph_end": int(chunk["paragraph_end"]),
                    "heading_path": list(chunk.get("heading_path") or []),
                    "stable_anchor": _clean(chunk.get("stable_anchor"), 300),
                    "content": str(chunk["content"]),
                    "embedding": chunk.get("embedding"),
                    "embedding_model": _clean(chunk.get("embedding_model"), 120),
                }, upsert=True))
            if operations:
                chunk_collection.bulk_write(operations, ordered=False)
            return {"status": "archived", "archived_chunks": len(chunks)}
        finally:
            client.close()


class Neo4jOpportunityGraph:
    # Known competencies are the only labels inferred from unstructured public
    # text or generic tags. Explicit skill fields can name other competencies.
    _SKILL_ALIASES = (
        ("Python", ("python", "python语言", "python编程")),
        ("SQL", ("sql", "sql语言")),
        ("Excel", ("excel",)),
        ("PostgreSQL", ("postgresql", "postgres")),
        ("MySQL", ("mysql",)),
        ("MongoDB", ("mongodb",)),
        ("Neo4j", ("neo4j",)),
        ("Java", ("java",)),
        ("JavaScript", ("javascript",)),
        ("TypeScript", ("typescript",)),
        ("C++", ("c++",)),
        ("C#", ("c#",)),
        ("Go", ("go", "golang", "go语言")),
        ("R", ("r", "r语言")),
        ("Tableau", ("tableau",)),
        ("Power BI", ("power bi", "powerbi")),
        ("PyTorch", ("pytorch",)),
        ("TensorFlow", ("tensorflow",)),
        ("Docker", ("docker",)),
        ("Kubernetes", ("kubernetes", "k8s")),
        ("Linux", ("linux",)),
        ("Git", ("git",)),
        ("数据分析", ("数据分析", "data analysis", "data analytics")),
        ("机器学习", ("机器学习", "machine learning")),
        ("深度学习", ("深度学习", "deep learning")),
        ("统计分析", ("统计分析", "statistical analysis")),
        ("财务建模", ("财务建模", "financial modeling", "financial modelling")),
    )
    _VERSIONED_LANGUAGE_NAMES = frozenset({"Python", "Java", "C++", "C#"})
    _SKILL_CLAUSE_BOUNDARY = re.compile(
        r"[,，;；。!?！？]|\.(?:\s|$)|但(?:是)?|然而|不过|\b(?:but|however|whereas)\b", re.IGNORECASE)
    _SKILL_LIST_CONNECTOR = re.compile(r"\s*(?:[/|&、]|和|及|与|或|或者|and|or)\s*", re.IGNORECASE)
    _SKILL_NEGATION_PREFIX = re.compile(
        r"(?:不(?:要求|需要|需|必|用|强求)|无(?:需|须)|未要求)\s*"
        r"(?:(?:掌握|熟悉|精通|了解|具备|具有|使用|学习|有)\s*)*$"
        r"|\b(?:no|without|optional)\s+"
        r"(?:(?:prior|previous|any|practical|working|professional)\s+)*"
        r"(?:(?:experience|knowledge|skills?|proficiency|requirements?)(?:\s+(?:in|of|with|for))?\s+)?$"
        r"|\b(?:not\s+(?:required|needed|necessary|mandatory|essential)|no\s+need)"
        r"(?:\s+to(?:\s+(?:know|use|learn|understand|have))?)?\s*$", re.IGNORECASE)
    _SKILL_NEGATION_SUFFIX = re.compile(
        r"^\s*[(（]?\s*(?:(?:编程|语言|技能|经验|能力|知识|基础)\s*)*"
        r"(?:(?:并非|不是|非|不属于)\s*(?:必需|必备|必要|必须|要求|必选)"
        r"|不(?:要求|需要|必需|必要|作要求|做要求|强求)|无需|无须|可选)"
        r"|^\s*[(（]?\s*(?:(?:programming|language|skills?|experience|knowledge|proficiency)\s+)*"
        r"(?:(?:is|are)\s+)?(?:not\s+(?:required|needed|necessary|mandatory|essential|a\s+requirement)"
        r"|(?:isn['’]t|aren['’]t)\s+(?:required|needed|necessary|mandatory|essential)|optional)\b",
        re.IGNORECASE)

    def __init__(self, uri: str = "", username: str = "", password: str = "", database_name: str = "neo4j"):
        self.uri, self.username, self.password = uri.strip(), username.strip(), password
        self.database_name = database_name.strip() or "neo4j"

    def _driver(self):
        from neo4j import GraphDatabase
        if not self.uri:
            return None
        return GraphDatabase.driver(self.uri, auth=(self.username, self.password), connection_timeout=2)

    def status(self) -> dict[str, Any]:
        if not self.uri:
            return {"configured": False, "status": "not_configured", "role": "Future Radar employer-job-skill graph"}
        try:
            driver = self._driver()
            with driver.session(database=self.database_name) as session:
                session.run("RETURN 1").consume()
            return {"configured": True, "status": "connected", "role": "Future Radar employer-job-skill graph"}
        except Exception as exc:
            return {"configured": True, "status": "unavailable", "error_type": type(exc).__name__,
                    "role": "Future Radar employer-job-skill graph"}
        finally:
            if "driver" in locals() and driver is not None:
                driver.close()

    @staticmethod
    def _skills(job: dict[str, Any]) -> list[str]:
        def labels(value: Any) -> list[str]:
            if isinstance(value, str) and value.strip().startswith("["):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    return []
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list):
                return []
            return [_clean(part, 100)
                    for item in value if isinstance(item, str)
                    for part in re.split(r"[,;/|，；、]+", item) if _clean(part, 100)]

        aliases = {alias.casefold(): name
                   for name, names in Neo4jOpportunityGraph._SKILL_ALIASES
                   for alias in names}
        # Do not backtrack to a partial version in e.g. Python3.11Script.
        version_suffix = r"\d+(?:\.\d+)*(?!\.\d)"

        def recognized(value: str) -> str | None:
            name = aliases.get(value.casefold())
            if name:
                return name
            for language in Neo4jOpportunityGraph._VERSIONED_LANGUAGE_NAMES:
                if re.fullmatch(re.escape(language) + version_suffix, value, flags=re.IGNORECASE):
                    return language
            return None

        seen = set()
        output = []

        def add(value: str) -> None:
            name = recognized(value) or value
            key = name.casefold()
            if key not in seen:
                seen.add(key)
                output.append(name)

        explicit = labels(job.get("skills")) + labels(job.get("required_skills"))
        for value in explicit:
            add(value)
        positive = set()
        negated = set()
        if not explicit:
            # Keep fields and short clauses separate: a non-required SQL mention
            # must not negate a Python requirement in the next clause or field.
            clauses = [clause for key in ("title", "description", "requirements", "responsibilities")
                       for clause in Neo4jOpportunityGraph._SKILL_CLAUSE_BOUNDARY.split(_clean(job.get(key), 8_000))]
            patterns = []
            for name, names in Neo4jOpportunityGraph._SKILL_ALIASES:
                # Go and R are ordinary words/letters. Require their language
                # names in prose; exact explicit fields and tags remain valid.
                prose_names = tuple(alias for alias in names if alias not in {"go", "r"})
                alternatives = [re.escape(alias) + (rf"(?:{version_suffix})?"
                                if name in Neo4jOpportunityGraph._VERSIONED_LANGUAGE_NAMES
                                and alias.casefold() == name.casefold() else "")
                                for alias in sorted(prose_names, key=len, reverse=True)]
                pattern = r"(?<![A-Za-z0-9_])(?:" + "|".join(alternatives) + r")(?![A-Za-z0-9_])"
                patterns.append((name, re.compile(pattern, flags=re.IGNORECASE)))
            for clause in clauses:
                mentions = sorted((match.start(), match.end(), name)
                                  for name, pattern in patterns for match in pattern.finditer(clause))
                groups = []
                for start, end, name in mentions:
                    # A simple skill list shares its qualifier, e.g. no Python
                    # or SQL experience. Other intervening prose starts a new
                    # group, so SQL not required and Python essential is safe.
                    if groups and Neo4jOpportunityGraph._SKILL_LIST_CONNECTOR.fullmatch(clause[groups[-1][1]:start]):
                        groups[-1][1] = end
                        groups[-1][2].add(name)
                    else:
                        groups.append([start, end, {name}])
                for start, end, names in groups:
                    if (Neo4jOpportunityGraph._SKILL_NEGATION_PREFIX.search(clause[:start])
                            or Neo4jOpportunityGraph._SKILL_NEGATION_SUFFIX.search(clause[end:])):
                        negated.update(names)
                    else:
                        positive.update(names)
        # Operational tags also contain industries, cities and verification
        # states; only a complete, recognized skill label is evidence here.
        for value in labels(job.get("tags")):
            name = recognized(value)
            # Generic tags cannot override prose that only mentions this skill
            # to explicitly say it is not required. Explicit skill fields keep
            # their existing authoritative behavior.
            if name and (name not in negated or name in positive):
                add(value)
        # Preserve the established deterministic canonical order after tags.
        for name, _names in Neo4jOpportunityGraph._SKILL_ALIASES:
            if name in positive:
                add(name)
        return output[:30]

    def sync_opportunities(self, jobs: list[dict[str, Any]]) -> dict[str, Any]:
        if not self.uri:
            return {"status": "not_configured", "opportunities": len(jobs), "relationships_written": 0}
        driver = self._driver()
        relationship_count = 0
        try:
            rows = []
            catalog = china_place_catalog()
            for job in jobs[:500]:
                job_id = _clean(job.get("id") or job.get("job_id"), 180)
                title = _clean(job.get("title") or job.get("role") or job.get("position"), 240)
                company = _clean(job.get("company_name") or job.get("employer_name") or job.get("company"), 200)
                if not job_id or not title or not company:
                    continue
                company_key = hashlib.sha256(company.casefold().encode()).hexdigest()[:24]
                places, location_status = resolve_opportunity_places(job, catalog=catalog)
                locations, within = catalog.hierarchy(places)
                rows.append({"id": job_id, "title": title, "company": company,
                             "company_key": company_key, "location": _clean(job.get("location") or job.get("city"), 180),
                             "url": _clean(job.get("application_url") or job.get("official_url")
                                           or job.get("url") or job.get("source_url"), 1000),
                             "skills": self._skills(job), "places": places,
                             "locations": locations, "within": within,
                             "location_status": location_status})
            rows_by_id = {row["id"]: row for row in rows}
            read_scope = {"ids": [row["id"] for row in rows],
                          "company_keys": {row["id"]: row["company_key"] for row in rows},
                          "skills_by_id": {row["id"]: row["skills"] for row in rows},
                          "place_ids_by_id": {row["id"]: [place["id"] for place in row["places"]] for row in rows},
                          "location_keys": list(dict.fromkeys(place["id"] for row in rows for place in row["locations"])),
                          "within_pairs": [{"child": child, "parent": parent} for child, parent in dict.fromkeys(
                              (link["child"], link["parent"]) for row in rows for link in row["within"])],
                          }
            with driver.session(database=self.database_name) as session:
                session.run("CREATE CONSTRAINT opportunity_id IF NOT EXISTS FOR (n:Opportunity) REQUIRE n.id IS UNIQUE").consume()
                session.run("CREATE CONSTRAINT employer_key IF NOT EXISTS FOR (n:Employer) REQUIRE n.key IS UNIQUE").consume()
                session.run("CREATE CONSTRAINT skill_name IF NOT EXISTS FOR (n:Skill) REQUIRE n.name IS UNIQUE").consume()
                session.run("CREATE CONSTRAINT location_key IF NOT EXISTS FOR (n:Location) REQUIRE n.key IS UNIQUE").consume()
                session.run("""UNWIND $rows AS row
                    MERGE (e:Employer {key: row.company_key}) SET e.name=row.company
                    MERGE (o:Opportunity {id: row.id}) SET o.title=row.title,o.location=row.location,o.url=row.url
                    MERGE (e)-[:POSTS]->(o)
                    FOREACH (place IN row.locations |
                        MERGE (l:Location {key: place.id})
                        SET l.name=place.name,l.level=place.level,l.province_id=place.province_id,
                            l.province_name=place.province_name,l.city_id=place.city_id,l.city_name=place.city_name,
                            l.longitude=place.longitude,l.latitude=place.latitude,l.accuracy=place.accuracy,
                            l.source_crs=place.crs,l.coordinate_source=place.coordinate_source,l.catalog_source=place.catalog_source,
                            l.position=CASE WHEN place.crs='WGS84' AND place.longitude IS NOT NULL
                                AND place.latitude IS NOT NULL THEN point({longitude:place.longitude,
                                latitude:place.latitude,srid:4326}) ELSE null END)
                    FOREACH (place IN row.places |
                        MERGE (l:Location {key: place.id}) MERGE (o)-[:LOCATED_IN]->(l))
                    FOREACH (link IN row.within |
                        MERGE (child:Location {key: link.child})
                        MERGE (parent:Location {key: link.parent}) MERGE (child)-[:WITHIN]->(parent))
                    WITH o,row UNWIND row.skills AS skill_name
                    MERGE (s:Skill {name: skill_name}) MERGE (o)-[:REQUIRES]->(s)""", rows=rows).consume()
                record = session.run("""MATCH (e:Employer)-[:POSTS]->(o:Opportunity)
                    WHERE o.id IN $ids AND e.key = $company_keys[o.id]
                    OPTIONAL MATCH (o)-[:REQUIRES]->(s:Skill)
                    WHERE s.name IN $skills_by_id[o.id]
                    WITH e,o,collect(DISTINCT s.name)[0..12] AS skills
                    OPTIONAL MATCH (o)-[:LOCATED_IN]->(l:Location)
                    WHERE l.key IN $place_ids_by_id[o.id]
                    RETURN e.name AS employer,o.id AS id,o.title AS title,o.location AS location,o.url AS url,
                           skills,collect(DISTINCT l.key) AS place_keys ORDER BY employer,title LIMIT 500""",
                                     **read_scope).data()
                relationship_count = session.run("""MATCH (e:Employer)-[posts:POSTS]->(o:Opportunity)
                    WHERE o.id IN $ids AND e.key = $company_keys[o.id]
                    OPTIONAL MATCH (o)-[requires:REQUIRES]->(s:Skill)
                    WHERE s.name IN $skills_by_id[o.id]
                    OPTIONAL MATCH (o)-[located:LOCATED_IN]->(l:Location)
                    WHERE l.key IN $place_ids_by_id[o.id]
                    WITH count(DISTINCT posts) + count(DISTINCT requires) + count(DISTINCT located) AS job_edges
                    OPTIONAL MATCH (child:Location)-[within:WITHIN]->(parent:Location)
                    WHERE child.key IN $location_keys AND parent.key IN $location_keys
                      AND {child:child.key,parent:parent.key} IN $within_pairs
                    RETURN job_edges + count(DISTINCT within) AS count""",
                                                 **read_scope).single()["count"]
            items = []
            for item in record:
                row = rows_by_id.get(str(item.get("id") or ""))
                if row is not None:
                    # Current public catalog resolution, never historical or
                    # caller-supplied coordinates/places from an old graph.
                    items.append({"employer": row["company"], "id": row["id"], "title": row["title"],
                                  "location": row["location"], "url": row["url"],
                                  "skills": [skill for skill in item.get("skills") or [] if skill in row["skills"]][:12],
                                  "places": row["places"],
                                  "location_status": row["location_status"]})
            return {"status": "synced", "opportunities": len(rows),
                    "relationships_stored": int(relationship_count),
                    "relationships_written": int(relationship_count),
                    "relationship_count_scope": "current_public_projection",
                    "relationship_count_semantics": "relationships_written is a compatibility alias for stored relationships, not newly created relationships.",
                    "items": items}
        finally:
            driver.close()


class GraphDBAcquisitionStore:
    """GraphDB RDF projection for acquisition assumptions and risk scenarios.

    The configured application database remains authoritative for model history. GraphDB is
    an optional relationship/lineage view, never a replacement for that data.
    """

    NS = "https://frostfire.local/acquisition/"

    def __init__(self, base_url: str = "", repository: str = "frostfire-acquisitions",
                 username: str = "", password: str = ""):
        self.base_url = base_url.strip().rstrip("/")
        self.repository = repository.strip() or "frostfire-acquisitions"
        self.username, self.password = username, password

    def _auth(self):
        return (self.username, self.password) if self.username else None

    def _repository_url(self) -> str:
        return f"{self.base_url}/repositories/{quote(self.repository, safe='')}"

    def status(self) -> dict[str, Any]:
        if not self.base_url:
            return {"configured": False, "status": "not_configured", "role": "烈火域并购情景 RDF 关系图"}
        try:
            import httpx
            response = httpx.get(self._repository_url(), params={"query": "ASK { }", "infer": "false"},
                                 auth=self._auth(), headers={"Accept": "application/sparql-results+json"}, timeout=2.5)
            response.raise_for_status()
            return {"configured": True, "status": "connected", "repository": self.repository,
                    "role": "烈火域并购情景 RDF 关系图"}
        except Exception as exc:
            return {"configured": True, "status": "unavailable", "repository": self.repository,
                    "error_type": type(exc).__name__, "role": "烈火域并购情景 RDF 关系图"}

    @staticmethod
    def _literal(value: Any) -> str:
        return json.dumps(str(value), ensure_ascii=True)

    def store_acquisition_run(self, run_id: str, user_id: int, result: dict[str, Any]) -> dict[str, Any]:
        if not self.base_url:
            return {"status": "not_configured", "triples_written": 0}
        if not re.fullmatch(r"[a-fA-F0-9-]{36}", run_id):
            raise ValueError("run_id must be a UUID")
        run_uri = f"<{self.NS}run/{run_id}>"
        graph_uri = f"<{self.NS}graph/{run_id}>"
        triples = [
            f'{run_uri} a <{self.NS}AcquisitionRun> ; <{self.NS}userId> {self._literal(user_id)} ; '
            f'<{self.NS}currency> {self._literal(result.get("currency", "AUD"))} ; '
            f'<{self.NS}purchasePrice> {float(result["inputs"]["purchase_price"])} ; '
            f'<{self.NS}newDebt> {float(result["new_debt"])} ; '
            f'<{self.NS}cashConsideration> {float(result["cash_consideration"])} ; '
            f'<{self.NS}annualInterest> {float(result["annual_incremental_interest"])} .'
        ]
        relation_count = 0
        for index, item in enumerate(result.get("financing_mix_sensitivity", [])):
            scenario_uri = f"<{self.NS}run/{run_id}/financing/{index}>"
            triples.append(f'{run_uri} <{self.NS}hasFinancingScenario> {scenario_uri} .')
            triples.append(f'{scenario_uri} a <{self.NS}FinancingScenario> ; '
                           f'<{self.NS}debtShare> {float(item["debt_share"])} ; '
                           f'<{self.NS}cashConsideration> {float(item["cash_consideration"])} ; '
                           f'<{self.NS}annualInterest> {float(item["annual_interest"])} .')
            relation_count += 1
        for index, item in enumerate(result.get("operating_stress_matrix", [])):
            scenario_uri = f"<{self.NS}run/{run_id}/stress/{index}>"
            triples.append(f'{run_uri} <{self.NS}hasStressScenario> {scenario_uri} .')
            coverage = item.get("interest_coverage")
            coverage_triple = f'<{self.NS}interestCoverage> {float(coverage)} ; ' if coverage is not None else ""
            triples.append(f'{scenario_uri} a <{self.NS}OperatingStressScenario> ; '
                           f'<{self.NS}rateShock> {float(item["rate_shock"])} ; '
                           f'<{self.NS}ebitChange> {float(item["ebit_change"])} ; '
                           f'{coverage_triple}<https://frostfire.local/acquisition/recorded> true .')
            relation_count += 1
        update = f"INSERT DATA {{ GRAPH {graph_uri} {{ {' '.join(triples)} }} }}"
        try:
            import httpx
            response = httpx.post(f"{self._repository_url()}/statements", content=update,
                                  auth=self._auth(), headers={"Content-Type": "application/sparql-update"}, timeout=5)
            response.raise_for_status()
            return {"status": "stored", "triples_written": len(triples), "scenario_relationships": relation_count,
                    "repository": self.repository}
        except Exception as exc:
            return {"status": "unavailable", "triples_written": 0, "error_type": type(exc).__name__,
                    "repository": self.repository}

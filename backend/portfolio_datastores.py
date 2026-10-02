"""Optional domain-specific MongoDB, Neo4j and GraphDB adapters.

The configured application database remains authoritative for business records.
MongoDB archives explicitly indexed Leap document editions; Neo4j holds the
public employer-opportunity-skill relationship graph for Future Radar.
"""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import quote
from typing import Any


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
        raw: list[Any] = []
        for key in ("skills", "required_skills", "tags", "categories", "industry"):
            value = job.get(key)
            if isinstance(value, list): raw.extend(value)
            elif isinstance(value, str):
                # The operational job table stores tags as JSON arrays. Treating
                # their serialized brackets/quotes as labels pollutes the graph.
                if value.strip().startswith("["):
                    try:
                        parsed = json.loads(value)
                    except json.JSONDecodeError:
                        parsed = None
                    if isinstance(parsed, list):
                        raw.extend(parsed)
                        continue
                raw.extend(re.split(r"[,;/|，；、]+", value))
        seen = set()
        output = []
        for value in raw:
            for part in re.split(r"[,;/|，；、]+", str(value or "")):
                name = _clean(part, 100)
                key = name.casefold()
                if name and key not in seen:
                    seen.add(key); output.append(name)
        return output[:30]

    def sync_opportunities(self, jobs: list[dict[str, Any]]) -> dict[str, Any]:
        if not self.uri:
            return {"status": "not_configured", "opportunities": len(jobs), "relationships_written": 0}
        driver = self._driver()
        relationship_count = 0
        try:
            rows = []
            for job in jobs[:500]:
                job_id = _clean(job.get("id") or job.get("job_id"), 180)
                title = _clean(job.get("title") or job.get("role") or job.get("position"), 240)
                company = _clean(job.get("company_name") or job.get("employer_name") or job.get("company"), 200)
                if not job_id or not title or not company:
                    continue
                company_key = hashlib.sha256(company.casefold().encode()).hexdigest()[:24]
                rows.append({"id": job_id, "title": title, "company": company,
                             "company_key": company_key, "location": _clean(job.get("location") or job.get("city"), 180),
                             "url": _clean(job.get("url") or job.get("source_url"), 1000),
                             "skills": self._skills(job)})
            with driver.session(database=self.database_name) as session:
                session.run("CREATE CONSTRAINT opportunity_id IF NOT EXISTS FOR (n:Opportunity) REQUIRE n.id IS UNIQUE").consume()
                session.run("CREATE CONSTRAINT employer_key IF NOT EXISTS FOR (n:Employer) REQUIRE n.key IS UNIQUE").consume()
                session.run("CREATE CONSTRAINT skill_name IF NOT EXISTS FOR (n:Skill) REQUIRE n.name IS UNIQUE").consume()
                session.run("""UNWIND $rows AS row
                    MERGE (e:Employer {key: row.company_key}) SET e.name=row.company
                    MERGE (o:Opportunity {id: row.id}) SET o.title=row.title,o.location=row.location,o.url=row.url
                    MERGE (e)-[:POSTS]->(o)
                    WITH o,row UNWIND row.skills AS skill_name
                    MERGE (s:Skill {name: skill_name}) MERGE (o)-[:REQUIRES]->(s)""", rows=rows).consume()
                record = session.run("""MATCH (e:Employer)-[:POSTS]->(o:Opportunity)
                    OPTIONAL MATCH (o)-[:REQUIRES]->(s:Skill)
                    RETURN e.name AS employer,o.id AS id,o.title AS title,o.location AS location,
                           collect(DISTINCT s.name)[0..12] AS skills ORDER BY employer,title LIMIT 500""").data()
                relationship_count = session.run("MATCH ()-[r:POSTS|REQUIRES]->() RETURN count(r) AS count").single()["count"]
            return {"status": "synced", "opportunities": len(rows), "relationships_written": int(relationship_count), "items": record}
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

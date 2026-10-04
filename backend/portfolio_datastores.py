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
    # Known competencies require positive public job text; generic operational
    # tags alone are not evidence. Explicit skill fields remain authoritative.
    _MAX_SKILLS_PER_OPPORTUNITY = 40
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
        ("Go", ("go", "golang", "go语言", "go programming", "go language", "programming in go")),
        ("R", ("r", "r语言", "r programming", "r language", "programming in r")),
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
        ("C", ("c", "c语言", "c编程", "c programming", "c language", "programming in c")),
        ("Rust", ("rust", "rust语言", "rust编程", "rust programming")),
        ("Scala", ("scala",)),
        ("Kotlin", ("kotlin",)),
        ("Swift", ("swift", "swift语言", "swift编程", "swift programming")),
        ("PHP", ("php",)),
        ("MATLAB", ("matlab",)),
        ("Julia", ("julia", "julia语言", "julia programming")),
        ("Bash", ("bash", "bash scripting", "bash脚本")),
        ("Shell scripting", ("shell scripting", "shell scripts", "shell脚本")),
        ("NumPy", ("numpy",)),
        ("pandas", ("pandas",)),
        ("SciPy", ("scipy",)),
        ("scikit-learn", ("scikit-learn", "sklearn")),
        ("XGBoost", ("xgboost",)),
        ("LightGBM", ("lightgbm",)),
        ("Keras", ("keras",)),
        ("OpenCV", ("opencv",)),
        ("自然语言处理", ("自然语言处理", "natural language processing", "nlp")),
        ("计算机视觉", ("计算机视觉", "computer vision")),
        ("大语言模型", ("大语言模型", "large language models", "large language model", "llm", "llms")),
        ("Node.js", ("node.js", "nodejs")),
        ("React", ("react", "react.js", "reactjs", "react框架")),
        ("React Native", ("react native",)),
        ("Vue.js", ("vue.js", "vuejs", "vue", "vue框架")),
        ("Angular", ("angular", "angularjs", "angular框架")),
        ("Django", ("django",)),
        ("Flask", ("flask",)),
        ("FastAPI", ("fastapi",)),
        ("Spring Boot", ("spring boot", "springboot")),
        (".NET", (".net", "dotnet")),
        ("HTML", ("html", "html5")),
        ("CSS", ("css", "css3")),
        ("REST API", ("rest api", "restful api", "restful接口", "rest接口")),
        ("GraphQL", ("graphql",)),
        ("SQLite", ("sqlite",)),
        ("Redis", ("redis",)),
        ("Elasticsearch", ("elasticsearch", "elastic search")),
        ("ClickHouse", ("clickhouse",)),
        ("Oracle Database", ("oracle database", "oracle数据库")),
        ("SQL Server", ("sql server", "microsoft sql server")),
        ("Snowflake", ("snowflake", "snowflake数据库")),
        ("BigQuery", ("bigquery",)),
        ("Apache Spark", ("apache spark", "spark", "pyspark", "spark sql")),
        ("Apache Hadoop", ("apache hadoop", "hadoop")),
        ("Apache Flink", ("apache flink", "flink")),
        ("Apache Kafka", ("apache kafka", "kafka")),
        ("dbt", ("dbt",)),
        ("Apache Airflow", ("apache airflow", "airflow")),
        ("ETL", ("etl", "数据抽取转换加载", "extract transform load")),
        ("Terraform", ("terraform",)),
        ("Ansible", ("ansible",)),
        ("AWS", ("aws", "amazon web services")),
        ("Azure", ("azure", "microsoft azure")),
        ("Google Cloud", ("google cloud", "google cloud platform", "gcp")),
        ("持续集成", ("持续集成", "continuous integration", "ci/cd")),
        ("单元测试", ("单元测试", "unit testing", "unit tests")),
        ("回归分析", ("回归分析", "线性回归", "逻辑回归", "regression analysis", "linear regression", "logistic regression")),
        ("时间序列分析", ("时间序列分析", "time series analysis", "time-series analysis")),
        ("概率论", ("概率论", "probability theory")),
        ("假设检验", ("假设检验", "hypothesis testing", "hypothesis tests")),
        ("实验设计", ("实验设计", "design of experiments", "experimental design", "a/b testing", "ab testing")),
        ("优化建模", ("优化建模", "数学优化", "mathematical optimization", "mathematical optimisation", "optimization modeling")),
        ("风险管理", ("风险管理", "risk management")),
        ("信用分析", ("信用分析", "信用风险分析", "credit analysis", "credit risk analysis")),
        ("财务分析", ("财务分析", "financial analysis")),
        ("财务报表分析", ("财务报表分析", "financial statement analysis")),
        ("估值分析", ("估值分析", "企业估值", "股权估值", "valuation analysis", "business valuation", "equity valuation")),
        ("投资分析", ("投资分析", "investment analysis", "投资研究", "investment research")),
        ("融资分析", ("融资分析", "投融资分析", "financing analysis", "capital raising analysis")),
        ("投资组合管理", ("投资组合管理", "portfolio management")),
        ("尽职调查", ("尽职调查", "due diligence")),
        ("预算管理", ("预算管理", "budget management", "budgeting")),
        ("需求分析", ("需求分析", "产品需求分析", "requirements analysis", "product requirements analysis")),
        ("用户研究", ("用户研究", "user research", "用户调研", "customer research")),
        ("市场研究", ("市场研究", "市场调研", "market research")),
        ("市场营销", ("市场营销", "marketing strategy", "数字营销", "digital marketing")),
        ("竞品分析", ("竞品分析", "竞争分析", "competitive analysis", "competitor analysis")),
        ("项目管理", ("项目管理", "project management")),
        ("供应链管理", ("供应链管理", "supply chain management")),
    )
    # Ambiguous bare names need requirements plus a technical qualifier/list.
    # Unambiguous technical phrases work in all public fields.
    _REQUIREMENTS_ONLY_ALIASES = frozenset({
        "rust", "swift", "julia", "react", "vue", "angular", "flask", "snowflake",
        "spark", "airflow", "azure",
    })
    _SKILL_TECHNICAL_PREFIX = re.compile(
        r"(?:掌握|熟悉|精通|了解|使用|具备|学习|擅长|基于|技能|技术|要求)\s*[:：]?\s*$"
        r"|\b(?:(?:experience|knowledge|skills?|expertise|proficiency|proficient)\s+(?:in|of|with)"
        r"|programming\s+in|use|using|required|requirements?|technologies)\s*[:：]?\s*$", re.IGNORECASE)
    _SKILL_TECHNICAL_SUFFIX = re.compile(
        r"^\s*(?:开发|编程|语言|框架|数据库|平台|经验|技能|知识|能力)"
        r"|^\s+(?:programming|language|development|framework|database|platform|experience|knowledge|skills?|proficiency)\b"
        r"|^\s+(?:(?:is|are)\s+)?(?:required|mandatory|essential|necessary)\b", re.IGNORECASE)
    _SKILL_COMPANY_SUFFIX = re.compile(
        r"^\s*(?:公司|集团|企业|机构|有限|股份)|^\s+(?:company|corporation|corp|inc|ltd)\b", re.IGNORECASE)
    _VERSIONED_LANGUAGE_NAMES = frozenset({"Python", "Java", "C++", "C#", "C", "R", "Go"})
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
                    for part in ([item] if recognized(_clean(item, 100))
                                 else re.split(r"[,;/|，；、]+", item)) if _clean(part, 100)]

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
        # Parse public prose even with explicit skills, so a generic tag can be
        # corroborated without changing the explicit-field fallback behavior.
        # Keep fields/clauses separate to prevent adjacent positive requirements
        # from inheriting another skill's negation.
        clauses = [(key, clause) for key in ("title", "description", "requirements", "responsibilities")
                   for clause in Neo4jOpportunityGraph._SKILL_CLAUSE_BOUNDARY.split(_clean(job.get(key), 8_000))]
        patterns = []
        for name, names in Neo4jOpportunityGraph._SKILL_ALIASES:
            # Bare C/R/Go are letters or ordinary words, not prose evidence.
            # Explicit language phrases and exact explicit skill labels work.
            prose_names = tuple(alias for alias in names if alias not in {"go", "r", "c"})

            def pattern_for(aliases: tuple[str, ...]) -> re.Pattern:
                if not aliases:
                    return re.compile(r"(?!)")
                alternatives = [re.escape(alias) + (rf"(?:{version_suffix})?"
                                if name in Neo4jOpportunityGraph._VERSIONED_LANGUAGE_NAMES
                                and alias.casefold() == name.casefold() else "")
                                for alias in sorted(aliases, key=len, reverse=True)]
                return re.compile(r"(?<![A-Za-z0-9_])(?:" + "|".join(alternatives)
                                  + r")(?![A-Za-z0-9_])", flags=re.IGNORECASE)

            public_names = tuple(alias for alias in prose_names
                                 if alias not in Neo4jOpportunityGraph._REQUIREMENTS_ONLY_ALIASES)
            patterns.append((name, pattern_for(prose_names), pattern_for(public_names)))
        for field, clause in clauses:
            mentions = sorted((match.start(), match.end(), name,
                               match.group().casefold() not in Neo4jOpportunityGraph._REQUIREMENTS_ONLY_ALIASES)
                              for name, requirement_pattern, public_pattern in patterns
                              for match in (requirement_pattern if field == "requirements"
                                            else public_pattern).finditer(clause))
            groups = []
            for start, end, name, unambiguous in mentions:
                # Nested aliases (SQL inside SQL Server, React inside React
                # Native) share the longest span and its negation. Connector
                # lists also share qualifiers, but intervening prose does not.
                if groups and (start < groups[-1][1]
                               or Neo4jOpportunityGraph._SKILL_LIST_CONNECTOR.fullmatch(clause[groups[-1][1]:start])):
                    groups[-1][1] = max(groups[-1][1], end)
                    groups[-1][2].add(name)
                    groups[-1][3] = groups[-1][3] or unambiguous
                else:
                    groups.append([start, end, {name}, unambiguous])
            for start, end, names, unambiguous in groups:
                # Check the complete span so SQL inside SQL Server公司 cannot
                # survive after the longer organization mention is rejected.
                if Neo4jOpportunityGraph._SKILL_COMPANY_SUFFIX.search(clause[end:]):
                    continue
                if not (unambiguous
                        or Neo4jOpportunityGraph._SKILL_TECHNICAL_PREFIX.search(clause[:start])
                        or Neo4jOpportunityGraph._SKILL_TECHNICAL_SUFFIX.search(clause[end:])):
                    continue
                if (Neo4jOpportunityGraph._SKILL_NEGATION_PREFIX.search(clause[:start])
                        or Neo4jOpportunityGraph._SKILL_NEGATION_SUFFIX.search(clause[end:])):
                    negated.update(names)
                else:
                    positive.update(names)
        # Operational tags also contain industries, cities and verification
        # states; only a complete, recognized skill label is evidence here.
        for value in labels(job.get("tags")):
            name = recognized(value)
            # A tag must have positive public-text evidence. It cannot override
            # a negated-only mention or manufacture a requirement on its own.
            if name and name in positive:
                add(value)
        # Preserve the established deterministic canonical order after tags.
        for name, _names in Neo4jOpportunityGraph._SKILL_ALIASES:
            if not explicit and name in positive:
                add(name)
        return output[:Neo4jOpportunityGraph._MAX_SKILLS_PER_OPPORTUNITY]

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
                          "skill_limit": self._MAX_SKILLS_PER_OPPORTUNITY,
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
                    WITH e,o,collect(DISTINCT s.name)[0..$skill_limit] AS skills
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
                    WITH job_edges, count(DISTINCT within) AS place_edges
                    RETURN job_edges + place_edges AS count""",
                                                 **read_scope).single()["count"]
            items = []
            for item in record:
                row = rows_by_id.get(str(item.get("id") or ""))
                if row is not None:
                    # Current public catalog resolution, never historical or
                    # caller-supplied coordinates/places from an old graph.
                    items.append({"employer": row["company"], "id": row["id"], "title": row["title"],
                                  "location": row["location"], "url": row["url"],
                                  # Use current deterministic public order;
                                  # Neo4j collect order/historical labels must
                                  # not decide which competencies are exposed.
                                  "skills": [skill for skill in row["skills"]
                                             if skill in (item.get("skills") or [])][:self._MAX_SKILLS_PER_OPPORTUNITY],
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

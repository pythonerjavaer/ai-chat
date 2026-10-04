"""Neo4j graph projection checks use a recording driver, never a live service."""

from types import SimpleNamespace

import pytest

from backend.portfolio_datastores import Neo4jOpportunityGraph


class Result:
    def __init__(self, *, records=None, count=0):
        self.records = records or []
        self.count = count

    def consume(self):
        return SimpleNamespace(counters=SimpleNamespace(relationships_created=0))

    def data(self):
        return self.records

    def single(self):
        return {"count": self.count}


class RecordingDriver:
    def __init__(self, *, records=None, relationship_count=0):
        self.calls = []
        self.records = records or []
        self.relationship_count = relationship_count
        self.closed = False
        self.database = None

    def session(self, *, database):
        self.database = database
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def run(self, query, **parameters):
        self.calls.append((query, parameters))
        if "RETURN e.name AS employer" in query:
            return Result(records=self.records)
        if "AS count" in query:
            return Result(count=self.relationship_count)
        return Result()

    def close(self):
        self.closed = True


def public_job(job_id="job-1", **changes):
    return {"id": job_id, "company": "示例科技", "title": "Python 数据分析岗位", "city": "上海",
            "application_url": f"https://careers.example.test/apply/{job_id}", **changes}


def test_explicit_skills_keep_clean_named_competencies_and_normalize_bilingual_aliases():
    skills = Neo4jOpportunityGraph._skills({
        "skills": [" Python ", "python", "SQL/Excel", "沟通  能力", None, {"private": "payload"}],
        "required_skills": '["Machine Learning", "机器学习", "Financial Modelling"]',
        "description": "JavaScript and Docker experience",
    })
    assert skills == ["Python", "SQL", "Excel", "沟通 能力", "机器学习", "财务建模"]


def test_json_tags_need_complete_recognized_labels_and_positive_public_evidence():
    job = {
        "tags": '["Python", "SQL", "Machine Learning", "机器学习", "北京", "互联网", "open", '
                '"示例科技", "校园招聘", "金融Python", "Python行业", "待官方核验"]',
        "categories": ["Java", "科技"],
        "industry": "Excel",
        "city": "Tableau",
        "status": "R",
        "company": "PostgreSQL",
    }
    assert Neo4jOpportunityGraph._skills(job) == []
    assert Neo4jOpportunityGraph._skills({
        **job, "requirements": "Python 和 SQL 为必备技能，掌握机器学习。",
    }) == ["Python", "SQL", "机器学习"]
    assert Neo4jOpportunityGraph._skills({"tags": '["Python",'}) == []


def test_public_title_and_prose_supply_only_bounded_named_competency_matches():
    skills = Neo4jOpportunityGraph._skills({
        "title": "数据分析工程师",
        "description": "JavaScript 开发；GitHub 上的材料提及 excelled 与 SQLAlchemy。",
        "requirements": "掌握 python、SQL、PostgreSQL 和 Excel。",
        "responsibilities": "Machine Learning、机器学习及统计分析。",
        "private_profile": "Docker",
        "source_payload": {"requirements": "Java"},
    })
    assert skills == ["Python", "SQL", "Excel", "PostgreSQL", "JavaScript", "数据分析", "机器学习", "统计分析"]
    assert Neo4jOpportunityGraph._skills({
        "title": "Research assistant", "description": "Go to the office; excelled at JavaScripted SQLAlchemy projects.",
    }) == []
    assert Neo4jOpportunityGraph._skills({"requirements": "Golang、R语言开发，熟悉 C++/C#。"}) == ["C++", "C#", "Go", "R"]


def test_skills_are_bounded_even_for_explicit_large_lists():
    assert len(Neo4jOpportunityGraph._skills({"skills": [f"competency-{index}" for index in range(100)]})) == 40


def test_language_versions_normalize_without_matching_longer_words():
    assert Neo4jOpportunityGraph._skills({
        "requirements": "熟悉Python3、Java8、C++17和C#8开发，了解 Python3.11 和 Java17。",
    }) == ["Python", "Java", "C++", "C#"]
    assert Neo4jOpportunityGraph._skills({
        "tags": '["Python3", "Java8", "C++17", "C#8", "Python3行业", "Java8Script"]',
        "requirements": "掌握 Python3、Java8、C++17和C#8。",
    }) == ["Python", "Java", "C++", "C#"]
    assert Neo4jOpportunityGraph._skills({
        "skills": ["Python3.11", "C++17", "沟通能力"],
    }) == ["Python", "C++", "沟通能力"]
    assert Neo4jOpportunityGraph._skills({
        "requirements": "Python3rd、Java8Script、C++17foo、C#8Project、Python3.11Script。",
    }) == []


def test_more_technical_competencies_have_bilingual_public_requirement_evidence():
    skills = Neo4jOpportunityGraph._skills({
        "requirements": "熟悉 NumPy、pandas、SciPy、scikit-learn、XGBoost、LightGBM、Keras、OpenCV；"
                        "掌握 NLP、计算机视觉与 LLM；具备 Node.js、React.js、Vue.js、FastAPI、Spring Boot、"
                        "RESTful API 和 GraphQL 开发经验；使用 Redis、ClickHouse、Apache Spark、"
                        "Apache Kafka、Apache Airflow、Terraform、AWS、Microsoft Azure 与 GCP。",
    })
    assert set(skills) == {
        "NumPy", "pandas", "SciPy", "scikit-learn", "XGBoost", "LightGBM", "Keras", "OpenCV",
        "自然语言处理", "计算机视觉", "大语言模型", "Node.js", "React", "Vue.js", "FastAPI",
        "Spring Boot", "REST API", "GraphQL", "Redis", "ClickHouse", "Apache Spark",
        "Apache Kafka", "Apache Airflow", "Terraform", "AWS", "Azure", "Google Cloud",
    }
    assert len(skills) == len(set(skills)) > 12


def test_business_competencies_need_specific_positive_public_phrases_not_generic_domains():
    skills = Neo4jOpportunityGraph._skills({
        "title": "金融产品校园招聘",
        "requirements": "掌握统计分析、线性回归、时间序列分析、概率论、假设检验、实验设计与数学优化；"
                        "熟悉风险管理、credit risk analysis、财务分析、财务报表分析、企业估值、"
                        "投资研究、投融资分析、portfolio management、due diligence 与预算管理。",
        "responsibilities": "开展产品需求分析、用户调研、市场研究、数字营销、竞品分析、"
                            "项目管理和供应链管理。",
        "tags": ["金融", "产品", "上海", "市场", "校园招聘"],
    })
    assert set(skills) == {
        "统计分析", "回归分析", "时间序列分析", "概率论", "假设检验", "实验设计", "优化建模",
        "风险管理", "信用分析", "财务分析", "财务报表分析", "估值分析", "投资分析", "融资分析",
        "投资组合管理", "尽职调查", "预算管理", "需求分析", "用户研究", "市场研究", "市场营销",
        "竞品分析", "项目管理", "供应链管理",
    }
    assert Neo4jOpportunityGraph._skills({"title": "金融产品市场校园招聘", "tags": ["金融", "产品"]}) == []


@pytest.mark.parametrize("job", [
    {"requirements": "C-level communication, R&D collaboration, Go to the office."},
    {"description": "Spark innovation with a swift response; react to changes; a flask in the lab."},
    {"description": "Spark公司、Swift集团、Oracle公司、React公司。"},
    {"requirements": "We value a swift response and the ability to react to customer needs."},
    {"requirements": "Spark公司、Swift集团、React公司。"},
    {"requirements": "SQL Server公司、React Native company、Oracle Database公司。"},
    {"company": "Python AWS 风险管理", "employer_name": "NumPy", "industry": "估值分析",
     "city": "用户研究", "candidate": "React.js", "private_profile": "财务分析",
     "source_payload": {"requirements": "Terraform"}, "tags": ["Python", "AWS", "风险管理"]},
])
def test_letters_company_names_and_private_or_categorical_fields_are_not_skill_evidence(job):
    assert Neo4jOpportunityGraph._skills(job) == []


def test_short_languages_require_language_phrases_and_explicit_slash_aliases_stay_whole():
    assert Neo4jOpportunityGraph._skills({
        "requirements": "熟悉 C语言、R语言与Go语言，掌握 programming in C。",
    }) == ["Go", "R", "C"]
    assert Neo4jOpportunityGraph._skills({
        "skills": ["C", "C17", "R", "R4.3", "Go", "Go1.21", "CI/CD", "A/B testing"],
    }) == ["C", "R", "Go", "持续集成", "实验设计"]
    assert Neo4jOpportunityGraph._skills({
        "description": "Apache Spark、Swift语言、Oracle数据库、React.js 和 Rust programming。",
    }) == ["Rust", "Swift", "React", "Oracle Database", "Apache Spark"]


def test_ambiguous_bare_names_need_technical_qualifiers_or_an_evidenced_skill_list():
    assert Neo4jOpportunityGraph._skills({
        "requirements": "熟悉React、Vue和Rust；Spark experience required；Swift programming required。",
    }) == ["Rust", "Swift", "React", "Vue.js", "Apache Spark"]
    assert Neo4jOpportunityGraph._skills({
        "requirements": "熟悉React公司，但必须掌握React.js。",
    }) == ["React"]


@pytest.mark.parametrize("requirements", [
    "Spark SQL is optional; SQL Server is not required; React Native不是必需。",
    "不要求Spark SQL；无需SQL Server；React Native is not mandatory。",
    "No AWS or Azure experience required; financial analysis is not a requirement; 用户研究不作要求。",
    "风险管理不是必需；无需财务分析；需求分析可选；No prior marketing strategy experience required.",
])
def test_expanded_and_overlapping_skill_names_keep_local_negation(requirements):
    assert Neo4jOpportunityGraph._skills({"requirements": requirements}) == []


def test_expanded_negation_does_not_suppress_separate_positive_mentions_or_explicit_fields():
    assert Neo4jOpportunityGraph._skills({
        "requirements": "不要求Spark SQL，但SQL必须。React Native is optional but FastAPI is essential。"
                        "财务分析不是必需，但风险管理必须掌握。",
    }) == ["SQL", "FastAPI", "风险管理"]
    assert Neo4jOpportunityGraph._skills({
        "skills": ["财务分析", "React Native"],
        "requirements": "财务分析与React Native不是必需。",
    }) == ["财务分析", "React Native"]


@pytest.mark.parametrize("requirements", [
    "不要求Python，不需要SQL。No Java experience required.",
    "无需熟悉 Python；SQL 不是必需；Java 经验并非必要。",
    "Python experience is not required; SQL is optional; Java knowledge is not necessary.",
    "No prior Python experience required. Without SQL knowledge. Not required to know Java.",
    "不需要 Python3，SQL 技能不作要求，Java8 可选。",
    "不要求Python和SQL；Java8与C++17不是必需。",
    "No Python or SQL experience required; Java and C# are optional.",
    "Python isn't required; SQL isn’t mandatory; Java skills aren't necessary.",
    "No requirement for Python; SQL is not a requirement; Java optional.",
])
def test_non_required_public_mentions_do_not_create_skill_requirements(requirements):
    assert Neo4jOpportunityGraph._skills({"requirements": requirements}) == []


@pytest.mark.parametrize("requirements, expected", [
    ("SQL不是必需，但Python必须。", ["Python"]),
    ("SQL不是必需但Python必须。", ["Python"]),
    ("SQL is not required but Python is essential.", ["Python"]),
    ("SQL is not required and Python is mandatory.", ["Python"]),
    ("不要求SQL，但必须熟悉Python3和Java8。", ["Python", "Java"]),
    ("Python is not required; Python3 proficiency is required for this role.", ["Python"]),
    ("Python不是不需要，SQL is not only required but essential.", ["Python", "SQL"]),
    ("不要求SQL和Java，但Python3与C++17必须掌握。", ["Python", "C++"]),
])
def test_negation_is_local_to_the_skill_mention(requirements, expected):
    assert Neo4jOpportunityGraph._skills({"requirements": requirements}) == expected


def test_only_negated_public_prose_suppresses_generic_tags_not_explicit_skill_fields():
    job = {"tags": '["SQL", "Python3", "互联网"]',
           "requirements": "SQL不是必需，但Python必须。"}
    assert Neo4jOpportunityGraph._skills(job) == ["Python"]
    assert Neo4jOpportunityGraph._skills({**job, "skills": ["SQL"]}) == ["SQL", "Python"]
    assert Neo4jOpportunityGraph._skills({
        "title": "SQL analyst", "requirements": "Python is not required",
        "responsibilities": "Python3 proficiency is required",
    }) == ["Python", "SQL"]


def test_only_supported_public_prose_reaches_batched_write_and_scoped_reads(monkeypatch):
    driver = RecordingDriver()
    graph = Neo4jOpportunityGraph("neo4j://unused.test")
    monkeypatch.setattr(graph, "_driver", lambda: driver)

    graph.sync_opportunities([public_job(title="工程师", tags='["SQL", "Java8", "互联网"]',
                                        requirements="SQL不是必需，但Python3、Java8、C++17和C#8必须掌握。")])

    write = next(parameters for query, parameters in driver.calls if "UNWIND $rows AS row" in query)
    assert write["rows"][0]["skills"] == ["Java", "Python", "C++", "C#"]
    reads = [parameters for _query, parameters in driver.calls if "ids" in parameters]
    assert all(parameters["skills_by_id"] == {"job-1": ["Java", "Python", "C++", "C#"]}
               for parameters in reads)
    assert not any("DELETE" in query for query, _parameters in driver.calls)


@pytest.mark.parametrize("evidence", ["explicit", "public-requirements"])
def test_skill_cap_is_consistent_for_write_scoped_count_and_ordered_response(monkeypatch, evidence):
    if evidence == "explicit":
        candidates = [f"competency-{index}" for index in range(100)]
        fields = {"skills": candidates}
    else:
        candidates = [name for name, _aliases in Neo4jOpportunityGraph._SKILL_ALIASES]
        labels = [next(alias for alias in aliases if alias not in {"c", "r", "go"})
                  for _name, aliases in Neo4jOpportunityGraph._SKILL_ALIASES]
        fields = {"requirements": "必须掌握 " + "、".join(labels)}
    expected = candidates[:40]
    driver = RecordingDriver(
        records=[{"id": "job-1", "skills": ["OLD_UNREQUESTED_SKILL", *reversed(candidates), candidates[0]]}],
        relationship_count=41,
    )
    graph = Neo4jOpportunityGraph("neo4j://unused.test")
    monkeypatch.setattr(graph, "_driver", lambda: driver)

    result = graph.sync_opportunities([public_job(title="工程师", city="", **fields)])

    write = next(parameters for query, parameters in driver.calls if "UNWIND $rows AS row" in query)
    assert write["rows"][0]["skills"] == expected
    reads = [(query, parameters) for query, parameters in driver.calls if "ids" in parameters]
    assert len(reads) == 2
    assert all(parameters["skill_limit"] == 40 for _query, parameters in reads)
    assert all(parameters["skills_by_id"] == {"job-1": expected} for _query, parameters in reads)
    assert "collect(DISTINCT s.name)[0..$skill_limit] AS skills" in reads[0][0]
    assert result["items"][0]["skills"] == expected
    assert len(result["items"][0]["skills"]) == len(set(result["items"][0]["skills"])) == 40
    assert result["relationships_stored"] == result["relationships_written"] == 41
    assert result["relationship_count_scope"] == "current_public_projection"
    assert "OLD_UNREQUESTED_SKILL" not in repr(result)


def test_two_hundred_public_jobs_use_one_bounded_unwind_write_without_private_payload(monkeypatch):
    driver = RecordingDriver(relationship_count=717)
    graph = Neo4jOpportunityGraph("neo4j://unused.test", database_name="graph-tests")
    monkeypatch.setattr(graph, "_driver", lambda: driver)
    jobs = [public_job(f"job-{index}", tags='["Python", "校园招聘"]',
                       requirements="SQL required", application_status="skipped", private_profile="private",
                       source_payload={"private": "secret"}, raw_text="private narrative")
            for index in range(200)]

    result = graph.sync_opportunities(jobs)

    writes = [(query, parameters) for query, parameters in driver.calls if "UNWIND $rows AS row" in query]
    assert len(writes) == 1
    query, parameters = writes[0]
    assert "MERGE (e)-[:POSTS]->(o)" in query
    assert "MERGE (o)-[:REQUIRES]->(s)" in query
    assert len(parameters["rows"]) == 200
    assert all(set(row) == {"id", "title", "company", "company_key", "location", "url", "skills",
                            "places", "locations", "within", "location_status"}
               for row in parameters["rows"])
    assert parameters["rows"][0]["skills"] == ["Python", "SQL", "数据分析"]
    assert "private" not in repr(driver.calls)
    assert "secret" not in repr(driver.calls)
    assert sum("CREATE CONSTRAINT" in query for query, _parameters in driver.calls) == 4
    assert not any("DELETE" in query for query, _parameters in driver.calls)
    assert result["opportunities"] == 200
    # This is the driver's stored-edge read, not an estimate or created count.
    assert result["relationships_stored"] == result["relationships_written"] == 717
    assert result["relationship_count_scope"] == "current_public_projection"
    assert "not newly created" in result["relationship_count_semantics"]
    count_query = " ".join(next(query for query, _parameters in driver.calls if "AS count" in query).split())
    # Aura/Cypher 5 rejects job_edges + count(...) as an implicit grouping
    # expression (42I18). Project the grouping key and aggregate separately.
    assert "WITH job_edges, count(DISTINCT within) AS place_edges" in count_query
    assert "RETURN job_edges + place_edges AS count" in count_query
    assert "RETURN job_edges + count(" not in count_query
    assert driver.database == "graph-tests"
    assert driver.closed


def test_graph_reads_only_current_validated_ids_employer_keys_and_skills(monkeypatch):
    records = [{"employer": "示例科技", "id": "visible", "title": "Python 数据分析岗位", "location": "上海",
                "skills": ["Python", "数据分析"]}]
    driver = RecordingDriver(records=records, relationship_count=3)
    graph = Neo4jOpportunityGraph("neo4j://unused.test")
    monkeypatch.setattr(graph, "_driver", lambda: driver)

    result = graph.sync_opportunities([public_job("visible"), public_job("invalid", company=""),
                                       public_job("", title="")])

    reads = [(query, parameters) for query, parameters in driver.calls
             if "RETURN e.name AS employer" in query or "AS count" in query]
    assert len(reads) == 2
    for query, parameters in reads:
        assert "o.id IN $ids" in query
        assert "e.key = $company_keys[o.id]" in query
        assert "s.name IN $skills_by_id[o.id]" in query
        assert parameters["ids"] == ["visible"]
        assert set(parameters["company_keys"]) == {"visible"}
        assert parameters["skills_by_id"] == {"visible": ["Python", "数据分析"]}
    assert {key: result["items"][0][key] for key in records[0]} == records[0]
    assert result["items"][0]["url"] == public_job("visible")["application_url"]
    assert result["items"][0]["location_status"] in {"mapped", "unmapped", "ambiguous"}
    assert result["opportunities"] == 1


def test_empty_input_uses_empty_read_scope_instead_of_returning_old_jobs(monkeypatch):
    driver = RecordingDriver()
    graph = Neo4jOpportunityGraph("neo4j://unused.test")
    monkeypatch.setattr(graph, "_driver", lambda: driver)

    result = graph.sync_opportunities([])

    assert result["items"] == []
    assert result["opportunities"] == result["relationships_stored"] == 0
    assert all(parameters["ids"] == [] for _query, parameters in driver.calls if "ids" in parameters)


@pytest.mark.parametrize("public_urls, expected", [
    ({"application_url": "https://example.test/apply", "official_url": "https://example.test/careers"},
     "https://example.test/apply"),
    ({"application_url": "", "official_url": "https://example.test/careers"}, "https://example.test/careers"),
    ({"application_url": "", "official_url": "", "url": "https://example.test/job"}, "https://example.test/job"),
])
def test_graph_url_prefers_public_application_then_official_page(monkeypatch, public_urls, expected):
    driver = RecordingDriver()
    graph = Neo4jOpportunityGraph("neo4j://unused.test")
    monkeypatch.setattr(graph, "_driver", lambda: driver)

    graph.sync_opportunities([public_job(**public_urls)])

    write = next(parameters for query, parameters in driver.calls if "UNWIND $rows AS row" in query)
    assert write["rows"][0]["url"] == expected


def test_graph_driver_closes_if_batched_write_fails(monkeypatch):
    driver = RecordingDriver()
    original_run = driver.run

    def fail_write(query, **parameters):
        if "UNWIND $rows AS row" in query:
            raise RuntimeError("simulated graph failure")
        return original_run(query, **parameters)

    monkeypatch.setattr(driver, "run", fail_write)
    graph = Neo4jOpportunityGraph("neo4j://unused.test")
    monkeypatch.setattr(graph, "_driver", lambda: driver)

    with pytest.raises(RuntimeError, match="simulated graph failure"):
        graph.sync_opportunities([public_job()])
    assert driver.closed

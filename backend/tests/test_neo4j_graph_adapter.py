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


def test_json_tags_only_contribute_complete_recognized_competencies():
    skills = Neo4jOpportunityGraph._skills({
        "tags": '["Python", "SQL", "Machine Learning", "机器学习", "北京", "互联网", "open", '
                '"示例科技", "校园招聘", "金融Python", "Python行业", "待官方核验"]',
        "categories": ["Java", "科技"],
        "industry": "Excel",
        "city": "Tableau",
        "status": "R",
        "company": "PostgreSQL",
    })
    assert skills == ["Python", "SQL", "机器学习"]
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
    assert len(Neo4jOpportunityGraph._skills({"skills": [f"competency-{index}" for index in range(100)]})) == 30


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
    assert all(set(row) == {"id", "title", "company", "company_key", "location", "url", "skills"}
               for row in parameters["rows"])
    assert parameters["rows"][0]["skills"] == ["Python", "SQL", "数据分析"]
    assert "private" not in repr(driver.calls)
    assert "secret" not in repr(driver.calls)
    assert sum("CREATE CONSTRAINT" in query for query, _parameters in driver.calls) == 3
    assert not any("DELETE" in query for query, _parameters in driver.calls)
    assert result["opportunities"] == 200
    # This is the driver's stored-edge read, not an estimate or created count.
    assert result["relationships_stored"] == result["relationships_written"] == 717
    assert result["relationship_count_scope"] == "current_public_projection"
    assert "not newly created" in result["relationship_count_semantics"]
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
    assert result["items"] == records
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

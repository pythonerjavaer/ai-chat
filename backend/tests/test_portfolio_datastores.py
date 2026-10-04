from backend.portfolio_datastores import GraphDBAcquisitionStore, MongoDocumentArchive, Neo4jOpportunityGraph
from backend.finance_analysis import acquisition_scenario


def test_optional_datastores_report_configuration_without_breaking_boot():
    mongo = MongoDocumentArchive()
    graph = Neo4jOpportunityGraph()
    graphdb = GraphDBAcquisitionStore()
    assert mongo.status()["status"] == "not_configured"
    assert graph.status()["status"] == "not_configured"
    assert graphdb.status()["status"] == "not_configured"
    assert mongo.archive_leap_edition(1, {"id": "d", "version": 1}, [])["status"] == "not_configured"
    assert graph.sync_opportunities([{"id": "job"}])["status"] == "not_configured"


def test_graphdb_stores_user_scoped_acquisition_and_sensitivity_relationships(monkeypatch):
    import httpx

    calls = []

    class Response:
        def raise_for_status(self):
            return None

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(httpx, "post", post)
    output = acquisition_scenario({"purchase_price": 160, "debt_share": 0.7, "debt_rate": 0.0843,
                                   "tax_rate": 0.3, "target_ebit": 20})
    run_id = "12345678-1234-5678-1234-567812345678"
    stored = GraphDBAcquisitionStore("http://127.0.0.1:7200", "acquisition-tests").store_acquisition_run(
        run_id, 42, output)
    assert stored["status"] == "stored"
    assert stored["scenario_relationships"] == len(output["financing_mix_sensitivity"]) + len(output["operating_stress_matrix"])
    assert calls[0][0].endswith("/repositories/acquisition-tests/statements")
    query = calls[0][1]["content"]
    assert "userId> \"42\"" in query
    assert "hasFinancingScenario" in query
    assert "hasStressScenario" in query


def test_future_radar_graph_only_keeps_unique_normalized_public_skill_labels():
    skills = Neo4jOpportunityGraph._skills({"skills": ["Python", " python ", "SQL/Excel"]})
    assert skills == ["Python", "SQL", "Excel"]
    assert Neo4jOpportunityGraph._skills({"tags": '["Python","SQL"]'}) == []
    assert Neo4jOpportunityGraph._skills({"tags": '["Python","SQL"]',
                                         "requirements": "Python and SQL required"}) == ["Python", "SQL"]


def test_mongo_archive_splits_large_materials_into_edition_and_chunk_documents(monkeypatch):
    import pymongo

    calls = {"replacements": [], "bulk": []}

    class Collection:
        def create_index(self, *_args, **_kwargs):
            return None

        def replace_one(self, identity, document, upsert=False):
            calls["replacements"].append((identity, document, upsert))

        def bulk_write(self, operations, ordered=False):
            calls["bulk"].extend(operations)

    class Database:
        def __getitem__(self, _name):
            return Collection()

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def __getitem__(self, _name):
            return Database()

        def close(self):
            pass

    monkeypatch.setattr(pymongo, "MongoClient", Client)
    result = MongoDocumentArchive("mongodb://localhost").archive_leap_edition(
        7,
        {"id": "doc-1", "version": 3, "title": "Annual report", "content_hash": "abc"},
        [{"chunk_position": 0, "paragraph_start": 1, "paragraph_end": 2,
          "heading_path": ["Results"], "stable_anchor": "results-1", "content": "Evidence",
          "embedding": [0.1, 0.2], "embedding_model": "local-test"}],
    )
    assert result == {"status": "archived", "archived_chunks": 1}
    assert len(calls["replacements"]) == 1
    assert len(calls["bulk"]) == 1

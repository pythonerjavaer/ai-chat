"""Current public geographic graph projection, with a recording Neo4j driver."""

from types import SimpleNamespace

from backend import portfolio_datastores
from backend.future_radar import geography
from backend.portfolio_datastores import Neo4jOpportunityGraph
from backend.tests.test_future_radar_geography import synthetic_catalog
from backend.tests.test_neo4j_graph_adapter import RecordingDriver, public_job


def graph_with_catalog(monkeypatch, records, count=0):
    catalog = synthetic_catalog()
    driver = RecordingDriver(records=records, relationship_count=count)
    graph = Neo4jOpportunityGraph("neo4j://unused.test")
    monkeypatch.setattr(portfolio_datastores, "china_place_catalog", lambda: catalog)
    monkeypatch.setattr(geography, "china_place_catalog", lambda: catalog)
    monkeypatch.setattr(graph, "_driver", lambda: driver)
    return graph, driver


def test_location_nodes_hierarchy_and_points_stay_inside_one_bounded_public_batch(monkeypatch):
    graph, driver = graph_with_catalog(monkeypatch, [{"id": "visible", "skills": ["Python"]}], count=9)
    result = graph.sync_opportunities([
        public_job("visible", city="广东省深圳市南山区", private_profile="PRIVATE_ACCOUNT",
                   source_payload={"private": "PRIVATE_SOURCE"},
                   places=[{"id": "PRIVATE_LOCATION", "longitude": 0, "latitude": 0}]),
        public_job("remote", city="全国", description="面向北京、上海、香港客户"),
        public_job("unknown", city="公开未知坐标区"),
    ])
    writes = [(query, parameters) for query, parameters in driver.calls if "UNWIND $rows AS row" in query]
    assert len(writes) == 1
    query, parameters = writes[0]
    visible, remote, unknown = parameters["rows"]
    assert [place["id"] for place in visible["places"]] == ["440305"]
    assert [place["id"] for place in visible["locations"]] == ["440305", "440300", "440000"]
    assert visible["within"] == [{"child": "440305", "parent": "440300"}, {"child": "440300", "parent": "440000"}]
    assert remote["places"] == remote["locations"] == remote["within"] == []
    assert unknown["places"][0]["crs"] == "unknown-display"
    assert "MERGE (o)-[:LOCATED_IN]->(l)" in query
    assert "MERGE (child)-[:WITHIN]->(parent)" in query
    assert "place.crs='WGS84'" in query and "srid:4326" in query and "ELSE null END" in query
    assert query.index("FOREACH (place IN row.locations") < query.index("UNWIND row.skills")
    assert "(e)-[:LOCATED_IN]" not in query
    assert "PRIVATE_" not in repr(driver.calls)
    assert result["items"][0]["places"] == visible["places"]
    assert result["relationships_stored"] == result["relationships_written"] == 9


def test_refresh_reads_count_and_response_exclude_historical_places_and_unscoped_jobs(monkeypatch):
    graph, driver = graph_with_catalog(monkeypatch, [
        {"id": "visible", "employer": "old employer", "title": "old title", "location": "深圳",
         "url": "https://private.invalid/old", "skills": ["Python", "OLD_UNSUPPORTED_SKILL"],
         "place_keys": ["440300"], "private_payload": "PRIVATE_SOURCE"},
        {"id": "old-job", "skills": ["Python"], "place_keys": ["110100"]},
    ])
    result = graph.sync_opportunities([public_job("visible", city="广州"), public_job("invalid", company="")])
    reads = [(query, parameters) for query, parameters in driver.calls if "ids" in parameters]
    assert len(reads) == 2
    for query, scope in reads:
        assert scope["ids"] == ["visible"]
        assert scope["place_ids_by_id"] == {"visible": ["440100"]}
        assert scope["location_keys"] == ["440100", "440000"]
        assert scope["within_pairs"] == [{"child": "440100", "parent": "440000"}]
        assert "l.key IN $place_ids_by_id[o.id]" in query
    count = next(query for query, _parameters in reads if "AS count" in query)
    assert "child.key IN $location_keys AND parent.key IN $location_keys" in count
    assert "{child:child.key,parent:parent.key} IN $within_pairs" in count
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["location"] == "广州" and item["employer"] == "示例科技"
    assert item["skills"] == ["Python"] and item["places"][0]["id"] == "440100"
    assert item["url"] == public_job("visible")["application_url"]
    assert "PRIVATE_" not in repr(result) and "old-job" not in repr(result)


def test_empty_input_never_counts_or_returns_old_location_nodes(monkeypatch):
    graph, driver = graph_with_catalog(monkeypatch, [{"id": "old-job", "skills": ["Python"]}])
    result = graph.sync_opportunities([])
    assert result["items"] == []
    for _query, scope in driver.calls:
        if "ids" in scope:
            assert scope["ids"] == scope["location_keys"] == scope["within_pairs"] == []
            assert scope["place_ids_by_id"] == {}


def test_graph_route_exposes_geojson_parent_aggregates_public_urls_and_spatial_edges_only(monkeypatch):
    from backend import main
    from backend.future_radar import personal

    jobs = [public_job("one", city="深圳市南山区"), public_job("two", city="Hong Kong"),
            public_job("three", city="remote")]
    graph, _driver = graph_with_catalog(monkeypatch, [
        {"id": job["id"], "skills": ["Python"]} for job in jobs])
    observed = []

    def projection(**options):
        observed.append(options)
        assert options["application_states"] == {"not-visible": "skipped"}
        assert options["input_sanitizer"] is main._public_search_update_detail
        assert isinstance(options["cache_scope"], str)
        return [options["input_sanitizer"]({**job, "private_profile": "PRIVATE_ACCOUNT"}) for job in jobs]

    monkeypatch.setattr(main, "settings", SimpleNamespace(neo4j_uri="neo4j://unused.test"))
    monkeypatch.setattr(main, "future_radar_service", SimpleNamespace(repository=SimpleNamespace(list_graph_opportunities=projection)))
    monkeypatch.setattr(main, "neo4j_opportunity_graph", graph)
    monkeypatch.setattr(main, "_radar_company_aliases", lambda: {})
    monkeypatch.setattr(personal, "application_states", lambda _connect, user_id: {"not-visible": "skipped"} if user_id == 41 else {})
    result = main.future_radar_relationship_graph({"id": 41})
    assert len(observed) == 1 and result["postgres_opportunities_considered"] == 3
    assert {item["id"] for item in result["items"]} == {"one", "two", "three"}
    assert all(item["url"].startswith("https://careers.example.test/") for item in result["items"])
    assert result["geography"]["matched_opportunities"] == 2
    assert result["geography"]["unmapped_opportunities"] == 1
    assert {feature["id"] for feature in result["geography"]["features"]["features"]} == {
        "440305", "440300", "440000", "810000"}
    locations = [node for node in result["nodes"] if node["kind"] == "location"]
    assert {node["id"] for node in locations} == {"location:440305", "location:440300", "location:440000", "location:810000"}
    edges = result["relationships"]
    assert len([edge for edge in edges if edge["kind"] == "LOCATED_IN"]) == 2
    assert len([edge for edge in edges if edge["kind"] == "WITHIN"]) == 2
    assert not any(edge["source"].startswith("employer:") and edge["kind"] in {"LOCATED_IN", "WITHIN"} for edge in edges)
    assert "PRIVATE_" not in repr(result) and "not-visible" not in repr(result)


def test_graph_route_preserves_not_configured_without_reading_jobs(monkeypatch):
    from backend import main

    def forbidden(**_options):
        raise AssertionError("Unconfigured Neo4j must retain its existing no-read behavior")

    monkeypatch.setattr(main, "settings", SimpleNamespace(neo4j_uri=""))
    monkeypatch.setattr(main, "future_radar_service", SimpleNamespace(repository=SimpleNamespace(list_graph_opportunities=forbidden)))
    result = main.future_radar_relationship_graph({"id": 41})
    assert result["status"] == "not_configured" and result["nodes"] == result["relationships"] == []

"""Lossless derived-cache encoding and production-sized public record replay.

No database, account, environment file or source network is used here. The
6,082-row case scores synthetic public jobs with the real public presenter.
"""

import math
import os
from dataclasses import dataclass
from datetime import date

import pytest

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ.setdefault("OPENAI_API_KEY", "not-used-in-compact-cache-tests")
os.environ.setdefault("JWT_SECRET", "isolated-compact-cache-test-secret-at-least-32-bytes")
os.environ.setdefault("FUTURE_RADAR_ENABLED", "false")
os.environ.setdefault("RECRUITMENT_REFRESH_MINUTES", "0")

from backend.future_radar.opportunity_cache import (
    _retained_size, decode_scoring_overrides, encode_scoring_overrides,
)
from backend.future_radar.repository import RadarRepository


@dataclass
class CustomScore:
    tags: list[str]


class CustomString(str):
    pass


def test_exact_json_public_types_round_trip_and_decode_is_independent():
    original = {
        "label": "公开岗位评分",
        "integer": 100,
        "float": -0.0,
        "bool": True,
        "none": None,
        "nested": [{"scores": [1, 2.5, None], "tags": ["公开来源"]}],
    }
    encoded = encode_scoring_overrides(original)
    assert type(encoded) is bytes
    decoded = decode_scoring_overrides(encoded)
    assert decoded == original
    assert type(decoded["integer"]) is int
    assert type(decoded["float"]) is float
    assert type(decoded["bool"]) is bool
    assert math.copysign(1, decoded["float"]) == -1
    decoded["nested"][0]["tags"].append("client mutation")
    assert decode_scoring_overrides(encoded) == original


@pytest.mark.parametrize("custom", (
    ("tuple", 1), {1: "non-string key"}, date(2026, 10, 2),
    CustomScore(["public"]), CustomString("public"),
    float("nan"), float("inf"), float("-inf"), "\ud800",
))
def test_custom_or_nonfinite_types_keep_legacy_dictionary_deepcopy(custom):
    original = {"custom": custom, "mutable": ["public"]}
    encoded = encode_scoring_overrides(original)
    assert encoded is original
    decoded = decode_scoring_overrides(encoded)
    assert type(decoded["custom"]) is type(custom)
    if type(custom) is float and math.isnan(custom):
        assert math.isnan(decoded["custom"])
    else:
        assert decoded == original
    decoded["mutable"].append("client mutation")
    assert original["mutable"] == ["public"]
    if isinstance(custom, CustomScore):
        decoded["custom"].tags.append("client mutation")
        assert custom.tags == ["public"]


def test_shared_mutable_containers_and_cycles_keep_legacy_aliases():
    shared = ["public"]
    original = {"first": shared, "second": shared}
    assert encode_scoring_overrides(original) is original
    decoded = decode_scoring_overrides(original)
    assert decoded["first"] is decoded["second"]
    assert decoded["first"] is not shared
    cycle = {}
    cycle["self"] = cycle
    assert encode_scoring_overrides(cycle) is cycle
    decoded_cycle = decode_scoring_overrides(cycle)
    assert decoded_cycle["self"] is decoded_cycle


def test_only_fixed_schema_keys_are_shared_between_decoded_public_records():
    encoded = encode_scoring_overrides({"job_score": 88, "custom-public-key-not-in-schema": 1})
    first = decode_scoring_overrides(encoded)
    second = decode_scoring_overrides(encoded)
    assert next(key for key in first if key == "job_score") is next(key for key in second if key == "job_score")
    assert next(key for key in first if key.startswith("custom-")) is not next(key for key in second if key.startswith("custom-"))


def test_6082_public_records_fit_unchanged_budget_and_observation_replay_never_rescores():
    from backend import main

    repository = RadarRepository(lambda: pytest.fail("Synthetic replay must not open a database"))
    companies = (
        "中国移动通信集团山东有限公司济南分公司", "中国电信股份有限公司上海分公司",
        "中国联合网络通信有限公司广东省分公司", "腾讯", "中国工商银行杭州分行", "示例科技公开实体",
    )
    cities = ("济南", "上海", "广州", "深圳", "杭州", "北京")
    roles = ("数据分析", "软件研发", "金融风险分析", "产品研究", "业务咨询", "数字化研发")
    profile = {"desired_roles": ["数据分析"], "locations": ["上海", "杭州"],
               "private_note": "PRIVATE_PROFILE_NOT_RETAINED"}
    initial = "2026-10-02T00:00:00Z"
    current = "2026-10-03T00:00:00Z"
    scored = []

    def prepare_sanitized(raw, public):
        scored.append(raw["id"])
        return main._public_radar_opportunity(raw, profile, public_input=public)

    def legacy_prepare(_raw):
        pytest.fail("The opt-in public presenter must reuse the sanitized projection")

    def raw_record(index, stamp):
        branch = index % len(companies)
        key = f"synthetic-{index:05d}"
        return {
            "id": key, "external_id": "public-" + key,
            "company": companies[branch], "title": f"2027 校园招聘{roles[branch]}岗 {index}",
            "city": cities[branch], "region": "中国大陆", "employer_type": "科技或金融企业",
            "industry": "科技与金融", "primary_category": "state_tech_telecom" if branch < 3
            else "traditional_banks" if branch == 4 else "internet_tech",
            "industry_tags": ["科技"], "role_tags": ["数据分析"],
            "official_url": f"https://careers.example.invalid/campus/{key}",
            "application_url": f"https://careers.example.invalid/apply/{key}",
            "closing_date": "2027-10-01", "status": "open", "verification_status": "pending",
            "confidence_score": 0.8, "tags": ["校园招聘", "2027届", "公开招聘"],
            "description": f"公开岗位说明{index}，参与技术与业务数据研究。" * 25,
            "responsibilities": f"公开职责{index}，分析经营数据并构建业务看板。" * 20,
            "requirements": f"面向2027届毕业生，掌握数据分析、Python或SQL技能；公开岗位条件{index}。" * 20,
            "source_ratings": [], "first_seen_at": initial, "last_seen_at": stamp,
            "last_changed_at": stamp, "latest_event_type": "UPDATED", "latest_event_at": stamp,
            "evidence": "PRIVATE_EVIDENCE_NOT_RETAINED",
            "_member_ids": frozenset([key]), "_member_external_ids": frozenset(["public-" + key]),
            "_sort_position": index,
            "sources": [{
                "source_id": f"public-discovery-{source}", "name": f"公开招聘来源 {source}",
                "source_type": "chatgpt_sync", "trust_level": "discovery",
                "source_url": f"https://careers.example.invalid/source/{source}/{key}",
                "verification_role": "discovered", "active": True,
                "discovered_at": initial, "last_seen_at": stamp,
                "evidence": "PRIVATE_SOURCE_NOT_RETAINED",
            } for source in range(1 + index % 3)],
        }

    options = {
        "prepare": legacy_prepare, "input_sanitizer": main._public_search_update,
        "record_cache_scope": ("synthetic-private-database", "opaque-user-profile-rules"),
        "prepare_sanitized": prepare_sanitized,
    }
    samples = {}
    for index in range(6082):
        result = repository._prepare_opportunity_record(raw_record(index, initial), **options)
        if index in (0, 3000, 6081):
            samples[index] = result
    cache = repository._opportunity_record_cache
    assert cache.max_bytes == 64 * 1024 * 1024 and cache.max_entries == 12_000
    assert cache.info()["entries"] == len(scored) == 6082
    assert cache.info()["bytes"] <= cache.max_bytes
    assert all(type(entry[2]["overrides"]) is bytes for entry in cache._entries.values())
    retained = repr(cache._entries)
    assert "PRIVATE_PROFILE_NOT_RETAINED" not in retained
    assert "PRIVATE_EVIDENCE_NOT_RETAINED" not in retained
    assert "PRIVATE_SOURCE_NOT_RETAINED" not in retained
    for index in range(6082):
        raw = raw_record(index, current)
        result = repository._prepare_opportunity_record(raw, **options)
        assert result["last_seen_at"] == result["last_changed_at"] == result["latest_event_at"] == current
        assert all(source["last_seen_at"] == current for source in result["sources"])
        if index in samples:
            expected = main._public_radar_opportunity(raw, profile)
            assert result == expected
            assert result["score_breakdown"] == samples[index]["score_breakdown"]
            result["score_breakdown"].clear()
            result["sources"][0]["name"] = "client mutation"
            fresh = repository._prepare_opportunity_record(raw, **options)
            assert fresh == expected
    assert len(scored) == 6082, "Observation-only rebuild must reuse every score, not sequentially thrash"
    assert cache.info()["entries"] == 6082
    repository._opportunity_rows = lambda **_kwargs: [raw_record(index, current) for index in range(6082)]
    pool = repository._prepare_opportunity_pool(
        filters={}, public_url=main._public_reference_url, company_aliases=main._radar_company_aliases(),
        **options,
    )
    assert len(scored) == len(pool.items) == 6082
    pool_bytes = _retained_size(pool)
    print(f"synthetic-6082: compact-record-bytes={cache.info()['bytes']}, pool-bytes={pool_bytes}, replay-extra-scores=0")
    assert pool_bytes <= 72 * 1024 * 1024
    repository._opportunity_cache.get_or_compute(("synthetic-full-pool",), lambda: pool)
    assert repository._opportunity_cache.info()["entries"] == 1, "Decoded records must still fit the full-pool cache"
    assert repository._opportunity_cache.info()["bytes"] <= 72 * 1024 * 1024
    assert repository._opportunity_cache.get_or_compute(
        ("synthetic-full-pool",), lambda: pytest.fail("Warm full-pool hit repeated scoring"),
    ) is pool

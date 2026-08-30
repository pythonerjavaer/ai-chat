"""Bounded pure-vocabulary caches must preserve every classification and score."""

import re
from copy import deepcopy

import pytest

from backend import recruitment as ranking


def reference_marker_matches(text, marker):
    marker = marker.casefold().strip()
    if not marker:
        return False
    if re.fullmatch(r"[a-z0-9_+.# -]+", marker):
        escaped = re.escape(marker).replace(r"\ ", r"\s+")
        return re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", text) is not None
    return marker in text


def reference_categories(value):
    codes = set()
    for raw in ranking._as_values(value):
        text = raw.casefold().strip()
        if text in ranking.CATEGORY_ORDER:
            codes.add(text)
            continue
        private_context = any(reference_marker_matches(text, term) for term in (
            "私募", "私募基金", "对冲基金", "private fund", "private_fund",
            "hedge fund", "hedge_fund",
        ))
        for code, aliases in ranking.CATEGORY_ALIASES.items():
            for alias in aliases:
                alias_text = alias.casefold()
                if alias_text == "基金" and private_context:
                    continue
                if text == alias_text or reference_marker_matches(text, alias_text):
                    codes.add(code)
                    break
    return codes


@pytest.fixture(autouse=True)
def isolated_caches():
    ranking._marker_rule.cache_clear()
    ranking._category_codes_for_text.cache_clear()
    yield
    ranking._marker_rule.cache_clear()
    ranking._category_codes_for_text.cache_clear()


@pytest.mark.parametrize("marker", [
    "", " AI ", "ai", "c++", "j.p. morgan", "private fund", "hedge_fund",
    "data science", "人工智能", "公募基金", "Ⅰ", "税务", "devops",
])
@pytest.mark.parametrize("text", [
    "", "ai engineer", "paid operations", "c++ engineer", "j.p. morgan analyst",
    "private   fund", "hedge_fund analyst", "data\nscience graduate",
    "人工智能应用与公募基金", "ⅰ", "税务咨询", "devops development",
])
def test_compiled_marker_matches_reference_boundaries(marker, text):
    assert ranking._marker_matches(text, marker) == reference_marker_matches(text, marker)


@pytest.mark.parametrize("value", [
    None, "", [], {}, 0, 42, "UNKNOWN",
    ["银行/金融", "互联网企业", "私募基金", None],
    {"label": "私募基金"}, " public fund / quant ", "私募/基金/量化",
    "公募基金/量化/资产管理", "金融科技/综合金融/保险",
])
def test_taxonomy_cache_preserves_scalar_and_list_semantics(value):
    assert ranking._category_codes_for_value(value) == reference_categories(value)


def test_every_alias_and_canonical_category_is_unchanged():
    values = [
        *ranking.CATEGORY_ORDER,
        *(alias for aliases in ranking.CATEGORY_ALIASES.values() for alias in aliases),
    ]
    for value in values:
        assert ranking._category_codes_for_value(value) == reference_categories(value)
        assert ranking._category_codes_for_value([value, value.upper(), "岗位"]) == reference_categories(
            [value, value.upper(), "岗位"]
        )


def test_returned_category_sets_cannot_mutate_shared_cache():
    result = ranking._category_codes_for_value("私募基金")
    expected = reference_categories("私募基金")
    result.clear()
    result.add("internet_tech")
    assert ranking._category_codes_for_value("私募基金") == expected


def test_changed_record_content_uses_a_new_key_without_stale_job_id_cache():
    job = {"id": "same-local-job", "employer_type": "互联网企业"}
    assert ranking.semantic_employer_categories(job) == {"internet_tech"}
    job["employer_type"] = "烟草/专卖"
    assert ranking.semantic_employer_categories(job) == {"tobacco_monopoly"}
    assert ranking._marker_matches("ai product", "ai")
    assert not ranking._marker_matches("paid operations", "ai")


def test_taxonomy_cache_entry_and_retained_text_sizes_are_bounded():
    cache = ranking._category_codes_for_text
    maximum = cache.cache_info().maxsize
    assert maximum == 2048
    for index in range(maximum + 20):
        ranking._category_codes_for_value(f"unclassified-unit-{index}")
    assert cache.cache_info().currsize == maximum
    before = cache.cache_info()
    long_text = "公募基金 " + ("没有更多类别 " * 100)
    assert len(long_text) > 512
    assert ranking._category_codes_for_value(long_text) == reference_categories(long_text)
    after = cache.cache_info()
    assert (after.hits, after.misses, after.currsize) == (
        before.hits, before.misses, before.currsize,
    )


def test_compiled_marker_cache_is_bounded():
    maximum = ranking._marker_rule.cache_info().maxsize
    assert maximum == 1024
    for index in range(maximum + 20):
        ranking._marker_matches("public data", f"unit-marker-{index}")
    assert ranking._marker_rule.cache_info().currsize == maximum


def test_all_category_role_profile_scores_equal_uncached_algorithm(monkeypatch):
    jobs = []
    for category in ranking.CATEGORY_ORDER:
        for role in ("金融科技产品经理", "风险分析师", "普通运营支持岗"):
            jobs.append({
                "id": "same-job-identity",
                "company": "示例科技银行",
                "title": f"2027校园招聘 {role}",
                "city": "上海",
                "employer_type": category,
                "industry_tags": [category],
                "role_tags": [],
                "tags": ["校园招聘", "2027届"],
                "responsibilities": "负责分析经营数据与风险，参与产品研究和业务指标构建。",
                "requirements": "面向应届毕业生，金融或计算机相关专业。",
            })
    profiles = [
        {},
        {"desired_roles": ["产品经理"], "industries": ["金融科技"]},
        {"desired_roles": ["风险"], "industries": ["银行"]},
        {"desired_roles": None, "industries": {"label": "数据"}},
    ]
    unchanged_inputs = deepcopy(jobs)
    expected = []
    with monkeypatch.context() as patch:
        patch.setattr(ranking, "_category_codes_for_value", reference_categories)
        patch.setattr(ranking, "_marker_matches", reference_marker_matches)
        for job in jobs:
            expected.extend(ranking.score_job(job, profile) for profile in profiles)
    actual = [ranking.score_job(job, profile) for job in jobs for profile in profiles]
    assert actual == expected
    assert jobs == unchanged_inputs


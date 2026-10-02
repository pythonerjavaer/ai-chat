"""Equivalent cold-score work reuse, never shared job/profile text caches."""

from copy import deepcopy
import re

import pytest

from backend import recruitment
from backend.future_radar import normalization


def legacy_operator(company):
    """Independent oracle for the original per-call directory normalization."""
    key = normalization.normalized_key(company)
    if any(marker in key for marker in ("招聘", "合作伙伴", "代理商", "加盟", "外包", "服务商")):
        return None
    for operator, roots in normalization._OPERATOR_ROOTS.items():
        brands = tuple(normalization.normalized_key(name)
                       for name in normalization._OPERATOR_BRANDS[operator])
        if key in roots or key in brands:
            return operator
        for root in (*roots, *brands):
            if not key.startswith(root):
                continue
            branch = key[len(root):]
            if not branch.startswith(normalization._OPERATOR_REGIONS):
                continue
            if branch in normalization._OPERATOR_REGIONS:
                return operator
            if re.fullmatch(r"[\u4e00-\u9fff]{2,24}(?:公司|有限公司|分公司|研究院|分院)", branch):
                return operator
    return None


def test_static_operator_brand_lookup_matches_original_directory_and_guardrails():
    names = [name for values in normalization._OPERATOR_BRANDS.values() for name in values]
    names += [name for values in normalization._OPERATOR_ROOTS.values() for name in values]
    examples = ["", None, "中国银行", "中国移动互联网协会", "中国联通合作伙伴公司",
                "中国电信招聘平台", "天翼云外包服务商", "中国移动浙江分公司",
                "中国移动未知县分公司", "中移九天人工智能科技（北京）有限公司"]
    for name in names:
        examples.extend((name, name + "北京", name + "山东分公司", name + "供应链公司"))
    for company in examples:
        assert normalization.canonical_telecom_operator(company) == legacy_operator(company), company


def test_operator_lookup_only_normalizes_the_input_not_the_static_brands(monkeypatch):
    original = normalization.normalized_key
    calls = []

    def observed(value):
        calls.append(value)
        return original(value)

    monkeypatch.setattr(normalization, "normalized_key", observed)
    assert normalization.canonical_telecom_operator("公开未知企业") is None
    assert calls == ["公开未知企业"]


@pytest.mark.parametrize("job", [
    {"company": "腾讯", "title": "数据分析师", "requirements": "岗位职责：参与数据分析和产品研究。"},
    {"company": "南方基金", "title": "普通财务岗位", "requirements": "参与财务分析。"},
    {"company": "南方基金", "title": "AI产品支持助理", "requirements": "参与AI产品研究。"},
])
def test_unmatched_named_anchor_does_not_parse_duties(job, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("An ineligible company/title must not pay for duty parsing")

    monkeypatch.setattr(recruitment, "_role_source_text", forbidden)
    assert recruitment._curated_job_tier_anchor(job) == (None, None)


@pytest.mark.parametrize("duties,expected", [
    ("负责人工智能产品设计和数据分析。", ("T1", "named_example")),
    ("专业要求：人工智能、金融、计算机相关专业。", (None, None)),
    ("公司简介：本公司是人工智能和金融领域的知名企业。", (None, None)),
    ("", (None, None)),
])
def test_named_anchor_keeps_actual_duty_evidence_and_parses_it_once(duties, expected, monkeypatch):
    original = recruitment._role_source_text
    calls = []

    def observed(job):
        calls.append(job)
        return original(job)

    monkeypatch.setattr(recruitment, "_role_source_text", observed)
    job = {"company": "南方基金", "title": "AI产品经理", "responsibilities": duties}
    assert recruitment._curated_job_tier_anchor(job) == expected
    assert len(calls) == 1
    assert calls[0]["title"] == ""


def test_first_release_anchor_still_precedes_named_evidence(monkeypatch):
    anchor_id, (company, title, tier) = next(iter(recruitment.CURATED_JOB_TIER_ANCHORS_BY_ID.items()))

    def forbidden(*_args, **_kwargs):
        pytest.fail("A code-owned exact anchor must not require new duty evidence")

    monkeypatch.setattr(recruitment, "_role_source_text", forbidden)
    assert recruitment._curated_job_tier_anchor(
        {"id": anchor_id, "company": company, "title": title},
    ) == (tier, "first_release")


SCORE_CASES = [
    {"company": "腾讯", "title": "数据分析师", "requirements": "岗位职责：负责SQL数据分析和产品研究。任职要求：计算机专业。"},
    {"company": "中国联通山东分公司", "title": "数字化产品经理", "responsibilities": "负责产品设计、风险模型和数字化战略。"},
    {"company": "中信证券山东分公司数字化发展部", "title": "风险科技分析师", "responsibilities": "负责信用风险分析和模型验证。"},
    {"company": "南方基金", "title": "AI产品经理", "responsibilities": "负责人工智能产品设计和数据分析。"},
    {"company": "Kearney", "title": "Business Analyst", "responsibilities": "Analyze business strategy and deliver consulting research."},
    {"company": "中国银行上海分行", "title": "客户经理", "responsibilities": "负责销售目标、客户拓展和客户维护。"},
    {"company": "腾讯", "title": "普通运营支持岗位", "responsibilities": "负责日常台账和工单处理。"},
    {"company": "未知独立企业", "title": "专业人才岗", "requirements": "专业要求：金融、计算机、人工智能。"},
    {"company": "腾讯", "title": "AI产品研究岗位", "responsibilities": "专业要求：人工智能。", "requirements": "岗位职责：负责风险模型研究和产品设计。"},
    {"company": "腾讯", "title": "数据分析师", "responsibilities": "负责数据分析。", "source_ratings": [{"scope": "job", "tier_code": "T0.5", "score": 88.25, "source_id": "public-monitor"}]},
]


@pytest.mark.parametrize("job", SCORE_CASES)
def test_full_score_matches_context_recomputed_helper_oracle(job, monkeypatch):
    """Recomputing each existing helper must yield the same complete score."""
    job = {"city": "上海", "industry": "金融科技", "tags": [], **deepcopy(job)}
    profile = {"desired_roles": ["数据分析", "风险"], "industries": ["金融"]}
    originals = {name: getattr(recruitment, name) for name in (
        "_normalize_role_tags", "_role_text", "_score_dimensions", "_calibrated_job_score",
    )}
    unchanged_job, unchanged_profile = deepcopy(job), deepcopy(profile)
    optimized = recruitment.score_job(job, profile)
    for name, function in originals.items():
        # Drop only the new transient context parameters. The original helper
        # defaults re-derive the same text/tags/low-value evidence from the job.
        def recompute(*args, _function=function, **_kwargs):
            return _function(*args)
        monkeypatch.setattr(recruitment, name, recompute)
    assert recruitment.score_job(job, profile) == optimized
    assert job == unchanged_job
    assert profile == unchanged_profile


def test_one_scored_job_reuses_role_source_and_low_value_check(monkeypatch):
    calls = {"_role_source_text": 0, "_is_low_value_role": 0, "_normalize_role_tags": 0}
    for name in calls:
        function = getattr(recruitment, name)
        def observed(*args, _name=name, _function=function, **kwargs):
            calls[_name] += 1
            return _function(*args, **kwargs)
        monkeypatch.setattr(recruitment, name, observed)
    result = recruitment.score_job({
        "company": "腾讯", "title": "数据分析师", "city": "上海", "tags": [],
        "requirements": "岗位职责：负责SQL数据分析和产品研究。任职要求：计算机专业。",
    }, {})
    assert result["scoring_status"] == "scored"
    # One real job parse and one title-only guard; no shared content cache.
    assert calls == {"_role_source_text": 2, "_is_low_value_role": 1, "_normalize_role_tags": 2}


def test_explicit_empty_transient_source_is_not_recomputed(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("An explicitly empty source is a valid computed result")

    monkeypatch.setattr(recruitment, "_role_source_text", forbidden)
    assert recruitment._normalize_role_tags({"title": "数据分析"}, source_text="") == []
    assert recruitment._role_text({"title": "数据分析"}, [], source_text="") == ""

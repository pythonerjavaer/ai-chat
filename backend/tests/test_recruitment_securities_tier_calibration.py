import pytest

from backend.recruitment import SCORING_VERSION, SCORING_WEIGHTS, score_job


def _core_securities_job(company: str, **fields) -> dict:
    job = {
        "company": company,
        "title": "金融科技数据产品与风险分析岗",
        "city": "北京",
        "employer_type": "证券公司",
        "industry": "证券金融",
        "responsibilities": (
            "负责金融科技数据产品、风险模型和商业分析，参与AI产品策略、"
            "模型治理及数字化建设。"
        ),
        "requirements": "面向2027届毕业生，金融、会计、计算机或管理相关专业。",
    }
    job.update(fields)
    return job


@pytest.mark.parametrize(
    ("company", "institution_tier", "job_tier", "score"),
    [
        ("中信证券股份有限公司总部", "T0.5", "T0.5", 89),
        ("中国国际金融股份有限公司总部", "T0.5", "T0.5", 89),
        ("华泰证券股份有限公司总部", "T1.5", "T1", 84),
        ("中信建投证券股份有限公司总部", "T1.5", "T1", 84),
        ("国泰海通证券股份有限公司总部", "T1.5", "T1", 84),
        ("招商证券股份有限公司总部", "T2", "T1.5", 79),
        ("广发证券股份有限公司总部", "T2", "T1.5", 79),
    ],
)
def test_canonical_broker_legal_names_follow_the_company_pyramid(
    company, institution_tier, job_tier, score,
):
    ranked = score_job(_core_securities_job(company), {})
    assert ranked["organization_assessment"]["level"] == "group_headquarters"
    assert ranked["institution_tier_code"] == institution_tier
    assert ranked["tier_code"] == job_tier
    assert ranked["job_score"] == score
    assert ranked["tier_code"] != "T0"


@pytest.mark.parametrize(
    "company",
    [
        "中信证券山东分公司数字化发展部",
        "华泰证券江苏省分公司数字化发展部",
        "招商证券广东省分公司数字化发展部",
    ],
)
def test_branch_internal_department_keeps_parent_baseline_but_not_headquarters_score(company):
    ranked = score_job(_core_securities_job(company), {})
    assert ranked["organization_assessment"]["level"] == "provincial_branch"
    assert ranked["organization_assessment"]["is_group_headquarters"] is False
    assert ranked["institution_tier_code"] == "T2"
    assert ranked["job_score"] <= 74
    assert ranked["tier_code"] == "T2"


def test_structured_branch_only_name_cannot_be_hidden_by_parent_headquarters_headline():
    ranked = score_job(_core_securities_job(
        "中信证券集团总部",
        hiring_department="山东分公司数字化发展部",
    ), {})
    organization = ranked["organization_assessment"]
    assert organization["employer_identity"] == "山东分公司数字化发展部"
    assert organization["institution_identity"] == "中信证券"
    assert organization["level"] == "provincial_branch"
    assert ranked["institution_tier_code"] == "T2"
    assert ranked["job_score"] <= 74


@pytest.mark.parametrize(
    ("company", "level", "institution_tier", "maximum"),
    [
        ("中信证券石家庄分公司", "city_branch", "T2.5", 69),
        ("中信证券北京营业部", "local_branch", "T3", 64),
        ("中信证券（山东）有限责任公司", "subsidiary", "T2.5", 69),
    ],
)
def test_lower_broker_hiring_entities_retain_strict_score_ceilings(
    company, level, institution_tier, maximum,
):
    ranked = score_job(_core_securities_job(company), {})
    assert ranked["organization_assessment"]["level"] == level
    assert ranked["institution_tier_code"] == institution_tier
    assert ranked["job_score"] <= maximum


def test_outsourced_and_low_value_broker_roles_are_not_promoted():
    outsourced = score_job(_core_securities_job(
        "中信证券集团总部",
        contract_company="某人力资源外包有限公司",
    ), {})
    low_value = score_job(_core_securities_job(
        "中信证券股份有限公司总部",
        title="客户经理",
        responsibilities="负责客户拓展、渠道销售、开户和销售业绩达成。",
    ), {})
    assert outsourced["organization_assessment"]["level"] == "third_party"
    assert outsourced["institution_tier_code"] is None
    assert outsourced["job_score"] <= 59
    assert low_value["job_score"] <= 64
    assert low_value["tier_code"] in {"T3", "不建议投"}


@pytest.mark.parametrize(
    "company",
    ["中国证券业协会", "中信证券业协会", "中信证券合作伙伴服务有限公司"],
)
def test_broker_words_and_unsafe_prefix_continuations_do_not_inherit_a_tier(company):
    ranked = score_job(_core_securities_job(company), {})
    assert ranked["institution_tier_code"] is None
    assert ranked["organization_assessment"]["base_platform_points"] == 8


def test_securities_identity_change_keeps_the_original_eleven_dimension_weights():
    ranked = score_job(_core_securities_job("中信证券股份有限公司总部"), {})
    assert SCORING_VERSION == "future-radar-job-ranking-v4.1-securities-identity"
    assert SCORING_WEIGHTS == {
        "employer_platform": 16,
        "role_function": 41,
        "career_value": 20,
        "job_conditions": 23,
    }
    assert ranked["raw_job_score"] == sum(ranked["dimension_scores"].values())
    assert ranked["raw_job_score"] == sum(ranked["score_breakdown"].values())

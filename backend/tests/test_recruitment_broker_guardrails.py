import pytest

from backend.recruitment import score_job


def _core_job(company: str, **fields) -> dict:
    job = {
        "company": company,
        "title": "金融科技数据产品与风险分析岗",
        "city": "北京",
        "employer_type": "证券公司",
        "industry": "金融",
        "responsibilities": (
            "负责金融科技数据产品、风险模型和商业分析，参与AI产品策略、"
            "模型治理及数字化建设。"
        ),
        "requirements": "面向2027届毕业生，金融、会计、计算机或管理相关专业。",
    }
    job.update(fields)
    return job


@pytest.mark.parametrize(
    ("company", "expected_institution", "expected_job", "expected_score"),
    [
        ("中金公司总部", "T0.5", "T0.5", 89),
        ("中信证券总部", "T0.5", "T0.5", 89),
        ("华泰证券总部", "T1.5", "T1", 84),
        ("国泰海通证券总部", "T1.5", "T1", 84),
        ("中信建投证券总部", "T1.5", "T1", 84),
        ("招商证券总部", "T2", "T1.5", 79),
        ("广发证券总部", "T2", "T1.5", 79),
        ("申万宏源总部", "T2", "T1.5", 79),
        ("银河证券总部", "T2", "T1.5", 79),
        ("光大证券总部", "T2", "T1.5", 79),
        ("东方证券总部", "T2", "T1.5", 79),
        ("长城证券总部", "T2", "T1.5", 79),
        ("国投证券总部", "T2", "T1.5", 79),
        ("国信证券总部", "T2", "T1.5", 79),
    ],
)
def test_broker_headquarters_core_roles_keep_the_original_platform_ladder(
    company, expected_institution, expected_job, expected_score,
):
    scored = score_job(_core_job(company), {})
    assert scored["organization_assessment"]["level"] == "group_headquarters"
    assert scored["institution_tier_code"] == expected_institution
    assert scored["tier_code"] == expected_job
    assert scored["job_score"] == expected_score
    # Even an excellent securities role does not become T0 from the sector or
    # company logo alone. T0 remains scarce under the original product rules.
    assert scored["tier_code"] != "T0"


@pytest.mark.parametrize(
    "company",
    ["中金公司总部", "中信证券总部", "华泰证券总部", "招商证券总部"],
)
def test_broker_headquarters_ordinary_finance_role_is_not_promoted_by_logo(company):
    scored = score_job(_core_job(
        company,
        title="财务会计岗",
        responsibilities="负责会计核算、财务报表编制、预算管理和税务支持。",
    ), {})
    assert scored["job_score"] <= 74
    assert scored["tier_code"] not in {"T0", "T0.5", "T1", "T1.5"}


@pytest.mark.parametrize(
    ("title", "responsibilities"),
    [
        ("行政运营支持", "负责资料录入、会议安排、流程运营和日常行政支持。"),
        ("客户经理", "负责客户拓展、渠道销售、开户、产品营销和销售业绩达成。"),
        ("IT运维工程师", "负责系统运维、故障处理、日常维护和技术支持。"),
    ],
)
def test_elite_broker_headquarters_low_value_role_keeps_the_low_value_cap(
    title, responsibilities,
):
    scored = score_job(_core_job(
        "中信证券总部", title=title, responsibilities=responsibilities,
    ), {})
    assert scored["institution_tier_code"] == "T0.5"
    assert "低优先级" in scored["fit_tags"]
    assert scored["job_score"] <= 64
    assert scored["tier_code"] in {"T3", "不建议投"}


@pytest.mark.parametrize(
    ("company", "level", "institution", "tier", "maximum"),
    [
        ("中信证券总部", "group_headquarters", "T0.5", "T0.5", 89),
        ("中信证券河北省分公司", "provincial_branch", "T2", "T2", 74),
        ("中信证券石家庄分公司", "city_branch", "T2.5", "T2.5", 69),
        ("中信证券保定分公司", "branch_unspecified", "T2.5", "T2.5", 69),
        ("中信证券北京营业部", "local_branch", "T3", "T3", 64),
    ],
)
def test_same_elite_broker_role_descends_with_the_actual_hiring_unit(
    company, level, institution, tier, maximum,
):
    scored = score_job(_core_job(company), {})
    assert scored["organization_assessment"]["level"] == level
    assert scored["institution_tier_code"] == institution
    assert scored["tier_code"] == tier
    assert scored["job_score"] <= maximum


@pytest.mark.parametrize(
    "company",
    [
        "中信证券山东分公司数字化发展部",
        "华泰证券江苏省分公司数字化发展部",
        "招商证券广东省分公司数字化发展部",
    ],
)
def test_regional_broker_internal_department_cannot_hide_the_branch_level(company):
    scored = score_job(_core_job(company), {})
    assert scored["organization_assessment"]["level"] == "provincial_branch"
    assert scored["institution_tier_code"] == "T2"
    assert scored["job_score"] <= 74
    assert scored["tier_code"] not in {"T0", "T0.5", "T1", "T1.5"}


def test_structured_hiring_entity_overrides_a_parent_broker_headline():
    scored = score_job(_core_job(
        "中信证券集团总部", hiring_entity="中信证券北京营业部",
    ), {})
    assert scored["organization_assessment"]["employer_identity"] == "中信证券北京营业部"
    assert scored["organization_assessment"]["level"] == "local_branch"
    assert scored["institution_tier_code"] == "T3"
    assert scored["job_score"] <= 64


@pytest.mark.parametrize(
    "job",
    [
        _core_job("中信证券（山东）有限责任公司"),
        _core_job("中信证券集团", subsidiary="中信证券（山东）有限责任公司"),
    ],
)
def test_regional_broker_legal_subsidiary_does_not_inherit_parent_headquarters(job):
    scored = score_job(job, {})
    assert scored["organization_assessment"]["level"] == "subsidiary"
    assert scored["institution_tier_code"] == "T2.5"
    assert scored["job_score"] <= 69
    assert scored["tier_code"] not in {"T0", "T0.5", "T1", "T1.5", "T2"}


def test_broker_outsourced_role_cannot_inherit_the_client_or_parent_platform():
    scored = score_job(_core_job(
        "中信证券集团总部", contract_company="某人力资源外包有限公司",
    ), {})
    assert scored["organization_assessment"]["level"] == "third_party"
    assert scored["institution_tier_code"] is None
    assert scored["job_score"] <= 59
    assert scored["tier_code"] == "不建议投"


def test_securities_category_metadata_alone_cannot_create_a_platform_baseline():
    plain_job = _core_job("未建立平台基准的示例单位", employer_type="", industry="")
    labelled_job = {
        **plain_job,
        "employer_type": "证券公司",
        "industry": "证券金融",
        "primary_category": "securities_public_funds_asset_management",
        "tags": ["T0", "头部券商"],
    }
    plain = score_job(plain_job, {})
    labelled = score_job(labelled_job, {})
    assert labelled["institution_tier_code"] is None
    assert labelled["organization_assessment"]["platform_points"] == plain["organization_assessment"]["platform_points"]
    assert labelled["job_score"] == plain["job_score"]
    assert labelled["tier_code"] == plain["tier_code"]
    assert labelled["manual_override"] is False


@pytest.mark.parametrize("company", [
    "中国证券业协会", "证券时报", "中信证券业协会",
])
def test_securities_words_or_broker_prefix_collisions_do_not_inherit_a_broker_tier(company):
    scored = score_job(_core_job(company), {})
    assert scored["institution_tier_code"] is None
    assert scored["organization_assessment"]["base_platform_points"] == 8


def test_broker_partner_or_outsourcer_name_is_not_a_parent_broker_identity():
    scored = score_job(_core_job("中信证券合作伙伴服务有限公司"), {})
    assert scored["organization_assessment"]["level"] == "third_party"
    assert scored["institution_tier_code"] is None
    assert scored["job_score"] <= 59


def test_broker_generic_campaign_stays_unscored_despite_elite_platform():
    scored = score_job(_core_job(
        "中信证券总部",
        title="2027校园招聘启动",
        responsibilities="",
        requirements="面向应届毕业生，专业不限。",
    ), {})
    assert scored["institution_tier_code"] == "T0.5"
    assert scored["scoring_status"] == "unscored_insufficient_role_data"
    assert scored["tier_code"] is None
    assert scored["job_score"] is None

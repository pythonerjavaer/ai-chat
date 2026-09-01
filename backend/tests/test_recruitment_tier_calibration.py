"""Organization + actual role evidence, not a logo/monitor-list ranking."""

import pytest

from backend.recruitment import SCORING_VERSION, SCORING_WEIGHTS, score_job


def opportunity(company="中国电信", **changes):
    return {
        "company": company,
        "title": "数据产品分析师",
        "city": "北京",
        "employer_type": "央国企科技",
        "industry": "通信",
        "responsibilities": "负责数据产品、风险模型和商业分析，参与金融科技产品策略设计。",
        "requirements": "面向应届毕业生，金融、会计、计算机或管理专业。",
        **changes,
    }


@pytest.mark.parametrize("brand", ["中国电信", "中国联通", "工商银行"])
def test_same_core_job_distinguishes_headquarters_province_city_and_county(brand):
    cases = [
        f"{brand}集团总部" if "银行" not in brand else f"{brand}总行",
        f"{brand}河北省分公司" if "银行" not in brand else f"{brand}河北省分行",
        f"{brand}石家庄市分公司" if "银行" not in brand else f"{brand}石家庄分行",
        f"{brand}正定县分公司" if "银行" not in brand else f"{brand}正定县支行",
    ]
    ranked = [score_job(opportunity(company), {}) for company in cases]
    assert all(left["employer_score"] > right["employer_score"] for left, right in zip(ranked, ranked[1:]))
    assert all(left["job_score"] > right["job_score"] for left, right in zip(ranked, ranked[1:]))
    assert len({row["tier_code"] for row in ranked}) >= 3
    assert ranked[0]["organization_assessment"]["is_group_headquarters"]
    assert all(not row["organization_assessment"]["is_group_headquarters"] for row in ranked[1:])


def test_carrier_monitor_metadata_does_not_raise_the_tier():
    plain = opportunity("中国电信河北省分公司")
    monitored = {
        **plain, "tags": ["重点监控", "官网来源", "T0", "telecom_technology"],
        "source_id": "china-telecom-campus", "source": "新增运营商监控",
    }
    before, after = score_job(plain, {}), score_job(monitored, {})
    assert after["job_score"] == before["job_score"]
    assert after["tier_code"] == before["tier_code"]
    assert after["employer_score"] == before["employer_score"]


def test_group_legal_name_and_brand_have_same_platform_baseline():
    brand = score_job(opportunity("中国联通"), {})
    legal = score_job(opportunity("中国联合网络通信集团有限公司"), {})
    assert brand["organization_assessment"]["base_platform_points"] == legal["organization_assessment"]["base_platform_points"]


def test_major_options_are_not_three_career_functions():
    base = opportunity("中国电信河北省分公司", title="综合事务专员", responsibilities="办理内部流程与会议安排。", requirements="面向应届毕业生。")
    full_major_list = {**base, "requirements": "金融、会计、计算机、人工智能、数据科学、战略管理等相关专业均可申请。"}
    left, right = score_job(base, {}), score_job(full_major_list, {})
    assert right["role_score"] == left["role_score"]
    assert right["career_value_score"] == left["career_value_score"]
    assert not {"ai", "data_science", "strategy", "fintech"}.intersection(right["role_tags"])
    assert "复合背景" not in right["fit_tags"]


def test_old_campaign_role_tags_cannot_turn_support_into_ai_governance():
    base = opportunity("中国联通北京市分公司", title="行政支持专员", responsibilities="负责日常会议安排、行政支持与资料录入。")
    left = score_job(base, {})
    right = score_job({**base, "role_tags": ["ai", "fintech", "model_risk", "strategy"]}, {})
    assert right["role_score"] == left["role_score"]
    assert right["job_score"] == left["job_score"]
    assert right["tier_code"] not in {"T0", "T0.5", "T1"}


def test_corporate_marketing_does_not_override_explicit_responsibilities():
    base = opportunity("中国联通河北省分公司", title="客户经理", responsibilities="负责销售拓展、客户维护和销售业绩达成。")
    marketing = "集团总部位于北京，布局金融科技、AI、投资分析、模型治理、数据产品和战略咨询。"
    before = score_job(base, {})
    after = score_job({**base, "description": marketing, "requirements": f"{marketing} 面向金融、人工智能、计算机及管理专业。"}, {})
    assert after["employer_score"] == before["employer_score"]
    assert after["role_score"] == before["role_score"]
    assert after["tier_code"] not in {"T0", "T0.5", "T1"}
    assert not after["organization_assessment"]["is_group_headquarters"]


def test_flattened_ats_requirements_keep_duties_but_not_majors():
    job = opportunity(
        "中国电信安徽省庐江分公司", title="大数据及AI工程师(庐江县)", city="合肥市-庐江县",
        responsibilities="",
        requirements=(
            "招聘部门:中国电信安徽省庐江分公司;具体用人单位以岗位详情为准。 "
            "专业要求:金融、会计、管理科学与工程、计算机等相关专业 "
            "工作描述:1. 负责客户AI售前解决方案和经营数据分析；2. 承担平台开发、运维和日常维护。 "
            "职位要求:2027届本科及以上学历，熟悉Python、SQL。"
        ),
    )
    ranked = score_job(job, {})
    assert {"ai", "data_analysis"}.issubset(ranked["role_tags"])
    assert ranked["organization_assessment"]["level"] == "local_branch"
    assert ranked["technical_hard"]
    assert ranked["tier_code"] not in {"T0", "T0.5", "T1"}
    assert not {"investment", "fintech", "strategy"}.intersection(ranked["role_tags"])


def test_unknown_generic_role_does_not_get_tier_from_majors_or_marketing():
    ranked = score_job(opportunity(
        title="专业人才岗", responsibilities="", description="集团总部位于北京，布局金融科技和AI产品。",
        requirements="面向金融、会计、人工智能、数据科学、计算机科学、数学、统计、战略管理及其他专业毕业生。",
        role_tags=["ai", "data_science", "fintech", "strategy"],
    ), {})
    assert ranked["tier_code"] is None
    assert ranked["scoring_status"] == "unscored_insufficient_role_data"


def test_bracketed_ats_duties_exclude_the_following_major_list():
    job = opportunity(
        "中国联通安徽省分公司", title="财务会计岗", responsibilities="",
        requirements=(
            "中国联通2027校园招聘项目公开岗位。【岗位职责】负责会计核算、报表编制、成本分析、税收筹划等。"
            "【任职要求】学历要求:本科以上。专业要求:计算机、人工智能、数据科学、战略管理等专业。"
        ),
    )
    scored = score_job(job, {})
    assert "tax" in scored["role_tags"]
    assert not {"ai", "data_science", "strategy"}.intersection(scored["role_tags"])
    assert "复合背景" not in scored["fit_tags"]
    assert scored["tier_code"] not in {"T0", "T0.5", "T1"}


@pytest.mark.parametrize("heading", ["任职条件", "应聘条件", "任职资格", "应聘要求", "职位要求"])
def test_equivalent_qualification_headings_do_not_become_role_evidence(heading):
    base = opportunity("中国联通安徽省分公司", title="财务会计岗", responsibilities="负责会计核算、报表编制。")
    flattened = {
        **base, "responsibilities": "",
        "requirements": f"【岗位职责】负责会计核算、报表编制。【{heading}】金融、人工智能、数据科学、战略管理等相关专业。",
    }
    expected, result = score_job(base, {}), score_job(flattened, {})
    assert result["role_score"] == expected["role_score"]
    assert result["career_value_score"] == expected["career_value_score"]
    assert not {"ai", "data_science", "strategy"}.intersection(result["role_tags"])


@pytest.mark.parametrize("qualification", [
    "研究生学历，金融、计算机、人工智能、数据科学、战略管理相关专业。",
    "研究方向：金融科技、人工智能、数据科学、战略管理。",
    "管理学专业，金融、计算机、人工智能、数据科学相关背景。",
    "分析能力强，熟悉金融科技、人工智能、数据科学。",
    "开发经验：金融科技、人工智能、数据科学相关方向。",
    "Research degree in finance, artificial intelligence and data science.",
    "Management experience in financial technology, AI and data science.",
    "参与过金融科技、人工智能、数据科学或战略管理项目者优先。",
    "负责过金融科技数据产品，具备模型治理项目经验。",
    "参与金融科技及人工智能项目经验者优先。",
])
def test_qualification_nouns_are_not_work_actions(qualification):
    base = opportunity("中国联通安徽省分公司", title="财务会计岗", responsibilities="", requirements="硕士学历，金融相关专业。")
    expected, result = score_job(base, {}), score_job({**base, "requirements": qualification}, {})
    assert result["role_score"] == expected["role_score"]
    assert result["career_value_score"] == expected["career_value_score"]
    assert not {"ai", "data_science", "strategy", "fintech"}.intersection(result["role_tags"])


def test_unlabeled_requirements_keep_clear_duties_without_qualification_nouns():
    job = opportunity(
        "中国联通安徽省分公司", title="财务会计岗", responsibilities="",
        requirements="1.负责会计核算、报表编制。2.研究生学历，人工智能、数据科学、战略管理相关专业。",
    )
    scored = score_job(job, {})
    assert not {"ai", "data_science", "strategy"}.intersection(scored["role_tags"])
    assert scored["role_score"] == score_job({**job, "responsibilities": "负责会计核算、报表编制。"}, {})["role_score"]


def test_regional_product_support_is_not_core_financial_ai_product_management():
    job = opportunity(
        "中国联通江苏省分公司", title="产品经理", responsibilities="",
        requirements=(
            "【岗位职责】1.负责政企相关产品的培训及陪访；2.负责产品支撑与合规管理；"
            "3.负责业务宣传、数据分析、指标下达和收入完成。"
            "【任职要求】计算机、数学、统计、金融、管理相关专业。"
        ),
    )
    scored = score_job(job, {})
    assert scored["role_score"] < 80
    assert scored["tier_code"] not in {"T0", "T0.5", "T1"}
    assert "fintech" not in scored["role_tags"]


def test_management_trainee_title_does_not_claim_a_training_path():
    base = opportunity("中国电信河北省分公司", title="管理培训生", responsibilities="负责业务流程协调与报告整理。")
    plain = score_job(base, {})
    structured = score_job({**base, "responsibilities": "负责业务流程协调与报告整理，参加部门轮岗，由导师指导后定岗。"}, {})
    assert "培养路径" not in plain["fit_tags"]
    assert "培养路径" in structured["fit_tags"]
    assert structured["role_score"] > plain["role_score"]


def test_no_automatic_state_owned_salary_or_wlb_bonus():
    base = opportunity("某独立企业", employer_type="", industry="")
    state = score_job({**base, "employer_type": "央国企科技", "industry": "通信"}, {})
    neutral = score_job(base, {})
    assert state["job_condition_score"] == neutral["job_condition_score"]


def test_original_eleven_dimension_score_stays_exact_and_invalidates_old_cache():
    row = score_job(opportunity("中国电信河北省分公司"), {})
    assert SCORING_VERSION != "future-radar-job-ranking-v2"
    assert SCORING_WEIGHTS == {
        "employer_platform": 16, "role_function": 41,
        "career_value": 20, "job_conditions": 23,
    }
    assert row["raw_job_score"] == sum(row["score_breakdown"].values())
    assert row["job_score"] == row["raw_job_score"] + row["calibration_adjustment"]
    assert row["raw_job_score"] == sum(row["dimension_scores"].values())
    assert row["employer_score"] == round(row["organization_assessment"]["platform_points"] / 16 * 100)
    assert row["employer_score"] == row["organization_assessment"]["platform_score"]

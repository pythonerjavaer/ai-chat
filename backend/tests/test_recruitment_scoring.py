from backend.recruitment import (
    SCORING_VERSION,
    SCORING_WEIGHTS,
    job_matches_profile,
    score_job,
    semantic_employer_categories,
    tier_for_score,
)


def test_score_boundaries_match_the_product_rules():
    expected = {
        90: "T0",
        89: "T0.5",
        85: "T0.5",
        84: "T1",
        80: "T1",
        79: "T1.5",
        75: "T1.5",
        74: "T2",
        70: "T2",
        69: "T2.5",
        65: "T2.5",
        64: "T3",
        60: "T3",
        59: "不建议投",
    }
    assert {score: tier_for_score(score) for score in expected} == expected


def test_structured_categories_support_new_starfields_without_reading_job_prose():
    metadata_job = {
        "company": "名称不参与分类",
        "title": "Deloitte 量化基金 AI 岗位名称也不参与分类",
        "requirements": "JD 中写四大、私募和对冲基金也不参与组织分类",
        "employer_type": "四大/专业服务",
        "industry": "专业服务",
        "organization_category": "big_four_professional_services",
        "primary_category": "big_four_professional_services",
        "industry_tags": ["professional_services"],
        "tags": ["big_four", "campus"],
    }
    assert semantic_employer_categories(metadata_job) == {"big_four_professional_services"}
    assert job_matches_profile(metadata_job, {"employer_types": ["四大/专业服务"]})
    assert not job_matches_profile(metadata_job, {"employer_types": ["量化/私募/对冲"]})

    multi_category = {
        "company": "任意机构",
        "title": "任意岗位",
        "employer_type": "私募证券",
        "industry": "资产管理",
        "industry_tags": ["asset_management", "quant", "private_fund", "hedge_fund"],
        "tags": [],
    }
    assert semantic_employer_categories(multi_category) == {
        "securities_public_funds_asset_management",
        "quant_private_hedge",
    }


def test_legacy_chinese_profile_aliases_match_machine_categories():
    technology_job = {
        "company": "BytePlus（字节跳动）",
        "title": "Strategy Manager Graduate",
        "city": "香港",
        "employer_type": "民营科技企业",
        "industry": "人工智能/云计算/SaaS",
        "requirements": "2027 graduate program",
        "tags": ["校园招聘"],
    }
    assert semantic_employer_categories(technology_job) == {"internet_tech"}
    assert job_matches_profile(technology_job, {"employer_types": ["互联网企业"]})
    assert not job_matches_profile(technology_job, {"employer_types": ["银行/金融"]})

    tobacco_job = {
        "company": "上海烟草集团",
        "title": "2027届数字化管理校园招聘",
        "city": "上海",
        "employer_type": "国有企业",
        "industry": "烟草专卖体系",
        "requirements": "面向应届毕业生",
        "tags": ["校园招聘"],
    }
    assert job_matches_profile(tobacco_job, {"employer_types": ["烟草/专卖"]})
    assert not job_matches_profile(tobacco_job, {"employer_types": ["互联网企业"]})


def test_same_employer_different_roles_receive_different_job_tiers():
    common = {
        "company": "Deloitte 德勤",
        "city": "上海",
        "employer_type": "四大/专业服务",
        "industry": "专业服务",
        "organization_category": "big_four_professional_services",
        "tags": ["校园招聘"],
    }
    ai_consulting = score_job(
        {
            **common,
            "title": "AI & Data Consulting Graduate",
            "responsibilities": "负责 AI、Data Science 与 Technology Consulting 项目，完成数据分析、模型治理和数字化转型方案。",
            "requirements": "熟悉 Python、SQL、machine learning，并具备 business analytics 能力。",
        },
        {},
    )
    routine_support = score_job(
        {
            **common,
            "title": "Shared Services Customer Support",
            "responsibilities": "负责重复性客户服务、录入、行政支持和 routine operations。",
            "requirements": "完成日常 shared service 流程。",
        },
        {},
    )
    assert ai_consulting["role_score"] > routine_support["role_score"]
    assert ai_consulting["match_score"] > routine_support["match_score"]
    assert ai_consulting["tier_code"] != routine_support["tier_code"]
    assert {"ai", "data_science", "technology_consulting"}.issubset(ai_consulting["role_tags"])


def test_high_value_role_at_ordinary_platform_beats_elite_logo_low_value_role():
    ordinary_quant = score_job(
        {
            "company": "普通资产管理机构",
            "title": "Quantitative Researcher",
            "city": "上海",
            "employer_type": "私募证券",
            "industry": "量化私募",
            "organization_category": "quant_private_hedge",
            "description": "Systematic research for investment portfolios.",
            "responsibilities": "Build alpha research models with machine learning, data science and portfolio analytics.",
            "requirements": "Python, statistics, investment research and risk research.",
            "tags": ["校园招聘"],
        },
        {},
    )
    elite_support = score_job(
        {
            "company": "BlackRock",
            "title": "Customer Service Support",
            "city": "上海",
            "employer_type": "公募基金",
            "industry": "资产管理",
            "organization_category": "securities_public_funds_asset_management",
            "responsibilities": "Routine customer service, sales support and repetitive shared service operations.",
            "requirements": "Administrative support and data entry.",
            "tags": ["校园招聘"],
        },
        {},
    )
    assert ordinary_quant["employer_score"] < elite_support["employer_score"]
    assert ordinary_quant["role_score"] > elite_support["role_score"]
    assert ordinary_quant["match_score"] > elite_support["match_score"]


def test_generic_program_without_substantive_jd_is_explicitly_unscored():
    scored = score_job(
        {
            "company": "某基金管理公司",
            "title": "2027校园招聘启动",
            "city": "上海",
            "employer_type": "公募基金",
            "industry": "资产管理",
            "requirements": "面向应届毕业生，专业不限。",
            "tags": ["校园招聘", "T0"],
        },
        {},
    )
    assert scored["scoring_status"] == "unscored_insufficient_role_data"
    assert scored["scoring_version"] == SCORING_VERSION
    assert scored["job_score"] is None
    assert scored["match_score"] is None
    assert scored["tier_code"] is None
    assert scored["employer_score"] is None
    assert scored["role_score"] is None
    assert scored["career_value_score"] is None
    assert scored["job_condition_score"] is None
    assert all(value is None for value in scored["score_breakdown"].values())


def test_tier_tag_cannot_override_job_score_and_weighted_breakdown_is_exact():
    base = {
        "company": "示例专业服务机构",
        "title": "Technology Consulting Data Analyst",
        "city": "上海",
        "employer_type": "四大/专业服务",
        "industry": "专业服务",
        "organization_category": "big_four_professional_services",
        "responsibilities": "负责 data analytics、technology consulting 与 digital transformation 项目。",
        "requirements": "要求 SQL、Python、business analytics 与沟通能力。",
        "tags": ["校园招聘"],
    }
    without_hint = score_job(base, {})
    with_hint = score_job({**base, "tags": ["校园招聘", "T0"]}, {})
    assert with_hint["match_score"] == without_hint["match_score"]
    assert with_hint["job_score"] == with_hint["match_score"]
    assert with_hint["tier_code"] == without_hint["tier_code"]
    assert with_hint["manual_override"] is False
    assert sum(with_hint["score_breakdown"].values()) == with_hint["match_score"]
    expected = round(
        with_hint["employer_score"] * SCORING_WEIGHTS["employer_platform"] / 100
        + with_hint["role_score"] * SCORING_WEIGHTS["role_function"] / 100
        + with_hint["career_value_score"] * SCORING_WEIGHTS["career_value"] / 100
        + with_hint["job_condition_score"] * SCORING_WEIGHTS["job_conditions"] / 100
    )
    assert with_hint["match_score"] == expected
    assert with_hint["scoring_status"] == "scored"
    assert with_hint["scoring_version"] == SCORING_VERSION
    assert set(with_hint["scoring_factors"]) == set(SCORING_WEIGHTS)


def test_organization_category_is_not_overwritten_by_primary_category():
    scored = score_job(
        {
            "company": "示例量化机构",
            "title": "Investment Research Analyst",
            "city": "上海",
            "employer_type": "量化/私募/对冲",
            "industry": "资产管理",
            "organization_category": "private_fund",
            "primary_category": "quant_private_hedge",
            "responsibilities": "负责 investment research、portfolio analytics 与风险研究。",
            "requirements": "金融、统计或数据分析背景。",
            "tags": ["校园招聘"],
        },
        {},
    )
    assert scored["organization_category"] == "private_fund"
    assert scored["primary_category"] == "quant_private_hedge"


def test_primary_category_inference_prefers_private_quant_over_generic_asset_management():
    private_fund = score_job(
        {
            "company": "示例量化私募",
            "title": "Investment Research Analyst",
            "city": "上海",
            "employer_type": "私募证券",
            "industry": "资产管理",
            "industry_tags": ["asset_management", "quant", "private_fund"],
            "responsibilities": "负责投资研究、组合分析与量化模型研究。",
        },
        {},
    )
    assert semantic_employer_categories(private_fund) == {
        "securities_public_funds_asset_management",
        "quant_private_hedge",
    }
    assert private_fund["primary_category"] == "quant_private_hedge"

    public_fund = score_job(
        {
            "company": "示例公募基金",
            "title": "Quantitative Analytics",
            "city": "上海",
            "employer_type": "公募基金",
            "industry": "资产管理",
            "industry_tags": ["asset_management", "public_fund", "quant"],
            "responsibilities": "负责公募基金组合的量化分析与风险研究。",
        },
        {},
    )
    assert public_fund["primary_category"] == "securities_public_funds_asset_management"


def test_specific_recruitment_titles_score_while_pure_placeholder_titles_do_not():
    common = {
        "company": "示例机构",
        "city": "上海",
        "employer_type": "量化/私募/对冲",
        "industry": "资产管理",
        "requirements": "面向应届毕业生。",
    }
    ai_product = score_job({**common, "title": "AI产品经理校园招聘"}, {})
    quant_research = score_job({**common, "title": "量化研究校园招聘"}, {})
    assert ai_product["scoring_status"] == "scored"
    assert {"ai", "product"}.issubset(ai_product["role_tags"])
    assert quant_research["scoring_status"] == "scored"
    assert "quant_research" in quant_research["role_tags"]

    for title in ("任意岗位", "招聘岗位", "岗位信息", "全部岗位", "职位"):
        unscored = score_job({**common, "title": title}, {})
        assert unscored["scoring_status"] == "unscored_insufficient_role_data"
        assert unscored["job_score"] is None
        assert unscored["tier_code"] is None


def test_reliable_role_tags_derived_from_jd_can_score_a_generic_title():
    scored = score_job(
        {
            "company": "示例机构",
            "title": "专项人才",
            "city": "上海",
            "employer_type": "量化/私募/对冲",
            "industry": "资产管理",
            "responsibilities": "负责量化研究与数据科学模型构建。",
        },
        {},
    )
    assert scored["scoring_status"] == "scored"
    assert {"quant_research", "data_science"}.issubset(scored["role_tags"])

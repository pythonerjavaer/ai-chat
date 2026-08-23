from backend.recruitment import job_matches_profile, score_job, tier_for_score


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


def test_semantic_employer_filter_is_real_and_not_literal_only():
    technology_job = {
        "company": "BytePlus（字节跳动）",
        "title": "Strategy Manager Graduate",
        "city": "香港",
        "employer_type": "民营科技企业",
        "industry": "人工智能/云计算/SaaS",
        "requirements": "2027 graduate program",
        "tags": ["校园招聘"],
    }
    assert job_matches_profile(technology_job, {"employer_types": ["互联网企业"]})
    assert not job_matches_profile(technology_job, {"employer_types": ["银行/金融"]})


def test_populated_filter_dimensions_reduce_results_with_or_inside_each_field():
    job = {
        "company": "示例科技",
        "title": "AI 产品经理",
        "city": "深圳",
        "employer_type": "互联网企业",
        "industry": "金融科技",
        "requirements": "数据产品与风险策略",
        "tags": ["校园招聘"],
    }
    assert job_matches_profile(
        job,
        {
            "desired_roles": ["产品经理", "投资"],
            "industries": ["金融科技"],
            "locations": ["深圳", "香港"],
            "employer_types": ["互联网企业"],
        },
    )
    assert not job_matches_profile(job, {"locations": ["北京"]})


def test_curated_tier_anchor_produces_varied_tier_and_consistent_breakdown():
    job = {
        "company": "示例头部机构",
        "title": "金融科技产品岗位",
        "city": "上海",
        "employer_type": "重点机构",
        "industry": "金融科技",
        "requirements": "Finance、AI 与产品管理复合岗位",
        "tags": ["校园招聘", "T1.5"],
    }
    scored = score_job(job, {})
    assert scored["tier_code"] == "T1.5"
    assert scored["match_score"] == 77
    assert sum(scored["score_breakdown"].values()) == 77
    assert scored["manual_override"] is True
    assert scored["positive_reasons"]
    assert scored["negative_reasons"]

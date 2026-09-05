"""Public/synthetic organization evidence only; no network, database or AI."""

import pytest

from backend.recruitment_organizations import assess_organization, collect_organization_evidence


def assess(company="示例集团", **fields):
    return assess_organization(
        {"company": company, "title": "数据产品经理", **fields},
        base_platform_points=13, platform_band="strong",
    )


@pytest.mark.parametrize("company,level,points", [
    ("示例集团总部", "group_headquarters", 15),
    ("示例集团河北省分公司", "provincial_branch", 10),
    ("示例集团石家庄市分公司", "city_branch", 8),
    ("示例集团石家庄分公司", "city_branch", 8),
    ("示例集团正定县分公司", "local_branch", 6),
    ("示例集团分公司", "branch_unspecified", 9),
    ("示例集团科技子公司", "subsidiary", 10),
    ("示例集团研究院", "research_institute", 12),
    ("示例集团外包服务商", "third_party", 6),
    ("示例集团有限公司", "unspecified", 13),
])
def test_same_role_uses_actual_organization_level(company, level, points):
    result = assess(company, responsibilities="负责数据产品和经营分析。")
    assert result["level"] == level
    assert result["platform_points"] == points
    assert result["base_platform_points"] == 13
    assert result["platform_adjustment"] == points - 13
    assert result["is_group_headquarters"] is (level == "group_headquarters")
    assert result["confidence"] in {"explicit", "inferred", "unknown"}
    assert result["note"] and result["basis"] and result["evidence"]


def test_hierarchy_points_decrease_with_identical_role_evidence():
    employers = [
        "示例集团总部", "示例集团河北省分公司", "示例集团石家庄市分公司",
        "示例集团正定县分公司",
    ]
    assert [assess(company)["platform_points"] for company in employers] == [15, 10, 8, 6]


@pytest.mark.parametrize("company", [
    "中国联通上海市分公司", "中国电信北京市分公司", "示例集团重庆市分公司",
    "示例银行天津市分行", "示例集团河北分公司", "示例集团河南省公司",
    "示例集团广西壮族自治区分公司", "示例集团宁夏回族自治区分公司",
    "示例集团新疆维吾尔自治区分公司", "示例集团西藏自治区分公司",
    "示例集团内蒙古自治区分公司", "示例集团省分公司总部",
    "示例集团华东区域总部", "某外企亚太总部", "某外企中国区总部",
    "Example EMEA Head Office",
])
def test_province_municipality_and_region_are_not_group_headquarters(company):
    result = assess(company)
    assert result["level"] == "provincial_branch"
    assert result["platform_points"] == 10
    assert result["is_group_headquarters"] is False


@pytest.mark.parametrize("company,level", [
    ("中国工商银行河北省分行石家庄市分行", "city_branch"),
    ("中国工商银行河北省分行石家庄市分行正定县支行", "local_branch"),
    ("中国联通上海市分公司浦东新区支公司", "local_branch"),
    ("示例集团省分公司下辖苏州市公司", "city_branch"),
    ("示例集团省分公司下辖苏州市公司吴中区支公司", "local_branch"),
    ("示例集团河北省公司下辖某分公司", "branch_unspecified"),
    ("示例集团总部下辖河北省分公司", "provincial_branch"),
    ("示例集团核心科技子公司总部", "subsidiary"),
    ("示例集团研究院总部", "research_institute"),
    ("示例集团科技子公司浙江分公司", "provincial_branch"),
    ("示例银行总行直属科技子公司", "subsidiary"),
    ("示例银行总行直属研究院", "research_institute"),
])
def test_deepest_hiring_entity_wins_over_ancestor_or_headquarters_word(company, level):
    result = assess(company)
    assert result["level"] == level
    assert not result["is_group_headquarters"]


@pytest.mark.parametrize("field,company", [
    ("department", "示例集团河北省分公司"),
    ("hiring_department", "示例集团苏州市分公司"),
    ("recruitment_unit", "示例集团海淀区分公司"),
    ("subsidiary", "示例数字科技有限公司"),
])
def test_specific_structured_unit_takes_precedence_over_group_company(field, company):
    result = assess("示例集团总部", **{field: company})
    assert not result["is_group_headquarters"]
    assert result["platform_points"] < 13
    assert result["basis"] == field


def test_explicit_parent_relation_and_nested_legal_names_are_subsidiary_evidence():
    explicit = assess("示例数字科技有限公司", parent_company="示例集团")
    assert explicit["level"] == "subsidiary"
    assert explicit["confidence"] == "explicit"
    inferred = assess("示例集团数字科技有限公司")
    assert inferred["level"] == "subsidiary"
    assert inferred["confidence"] == "inferred"
    assert assess("示例集团有限公司")["level"] == "unspecified"


@pytest.mark.parametrize("company,level,points", [
    ("天翼云科技有限公司", "subsidiary", 10),
    ("天翼云", "subsidiary", 10),
    ("天翼云科技有限公司总部", "subsidiary", 10),
    ("中电信人工智能科技（北京）有限公司", "subsidiary", 10),
    ("中电信数智科技有限公司集成公司", "subsidiary", 10),
    ("联通数字科技有限公司", "subsidiary", 10),
    ("联通数字科技有限公司数科本部", "subsidiary", 10),
    ("联通数科", "subsidiary", 10),
    ("中移金融科技有限公司", "subsidiary", 10),
    ("咪咕文化科技有限公司", "subsidiary", 10),
    ("中国电信研究院", "research_institute", 12),
    ("中国联通软件研究院", "research_institute", 12),
])
def test_existing_identity_directory_distinguishes_affiliates_without_core_bonus(company, level, points):
    result = assess(company)
    assert result["level"] == level
    assert result["platform_points"] == points
    assert result["confidence"] == "inferred"
    assert result["basis"] == "company.单位目录"
    assert not result["is_group_headquarters"]
    assert "不代表实际用工或核心资质已获核验" in result["note"]


@pytest.mark.parametrize("company,level", [
    ("天翼云科技有限公司青海分公司", "provincial_branch"),
    ("联通数字科技有限公司苏州市分公司", "city_branch"),
    ("天翼云科技有限公司海淀区分公司", "local_branch"),
    ("中国电信研究院县级分院", "local_branch"),
])
def test_affiliate_branch_is_assessed_at_actual_lower_hiring_level(company, level):
    result = assess(company)
    assert result["level"] == level
    assert not result["is_group_headquarters"]


@pytest.mark.parametrize("company", [
    "中国移动通信集团有限公司", "中国电信集团有限公司",
    "中国联合网络通信集团有限公司", "中国移动设备有限公司", "天翼咨询有限公司",
    "中国电信云网运营部",
])
def test_directory_does_not_invent_affiliates_or_group_headquarters(company):
    result = assess(company)
    assert result["level"] == "unspecified"
    assert result["platform_points"] == 13


def test_directory_identity_uses_no_url_checks_or_fetches(monkeypatch):
    from backend.future_radar import normalization
    from backend import recruitment_watch

    def unexpected_io(*args, **kwargs):
        pytest.fail("organization assessment must not fetch or validate a URL")

    monkeypatch.setattr(normalization, "validate_public_https_url", unexpected_io)
    monkeypatch.setattr(recruitment_watch, "fetch_watch_page", unexpected_io)
    assert assess("天翼云科技有限公司")["level"] == "subsidiary"


@pytest.mark.parametrize("field", ["description", "requirements", "responsibilities"])
@pytest.mark.parametrize("prose", [
    "需对接集团总部，向总部汇报。", "本岗位不属于总部。",
    "公司介绍：集团总部位于北京，并设有研究院和核心科技子公司。",
    "公司拥有集团总部、研究院和各省分公司。", "参与总部与省分公司的协同项目。",
    "负责外包商、合作伙伴与代理商管理。",
])
def test_arbitrary_jd_prose_does_not_change_organization(field, prose):
    baseline = assess("示例集团河北分公司")
    assert assess("示例集团河北分公司", **{field: prose}) == baseline


@pytest.mark.parametrize("company,title", [
    ("示例集团（总部位于北京）", "数据分析师"),
    ("示例集团", "对接集团总部的数据分析师"),
    ("示例集团", "数据分析师（非总部）"),
    ("示例集团", "数据分析师（不属于集团总部）"),
    ("示例集团", "数据分析师（向总部汇报）"),
    ("示例集团", "数据分析师（总部位于北京）"),
    ("示例集团", "Research Analyst reporting to headquarters"),
    ("示例集团", "数据分析师（外包管理）"),
])
def test_non_affiliation_negation_and_intro_never_prove_headquarters(company, title):
    result = assess(company, title=title)
    assert result["level"] == "unspecified"
    assert result["platform_points"] == 13
    assert result["confidence"] == "unknown"


@pytest.mark.parametrize("title", ["集团总部数据分析师", "总行数据产品经理", "数据分析师（集团总部）"])
def test_explicit_role_signature_can_identify_headquarters(title):
    result = assess("示例集团", title=title)
    assert result["level"] == "group_headquarters"
    assert result["basis"] == "title.岗位署名"
    assert result["confidence"] == "explicit"


@pytest.mark.parametrize("title", [
    "风险数据产品经理（对接省分行）", "总行数据分析师（支持地市分公司）",
    "风险数据产品经理（服务基层支行）", "风险数据产品经理（覆盖县级分公司）",
    "对接省分行的风险数据产品经理", "支持地市分公司的数据分析师",
    "覆盖基层支行的数据产品经理", "集团总部数据分析师（协助分公司）",
    "总行数据分析师（向总部汇报）",
])
def test_headquarters_role_service_objects_are_not_hiring_units(title):
    result = assess("工商银行总行", title=title)
    assert result["level"] == "group_headquarters"
    assert result["platform_points"] == 15
    assert result["is_group_headquarters"] is True
    assert result["basis"] == "company"


@pytest.mark.parametrize("title", [
    "集团总部数据分析师（支持地市分公司）", "总行数据分析师（对接省分行）",
    "集团总部数据分析师（向总部汇报）",
])
def test_valid_title_prefix_survives_separate_service_object_parentheses(title):
    result = assess("示例集团", title=title)
    assert result["level"] == "group_headquarters"
    assert result["basis"] == "title.岗位署名"


def test_headquarters_company_ignores_service_prose_but_accepts_labelled_hiring_unit():
    result = assess("工商银行总行", description="负责支持地市分公司并服务基层支行。")
    assert result["level"] == "group_headquarters"
    scoped = assess(
        "工商银行总行", description="负责支持地市分公司并服务基层支行。",
        requirements="招聘部门：工商银行河北省分行；其余职责另见。",
    )
    assert scoped["level"] == "provincial_branch"
    assert scoped["basis"] == "requirements.招聘部门"


@pytest.mark.parametrize("prose", [
    "本岗位负责外包商管理。", "本岗位支持中国电信合作伙伴。",
    "本岗位负责劳务派遣人员管理。", "本岗位为外包商提供产品支持。",
    "本岗位为外包管理岗位。", "本岗位与外包供应商沟通。",
])
@pytest.mark.parametrize("field", ["description", "responsibilities", "requirements"])
def test_duties_about_outside_firms_are_not_external_employment(prose, field):
    result = assess("中国电信集团总部", title="数据产品经理", **{field: prose})
    assert result["level"] == "group_headquarters"
    assert result["platform_points"] == 15
    assert result["basis"] == "company"


@pytest.mark.parametrize("department", ["外包管理部", "合作伙伴支持部", "派遣人员管理部", "代理商运营部"])
def test_internal_service_department_does_not_imply_outsourced_employee(department):
    for fields in ({"department": department}, {"requirements": f"招聘部门：{department}；岗位职责另见。"}):
        result = assess("中国电信集团总部", title="数据产品经理", **fields)
        assert result["level"] == "group_headquarters"
        assert result["platform_points"] == 15


@pytest.mark.parametrize("prose", [
    "本岗位为劳务派遣。", "本岗位属于外包用工。", "本岗位采用劳务派遣制度。",
    "本岗位的用工形式为劳务派遣。", "本岗位为劳务派遣，负责外包商管理。",
    "用工形式：劳务派遣。", "本岗位与某外包公司签约。", "本岗位由某劳务派遣公司聘用。",
])
def test_actual_employment_predicate_still_overrides_group_headquarters(prose):
    result = assess("中国电信集团总部", requirements=prose)
    assert result["level"] == "third_party"
    assert result["platform_points"] == 6


def test_explicit_employing_company_is_not_mistaken_for_internal_management_department():
    assert assess("示例外包管理有限公司")["level"] == "third_party"
    assert assess("中国电信集团总部", department="示例外包服务有限公司")["level"] == "third_party"


def test_plain_county_signature_still_refines_a_branch_but_service_object_does_not():
    company = "中国电信安徽省庐江分公司"
    assert assess(company, title="工程师（庐江县）")["level"] == "local_branch"
    result = assess(company, title="工程师（庐江县客户支持）")
    assert result["level"] == "branch_unspecified"


@pytest.mark.parametrize("fields,level", [
    ({"requirements": "签约单位：某人力资源有限公司；其余职责另见。"}, "unspecified"),
    ({"contract_company": "某人力资源有限公司"}, "unspecified"),
    ({"requirements": "劳动合同签订单位：某人力资源外包有限公司；其余职责另见。"}, "third_party"),
    ({"requirements": "签约单位：示例银行河北省分行；其余职责另见。"}, "provincial_branch"),
    ({"requirements": "签约单位：另一银行总行；其余职责另见。"}, "unspecified"),
    ({"requirements": "签约单位：示例银行总行；签约单位：某人力资源有限公司。"}, "unspecified"),
])
def test_conflicting_signing_entities_never_confirm_the_highest_level_headline(fields, level):
    result = assess("示例银行总行", title="总行数据分析师", **fields)
    assert result["level"] == level
    assert not result["is_group_headquarters"]
    assert result["platform_points"] <= 10
    assert "签约主体冲突" in result["basis"]
    assert "签约单位署名存在差异或冲突" in result["note"]
    assert "核验真实签约主体" in result["note"]
    assert len(result["evidence"]) >= 2


@pytest.mark.parametrize("company,contract", [
    ("示例集团总部", "示例集团有限公司"),
    ("工商银行总行", "中国工商银行股份有限公司"),
    ("中国联通总部", "中国联合网络通信集团有限公司"),
])
def test_same_entity_legal_suffix_or_known_group_alias_is_not_a_contract_conflict(company, contract):
    result = assess(company, requirements=f"签约单位：{contract}；其余职责另见。")
    assert result["level"] == "group_headquarters"
    assert result["platform_points"] == 15
    assert "冲突" not in result["basis"]


@pytest.mark.parametrize("fields,unit,contract_conflict", [
    ({
        "title": "财务管理(省业财)",
        "requirements": "招聘部门:湖北业财赋能中心;具体用人单位以岗位详情为准。 工作描述:负责报表管理、风险管理。",
    }, "湖北业财赋能中心", False),
    ({
        "title": "后端应用开发工程师(百事应)",
        "requirements": "招聘部门:上海电信百事应信息有限公司;具体用人单位以岗位详情为准。 工作描述:负责后端开发。 合同签署方:上海电信百事应信息有限公司",
    }, "上海电信百事应信息有限公司", True),
    ({"department": "上海电信百事应信息有限公司"}, "上海电信百事应信息有限公司", False),
    ({"requirements": "招聘部门:上海电信百事应信息有限公司;具体用人单位以岗位详情为准。"}, "上海电信百事应信息有限公司", False),
])
def test_specific_unresolved_hiring_unit_does_not_inherit_campaign_group_platform(fields, unit, contract_conflict):
    result = assess("中国电信集团有限公司", **fields)
    assert result["level"] == "unspecified"
    assert result["confidence"] == "unknown"
    assert result["base_platform_points"] == 13
    assert result["platform_points"] == 10
    assert result["platform_adjustment"] == -3
    assert not result["is_group_headquarters"]
    assert result["basis"].startswith("requirements.招聘部门" if "requirements" in fields else "department")
    assert "具体单位待核验" in result["basis"]
    assert unit in result["evidence"][0]
    assert unit in result["note"]
    assert "不据此推定省级机构、子公司或外包关系" in result["note"]
    assert ("签约主体冲突" in result["basis"]) is contract_conflict
    if contract_conflict:
        assert any("合同签署方" in item for item in result["evidence"])


@pytest.mark.parametrize("company,level,points", [
    ("中国电信集团有限公司", "unspecified", 13),
    ("中国电信集团总部", "group_headquarters", 15),
    ("工商银行总行", "group_headquarters", 15),
])
@pytest.mark.parametrize("department", ["财务部", "财务管理中心", "人力资源部"])
def test_plain_internal_department_preserves_its_actual_group_or_headquarters(company, level, points, department):
    for fields in ({"department": department}, {"requirements": f"招聘部门：{department}；岗位职责另见。"}):
        result = assess(company, **fields)
        assert result["level"] == level
        assert result["platform_points"] == points
        assert "具体单位待核验" not in result["basis"]


def test_same_legal_company_in_hiring_department_is_not_a_different_unresolved_entity():
    result = assess("中国电信集团有限公司", department="中国电信集团有限公司")
    assert result["platform_points"] == 13
    assert "具体单位待核验" not in result["basis"]


def test_labelled_recruiting_unit_is_not_confused_with_following_company_intro():
    result = assess(
        "中国电信集团有限公司",
        requirements="招聘部门：中国电信安徽省庐江分公司；具体用人单位以岗位详情为准。集团总部位于北京。",
        title="大数据及AI工程师(庐江县)",
        city="合肥市-庐江县",
    )
    assert result["level"] == "local_branch"
    assert result["platform_points"] == 6
    assert result["confidence"] == "inferred"
    assert "requirements.招聘部门" in result["basis"]
    assert result["evidence"] == [
        "requirements.招聘部门：中国电信安徽省庐江分公司", "岗位署名：庐江县",
    ]
    assert "位于" not in str(result["evidence"])


@pytest.mark.parametrize("company,title,level", [
    ("中国电信安徽省庐江分公司", "大数据及AI工程师(庐江县)", "local_branch"),
    ("中国电信安徽省巢湖分公司", "大数据及AI工程师(巢湖市)", "city_branch"),
    ("中国电信安徽省庐江分公司", "大数据及AI工程师", "branch_unspecified"),
    ("中国电信安徽省分公司", "大数据及AI工程师(合肥市)", "provincial_branch"),
    ("中国联通上海市分公司", "数据分析师(浦东新区)", "provincial_branch"),
    ("中国电信集团总部", "大数据及AI工程师(庐江县)", "group_headquarters"),
])
def test_title_location_only_refines_a_matching_existing_branch(company, title, level):
    result = assess(company, title=title, city="合肥市-庐江县")
    assert result["level"] == level


def test_city_field_alone_cannot_change_employer_hierarchy():
    for company in ("示例集团", "示例集团总部", "中国电信安徽省庐江分公司"):
        assert assess(company, city="合肥市-庐江县") == assess(company)


@pytest.mark.parametrize("fields", [
    {"company": "中国电信外包服务商"},
    {"company": "示例集团合作伙伴"},
    {"company": "示例品牌代理商"},
    {"company": "示例银行（劳务派遣）"},
    {"employment_type": "劳务派遣"},
    {"contract_type": "outsourced"},
    {"requirements": "本岗位为劳务派遣，与第三方公司签约。"},
    {"requirements": "用工形式：劳务派遣；具体职责另行说明。"},
    {"requirements": "招聘单位：示例人力资源外包公司；服务于示例集团总部。"},
])
def test_third_party_or_dispatch_does_not_inherit_service_client_platform(fields):
    result = assess(**fields)
    assert result["level"] == "third_party"
    assert result["platform_points"] == 6
    assert not result["is_group_headquarters"]


def test_non_dispatch_statement_does_not_demote_direct_employment():
    assert assess(requirements="本岗位非劳务派遣。") == assess()
    assert assess(contract_type="not outsourced") == assess()


def test_negating_dispatch_does_not_hide_a_separate_outsourcing_relationship():
    result = assess(employment_type="非劳务派遣，仅为外包岗位")
    assert result["level"] == "third_party"
    assert result["platform_points"] == 6


@pytest.mark.parametrize("prose", [
    "本岗位非劳务派遣，仅为外包岗位。",
    "本岗位不是劳务派遣，但属于外包用工。",
    "本岗位非劳务派遣，而是外包岗位。",
    "本岗位非劳务派遣，实际由某外包公司聘用。",
    "用工形式：非劳务派遣，仅为外包岗位。",
])
def test_prose_negation_retains_subject_for_later_affirmative_employment_clause(prose):
    result = assess("中国电信集团总部", requirements=prose)
    assert result["level"] == "third_party"
    assert result["platform_points"] == 6
    assert not result["is_group_headquarters"]


@pytest.mark.parametrize("prose", [
    "本岗位非劳务派遣，仅负责外包商管理。",
    "本岗位非劳务派遣，仅为外包管理岗位。",
    "本岗位非劳务派遣，仅支持中国电信合作伙伴。",
])
def test_later_duty_clause_after_employment_negation_is_not_external_employment(prose):
    result = assess("中国电信集团总部", requirements=prose)
    assert result["level"] == "group_headquarters"
    assert result["platform_points"] == 15


def test_research_role_is_not_an_institute_and_core_adjective_is_not_headquarters():
    assert assess(title="量化研究员", responsibilities="负责研究与模型分析。")["level"] == "unspecified"
    result = assess("示例集团核心科技子公司")
    assert result["level"] == "subsidiary"
    assert result["platform_points"] == 10
    assert not result["is_group_headquarters"]


def test_evidence_is_short_public_unit_text_without_contact_details():
    result = assess(
        requirements="招聘单位：示例集团总部 联系人测试先生 电话13800138000 test@example.com；岗位职责另见。",
        private_profile="PRIVATE_PROFILE_MUST_NOT_BE_READ",
    )
    evidence = str(result["evidence"])
    assert "示例集团总部" in evidence
    for private in ("测试先生", "13800138000", "test@example.com", "PRIVATE_PROFILE_MUST_NOT_BE_READ"):
        assert private not in evidence


@pytest.mark.parametrize("base", [-2, 0, 1, 3, 4, 8, 13, 14, 16, 20])
def test_points_remain_bounded_and_non_headquarters_never_gain_from_floors(base):
    for company in ("示例集团总部", "示例集团分公司", "示例集团科技子公司", "示例集团外包服务商"):
        result = assess_organization({"company": company}, base_platform_points=base, platform_band="test")
        assert 0 <= result["platform_points"] <= 16
        assert 0 <= result["base_platform_points"] <= 16
        if not result["is_group_headquarters"]:
            assert result["platform_adjustment"] <= 0


def test_reused_evidence_keeps_platform_calculation_and_mutable_outputs_independent():
    job = {"company": "中国电信集团总部", "title": "数据产品经理",
           "hiring_unit": "中国电信河北省分公司"}
    evidence = collect_organization_evidence(job)
    low = assess_organization(job, base_platform_points=8, platform_band="probe", evidence=evidence)
    expected = assess_organization(job, base_platform_points=14, platform_band="top")
    actual = assess_organization(job, base_platform_points=14, platform_band="top", evidence=evidence)
    assert actual == expected
    assert actual["platform_points"] != low["platform_points"]
    actual["evidence"].append("caller-owned modification")
    actual["level"] = "group_headquarters"
    assert assess_organization(job, base_platform_points=14, platform_band="top", evidence=evidence) == expected


def test_public_directory_lookup_cache_does_not_share_mutable_assessment_outputs():
    first = assess("天翼云科技有限公司")
    first["evidence"].clear()
    first["platform_points"] = 16
    second = assess("天翼云科技有限公司")
    assert second["evidence"]
    assert second["platform_points"] == 10
    assert second["level"] == "subsidiary"

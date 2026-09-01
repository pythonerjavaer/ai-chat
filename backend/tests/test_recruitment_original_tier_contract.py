import json
from pathlib import Path

import pytest

from backend.recruitment import SCORING_VERSION, score_job


def core_job(company: str, *, employer_type: str, industry: str) -> dict:
    return {
        "company": company,
        "title": "金融科技数据产品与风险分析岗",
        "city": "北京",
        "employer_type": employer_type,
        "industry": industry,
        "responsibilities": (
            "负责金融科技数据产品、风险模型和商业分析，参与AI产品策略、"
            "模型治理及数字化建设。"
        ),
        "requirements": "面向2027届毕业生，金融、会计、计算机或管理相关专业。",
    }


@pytest.mark.parametrize(
    ("company", "employer_type", "industry", "expected_job", "expected_institution"),
    [
        ("国家开发银行总行", "政策性金融", "银行", "T0", "T0"),
        ("工商银行总行", "国有大行", "银行", "T0.5", "T0.5"),
        ("工商银行河北省分行", "国有大行", "银行", "T1.5", "T1.5"),
        ("工商银行石家庄分行", "国有大行", "银行", "T2.5", "T2.5"),
        ("工商银行正定县支行", "国有大行", "银行", "T3", "T3"),
        # A particularly strong headquarters role may rise above the
        # independent institution baseline; regional branches retain ceilings.
        ("中国电信集团总部", "央国企科技", "通信", "T1", "T1.5"),
        ("中国电信河北省分公司", "央国企科技", "通信", "T2", "T2"),
        ("中国电信石家庄市分公司", "央国企科技", "通信", "T2.5", "T2.5"),
        ("中国电信正定县分公司", "央国企科技", "通信", "T3", "T3"),
    ],
)
def test_original_hiring_unit_pyramid_is_an_exact_contract(
    company, employer_type, industry, expected_job, expected_institution,
):
    scored = score_job(core_job(company, employer_type=employer_type, industry=industry), {})
    assert scored["tier_code"] == expected_job
    assert scored["institution_tier_code"] == expected_institution
    assert scored["raw_job_score"] == sum(scored["score_breakdown"].values())
    assert scored["raw_job_score"] == sum(scored["dimension_scores"].values())
    assert scored["job_score"] == scored["raw_job_score"] + scored["calibration_adjustment"]


def test_financial_markets_operations_is_not_treated_as_routine_operations():
    scored = score_job({
        "company": "平安银行资金运营中心",
        "title": "金融市场培训生",
        "city": "深圳",
        "employer_type": "保险/综合金融",
        "industry": "银行金融",
        "responsibilities": (
            "负责金融市场交易、资金管理、投资组合分析与风险管理，"
            "参与轮岗及导师培养。"
        ),
        "requirements": "面向2027届金融、计算机或数据分析专业毕业生。",
    }, {})
    assert scored["tier_code"] == "T1.5"
    assert "低优先级" not in scored["fit_tags"]


def test_category_metadata_alone_does_not_raise_platform_or_job_score():
    base = core_job("未建立平台基准的示例单位", employer_type="", industry="")
    categorized = {
        **base,
        "employer_type": "央国企科技",
        "industry": "通信",
        "primary_category": "state_tech_telecom",
    }
    plain, labelled = score_job(base, {}), score_job(categorized, {})
    assert labelled["organization_assessment"]["platform_points"] == plain["organization_assessment"]["platform_points"]
    assert labelled["job_score"] == plain["job_score"]
    assert labelled["tier_code"] == plain["tier_code"]
    assert labelled["institution_tier_code"] is None


def test_first_release_public_job_anchors_remain_exact_without_trusting_tags():
    fixture = Path(__file__).parents[1] / "radar_bootstrap_jobs.json"
    jobs = json.loads(fixture.read_text(encoding="utf-8"))
    assert len(jobs) == 16
    for job in jobs:
        expected = next(
            tag.replace("T1.0", "T1")
            for tag in job["tags"]
            if str(tag).startswith("T")
        )
        scored = score_job(job, {})
        assert scored["tier_code"] == expected, (job["company"], job["title"])
        assert scored["manual_override"] is True
        assert scored["scoring_version"] == SCORING_VERSION

        # The trust boundary is the stable public identity, not the source tag.
        without_tier_tag = {
            **job,
            "tags": [tag for tag in job["tags"] if not str(tag).startswith("T")],
        }
        assert score_job(without_tier_tag, {})["tier_code"] == expected


def test_untrusted_tier_tag_still_cannot_override_a_new_job():
    job = core_job("未建立平台基准的示例单位", employer_type="", industry="")
    plain = score_job(job, {})
    tagged = score_job({**job, "tags": ["T0", "重点监控"]}, {})
    assert tagged["job_score"] == plain["job_score"]
    assert tagged["tier_code"] == plain["tier_code"]
    assert tagged["manual_override"] is False


@pytest.mark.parametrize("company", [
    "中国银行业协会",
    "中国银行保险信息技术管理有限公司",
    "中国银行间市场交易商协会",
    "中国银行业协会北京分部",
    "中国银行间市场交易商协会上海分部",
    "中国银行保险信息技术管理有限公司北京分公司",
    "中国移动互联网有限公司北京分公司",
])
def test_bank_of_china_prefix_collisions_do_not_inherit_its_institution_tier(company):
    scored = score_job(core_job(company, employer_type="银行相关", industry="金融"), {})
    assert scored["institution_tier_code"] is None
    assert scored["organization_assessment"]["base_platform_points"] == 8


def test_category_metadata_cannot_change_a_known_company_hierarchy():
    base = core_job("腾讯河北省分公司", employer_type="科技企业", industry="互联网")
    plain = score_job(base, {})
    mislabeled = score_job({**base, "primary_category": "policy_state_banks"}, {})
    assert plain["organization_assessment"]["level"] == "provincial_branch"
    assert mislabeled["institution_tier_code"] == plain["institution_tier_code"]
    assert mislabeled["job_score"] == plain["job_score"]


def test_unresolved_named_branches_are_conservatively_t25_and_xiongan_is_city_level():
    for company in ("中国电信保定分公司", "中国电信唐山分公司", "工商银行保定分行"):
        scored = score_job(core_job(company, employer_type="重点企业", industry=""), {})
        assert scored["organization_assessment"]["level"] == "branch_unspecified"
        assert scored["institution_tier_code"] == "T2.5"
        assert scored["tier_code"] == "T2.5"

    xiongan = score_job(core_job("中国电信雄安新区分公司", employer_type="央国企科技", industry="通信"), {})
    assert xiongan["organization_assessment"]["level"] == "city_branch"
    assert xiongan["institution_tier_code"] == "T2.5"


def test_support_remains_low_value_but_financial_markets_operations_does_not():
    support = score_job({
        "company": "腾讯集团总部",
        "title": "Business Support Analyst",
        "city": "深圳",
        "responsibilities": "负责AI与数据团队的业务支持、资料整理、流程运营和战略报告支持。",
        "requirements": "面向2027届毕业生。",
    }, {})
    professional = score_job({
        "company": "平安银行资金运营中心",
        "title": "金融市场培训生",
        "city": "深圳",
        "responsibilities": "负责金融市场交易、资金管理、投资组合分析与风险管理，参与轮岗及导师培养。",
        "requirements": "面向2027届金融、计算机或数据分析专业毕业生。",
    }, {})
    assert "低优先级" in support["fit_tags"]
    assert support["job_score"] <= 64
    assert support["tier_code"] in {"T3", "不建议投"}
    assert support["job_score"] < professional["job_score"]
    assert professional["tier_code"] == "T1.5"


@pytest.mark.parametrize(("company", "expected_level", "expected_institution", "maximum"), [
    ("中国华能正定县分公司", "local_branch", "T3", 64),
    ("招商银行保定分行", "branch_unspecified", None, 69),
])
def test_hiring_unit_ceiling_applies_even_without_an_institution_calibration(
    company, expected_level, expected_institution, maximum,
):
    scored = score_job(core_job(company, employer_type="重点企业", industry=""), {})
    assert scored["organization_assessment"]["level"] == expected_level
    assert scored["institution_tier_code"] == expected_institution
    assert scored["job_score"] <= maximum


def test_named_role_rule_cannot_promote_a_regional_branch_above_its_ceiling():
    scored = score_job({
        **core_job("中信期货石家庄分公司", employer_type="期货公司", industry="金融"),
        "title": "风险数据分析岗",
    }, {})
    assert scored["manual_override"] is True
    assert scored["organization_assessment"]["level"] == "city_branch"
    assert scored["job_score"] <= 69
    assert scored["tier_code"] == "T2.5"


def test_regional_legal_subsidiary_does_not_inherit_parent_headquarters_tier():
    scored = score_job(core_job(
        "中信证券（山东）有限责任公司", employer_type="证券公司", industry="金融",
    ), {})
    assert scored["organization_assessment"]["level"] == "subsidiary"
    assert scored["institution_tier_code"] == "T2.5"
    assert scored["job_score"] <= 69


@pytest.mark.parametrize("company", [
    "麦肯锡", "McKinsey & Company",
    "波士顿咨询", "Boston Consulting Group", "BCG",
    "Amazon Web Services", "Amazon", "AWS", "亚马逊",
    "J.P. Morgan", "JPMorgan", "摩根大通",
])
def test_maintained_aliases_share_one_platform_and_institution_calibration(company):
    scored = score_job(core_job(company, employer_type="外企/咨询", industry="咨询"), {})
    canonical_scores = {
        "麦肯锡": ("T1", 14),
        "McKinsey & Company": ("T1", 14),
        "波士顿咨询": ("T1", 14),
        "Boston Consulting Group": ("T1", 14),
        "BCG": ("T1", 14),
        "Amazon Web Services": ("T1", 14),
        "Amazon": ("T1", 14),
        "AWS": ("T1", 14),
        "亚马逊": ("T1", 14),
        "J.P. Morgan": ("T0.5", 14),
        "JPMorgan": ("T0.5", 14),
        "摩根大通": ("T0.5", 14),
    }
    expected_tier, expected_platform = canonical_scores[company]
    assert scored["institution_tier_code"] == expected_tier
    assert scored["organization_assessment"]["base_platform_points"] == expected_platform


@pytest.mark.parametrize("company", ["HSBC 汇丰", "Goldman Sachs 高盛"])
def test_professional_markets_sales_is_not_treated_as_routine_sales_without_anchor(company):
    scored = score_job({
        "company": company,
        "title": "Markets – Sales and Trading – Graduate",
        "city": "香港",
        "responsibilities": (
            "负责 Global Markets institutional sales, trading, market data analysis, "
            "risk and P&L management。"
        ),
        "requirements": "面向2027届毕业生。",
    }, {})
    assert "financial_markets" in scored["role_tags"]
    assert "低优先级" not in scored["fit_tags"]
    assert scored["job_score"] >= 65


@pytest.mark.parametrize(("title", "responsibilities"), [
    (
        "AI产品经理",
        "负责AI产品规划、数据分析、风险治理，并与销售团队合作推动落地。",
    ),
    (
        "战略分析岗",
        "负责商业分析和战略研究，支持销售团队制定客户策略。",
    ),
])
def test_sales_collaboration_does_not_turn_a_core_role_into_a_sales_job(title, responsibilities):
    scored = score_job({
        "company": "腾讯集团总部", "title": title, "city": "深圳",
        "responsibilities": responsibilities, "requirements": "面向2027届毕业生。",
    }, {})
    assert "低优先级" not in scored["fit_tags"]
    assert scored["job_score"] > 64


def test_actual_channel_sales_remains_low_priority():
    scored = score_job({
        "company": "腾讯集团总部", "title": "渠道销售客户经理", "city": "深圳",
        "responsibilities": "负责客户拓展、渠道销售、商机转化与销售业绩指标。",
        "requirements": "面向2027届毕业生。",
    }, {})
    assert "低优先级" in scored["fit_tags"]
    assert scored["job_score"] <= 64


@pytest.mark.parametrize(("title", "responsibilities"), [
    (
        "AI Product Manager",
        "Build AI data products and support risk decision-making and product strategy.",
    ),
    (
        "Risk Data Analyst",
        "Build risk analytics and data models to support model governance.",
    ),
    (
        "Strategy and Operations Analyst",
        "Own strategy analysis, operating-model design and data-driven decision support.",
    ),
])
def test_support_or_operations_in_a_core_role_is_not_routine_work(title, responsibilities):
    scored = score_job({
        "company": "腾讯集团总部", "title": title, "city": "深圳",
        "responsibilities": responsibilities, "requirements": "2027 graduate role.",
    }, {})
    assert "低优先级" not in scored["fit_tags"]
    assert scored["job_score"] > 64


def test_plain_business_support_still_remains_low_priority_after_context_exceptions():
    scored = score_job({
        "company": "腾讯集团总部", "title": "Business Support Analyst", "city": "深圳",
        "responsibilities": "Provide administrative support, document filing and routine process support.",
        "requirements": "2027 graduate role.",
    }, {})
    assert "低优先级" in scored["fit_tags"]
    assert scored["job_score"] <= 64


@pytest.mark.parametrize(("title", "responsibilities"), [
    (
        "AI Product Manager",
        "Build AI products and data analytics for customer service automation and risk decision-making.",
    ),
    (
        "客户体验数据产品经理",
        "负责客户服务数据产品、AI自动化和风险决策能力建设。",
    ),
    (
        "AI产品经理",
        "负责产品规划、协调测试验收与业务实施落地。",
    ),
    (
        "数字化转型顾问",
        "负责数字化战略咨询、系统实施路线设计与转型项目治理。",
    ),
])
def test_service_testing_or_implementation_context_does_not_make_a_core_role_routine(
    title, responsibilities,
):
    scored = score_job({
        "company": "腾讯集团总部", "title": title, "city": "深圳",
        "responsibilities": responsibilities, "requirements": "面向2027届毕业生。",
    }, {})
    assert "低优先级" not in scored["fit_tags"]
    assert scored["job_score"] > 64


@pytest.mark.parametrize(("title", "responsibilities"), [
    ("Customer Service Representative", "Answer customer calls and handle customer complaints."),
    ("客服专员", "负责客服热线、处理客户投诉和工单。"),
    ("测试工程师", "负责测试执行、缺陷记录与回归测试。"),
    ("实施工程师", "负责系统实施、驻场配置与售后支持。"),
])
def test_actual_service_testing_and_implementation_roles_remain_low_priority(title, responsibilities):
    scored = score_job({
        "company": "腾讯集团总部", "title": title, "city": "深圳",
        "responsibilities": responsibilities, "requirements": "面向2027届毕业生。",
    }, {})
    assert "低优先级" in scored["fit_tags"]
    assert scored["job_score"] <= 64


def test_quant_research_needs_explicit_barrier_evidence_before_being_marked_high_barrier():
    base = {
        "company": "Point72", "title": "Quant Research Analyst", "city": "香港",
        "responsibilities": "Use machine learning, investment data and risk models for quantitative research.",
        "requirements": "2027 graduate role.",
    }
    ordinary = score_job(base, {})
    extreme = score_job({
        **base,
        "responsibilities": "Build HFT low latency C++ systems and stochastic-calculus alpha models.",
    }, {})
    assert ordinary["quant_barrier"] is False
    assert "量化高门槛" not in ordinary["fit_tags"]
    assert extreme["quant_barrier"] is True
    assert "量化高门槛" in extreme["fit_tags"]
    assert ordinary["job_score"] > extreme["job_score"]


def test_campaign_url_or_internal_id_alone_cannot_spoof_a_curated_anchor():
    jobs = json.loads((Path(__file__).parents[1] / "radar_bootstrap_jobs.json").read_text(encoding="utf-8"))
    dji = next(job for job in jobs if job["company"].startswith("DJI"))
    spoofed = {
        **dji,
        "company": "某外包服务商",
        "title": "销售客服支持",
        "responsibilities": "负责外包客户销售、客服和重复资料录入。",
        "requirements": "第三方派遣。",
    }
    scored = score_job(spoofed, {})
    assert scored["manual_override"] is False
    assert scored["tier_code"] == "不建议投"


@pytest.mark.parametrize(("company", "title", "responsibilities", "expected"), [
    ("南方基金", "AI产品经理（AI应用与风险管理）", "负责AI产品规划、金融风险管理、模型治理与投资科技产品建设。", "T1"),
    ("Kearney 科尔尼", "Business Analyst", "负责战略咨询、商业分析、金融服务与数字化转型项目。", "T1"),
    ("L.E.K. Consulting", "Associate", "负责战略咨询、商业分析、市场研究与客户项目交付。", "T1"),
    ("中信期货总部", "风险管理与金融科技岗", "负责衍生品风险管理、金融科技、数据分析与研究。", "T1.5"),
    ("平安银行资金运营中心", "金融市场培训生", "负责金融市场交易、资金管理、投资组合分析与风险管理，参与轮岗及导师培养。", "T1.5"),
    ("华为终端云", "AI产品经理", "负责AI应用、终端云产品、数据分析与产品策略。", "T1.5"),
    ("中证信用", "风险数据产品经理", "负责信用风险、金融数据产品、量化分析与模型治理。", "T1.5"),
])
def test_original_named_role_examples_remain_calibration_anchors(company, title, responsibilities, expected):
    scored = score_job({
        "company": company,
        "title": title,
        "city": "上海",
        "responsibilities": responsibilities,
        "requirements": "面向2027届金融、会计、计算机或管理专业毕业生。",
    }, {})
    assert scored["tier_code"] == expected
    assert scored["manual_override"] is True
    assert scored["raw_job_score"] == sum(scored["dimension_scores"].values())
    assert scored["job_score"] == scored["raw_job_score"] + scored["calibration_adjustment"]


def test_one_institution_can_have_different_job_tiers():
    strong = score_job(core_job("工商银行总行", employer_type="国有大行", industry="银行"), {})
    weak = score_job({
        "company": "工商银行总行",
        "title": "客户销售支持",
        "city": "北京",
        "employer_type": "国有大行",
        "industry": "银行",
        "responsibilities": "负责客户销售、电话营销、资料录入和日常行政支持。",
        "requirements": "面向应届毕业生。",
    }, {})
    assert strong["institution_tier_code"] == weak["institution_tier_code"] == "T0.5"
    assert strong["job_score"] > weak["job_score"]
    assert strong["tier_code"] != weak["tier_code"]


@pytest.mark.parametrize("company", [
    "国家能源集团", "中国航天科技", "云南中烟", "招商证券", "中国人保",
    "小米", "宝洁", "幻方",
])
def test_every_monitored_pyramid_area_has_an_exact_conservative_platform_baseline(company):
    scored = score_job(core_job(company, employer_type="", industry=""), {})
    assert scored["institution_tier_code"] is not None
    assert scored["organization_assessment"]["base_platform_points"] >= 11


@pytest.mark.parametrize(("company", "expected_institution"), [
    ("中国人民银行金融科技司", "T0"),
    ("中国人民银行宏观审慎管理局", "T0"),
    ("国家开发银行信息科技局", "T0"),
    ("工商银行数据管理部", "T0.5"),
    ("中信证券数字化发展部", "T0.5"),
    ("南方基金信息技术部", "T1"),
])
def test_head_office_internal_units_keep_their_parent_institution_baseline(
    company, expected_institution,
):
    scored = score_job(core_job(company, employer_type="", industry=""), {})
    assert scored["institution_tier_code"] == expected_institution
    assert scored["organization_assessment"]["base_platform_points"] >= 14


@pytest.mark.parametrize(("short_name", "legal_name"), [
    ("天翼云", "天翼云科技有限公司"),
    ("联通数科", "联通数字科技有限公司"),
    ("中证信用", "中证信用增进股份有限公司"),
    ("华为终端云", "华为终端云服务有限公司"),
    ("平安科技", "平安科技（深圳）有限公司"),
])
def test_core_subsidiary_short_and_legal_names_score_identically(short_name, legal_name):
    short = score_job(core_job(short_name, employer_type="", industry=""), {})
    legal = score_job(core_job(legal_name, employer_type="", industry=""), {})
    assert short["institution_tier_code"] == legal["institution_tier_code"] == "T1.5"
    for key in (
        "level", "base_platform_points", "platform_points", "platform_adjustment",
        "is_group_headquarters",
    ):
        assert short["organization_assessment"][key] == legal["organization_assessment"][key]
    assert short["job_score"] == legal["job_score"]


def test_regional_product_title_cannot_hide_training_publicity_and_support_duties():
    scored = score_job({
        "company": "中国联通江苏省分公司",
        "title": "产品经理",
        "city": "南京",
        "responsibilities": (
            "负责政企产品培训陪访、产品支撑、业务宣传、指标下达和收入完成。"
        ),
        "requirements": "面向2027届毕业生。",
    }, {})
    assert "低优先级" in scored["fit_tags"]
    assert scored["role_score"] < 80
    assert scored["job_score"] <= 64


@pytest.mark.parametrize(("company", "parent_company", "expected_institution"), [
    ("中国人民银行金融科技司", "中国人民银行", "T0"),
    ("国家开发银行金融科技部", "国家开发银行", "T0"),
    ("工商银行数据管理部", "工商银行", "T0.5"),
    ("中信证券数字化发展部", "中信证券", "T0.5"),
    ("南方基金信息技术部", "南方基金", "T1"),
    ("中国人民银行金融研究所", "中国人民银行", "T0"),
])
def test_explicit_parent_metadata_does_not_turn_an_internal_unit_into_a_subsidiary(
    company, parent_company, expected_institution,
):
    scored = score_job({
        **core_job(company, employer_type="", industry=""),
        "parent_company": parent_company,
    }, {})
    assert scored["institution_tier_code"] == expected_institution
    assert scored["organization_assessment"]["level"] != "subsidiary"


@pytest.mark.parametrize(("parent", "field", "short_name", "legal_name"), [
    ("中国电信", "subsidiary", "天翼云", "天翼云科技有限公司"),
    ("中国联通", "hiring_entity", "联通数科", "联通数字科技有限公司"),
    ("中信证券", "subsidiary", "中证信用", "中证信用增进股份有限公司"),
    ("华为", "hiring_entity", "华为终端云", "华为终端云服务有限公司"),
    ("中国平安", "subsidiary", "平安科技", "平安科技（深圳）有限公司"),
])
def test_structured_core_hiring_entity_uses_its_own_calibration(
    parent, field, short_name, legal_name,
):
    direct = score_job(core_job(legal_name, employer_type="", industry=""), {})
    structured_job = core_job(parent, employer_type="", industry="")
    structured_job[field] = short_name
    structured = score_job(structured_job, {})
    assert structured["institution_tier_code"] == direct["institution_tier_code"] == "T1.5"
    assert structured["organization_assessment"]["level"] == direct["organization_assessment"]["level"] == "subsidiary"
    assert structured["organization_assessment"]["platform_points"] == direct["organization_assessment"]["platform_points"]
    assert structured["job_score"] == direct["job_score"]


@pytest.mark.parametrize(("title", "responsibilities"), [
    ("AI产品经理", "负责AI产品和数据风险治理，与运维团队协作落地。"),
    ("AI产品经理", "负责AI产品和数据风险治理，协调后端开发团队。"),
    ("AI芯片产品经理", "负责AI芯片产品规划、数据分析、风险治理和商业策略。"),
])
def test_technical_collaboration_or_chip_product_context_is_not_pure_engineering(
    title, responsibilities,
):
    scored = score_job({
        "company": "腾讯集团总部", "title": title, "city": "深圳",
        "responsibilities": responsibilities, "requirements": "面向2027届毕业生。",
    }, {})
    assert scored["technical_hard"] is False


@pytest.mark.parametrize(("company", "title", "responsibilities"), [
    ("腾讯集团总部", "AI产品经理", "负责售后服务产品规划、AI数据分析和风险治理。"),
    ("腾讯集团总部", "AI产品经理", "负责产品培训、模型治理和AI数据产品规划。"),
    ("麦肯锡", "数字化转型顾问", "负责系统实施、数字化战略咨询和转型治理。"),
    ("腾讯集团总部", "Legal Counsel", "Provide legal advice and support business decisions."),
    ("腾讯集团总部", "Research Fellow", "Research new AI methods and support senior researchers."),
])
def test_incidental_support_or_implementation_does_not_trigger_low_value_cap(
    company, title, responsibilities,
):
    scored = score_job({
        "company": company, "title": title, "city": "深圳",
        "responsibilities": responsibilities, "requirements": "2027 graduate role.",
    }, {})
    assert "低优先级" not in scored["fit_tags"]
    assert not (scored["calibration_reason"] or "").startswith("纯销售")


def test_plain_cpp_mention_is_not_an_extreme_quant_barrier():
    scored = score_job({
        "company": "Point72", "title": "Quant Research Analyst", "city": "香港",
        "responsibilities": "Use C++ for quantitative research, machine learning and risk models.",
        "requirements": "2027 graduate role.",
    }, {})
    assert scored["technical_hard"] is False
    assert scored["quant_barrier"] is False


@pytest.mark.parametrize(("company", "title", "responsibilities"), [
    ("L.E.K. Consulting", "Associate Director", "负责行政排班与会议支持。"),
    ("中信期货总部", "风险运营助理", "负责后台支持、资料整理和流程运营。"),
    ("Kearney 科尔尼", "Business Analyst Intern", "负责战略咨询和商业分析。"),
])
def test_named_examples_do_not_promote_nearby_but_different_titles(
    company, title, responsibilities,
):
    scored = score_job({
        "company": company, "title": title, "city": "上海",
        "responsibilities": responsibilities, "requirements": "面向2027届毕业生。",
    }, {})
    assert scored["manual_override"] is False

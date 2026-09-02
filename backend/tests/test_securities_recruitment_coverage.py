"""Brokerage discovery coverage without network or paid model calls."""

import os


os.environ.setdefault("OPENAI_API_KEY", "brokerage-coverage-test-unused")
os.environ.setdefault("JWT_SECRET", "brokerage-coverage-test-secret-32-chars")
os.environ.setdefault("RECRUITMENT_WEB_SEARCH_ENABLED", "false")

from backend import recruitment_search as search
from backend.recruitment_directory import (
    PERSONAL_MONITOR_POOLS,
    employer_directory_category,
)


SECURITIES_CATEGORY = "securities_public_funds_asset_management"


def _securities_pool():
    return next(
        pool for pool in PERSONAL_MONITOR_POOLS
        if pool["primary_category"] == SECURITIES_CATEGORY
    )


def test_worthwhile_brokerage_scope_is_explicit_and_company_level():
    expected = {
        "中信证券", "中金公司", "华泰证券", "国泰海通", "中信建投", "招商证券",
        "广发证券", "申万宏源", "银河证券", "光大证券", "东方证券", "长城证券",
        "国投证券", "国信证券", "兴业证券", "国金证券", "中泰证券", "浙商证券",
        "财通证券", "长江证券", "方正证券", "国联民生证券", "平安证券", "中银证券",
        "东吴证券", "国元证券",
    }
    employers = set(_securities_pool()["employers"])
    assert expected <= employers

    targets = search.build_employer_search_targets([_securities_pool()])
    canonical_names = [target.canonical_name for target in targets]
    assert expected <= set(canonical_names)
    assert len(canonical_names) == len(set(canonical_names))


def test_legal_and_english_brokerage_names_resolve_to_the_intended_target():
    targets = search.build_employer_search_targets([_securities_pool()])
    cases = {
        "中信证券股份有限公司": "中信证券",
        "CITIC Securities": "中信证券",
        "中国国际金融股份有限公司": "中金公司",
        "CICC": "中金公司",
        "中国银河证券股份有限公司": "银河证券",
        "申万宏源证券有限公司": "申万宏源",
        "中银国际证券股份有限公司": "中银证券",
        "国联民生证券股份有限公司": "国联民生证券",
    }
    for legal_name, expected in cases.items():
        target = search._target_for_company(legal_name, targets)
        assert target is not None
        assert target.canonical_name == expected
        assert employer_directory_category(legal_name) == SECURITIES_CATEGORY


def test_only_confirmed_brokerage_recruitment_hosts_are_employer_whitelisted():
    assert search.OFFICIAL_RECRUITMENT_DOMAINS_BY_EMPLOYER["中信证券"] == (
        "careers.citics.com",
    )
    assert "cicc.zhiye.com" in search.OFFICIAL_RECRUITMENT_DOMAINS_BY_EMPLOYER["中金公司"]
    assert "csc108.com" in search.OFFICIAL_RECRUITMENT_DOMAINS_BY_EMPLOYER["中信建投"]
    assert "job.gf.com.cn" in search.OFFICIAL_RECRUITMENT_DOMAINS_BY_EMPLOYER["广发证券"]
    assert "cms.hotjob.cn" in search.OFFICIAL_RECRUITMENT_DOMAINS_BY_EMPLOYER["招商证券"]
    assert "chinastock.zhiye.com" in search.OFFICIAL_RECRUITMENT_DOMAINS_BY_EMPLOYER["银河证券"]
    assert search.OFFICIAL_RECRUITMENT_DOMAINS_BY_EMPLOYER["华泰证券"] == (
        "job.htsc.com.cn",
    )
    assert search._official_domain_confirmed(
        "中信证券股份有限公司", "https://careers.citics.com/campus/headquarters/",
        ("中信证券",),
    )
    assert search._official_domain_confirmed(
        "中国国际金融股份有限公司", "https://cicc.zhiye.com/campus/jobs",
        ("中金公司", "CICC"),
    )
    assert search._official_domain_confirmed(
        "中信建投证券股份有限公司", "https://csc108.zhiye.com/campus/jobs",
        ("中信建投",),
    )
    assert not search._official_domain_confirmed(
        "中信证券", "https://careers-citics.example.com/campus",
    )


def test_securities_prompt_carries_core_role_focus_without_excluding_other_campus_jobs():
    batch = next(
        batch for batch in search.build_employer_search_batches([_securities_pool()])
        if batch.targets[0].canonical_name == "中信证券"
    )
    prompt = search._search_prompt(batch)
    for keyword in ("投行", "FICC", "资管投研", "金融科技", "战略岗位"):
        assert keyword in prompt
    assert "不得因此漏掉" in prompt

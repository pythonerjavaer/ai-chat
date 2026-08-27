"""Conservative initial Source Registry entries.

No private conversation IDs, cookies, account IDs, or unverified WeChat URLs
belong here.  Sources without a legally reliable public discovery endpoint are
explicitly marked ``discovery_limited`` until an administrator configures one.
"""

from __future__ import annotations

import urllib.parse
from typing import Any


WECHAT_SOURCE_NAMES = (
    ("wechat-guoyang-campus", "国央校招"),
    ("wechat-iguopin", "国聘"),
    ("wechat-sasac-xiaoxin", "国资小新"),
    ("wechat-guoyang-career", "国央求职网"),
    ("wechat-bank-recruitment", "银行招聘网"),
)

EXISTING_PUBLIC_SOURCES = (
    (
        "public-iguopin-campus",
        "国聘网公开校园频道",
        "https://job.iguopin.com/jobList?campus_nature=&channel=campus&key_word=&nature_cn=",
    ),
    (
        "public-sasac-xiaoxin-existing",
        "国资小新现有公开入口",
        "https://www.gdpdd.com/s/xiaoxin/index.html",
    ),
    ("public-bank-recruitment", "银行招聘网公开入口", "https://yhks.cn/"),
)

VERIFIED_OFFICIAL_SOURCES = (
    {
        "id": "official-dji-digital-2027",
        "name": "大疆 2027 数字管理构建者计划（官网）",
        "platform": "official_web",
        "company": "大疆创新",
        "source_type": "official_html",
        "url": "https://careers.dji.com/zh-CN/campus/digital-recruitment",
        "priority": 95,
        "trust_level": "verification",
        "interval_minutes": 60,
        "adapter_config": {
            "adapter": "official_html",
            "ai_extract": False,
            "recruitment_year": 2027,
            "recruitment_type": "campus",
            "program_name": "2027 大疆“拓疆者”校园招聘",
            "job_marker": "数字管理构建者计划",
            "job_title": "数字管理构建者计划",
            "opening_date": "2026-08-07",
            "employer_type": "大型科技企业",
            "industry": "科技",
            "requirements": "面向 2027 届及优秀 2026 届高校毕业生；具体资格以官网为准。",
        },
        "query_config": {"recruitment_year": 2027, "scope": "campus"},
        "region_config": {"timezone": "Asia/Shanghai", "regions": ["中国大陆", "香港"]},
        "status": "pending",
        "verification_status": "verified",
    },
)


def initial_sources(*, web_search_enabled: bool) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for source_id, name in WECHAT_SOURCE_NAMES:
        sources.append({
            "id": source_id,
            "name": name,
            "platform": "wechat",
            "source_type": "wechat_public",
            "url": None,
            "domain": None,
            "account_name": name,
            "account_id": None,
            "enabled": True,
            "priority": 70,
            "trust_level": "discovery",
            "interval_minutes": 360,
            "adapter_config": {"adapter": "discovery_limited"},
            "query_config": {"recruitment_year": 2027, "scope": "campus"},
            "region_config": {"timezone": "Asia/Shanghai", "regions": ["中国大陆", "香港"]},
            "status": "discovery_limited",
            "verification_status": "unverified",
        })
    for source in VERIFIED_OFFICIAL_SOURCES:
        item = dict(source)
        item["domain"] = urllib.parse.urlsplit(str(item["url"])).hostname
        sources.append(item)
    for source_id, name, url in EXISTING_PUBLIC_SOURCES:
        sources.append({
            "id": source_id,
            "name": name,
            "platform": "public_web",
            "source_type": "other_public_source",
            "url": url,
            "domain": urllib.parse.urlsplit(url).hostname,
            "enabled": True,
            "priority": 55,
            "trust_level": "discovery",
            "interval_minutes": 120,
            "adapter_config": {"adapter": "official_html", "ai_extract": False},
            "query_config": {"recruitment_year": 2027, "scope": "campus"},
            "region_config": {"timezone": "Asia/Shanghai", "regions": ["中国大陆", "香港"]},
            "status": "pending",
            "verification_status": "unverified",
        })
    sources.extend([
        {
            "id": "legacy-recruitment-pipeline",
            "name": "冰焰现有已核验岗位池",
            "platform": "internal",
            "source_type": "manual",
            "enabled": True,
            "priority": 90,
            "trust_level": "verification",
            "interval_minutes": 30,
            "adapter_config": {"adapter": "legacy_database"},
            "status": "pending",
            "verification_status": "verified",
        },
        {
            "id": "openai-public-web-search",
            "name": "OpenAI 公共网页补漏",
            "platform": "openai",
            "source_type": "openai_web_search",
            "enabled": web_search_enabled,
            "priority": 30,
            "trust_level": "discovery",
            "interval_minutes": 360,
            "adapter_config": {"adapter": "openai_web_search"},
            "query_config": {"recruitment_year": 2027, "scope": "campus"},
            "region_config": {"timezone": "Asia/Shanghai", "regions": ["中国大陆", "香港"]},
            "status": "pending" if web_search_enabled else "disabled",
            "verification_status": "unverified",
        },
        {
            "id": "mock-future-radar",
            "name": "Future Radar Mock Lifecycle",
            "platform": "mock",
            "source_type": "manual",
            "enabled": False,
            "priority": 100,
            "trust_level": "verification",
            "interval_minutes": 1_440,
            "adapter_config": {"adapter": "mock", "round": 1},
            "status": "disabled",
            "verification_status": "verified",
        },
    ])
    return sources

from datetime import date
from typing import Any


SAMPLE_JOBS = [
    {
        "id": "sample-byteplus-product-2026",
        "company": "字节跳动",
        "employer_type": "互联网企业",
        "title": "产品经理（2026届秋招）",
        "city": "北京 / 上海 / 深圳",
        "industry": "互联网",
        "source": "示例岗位，等待接入官方源",
        "opening_date": "2026-08-15",
        "closing_date": "2026-10-15",
        "requirements": "产品设计、数据分析、用户研究；有项目经历优先。",
        "tags": ["产品", "互联网", "应届生"],
        "historical_applicants": 1200,
        "historical_offers": 36,
    },
    {
        "id": "sample-hsbc-analyst-2026",
        "company": "汇丰银行",
        "employer_type": "外企",
        "title": "Management Trainee / Business Analyst",
        "city": "上海 / 香港",
        "industry": "金融科技",
        "source": "示例岗位，等待接入官方源",
        "opening_date": "2026-07-20",
        "closing_date": "2026-09-30",
        "requirements": "英语沟通、商业分析、跨团队协作；接受海外背景。",
        "tags": ["英语", "分析", "外企"],
        "historical_applicants": 680,
        "historical_offers": 28,
    },
    {
        "id": "sample-state-tech-2026",
        "company": "国家电网",
        "employer_type": "央国企",
        "title": "数字化技术岗（2026届）",
        "city": "全国多地",
        "industry": "央国企",
        "source": "示例岗位，等待接入官方源",
        "opening_date": "2026-09-01",
        "closing_date": "2026-11-10",
        "requirements": "计算机、数据科学、信息管理相关专业；需要通过网申和笔试。",
        "tags": ["数字化", "央国企", "技术岗"],
        "historical_applicants": 4500,
        "historical_offers": 180,
    },
]


def _words(values: Any) -> set[str]:
    if not isinstance(values, list):
        return set()
    return {str(value).strip().lower() for value in values if str(value).strip()}


def score_job(job: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    desired_roles = _words(profile.get("desired_roles"))
    industries = _words(profile.get("industries"))
    locations = _words(profile.get("locations"))
    employer_types = _words(profile.get("employer_types"))
    haystack = " ".join(
        str(job.get(key, "")) for key in ("title", "company", "city", "industry", "requirements", "tags")
    ).lower()
    reasons: list[str] = []
    score = 35
    role_hits = [word for word in desired_roles if word in haystack]
    if role_hits:
        score += min(28, 12 + 8 * len(role_hits))
        reasons.append("岗位方向匹配")
    if industries and any(word in haystack for word in industries):
        score += 14
        reasons.append("行业偏好匹配")
    if locations and any(word in str(job.get("city", "")).lower() for word in locations):
        score += 8
        reasons.append("城市偏好匹配")
    if employer_types and str(job.get("employer_type", "")).lower() in employer_types:
        score += 12
        reasons.append("雇主类型匹配")
    score = min(98, score)
    if score >= 85:
        tier_code = "T0"
    elif score >= 72:
        tier_code = "T1"
    elif score >= 58:
        tier_code = "T2"
    else:
        tier_code = "T3"
    closing_date = job.get("closing_date")
    days_left = None
    if closing_date:
        try:
            days_left = (date.fromisoformat(closing_date) - date.today()).days
        except ValueError:
            pass
    return {
        **job,
        "match_score": score,
        "match_reasons": reasons or ["当前未设置筛选条件，按最新岗位展示"],
        "tier_code": tier_code,
        "days_left": days_left,
    }

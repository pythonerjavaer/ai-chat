from datetime import date
from typing import Any


# UI categories intentionally stay human-readable while the stored job feed
# keeps its stable employer_type values.  Expand them here so a user can pick
# a clean category (for example “外企/咨询”) without losing matches from an
# existing official card tagged simply “外企”.
EMPLOYER_TYPE_ALIASES: dict[str, set[str]] = {
    "央国企": {"央国企", "央国企能源", "央国企科技"},
    "央国企科技": {"央国企", "央国企科技"},
    "银行/金融": {"银行/金融", "政策性金融"},
    "券商/基金": {"券商/基金", "资管"},
    "保险/综合金融": {"保险/综合金融", "保险"},
    "互联网企业": {"互联网企业", "互联网"},
    "快消/消费": {"快消/消费", "快消"},
    "外企/咨询": {"外企/咨询", "外企", "咨询"},
    "量化私募": {"量化私募", "量化", "私募"},
}

# Tiers are deliberately deterministic and explainable.  They are a fit
# priority, not a probability of receiving an offer.
TIER_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {"code": "T0", "min_score": 90, "label": "强匹配", "description": "岗位方向、行业/雇主与城市等多项核心条件同时命中"},
    {"code": "T0.5", "min_score": 85, "label": "高匹配", "description": "核心方向高度匹配，仍需核对具体门槛"},
    {"code": "T1", "min_score": 78, "label": "主力", "description": "岗位方向与至少一项偏好明确匹配"},
    {"code": "T1.5", "min_score": 72, "label": "较主力", "description": "方向基本匹配，但城市或行业信息不完整"},
    {"code": "T2", "min_score": 64, "label": "可投", "description": "具备部分匹配信号，建议结合公告筛选"},
    {"code": "T2.5", "min_score": 56, "label": "观察", "description": "弱匹配或信息不足，仅在岗位条件合适时考虑"},
    {"code": "T3", "min_score": 0, "label": "低匹配", "description": "暂未命中核心偏好，不进入优先投递队列"},
)


def tier_for_score(score: int) -> str:
    safe_score = max(0, min(100, int(score)))
    for definition in TIER_DEFINITIONS:
        if safe_score >= definition["min_score"]:
            return definition["code"]
    return "T3"


def _words(values: Any) -> set[str]:
    if not isinstance(values, list):
        return set()
    return {str(value).strip().lower() for value in values if str(value).strip()}


def score_job(job: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    desired_roles = _words(profile.get("desired_roles"))
    industries = _words(profile.get("industries"))
    locations = _words(profile.get("locations"))
    employer_types = _words(profile.get("employer_types"))
    selected_employer_types = {
        value
        for category in employer_types
        for value in EMPLOYER_TYPE_ALIASES.get(category, {category})
    }
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
    if selected_employer_types and str(job.get("employer_type", "")).lower() in selected_employer_types:
        score += 12
        reasons.append("雇主类型匹配")
    score = min(98, score)
    tier_code = tier_for_score(score)
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

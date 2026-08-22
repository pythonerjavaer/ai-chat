from datetime import date
from typing import Any


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

"""Auditable weighted title rules; no model, network, API key or SDK calls."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod

from .models import TitleClassification


# Within a family use the largest matching weight, so overlapping phrases
# such as 招聘 / 校园招聘 / 校园招聘启动 do not inflate a title's score.
POSITIVE_RULES = {
    "campus": {
        "秋季校园招聘": 55, "校园招聘启动": 55, "校园招聘": 52,
        "毕业生招聘": 52, "秋招": 48, "春招": 48, "应届生": 38,
        "管理培训生": 45, "管培生": 43,
    },
    "recruitment": {
        "招聘正式启动": 35, "招聘开启": 32, "招聘公告": 32,
        "招聘简章": 28, "招聘计划": 25, "招聘岗位": 24,
        "人才招聘": 24, "招聘": 22,
    },
    "application": {
        "报名开启": 22, "报名截止": 20, "网申": 20, "报名": 14, "宣讲会": 18,
    },
    "domain": {
        "总行管培": 12, "金融科技": 10, "中央企业": 10, "央企": 9,
        "国企": 9, "银行": 9, "总行": 8, "分行": 7,
        "科技岗": 8, "数据": 5, "人工智能": 8, "管培": 8,
    },
}
NEGATIVE_RULES = {
    "社会招聘": 45, "社招": 45, "内部招聘": 60, "劳务派遣": 55,
    "培训课程": 45, "备考资料": 40, "招聘骗局": 70, "虚假招聘": 70,
    "拟录用": 35, "录用公示": 35,
}
_CURRENT_COHORT = re.compile(r"2027\s*(?:届|年(?:度)?)?")

# A lead remains an early signal regardless of this label.  The label only
# prevents a reading guide or a multi-employer roundup from looking like one
# concrete application opportunity in the UI.
LEAD_TYPE_RULES = {
    "advice": ("怎么选", "攻略", "备考", "经验", "面试", "笔试", "交流群"),
    "roundup": ("汇总", "合集", "盘点", "各大", "多家", "一览", "秋招爆了", "岗位表", "大型银行"),
}


def classify_lead_type(title: str) -> str:
    """Classify a saved recruitment signal without network or model calls."""
    text = re.sub(r"\s+", "", str(title or "")).casefold()
    if any(token in text for token in LEAD_TYPE_RULES["advice"]):
        return "advice"
    if any(token in text for token in LEAD_TYPE_RULES["roundup"]):
        return "roundup"
    if any(token in text for token in ("校园招聘", "招聘公告", "招聘启动", "招聘正式启动", "招聘简章", "招聘计划")):
        return "direct_opportunity"
    return "unknown"


class TitleClassifier(ABC):
    @abstractmethod
    def classify(self, title: str) -> TitleClassification:
        raise NotImplementedError


class RuleBasedTitleClassifier(TitleClassifier):
    def classify(self, title: str) -> TitleClassification:
        return classify_recruitment_title(title)


def classify_recruitment_title(title: str) -> TitleClassification:
    text = re.sub(r"\s+", "", str(title or "")).casefold()
    matches: list[str] = []
    score = 0
    family_scores: dict[str, int] = {}
    for family, rules in POSITIVE_RULES.items():
        found = [(keyword, weight) for keyword, weight in rules.items() if keyword in text]
        family_scores[family] = max((weight for _, weight in found), default=0)
        score += family_scores[family]
        matches.extend(keyword for keyword, _ in found)
    cohort = _CURRENT_COHORT.search(text)
    if cohort:
        matches.append("2027届" if "2027届" in text else "2027")
        score += 15
    if cohort and family_scores["campus"]:
        score += 25
    # Domain words alone are not evidence of recruitment.
    if not any(family_scores[key] for key in ("campus", "recruitment", "application")):
        score = min(score, 15)
    negatives = [(keyword, weight) for keyword, weight in NEGATIVE_RULES.items() if keyword in text]
    if negatives:
        score -= max(weight for _, weight in negatives)
        matches.extend(keyword for keyword, _ in negatives)
    score = max(0, min(100, score))
    status = "relevant" if score >= 65 else "possible" if score >= 25 else "irrelevant"
    return TitleClassification(
        relevance_status=status, relevance_score=score,
        matched_keywords=list(dict.fromkeys(matches)),
    )

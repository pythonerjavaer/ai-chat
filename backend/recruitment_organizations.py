"""Conservative organization-level evidence for deterministic job ranking.

This module does not classify industries, authenticate employers or assign T
tiers.  It reads employer/unit names and narrowly labelled hiring statements,
not arbitrary mentions of an employer's headquarters in a job description.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .future_radar.normalization import (
    _OPERATOR_BRANDS,
    _OPERATOR_ROOTS,
    canonical_telecom_operator,
    normalized_key,
)


_ENTITY_FIELDS = (
    "company", "employer_entity", "hiring_entity", "recruiting_entity",
    "hiring_unit", "recruitment_unit", "subsidiary", "subsidiary_name",
    "department", "department_name", "hiring_department",
)
_SUBSIDIARY_FIELDS = {"subsidiary", "subsidiary_name"}
_PROVINCES = (
    "河北", "山西", "辽宁", "吉林", "黑龙江", "江苏", "浙江", "安徽", "福建",
    "江西", "山东", "河南", "湖北", "湖南", "广东", "海南", "四川", "贵州",
    "云南", "陕西", "甘肃", "青海", "台湾",
)
_MUNICIPALITIES = ("北京", "上海", "天津", "重庆")
_AUTONOMOUS_REGIONS = (
    "内蒙古自治区", "广西壮族自治区", "宁夏回族自治区", "新疆维吾尔自治区",
    "西藏自治区", "内蒙古", "广西", "宁夏", "新疆", "西藏", "香港", "澳门",
)
# A small set of common recruiting cities, not a nationwide geography database.
# Unknown suffixless places remain branch_unspecified until there is evidence.
_COMMON_CITIES = (
    "石家庄", "广州", "深圳", "杭州", "南京", "苏州", "成都", "武汉", "西安",
    "长沙", "合肥", "厦门", "南昌",
)
_PROVINCE_SCOPE = (
    "(?:" + "|".join(_PROVINCES) + ")(?:省)?"
    + "|(?:" + "|".join(_MUNICIPALITIES) + ")(?:市)?"
    + "|(?:" + "|".join(_AUTONOMOUS_REGIONS) + ")"
)
_PROVINCIAL_UNIT = re.compile(
    rf"(?:{_PROVINCE_SCOPE})(?:分公司|分行|分部|公司|本部)"
    r"|(?:省级|省|自治区|直辖市)(?:分公司|分行|公司|分支机构)"
    r"|省分(?:公司|行|本部)?"
    r"|(?:华北|华东|华南|华中|东北|西北|西南|亚太|大中华|中国区|区域|大区)"
    r"(?:地区|区域|大区)?(?:总部|分公司|分行|分部|公司|本部|中心)"
    r"|\b(?:regional|apac|emea|greater\s+china)\s+(?:headquarters|head\s+office|office|branch)\b",
    re.I,
)
_REGIONAL_LEGAL_ENTITY = re.compile(
    rf"集团(?:{_PROVINCE_SCOPE})(?:有限责任公司|股份有限公司|有限公司)"
)
_LOCAL_UNIT = re.compile(
    r"支行|支公司|分理处|营业网点|营业厅|营业部|县级|县区|区县|基层|网点"
    r"|(?:县|旗)(?:分公司|公司|分行|支公司)"
    r"|(?<!自治)(?<!地)(?<!大)(?<!国)区(?:分公司|公司|分行|支公司)"
)
_CITY_UNIT = re.compile(
    r"地市|地级市|市级|(?:市|地区|自治州|盟)(?:分公司|分行|公司|分部)"
)
_COMMON_CITY_UNIT = re.compile(
    "(?:" + "|".join(_COMMON_CITIES) + r")(?:分公司|分行|公司|分部)"
)
_BRANCH = re.compile(r"分公司|分行|分部|分支机构|分支|\bbranch(?:\s+office)?\b", re.I)
_SUBSIDIARY = re.compile(r"子公司|子企业|附属公司|控股子企业|参股公司|\bsubsidiar(?:y|ies)\b", re.I)
_NESTED_LEGAL_ENTITY = re.compile(
    r"集团(?!有限责任公司|股份有限公司|有限公司|公司)"
    r"[\u4e00-\u9fffA-Za-z（）()·\s]{2,50}(?:有限责任公司|股份有限公司|有限公司)"
)
_RESEARCH_INSTITUTE = re.compile(r"研究院|研究所|研究分院|\bresearch\s+institute\b", re.I)
_HEADQUARTERS = re.compile(r"集团总部|总部|总行|\bhead\s+office\b|\bheadquarters\b", re.I)
_THIRD_PARTY = re.compile(
    r"外包|劳务派遣|劳务外派|派遣制|代理商|加盟商|合作伙伴|驻场服务商|第三方用工"
    r"|\b(?:outsourc(?:e|ed|ing)|staffing\s+agency|contractor|franchisee)\b", re.I,
)
_NEGATED_THIRD_PARTY = re.compile(
    r"(?:非|不属于|不是|并非|不采用|无)(?:劳务)?(?:外包|派遣|外派)"
    r"|\b(?:not|non)[ -]+(?:outsourc(?:e|ed|ing)|contractor)\b", re.I,
)
_NON_AFFILIATION = re.compile(
    r"非(?:集团)?总部|非总行|(?:不是|并非|不属于|不在|不含)(?:集团)?(?:总部|总行)"
    r"|(?:总部|总行)(?:位于|设在|设于|坐落|在[\u4e00-\u9fff]{1,8})"
    r"|(?:对接|向|向着|汇报|支持|服务|协助|配合|联系|面向|与|为)(?:集团)?(?:总部|总行)"
    r"|公司简介|公司介绍|集团介绍|拥有(?:集团)?总部"
    r"|\b(?:report(?:s|ing)?\s+to|liais\w*\s+with|support(?:s|ing)?|not\s+(?:at|in))\s+(?:the\s+)?(?:head\s+office|headquarters)\b"
    r"|\bheadquarters\s+(?:is|are|located|based)\b", re.I,
)
_TITLE_SERVICE_OBJECT = re.compile(
    r"对接|服务|支持|覆盖|面向|协助|配合|支援|联络|联系|汇报|沟通|负责|对口|协同"
    r"|\b(?:support(?:s|ing)?|serv(?:e|es|ing)|cover(?:s|ing)?|liais\w*|report(?:s|ing)?\s+to)\b",
    re.I,
)
_CONTRACT_FIELDS = {"contract_company", "contract_entity", "signing_company", "signing_entity", "legal_employer"}
_CONTRACT_LABELS = {"签约单位", "劳动合同签订单位", "合同签署方", "雇佣单位"}
_DEPARTMENT_FIELDS = {"department", "department_name", "hiring_department"}
_THIRD_PARTY_SERVICE_OBJECT = re.compile(
    r"(?:外包商?|派遣人员|合作伙伴|代理商)(?:管理|治理|协调|对接|支持|提供|拓展|运营|服务(?!商))"
)
_UNIT_STATEMENT = re.compile(
    r"(?:^|[\n\r。；;])\s*(招聘单位|招聘部门|用人单位|所属单位|所属公司|任职单位|"
    r"雇佣单位|签约单位|劳动合同签订单位|合同签署方|岗位归属|所属机构|招聘公司)\s*[:：]\s*([^\n\r。；;]{2,160})"
)
_LEGAL_ENTITY_ENDING = re.compile(r"(?:股份有限公司|有限责任公司|有限公司)$")
_NAMED_REGIONAL_UNIT = re.compile(
    rf"^(?:{_PROVINCE_SCOPE})[\u4e00-\u9fffA-Za-z·]{{2,60}}(?:中心|事业部|机构|办事处)$"
)
_EMPLOYMENT_STATEMENT = re.compile(
    r"(?:^|[\n\r。；;])\s*(?:(?:本|该)(?:岗位|职位)|用工形式|用工方式|合同性质|劳动关系)"
    r"[^\n\r。；;]{0,70}"
)
_EMPLOYMENT_DECLARATION = re.compile(
    r"^(?:(?:本|该)(?:岗位|职位)\s*(?:(?:的)?(?:用工形式|用工方式|合同性质|劳动关系)\s*)?"
    r"(?:为|属于|采用|实行|采取|系|是)|(?:用工形式|用工方式|合同性质|劳动关系)\s*"
    r"(?:[:：]|为|属于|采用|系|是))\s*(.+)"
)
_DIRECT_EMPLOYMENT_CONTRACT = re.compile(
    r"^(?:本|该)(?:岗位|职位)\s*(?:将)?(?:与|由)[^\n\r。；;，,]{2,50}"
    r"(?:签约|签订(?:劳动)?合同|订立(?:劳动)?合同|聘用|派遣|招聘)"
)
_NEGATED_EMPLOYMENT_SUBJECT = re.compile(
    r"^((?:本|该)(?:岗位|职位)|用工形式|用工方式|合同性质|劳动关系)\s*"
    r"(?:(?:的)?(?:用工形式|用工方式|合同性质|劳动关系)\s*)?(?:[:：]|为)?\s*"
    r"(?:非|并非|不是|不属于|不采用|无)(?:劳务)?(?:外包|派遣|外派)"
)
_PARENTHESES = re.compile(r"[（(]([^()（）]{1,100})[）)]")
_TITLE_UNIT_PREFIX = re.compile(
    r"^([^:：|，,。；;（）()\n]{0,60}?(?:集团总部|总部|总行|分公司|分行|支行|子公司|研究院))"
)
_LOCATION_ENDING = re.compile(r"([\u4e00-\u9fff]{2,24})(县|区|旗|市)")
_CONTACT_START = re.compile(r"联系人|联系电话|联系方式|联系邮箱|邮箱|电子邮件|手机号|电话")
_PLACEHOLDER = re.compile(r"^(?:以.*为准|待定|待确认|详见.*|不详|未知|各单位|各分公司)$")

# Reuse the existing public employer identity directory, not a second company
# ranking table.  Normalization imports recruitment only inside an unrelated
# category function; these name-only helpers do not cause a circular import.
_DIRECTORY_ROOTS = {
    operator: frozenset(normalized_key(name) for name in names)
    for operator, names in _OPERATOR_ROOTS.items()
}
_DIRECTORY_AFFILIATES = {
    operator: {
        normalized_key(name): (
            "research_institute" if _RESEARCH_INSTITUTE.search(name) else "subsidiary"
        )
        for name in names
        if _RESEARCH_INSTITUTE.search(name) or "公司" in name or name in {"天翼云", "联通数科"}
    }
    for operator, names in _OPERATOR_BRANDS.items()
}

_LABELS = {
    "group_headquarters": "集团总部/总行",
    "provincial_branch": "省级/区域分支",
    "city_branch": "地市分支",
    "local_branch": "县区/基层网点",
    "branch_unspecified": "层级待核验的分支",
    "subsidiary": "独立子公司",
    "research_institute": "研究机构",
    "third_party": "外包/代理/派遣",
    "unspecified": "组织层级待核验",
}
# A more specific lower hiring unit wins over an ancestor's headquarters label.
_SPECIFICITY = {
    "unspecified": 0, "group_headquarters": 10, "research_institute": 20,
    "subsidiary": 30, "provincial_branch": 40, "branch_unspecified": 45,
    "city_branch": 50, "local_branch": 60, "third_party": 70,
}
_BRANCH_LEVELS = {"provincial_branch", "city_branch", "local_branch", "branch_unspecified"}
_NOTES = {
    "group_headquarters": "招聘实体或岗位署名明确为总部/总行；该识别不代表真实性已获官方核验。",
    "provincial_branch": "按省级或区域分支计算平台资源，省分公司总部不等于集团总部。",
    "city_branch": "按实际地市招聘单位计算，不继承集团总部或省级公司的完整平台分。",
    "local_branch": "按县区或基层网点计算，需单独核对岗位内容、签约单位与发展空间。",
    "branch_unspecified": "已识别为分支，但行政层级不足以确认；不按集团总部或省级机构推定。",
    "subsidiary": "子公司的独立平台价值需另行核验；“核心”“科技”及子公司总部不自动获得集团总部加分。",
    "research_institute": "研究机构不等于集团总部；尚未独立核验其平台资源，保守计算且不加总部分。",
    "third_party": "外包、代理或派遣关系不能继承服务对象或母品牌的完整平台资源。",
    "unspecified": "招聘实体层级证据不足；未加总部或核心机构分，仍需核验具体用人单位。",
}


@dataclass(frozen=True)
class _Assessment:
    level: str
    source: str
    text: str
    confidence: str = "explicit"
    auxiliary: str = ""


def _text(value: Any) -> str:
    return re.sub(r"[ \t]+", " ", value).strip() if isinstance(value, str) else ""


def _public_evidence(value: str) -> str:
    """Keep only a short public organization/designation, never contact details."""
    value = _CONTACT_START.split(value, maxsplit=1)[0]
    value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[联系方式已省略]", value)
    value = re.sub(r"(?<!\d)\+?\d[\d ()-]{7,}\d(?!\d)", "[联系方式已省略]", value)
    return re.sub(r"\s+", " ", value).strip()[:160]


def _third_party(text: str) -> bool:
    # Negating dispatch must not erase a separate affirmative outsourcing fact.
    return bool(_THIRD_PARTY.search(_NEGATED_THIRD_PARTY.sub(" ", text)))


def _employment_relationship(text: str) -> bool:
    """Require an employment predicate, not a duty concerning outside firms."""
    text = text.lstrip(" \t\r\n。；;")
    declaration = _EMPLOYMENT_DECLARATION.search(text)
    if declaration:
        # Later duties do not negate an already stated employment arrangement.
        fact = re.split(r"[，,]", declaration[1], maxsplit=1)[0]
        if _third_party(fact) and not _THIRD_PARTY_SERVICE_OBJECT.search(fact):
            return True
    if _DIRECT_EMPLOYMENT_CONTRACT.search(text) and _third_party(text):
        return True
    clauses = re.split(r"[，,]", text)
    negated_subject = _NEGATED_EMPLOYMENT_SUBJECT.search(clauses[0])
    if negated_subject:
        # "本岗位非派遣，仅为外包" keeps the same subject in the next clause.
        # Reuse the strict predicate check, so "仅负责外包商管理" stays a duty.
        for clause in clauses[1:]:
            statement = re.sub(r"^(?:(?:但|而|仅|实际(?:上)?|仍|却)\s*)+", "", clause.strip())
            if _employment_relationship(f"{negated_subject[1]}{statement}"):
                return True
    return False


def _directory_affiliate(text: str) -> str | None:
    # A subsidiary's own 总部/本部 is still that subsidiary, not its parent HQ.
    variants = (text, _HEADQUARTERS.split(text, maxsplit=1)[0], text.split("本部", 1)[0])
    for name in variants:
        operator = canonical_telecom_operator(name)
        if not operator:
            continue
        key = normalized_key(name)
        if key in _DIRECTORY_ROOTS[operator]:
            continue
        level = _DIRECTORY_AFFILIATES[operator].get(key)
        if level:
            return level
    return None


def _assess_entity(text: str, source: str, *, subsidiary: bool = False) -> _Assessment:
    department_source = source in _DEPARTMENT_FIELDS or source.endswith(".招聘部门")
    internal_service_department = (
        department_source and "公司" not in text and _THIRD_PARTY_SERVICE_OBJECT.search(text)
    )
    if _third_party(text) and not internal_service_department:
        return _Assessment("third_party", source, text)
    # Mask province/municipality units before interpreting 市/区 suffixes.  This
    # still leaves an actual lower unit, e.g. 上海市分公司浦东新区支公司, visible.
    remainder = _PROVINCIAL_UNIT.sub(" ", text)
    if _LOCAL_UNIT.search(remainder):
        return _Assessment("local_branch", source, text)
    if _CITY_UNIT.search(remainder):
        return _Assessment("city_branch", source, text)
    if _COMMON_CITY_UNIT.search(remainder):
        return _Assessment("city_branch", source, text, "inferred")
    if _PROVINCIAL_UNIT.search(text):
        if _BRANCH.search(remainder):
            return _Assessment("branch_unspecified", source, text, "inferred")
        return _Assessment("provincial_branch", source, text)
    if _REGIONAL_LEGAL_ENTITY.search(text):
        return _Assessment("provincial_branch", source, text, "inferred")
    if _BRANCH.search(text):
        return _Assessment("branch_unspecified", source, text)
    if subsidiary or _SUBSIDIARY.search(text):
        return _Assessment("subsidiary", source, text)
    affiliate_level = _directory_affiliate(text)
    if affiliate_level:
        return _Assessment(affiliate_level, f"{source}.单位目录", text, "inferred")
    if _NESTED_LEGAL_ENTITY.search(text):
        return _Assessment("subsidiary", source, text, "inferred")
    if _RESEARCH_INSTITUTE.search(text):
        return _Assessment("research_institute", source, text)
    if _HEADQUARTERS.search(text) and not _NON_AFFILIATION.search(text):
        return _Assessment("group_headquarters", source, text)
    return _Assessment("unspecified", source, text, "unknown")


def _title_location_hint(assessment: _Assessment, title: str) -> _Assessment:
    if assessment.level not in _BRANCH_LEVELS:
        return assessment
    for parentheses in _PARENTHESES.finditer(title):
        if _TITLE_SERVICE_OBJECT.search(parentheses[1]) or _NON_AFFILIATION.search(parentheses[1]):
            continue
        for match in _LOCATION_ENDING.finditer(parentheses[1]):
            stem, suffix = match.groups()
            stem = re.split(r"自治区|自治州|地区|省|市", stem)[-1]
            if len(stem) < 2 or stem not in assessment.text:
                continue
            if stem in (*_PROVINCES, *_MUNICIPALITIES, *_AUTONOMOUS_REGIONS):
                continue
            level = "local_branch" if suffix in {"县", "区", "旗"} else "city_branch"
            if _SPECIFICITY[level] > _SPECIFICITY[assessment.level]:
                assessment = _Assessment(
                    level, assessment.source, assessment.text, "inferred", f"岗位署名：{stem}{suffix}",
                )
    return assessment


def _candidates(job: dict[str, Any]) -> list[_Assessment]:
    result: list[_Assessment] = []
    parent_company = _text(job.get("parent_company"))
    for field in (*_ENTITY_FIELDS, *sorted(_CONTRACT_FIELDS)):
        text = _text(job.get(field))
        if not text or _PLACEHOLDER.fullmatch(text):
            continue
        subsidiary = field in _SUBSIDIARY_FIELDS or bool(
            field == "company" and parent_company and parent_company != text
        )
        result.append(_assess_entity(text, field, subsidiary=subsidiary))
    for field in ("requirements", "responsibilities", "description"):
        text = _text(job.get(field))
        for match in _UNIT_STATEMENT.finditer(text):
            if not _PLACEHOLDER.fullmatch(match[2].strip()):
                result.append(_assess_entity(match[2].strip(), f"{field}.{match[1]}"))
        for match in _EMPLOYMENT_STATEMENT.finditer(text):
            if _employment_relationship(match[0]):
                result.append(_Assessment("third_party", f"{field}.用工关系", match[0].strip()))
    for field in ("employment_type", "employment_relationship", "contract_type", "employment_form"):
        text = _text(job.get(field))
        if text and _third_party(text):
            result.append(_Assessment("third_party", field, text))
    title = _text(job.get("title"))
    prefix = _TITLE_UNIT_PREFIX.search(title)
    signatures = ([prefix[1]] if prefix else []) + [match[1] for match in _PARENTHESES.finditer(title)]
    for signature in signatures:
        if _NON_AFFILIATION.search(signature) or _TITLE_SERVICE_OBJECT.search(signature):
            continue
        assessment = _assess_entity(signature, "title.岗位署名")
        if assessment.level == "third_party" and re.search(
            r"(?:外包|派遣|代理商|合作伙伴).*(?:管理|协调|治理)", signature,
        ):
            continue
        if assessment.level != "unspecified":
            result.append(assessment)
    return [_title_location_hint(item, title) for item in result]


def _entity_identity(text: str) -> str:
    """Compare declared entities, not their headlines or corporate suffixes."""
    name = _public_evidence(text)
    headquarters = _HEADQUARTERS.search(name)
    if headquarters:
        name = name[:headquarters.start()] + ("集团" if headquarters[0] == "集团总部" else "")
    key = normalized_key(name)
    operator = canonical_telecom_operator(name)
    if operator and key in _DIRECTORY_ROOTS[operator]:
        return f"{operator}:group"
    key = re.sub(r"(?:股份有限公司|有限责任公司|有限公司)$", "", key)
    return re.sub(r"^中国(?=.{2,}银行)", "", key)


def _conflicting_contracts(candidates: list[_Assessment]) -> list[_Assessment]:
    contracts = [
        item for item in candidates
        if item.source.removesuffix(".单位目录") in _CONTRACT_FIELDS
        or item.source.removesuffix(".单位目录").rsplit(".", 1)[-1] in _CONTRACT_LABELS
    ]
    if not contracts:
        return []
    company = next((item for item in candidates if item.source.removesuffix(".单位目录") == "company"), None)
    identities = {_entity_identity(item.text) for item in contracts}
    if company:
        identities.add(_entity_identity(company.text))
    identities.discard("")
    return contracts if len(identities) > 1 else []


def _specific_unresolved_units(candidates: list[_Assessment]) -> list[_Assessment]:
    """Do not transfer a campaign group's resources to a different named unit.

    A distinct company-form name or named regional institution is not evidence
    of a subsidiary relationship or an exact administrative level.  Plain
    internal departments such as 财务部 do not meet this narrow condition.
    """
    company = next((item for item in candidates if item.source.removesuffix(".单位目录") == "company"), None)
    if not company:
        return []
    company_identity = _entity_identity(company.text)
    result = []
    for item in candidates:
        explicit_unit = (
            item.source in (*_ENTITY_FIELDS, *_CONTRACT_FIELDS)
            or item.source.startswith(("requirements.", "responsibilities.", "description."))
        )
        if item.level != "unspecified" or item.source == "company" or not explicit_unit:
            continue
        name = _public_evidence(item.text)
        if _entity_identity(name) == company_identity:
            continue
        if _LEGAL_ENTITY_ENDING.search(name) or _NAMED_REGIONAL_UNIT.fullmatch(name):
            result.append(item)
    return result


def _platform_points(level: str, base: int) -> int:
    if level == "group_headquarters":
        return min(16, base + 2)
    reductions = {
        "provincial_branch": (3, 4, 16), "city_branch": (5, 4, 16),
        "local_branch": (7, 3, 16), "branch_unspecified": (4, 4, 16),
        "subsidiary": (3, 4, 10), "research_institute": (1, 4, 12),
        "third_party": (6, 3, 6),
    }
    if level not in reductions:
        return base
    reduction, floor, ceiling = reductions[level]
    # A low starting band must never gain points through a branch's floor.
    return min(base, ceiling, max(floor, base - reduction))


def assess_organization(
    job: dict[str, Any], *, base_platform_points: int, platform_band: str,
) -> dict[str, Any]:
    """Assess public hiring-unit hierarchy; confidence describes text evidence.

    ``explicit``/``inferred`` are not statements of official verification.
    ``city`` alone never changes the employer hierarchy.  This function is
    deterministic and has no network, persistence, profile or scoring imports.
    """
    base = max(0, min(16, int(base_platform_points)))
    candidates = _candidates(job)
    assessment = max(
        candidates,
        key=lambda item: (_SPECIFICITY[item.level], item.confidence == "explicit"),
        default=_Assessment("unspecified", "none", "", "unknown"),
    )
    unresolved_units = _specific_unresolved_units(candidates)
    specific_unresolved = bool(unresolved_units) and assessment.level in {"unspecified", "group_headquarters"}
    if specific_unresolved:
        assessment = unresolved_units[0]
    conflicting_contracts = _conflicting_contracts(candidates)
    if conflicting_contracts and assessment.level == "group_headquarters":
        # A highest-level headline cannot establish the employer when the
        # declared signing entities disagree.  Do not invent outsourcing, either.
        alternatives = [item for item in (*conflicting_contracts, *candidates) if item.level != "group_headquarters"]
        assessment = max(
            alternatives,
            key=lambda item: _SPECIFICITY[item.level],
            default=_Assessment("unspecified", conflicting_contracts[0].source, conflicting_contracts[0].text, "unknown"),
        )
    points = _platform_points(assessment.level, base)
    if conflicting_contracts or specific_unresolved:
        points = min(points, base, 10)
    evidence = []
    if assessment.text:
        evidence.append(f"{assessment.source}：{_public_evidence(assessment.text)}")
    if assessment.auxiliary:
        evidence.append(assessment.auxiliary)
    note = _NOTES[assessment.level]
    if specific_unresolved:
        note = (
            f"具体招聘单位“{_public_evidence(assessment.text)}”的层级与独立平台资源尚待核验；"
            "不继承统一招聘集团的完整平台基准，也不据此推定省级机构、子公司或外包关系。"
        )
    if assessment.source.endswith(".单位目录"):
        note += "依据已有公开单位目录识别关联实体，不代表实际用工或核心资质已获核验。"
    if conflicting_contracts:
        note += "招聘实体与签约单位署名存在差异或冲突，按保守层级处理；需核验真实签约主体，不将最高层级名称视为已确定雇主。"
    if conflicting_contracts or specific_unresolved:
        sources = [item for item in candidates if item.source.removesuffix(".单位目录") == "company"]
        for item in (*sources, *conflicting_contracts):
            snippet = f"{item.source}：{_public_evidence(item.text)}"
            if snippet not in evidence:
                evidence.append(snippet)
        evidence = evidence[:4]
    return {
        "level": assessment.level,
        "label": _LABELS[assessment.level],
        "confidence": assessment.confidence,
        "basis": assessment.source + ("+title.岗位署名" if assessment.auxiliary else "") + ("+具体单位待核验" if specific_unresolved else "") + ("+签约主体冲突" if conflicting_contracts else ""),
        "evidence": evidence,
        "base_platform_points": base,
        "platform_band": str(platform_band),
        "platform_points": points,
        "platform_adjustment": points - base,
        "is_group_headquarters": assessment.level == "group_headquarters",
        "note": note,
    }

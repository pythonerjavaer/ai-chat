from datetime import date, timedelta

import pytest

from backend import recruitment_search as search


YEAR = date.today().year + (1 if date.today().month >= 6 else 0)
BATCH = next(batch for batch in search.build_employer_search_batches() if batch.targets[0].canonical_name == "腾讯")


def candidate(**changes):
    value = {
        "company": "腾讯", "title": "软件开发工程师", "city": "深圳",
        "requirements": f"面向{YEAR}届，计算机相关专业，本科及以上。",
        "official_url": "https://careers.tencent.com/job/123",
        "opening_date": None, "closing_date": None,
    }
    value.update(changes)
    return value


def normalize(**changes):
    return search._normalize_job_with_reason(candidate(**changes), BATCH.pool, BATCH.targets[0])


def test_explicit_current_cohort_does_not_require_redundant_campus_word():
    result, reason = normalize()
    assert result and reason == "normalized"
    assert result["title"] == "软件开发工程师"
    assert result["opening_date"] is None
    assert result["closing_date"] is None
    assert "已验证" not in " ".join(result["tags"])


def test_short_cohort_and_branch_employer_are_recognized():
    result, reason = normalize(company="腾讯科技（深圳）有限公司", requirements=f"{str(YEAR)[-2:]}届，计算机相关专业")
    assert result and reason == "normalized"
    assert result["company"] == "腾讯科技（深圳）有限公司"


@pytest.mark.parametrize("changes, reason", [
    ({"company": ""}, "company_missing"),
    ({"company": "百度"}, "company_alias_mismatch"),
    ({"company": "腾讯以外的技术公司"}, "company_alias_mismatch"),
    ({"title": ""}, "title_missing"),
    ({"requirements": "要求三年以上开发经验"}, "not_campus"),
    ({"requirements": f"面向{YEAR-1}届"}, "wrong_or_missing_cohort"),
    ({"requirements": f"面向{YEAR+1}届"}, "wrong_or_missing_cohort"),
    ({"requirements": f"原公告为{YEAR-1}届校园招聘；{YEAR}届是否接收，待官方原文核对"}, "cohort_unconfirmed"),
    ({"requirements": f"原文为“{YEAR}届”或明确接收{YEAR}届时方可投递；以官方原文核对为准"}, "cohort_unconfirmed"),
    ({"requirements": f"如果接受{YEAR}届才能申请"}, "cohort_unconfirmed"),
    ({"requirements": f"不接受{YEAR}届"}, "cohort_unconfirmed"),
    ({"title": "软件开发工程师（社会招聘）"}, "explicit_non_campus"),
    ({"title": "Intern (IT) at the Head Office"}, "explicit_internship"),
    ({"title": "软件开发实习生"}, "explicit_internship"),
    ({"requirements": f"{YEAR}届以外，仅限社会招聘"}, "explicit_non_campus"),
    ({"requirements": f"仅招非应届毕业生，{YEAR}不接受应届"}, "explicit_non_campus"),
    ({"official_url": ""}, "official_url_missing"),
    ({"official_url": "http://careers.tencent.com/job/1"}, "official_url_not_https"),
    ({"official_url": "https://127.0.0.1/internal"}, "official_url_unsafe_or_discovery_host"),
    ({"official_url": "https://www.google.com/search?q=jobs"}, "official_url_unsafe_or_discovery_host"),
    ({"closing_date": date.today().isoformat()}, "deadline_passed"),
    ({"closing_date": (date.today()-timedelta(days=1)).isoformat()}, "deadline_passed"),
    ({"opening_date": (date.today()+timedelta(days=1)).isoformat()}, "not_open_yet"),
])
def test_reason_codes_preserve_known_bad_candidate_gates(changes, reason):
    result, actual = normalize(**changes)
    assert result is None
    assert actual == reason


def test_excluding_non_graduates_is_not_misread_as_excluding_graduates():
    result, reason = normalize(requirements=f"面向{YEAR}届，非应届请勿投递，不接受非应届人员。")
    assert result and reason == "normalized"


def test_real_campus_job_can_require_internship_experience():
    result, reason = normalize(requirements=f"面向{YEAR}届，有相关实习经历者优先。")
    assert result and reason == "normalized"


def test_invalid_unknown_dates_stay_null():
    result, reason = normalize(opening_date="待公布", closing_date="not a date")
    assert result and reason == "normalized"
    assert result["opening_date"] is None and result["closing_date"] is None


def test_uncited_candidate_remains_pending_without_network_or_verified_date(monkeypatch):
    monkeypatch.setattr(search, "_inspect_official_candidate_page", lambda _: pytest.fail("uncited URL must not acquire verification here"))
    job, _ = normalize(closing_date=(date.today()+timedelta(days=7)).isoformat())
    result, reason = search._inspect_normalized_search_candidate(job, BATCH.targets[0], cited=False)
    assert reason == "citation_unconfirmed"
    assert "搜索引用待确认" in result["tags"]
    assert "待官方核验" in result["tags"]
    assert "链接已验证" not in result["tags"]
    assert result["closing_date"] is None


def test_unreadable_js_page_is_pending_not_closed(monkeypatch):
    monkeypatch.setattr(search, "_inspect_official_candidate_page", lambda _: search.CandidatePageEvidence(readable=False, title_confirmed=False))
    job, _ = normalize()
    result, reason = search._inspect_normalized_search_candidate(job, BATCH.targets[0], cited=True)
    assert result and reason == "official_pending"
    assert "待官方核验" in result["tags"]


def test_official_closed_status_still_removes_candidate(monkeypatch):
    monkeypatch.setattr(search, "_inspect_official_candidate_page", lambda _: search.CandidatePageEvidence(readable=True, title_confirmed=False, closed=True))
    job, _ = normalize()
    result, reason = search._inspect_normalized_search_candidate(job, BATCH.targets[0], cited=True)
    assert result is None and reason == "official_page_closed"

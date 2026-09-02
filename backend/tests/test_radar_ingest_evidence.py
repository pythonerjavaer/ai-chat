"""Do not mistake undisclosed locations or ATS navigation for rejection evidence."""

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("JWT_SECRET", "test-secret-that-is-long-enough-for-tests")
os.environ.setdefault("FUTURE_RADAR_ENABLED", "false")
os.environ.setdefault("RECRUITMENT_WEB_SEARCH_ENABLED", "false")
os.environ.setdefault("RECRUITMENT_REFRESH_MINUTES", "0")

from backend import main, recruitment_search


def candidate(**changes):
    return {
        "company": "示例科技", "title": "2027 校园招聘数据分析岗", "city": "上海",
        "canonical_url": "https://careers.example.com/campus/data-analyst",
        "requirements": "面向应届毕业生。", "tags": ["校园招聘"],
        **changes,
    }


def mock_page(monkeypatch, text, **evidence):
    monkeypatch.setattr(main, "fetch_watch_page", lambda *_args, **_kwargs: SimpleNamespace(text=text))
    monkeypatch.setattr(main, "_evaluate_official_candidate_page", lambda *_args, **_kwargs: SimpleNamespace(
        **{
            "closed": False, "readable": True, "domain_confirmed": True,
            "employer_confirmed": True, "cohort_confirmed": True,
            "identity_confirmed": True, "open_confirmed": True,
            **evidence,
        }
    ))


@pytest.mark.parametrize("city", ["", "待具体岗位确认", "地点待公告确认", "待确认", "未知"])
def test_unknown_location_is_pending_not_rejected_or_falsely_verified(monkeypatch, city):
    mock_page(monkeypatch, "示例科技 2027 校园招聘数据分析岗 立即申请")
    status, reason, dates = main._verify_ingest_candidate(candidate(city=city))
    assert status == "pending"
    assert reason == "location_unconfirmed"
    assert dates == {"opening_date": None, "closing_date": None}


def test_exact_official_role_excerpt_can_confirm_location_missing_from_feed(monkeypatch):
    title = "2027 校园招聘数据分析岗"
    mock_page(
        monkeypatch,
        f"示例科技 {title} 工作地点上海 面向应届毕业生 立即申请",
    )

    status, reason, _ = main._verify_ingest_candidate(
        candidate(title=title, city="地点待公告确认")
    )

    assert (status, reason) == ("verified", None)


def test_unrelated_office_footer_does_not_confirm_missing_role_location(monkeypatch):
    title = "2027 校园招聘数据分析岗"
    mock_page(
        monkeypatch,
        f"示例科技 {title} 面向应届毕业生 立即申请 " + "岗位说明 " * 80 + "上海办公室",
    )

    status, reason, _ = main._verify_ingest_candidate(
        candidate(title=title, city="地点待公告确认")
    )

    assert (status, reason) == ("pending", "location_unconfirmed")


@pytest.mark.parametrize("city", ["新加坡", "Singapore", "伦敦", "London", "悉尼", "New York"])
def test_explicit_overseas_locations_remain_rejected(monkeypatch, city):
    def no_fetch(*_args, **_kwargs):
        raise AssertionError("Explicitly excluded location should not fetch")

    monkeypatch.setattr(main, "fetch_watch_page", no_fetch)
    status, reason, _ = main._verify_ingest_candidate(candidate(city=city))
    assert (status, reason) == ("rejected", "location_outside_scope")


@pytest.mark.parametrize("navigation", [
    "校园招聘 社会招聘 Experienced Hires 招聘入口",
    "首页 Experienced Professionals 关于我们 加载中",
    "社会招聘 校园招聘 请启用 JavaScript 查看岗位详情",
])
def test_mixed_ats_navigation_cannot_reject_a_campus_discovery(monkeypatch, navigation):
    mock_page(
        monkeypatch, navigation, cohort_confirmed=False, identity_confirmed=False,
    )
    status, reason, _ = main._verify_ingest_candidate(candidate())
    assert status == "pending"
    assert reason == "page_missing_current_cohort_evidence"


@pytest.mark.parametrize("restriction", [
    "仅限社会招聘", "本职位为社会招聘", "社招岗位", "Experienced hires only",
])
def test_explicit_role_level_social_restrictions_are_still_rejected(monkeypatch, restriction):
    mock_page(monkeypatch, f"示例科技 2027 校园招聘数据分析岗 {restriction}")
    status, reason, _ = main._verify_ingest_candidate(candidate())
    assert (status, reason) == ("rejected", "official_page_non_campus")


def test_unrelated_social_job_section_is_not_candidate_evidence(monkeypatch):
    page = "社招岗位 高级经理 " + "网站栏目 " * 100 + "2027 校园招聘数据分析岗 立即申请"
    mock_page(monkeypatch, page)
    assert main._verify_ingest_candidate(candidate())[0] == "verified"


def test_unknown_location_and_mixed_navigation_do_not_override_explicit_closed_state(monkeypatch):
    mock_page(monkeypatch, "校园招聘 社会招聘 该职位已下线", closed=True)
    status, reason, _ = main._verify_ingest_candidate(candidate(city="待具体岗位确认"))
    assert (status, reason) == ("closed", "official_page_closed")


@pytest.mark.parametrize("placeholder", [
    "{{jobsHeading}}", "{{ErrorMessageJobTitle}}", "{{vm.positionTitle}}",
])
def test_unrendered_ats_error_branch_is_not_a_closed_job(monkeypatch, placeholder):
    page_text = (
        f"示例科技 Careers {placeholder} Campus Graduate opportunities 2027 "
        "The job posting has expired or has already been filled. Apply now."
    )
    monkeypatch.setattr(main, "fetch_watch_page", lambda *_args, **_kwargs: SimpleNamespace(
        text=page_text, final_url="https://careers.example.com/job/123",
    ))
    job = candidate()
    evidence = recruitment_search._evaluate_official_candidate_page(
        {**job, "url": job["canonical_url"]}, page_text, job["canonical_url"],
    )
    assert evidence.readable is False
    assert evidence.closed is False
    assert evidence.open_confirmed is False
    assert evidence.title_confirmed is False
    status, reason, _ = main._verify_ingest_candidate(job)
    assert (status, reason) == ("pending", "official_page_unreadable")


@pytest.mark.parametrize("page_text", [
    "This job posting has expired and is no longer accepting applications.",
    "示例科技 2027 校园招聘数据分析岗 职位已关闭",
    "{{jobsHeading}} 示例科技 2027 校园招聘数据分析岗 职位已关闭",
])
def test_rendered_closed_notice_is_not_ignored_as_a_template(monkeypatch, page_text):
    monkeypatch.setattr(main, "fetch_watch_page", lambda *_args, **_kwargs: SimpleNamespace(
        text=page_text, final_url="https://careers.example.com/job/123",
    ))
    assert main._verify_ingest_candidate(candidate())[:2] == ("closed", "official_page_closed")

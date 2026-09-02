from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.future_radar import adapters
from backend.future_radar.adapters import HotjobCampusAdapter, adapter_for_source
from backend.future_radar.seeds import initial_sources
from backend.recruitment_watch import WatchFetchError


SUITE = "SU625527c30dcad4021443cdda"
POST_ID = "68aeb4cf0dcad47fa8493a31"


@pytest.fixture(autouse=True)
def _disable_test_rate_wait(monkeypatch):
    monkeypatch.setattr(adapters.DOMAIN_LIMITER, "wait", lambda *_args, **_kwargs: None)


def _source() -> dict:
    return {
        "id": "official-gf-securities-campus-2027",
        "name": "广发证券 2027 届校园招聘岗位（官方 ATS）",
        "platform": "official_ats",
        "company": "广发证券",
        "source_type": "official_api",
        "url": f"https://wecruit.hotjob.cn/{SUITE}/pb/school.html",
        "domain": "wecruit.hotjob.cn",
        "trust_level": "verification",
        "adapter_config": {
            "adapter": "official_api",
            "provider": "hotjob_campus",
            "suite_key": SUITE,
            "employer_aliases": ["广发证券股份有限公司"],
            "recruitment_year": 2027,
            "program_name": "广发证券 2027 届校园招聘",
            "max_pages": 5,
            "max_details": 20,
            "domain_delay_seconds": 0,
        },
    }


def test_hotjob_adapter_emits_only_current_cohort_verified_details(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def fake_request(suite_key, route, payload, **_kwargs):
        assert suite_key == SUITE
        calls.append((route, payload))
        if route == "config/get":
            return {"config": {"companyName": "广发证券股份有限公司"}}
        if route == "positionInfo/listPosition":
            return {
                "pageForm": {
                    "totalPage": 1,
                    "currentPage": 1,
                    "dataCount": 3,
                    "pageData": [
                        {
                            "postId": POST_ID,
                            "postName": "财富管理科技岗（2027届）",
                            "projectName": "广发证券2027届校园招聘",
                            "recruitType": 1,
                            "endDate": "2027-09-03 23:59:59",
                        },
                        {
                            "postId": "68aeb4cf0dcad47fa8493a32",
                            "postName": "日常招聘-研究助理",
                            "projectName": "校园招聘",
                            "recruitType": 1,
                        },
                        {
                            "postId": "68aeb4cf0dcad47fa8493a33",
                            "postName": "2026届校园招聘-投行岗",
                            "projectName": "2026届校园招聘",
                            "recruitType": 1,
                        },
                    ],
                }
            }
        assert route == "positionInfo/listPositionDetail"
        assert payload == {"postId": POST_ID}
        return {
            "postId": POST_ID,
            "postName": "财富管理科技岗（2027届）",
            "projectName": "广发证券2027届校园招聘",
            "subject": "面向2027届毕业生",
            "workContent": "负责财富管理平台的数据产品与智能化建设。",
            "serviceCondition": "具备金融与技术复合背景。",
            "department": "财富管理与经纪业务总部",
            "education": "硕士及以上",
            "company": "深圳分公司",
            "workPlaceStr": "深圳",
            "recruitType": 1,
            "endDate": "2027-09-03 23:59:59",
            "canDelivery": True,
            "showDeliverButton": 1,
        }

    monkeypatch.setattr(adapters, "_hotjob_public_request", fake_request)
    result = HotjobCampusAdapter().scan(_source())

    assert result.status == "healthy"
    assert result.snapshot_complete is True
    assert result.coverage == {
        "pages_scanned": 1,
        "rows_observed": 3,
        "target_year_rows": 1,
        "verified_jobs": 1,
        "program_only_rows": 0,
        "detail_failures": 0,
        "reported_total": 3,
    }
    assert [route for route, _payload in calls] == [
        "config/get",
        "positionInfo/listPosition",
        "positionInfo/listPositionDetail",
    ]
    assert len(result.programs) == 1
    assert len(result.jobs) == 1
    job = result.jobs[0]
    assert job["company"] == "广发证券深圳分公司"
    assert job["closing_date"] == "2027-09-03"
    assert job["opening_date"] is None
    assert job["verification_status"] == "verified"
    assert job["primary_category"] == "securities_public_funds_asset_management"
    assert job["official_url"] == (
        f"https://wecruit.hotjob.cn/{SUITE}/pb/posDetail.html?postId={POST_ID}"
    )
    assert "日常招聘" not in str(result.jobs)
    assert "2026届" not in str(result.jobs)


def test_hotjob_adapter_rejects_tenant_identity_mismatch(monkeypatch):
    monkeypatch.setattr(
        adapters,
        "_hotjob_public_request",
        lambda *_args, **_kwargs: {"config": {"companyName": "其他证券有限公司"}},
    )
    with pytest.raises(WatchFetchError, match="tenant identity"):
        HotjobCampusAdapter().scan(_source())


def test_hotjob_challenge_is_a_program_signal_not_a_job(monkeypatch):
    title = "AI & Code & Quant挑战赛—Quant赛道FICC方向（2027届）"

    def fake_request(_suite_key, route, _payload, **_kwargs):
        if route == "config/get":
            return {"config": {"companyName": "广发证券股份有限公司"}}
        row = {
            "postId": POST_ID,
            "postName": title,
            "projectName": "2027届-AI挑战赛",
            "recruitType": 1,
            "endDate": "2027-09-03 23:59:59",
            "canDelivery": True,
            "showDeliverButton": 1,
        }
        if route == "positionInfo/listPosition":
            return {"pageForm": {
                "totalPage": 1, "currentPage": 1, "dataCount": 1,
                "pageData": [row],
            }}
        return {**row, "company": "固定收益业务委员会", "subject": "面向2027届毕业生"}

    monkeypatch.setattr(adapters, "_hotjob_public_request", fake_request)
    result = HotjobCampusAdapter().scan(_source())
    assert result.jobs == []
    assert len(result.programs) == 1
    assert result.programs[0]["status"] == "open"
    assert result.coverage["program_only_rows"] == 1


def test_hotjob_delivery_flags_override_a_future_deadline(monkeypatch):
    title = "固定收益研究岗（2027届）"

    def fake_request(_suite_key, route, _payload, **_kwargs):
        if route == "config/get":
            return {"config": {"companyName": "广发证券股份有限公司"}}
        row = {
            "postId": POST_ID, "postName": title,
            "projectName": "广发证券2027届校园招聘", "recruitType": 1,
            "endDate": "2027-12-31 23:59:59",
        }
        if route == "positionInfo/listPosition":
            return {"pageForm": {
                "totalPage": 1, "currentPage": 1, "dataCount": 1,
                "pageData": [row],
            }}
        return {
            **row, "company": "固定收益业务委员会",
            "subject": "面向2027届毕业生；联系 test@example.com 或 13800138000",
            "workContent": "研究信用与利率市场。", "canDelivery": False,
            "showDeliverButton": 0,
        }

    monkeypatch.setattr(adapters, "_hotjob_public_request", fake_request)
    result = HotjobCampusAdapter().scan(_source())
    assert result.jobs[0]["status"] == "closed"
    assert result.programs[0]["status"] == "closed"
    assert "test@example.com" not in result.normalized_content
    assert "13800138000" not in result.normalized_content


def test_hotjob_repeated_page_ids_disable_complete_snapshot(monkeypatch):
    title = "投行项目执行岗（2027届）"
    row = {
        "postId": POST_ID, "postName": title,
        "projectName": "广发证券2027届校园招聘", "recruitType": 1,
    }

    def fake_request(_suite_key, route, _payload, **_kwargs):
        if route == "config/get":
            return {"config": {"companyName": "广发证券股份有限公司"}}
        if route == "positionInfo/listPosition":
            return {"pageForm": {
                "totalPage": 1, "currentPage": 1, "dataCount": 2,
                "pageData": [row, row],
            }}
        return {**row, "company": "投资银行管理委员会", "canDelivery": True}

    monkeypatch.setattr(adapters, "_hotjob_public_request", fake_request)
    result = HotjobCampusAdapter().scan(_source())
    assert result.snapshot_complete is False
    assert result.status == "partial"
    assert len(result.jobs) == 1


def test_hotjob_partial_details_cannot_close_the_program(monkeypatch):
    closed_id = POST_ID
    failed_id = "68aeb4cf0dcad47fa8493a32"
    rows = [
        {
            "postId": closed_id, "postName": "信用研究岗（2027届）",
            "projectName": "广发证券2027届校园招聘", "recruitType": 1,
        },
        {
            "postId": failed_id, "postName": "金融科技岗（2027届）",
            "projectName": "广发证券2027届校园招聘", "recruitType": 1,
        },
    ]

    def fake_request(_suite_key, route, payload, **_kwargs):
        if route == "config/get":
            return {"config": {"companyName": "广发证券股份有限公司"}}
        if route == "positionInfo/listPosition":
            return {"pageForm": {
                "totalPage": 1, "currentPage": 1, "dataCount": 2,
                "pageData": rows,
            }}
        if payload["postId"] == failed_id:
            raise WatchFetchError("temporary detail failure")
        return {
            **rows[0], "company": "固定收益业务委员会",
            "endDate": "2026-01-01 23:59:59", "canDelivery": False,
            "showDeliverButton": 0,
        }

    monkeypatch.setattr(adapters, "_hotjob_public_request", fake_request)
    result = HotjobCampusAdapter().scan(_source())
    assert result.snapshot_complete is False
    assert result.programs[0]["status"] == "unknown"
    assert result.programs[0]["closing_date"] is None


def test_hotjob_detail_semantics_change_content_hash(monkeypatch):
    responsibility = {"value": "研究信用市场。"}
    title = "固定收益研究岗（2027届）"
    row = {
        "postId": POST_ID, "postName": title,
        "projectName": "广发证券2027届校园招聘", "recruitType": 1,
    }

    def fake_request(_suite_key, route, _payload, **_kwargs):
        if route == "config/get":
            return {"config": {"companyName": "广发证券股份有限公司"}}
        if route == "positionInfo/listPosition":
            return {"pageForm": {
                "totalPage": 1, "currentPage": 1, "dataCount": 1,
                "pageData": [row],
            }}
        return {
            **row, "company": "固定收益业务委员会",
            "workContent": responsibility["value"], "canDelivery": True,
        }

    monkeypatch.setattr(adapters, "_hotjob_public_request", fake_request)
    first = HotjobCampusAdapter().scan(_source())
    responsibility["value"] = "研究信用市场与利率衍生品。"
    second = HotjobCampusAdapter().scan(_source())
    assert first.content_hash != second.content_hash


def test_gf_source_is_seeded_and_routes_to_deterministic_adapter():
    source = next(
        item
        for item in initial_sources(web_search_enabled=True)
        if item["id"] == "official-gf-securities-campus-2027"
    )
    assert source["trust_level"] == "verification"
    assert source["adapter_config"]["ai_extract"] is False
    selected = adapter_for_source(
        source,
        repository=SimpleNamespace(),
        openai_api_key="unused",
        ai_model="unused",
    )
    assert isinstance(selected, HotjobCampusAdapter)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2027-09-03 23:59:59", "2027-09-03"),
        ("2027年9月3日", "2027-09-03"),
        ("3000-01-01 00:00:00", None),
        ("", None),
    ],
)
def test_hotjob_public_date(value, expected):
    assert adapters._hotjob_public_date(value) == expected


def test_hotjob_cohort_and_hiring_entity_boundaries():
    assert adapters._hotjob_target_cohort({"postName": "2027届校园招聘研究岗"}, 2027)
    assert adapters._hotjob_target_cohort({"projectName": "2027年度校园招聘"}, 2027)
    assert not adapters._hotjob_target_cohort({"projectName": "2027年AI战略项目"}, 2027)
    assert adapters._hotjob_hiring_entity("广发证券", "另类投资") == "广发证券另类投资"
    assert adapters._hotjob_hiring_entity(
        "广发证券", "广发乾和投资有限公司"
    ) == "广发乾和投资有限公司"

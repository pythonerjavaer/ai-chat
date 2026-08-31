"""Deterministic operator-source regressions; no live HTTP, AI or user data."""

import hashlib
import json
import os
from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("OPENAI_API_KEY", "operator-test-unused")
os.environ.setdefault("JWT_SECRET", "operator-test-synthetic-secret")
os.environ.setdefault("FUTURE_RADAR_ENABLED", "false")
os.environ.setdefault("RECRUITMENT_REFRESH_MINUTES", "0")

from backend import database
from backend.future_radar import adapters
from backend.future_radar.adapters import (
    ChinaMobileNoticeAdapter, ChinaTelecomCampusAdapter, ChinaUnicomCampusAdapter,
    LegacyDiscoveryDatabaseAdapter, OfficialHtmlAdapter, _operator_role_is_current,
    _public_reference_url, _telecom_campus_rows, adapter_for_source,
)
from backend.future_radar.normalization import (
    canonical_telecom_operator, clean_text, normalize_job, stable_program_external_id,
)
from backend.future_radar.schema import OPERATOR_CATEGORY_MIGRATION, migrate
from backend.future_radar.seeds import VERIFIED_OFFICIAL_SOURCES
from backend.future_radar.service import FutureRadarService
from backend.live_sources import is_recruitment_program_listing
from backend.recruitment_watch import WatchFetchError, WatchFetchResult, normalize_html_text


def source(source_id):
    return deepcopy(next(item for item in VERIFIED_OFFICIAL_SOURCES if item["id"] == source_id))


def page(raw, *, url="https://official.example/campus"):
    text = normalize_html_text(raw)
    return WatchFetchResult(url, url, hashlib.sha256(text.encode()).hexdigest(),
                           ["校园招聘"] if "校园招聘" in text else [], len(raw.encode()), 200, text, raw)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(adapters.DOMAIN_LIMITER, "wait", lambda *args: None)
    monkeypatch.setattr(adapters, "fetch_watch_page", lambda *args, **kwargs: pytest.fail("unexpected HTTP"))
    monkeypatch.setattr(adapters, "_unicom_public_jobs_page", lambda *args: pytest.fail("unexpected ATS call"))


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "settings", SimpleNamespace(database_path=tmp_path / "operators.db"))
    database.init_db()
    result = FutureRadarService(connect=database.connect, openai_api_key="", ai_model="unused", web_search_enabled=False)
    result.seed_registry()
    return result


def test_real_operator_seeds_dispatch_and_keep_scheduler_intervals():
    expected = {
        "official-china-unicom-campus-2027": ChinaUnicomCampusAdapter,
        "official-china-telecom-campus-jobs-2027": ChinaTelecomCampusAdapter,
        "official-china-mobile-campus-notices": ChinaMobileNoticeAdapter,
    }
    for key, adapter_type in expected.items():
        item = source(key)
        assert item["interval_minutes"] == 60
        assert item["url"].startswith("https://")
        assert item["adapter_config"]["ai_extract"] is False
        assert isinstance(adapter_for_source(item, repository=None, openai_api_key="", ai_model=""), adapter_type)
    assert source("official-china-telecom-campus-2027")["interval_minutes"] == 180


@pytest.mark.parametrize("company,operator", [
    ("中国移动通信集团有限公司", "china_mobile"), ("中国移动广东", "china_mobile"),
    ("中国移动通信集团浙江有限公司", "china_mobile"), ("咪咕文化科技有限公司", "china_mobile"),
    ("中国电信云计算研究院", "china_telecom"), ("中国电信股份有限公司浙江分公司", "china_telecom"),
    ("天翼云科技有限公司青海分公司", "china_telecom"), ("中电信人工智能科技（北京）有限公司", "china_telecom"),
    ("中国联合网络通信集团有限公司", "china_unicom"), ("中国联通上海市分公司", "china_unicom"),
    ("联通数字科技有限公司", "china_unicom"),
])
def test_operator_identity_corrects_only_category_and_preserves_public_metadata(company, operator):
    assert canonical_telecom_operator(company) == operator
    item = {"company": company, "title": "2027校园招聘", "primary_category": "internet_tech",
            "employer_type": "历史标签", "industry": "旧行业", "organization_category": "existing",
            "industry_tags": ["custom"], "tags": ["原始公开标签"], "external_id": "stable-id",
            "official_url": "https://www.example.com/campus", "status": "unknown", "verification_status": "pending"}
    result = normalize_job(item)
    assert result["primary_category"] == "state_tech_telecom"
    for key in ("company", "title", "employer_type", "industry", "organization_category", "industry_tags", "tags", "external_id", "status", "verification_status"):
        assert result[key] == (clean_text(item[key]) if isinstance(item[key], str) else item[key])


@pytest.mark.parametrize("company", ["中国移动设备有限公司", "中国电信合作伙伴招聘", "腾讯", "中国联通广州招聘网", "天翼咨询有限公司", "联通数据外包商", "中国移动北京合作伙伴公司"])
def test_lookalikes_and_other_employers_keep_category(company):
    assert canonical_telecom_operator(company) is None
    assert normalize_job({"company": company, "title": "工程师", "primary_category": "internet_tech"})["primary_category"] == "internet_tech"


@pytest.mark.parametrize("title,details,current", [
    ("工程师", "面向2027届高校毕业生；有实习经验优先。", True),
    ("工程师", "2026届应届毕业生", False), ("2027实习生", "2027届", False),
    ("社会招聘工程师", "2027届", False), ("博士后", "2027年毕业", False),
    ("工程师", "仅招2025年毕业生。", False), ("工程师", "2025年毕业。", False),
    ("工程师", "毕业时间为2024年9月至2025年8月。", False),
    ("工程师", "毕业时间为2026年9月至2027年8月。", True),
    ("工程师", "公司成立于2025年。招应届毕业生。", True),
    ("工程师", "面向2027届高校毕业生。也接受2026年毕业的未就业人员。", True),
])
def test_current_cohort_is_row_specific(title, details, current):
    assert _operator_role_is_current(title, details, 2027) is current


def test_confirmed_telecom_program_enters_jobs_unranked_without_invented_role(monkeypatch, service):
    item = source("official-china-telecom-campus-2027")
    monkeypatch.setattr(adapters, "fetch_watch_page", lambda *a, **k: page("中国电信集团有限公司2027年度校园招聘。2026年8月24日起网申。"))
    result = OfficialHtmlAdapter(repository=service.repository, api_key="", ai_model="").scan(item)
    assert len(result.programs) == len(result.jobs) == 1
    job = result.jobs[0]
    assert job["program_external_id"] == stable_program_external_id(result.programs[0])
    assert is_recruitment_program_listing(job)
    assert job["opening_date"] == "2026-08-24" and job["closing_date"] is None
    assert "job_score" not in job and "tier_code" not in job
    run = service.repository.create_run(trigger_type="test", source_ids=[item["id"]])
    outcome = service.process_result(source=service.repository.get_source(item["id"]), result=result, run_id=run["id"])
    assert not outcome["errors"]
    with database.connect() as connection:
        stored = dict(connection.execute("SELECT * FROM radar_jobs WHERE external_id=?", (job["external_id"],)).fetchone())
    assert stored["program_id"]
    assert stored["primary_category"] == "state_tech_telecom"
    from backend.main import _public_radar_opportunity
    public = _public_radar_opportunity(stored, {})
    assert public["listing_kind"] == "recruitment_program"
    assert public["tier_code"] is None and public["tier_bucket"] == "UNRANKED"


def test_campaign_is_opt_in_and_must_confirm_configured_year(monkeypatch):
    item = source("official-china-telecom-campus-2027")
    item["adapter_config"]["required_markers"] = ["中国电信"]
    monkeypatch.setattr(adapters, "fetch_watch_page", lambda *a, **k: page("中国电信2026年度校园招聘"))
    adapter = OfficialHtmlAdapter(repository=None, api_key="", ai_model="")
    assert adapter.scan(item).jobs == []
    item["adapter_config"]["emit_program_listing"] = False
    monkeypatch.setattr(adapters, "fetch_watch_page", lambda *a, **k: page("中国电信2027年度校园招聘"))
    assert adapter.scan(item).jobs == []


def unicom_row(number, *, title="工程师", details="2027届，具有实习经验者优先。"):
    job_id = f"CC145093010J{40000000000+number}"
    return {"company": {"companyId": 105347, "campusOrgName": "中国联通上海市分公司"},
            "job": {"jobNumber": job_id, "title": title, "url": f"https://xiaoyuan.zhaopin.com/job/{job_id}",
                    "positionSourceType": 2, "cityName": "上海", "detail": details, "modifiedTime": 1787909107814},
            "staff": {"name": "private-staff-marker", "phone": "13800138000"}}


def unicom_campaign(monkeypatch):
    monkeypatch.setattr(adapters, "fetch_watch_page", lambda *a, **k: page("<title>中国联合网络通信有限公司2027校园招聘</title>"))


def test_unicom_reads_every_page_beyond_500_no_ai_or_publish_date_inference(monkeypatch):
    unicom_campaign(monkeypatch)
    calls = []
    def api(number, size, timeout):
        calls.append(number)
        assert size == 50 and 0 < timeout <= 20
        rows = [unicom_row(i) for i in range((number-1)*50, min(551, number*50))]
        return {"jobList": rows, "pageInfo": {"pageIndex": number, "totalPage": 12, "totalNum": 551}}
    monkeypatch.setattr(adapters, "_unicom_public_jobs_page", api)
    result = ChinaUnicomCampusAdapter().scan(source("official-china-unicom-campus-2027"))
    assert calls == list(range(1, 13))
    assert len(result.jobs) == 552 and result.snapshot_complete
    assert result.ai_calls == result.model_tokens_used == 0
    assert all(job["opening_date"] is None and job["closing_date"] is None for job in result.jobs)
    assert "private-staff-marker" not in json.dumps(result.jobs)
    assert "13800138000" not in json.dumps(result.jobs)


@pytest.mark.parametrize("failure", ["network", "repeated", "count_changed", "bad_identity", "page_budget"])
def test_unicom_partial_pages_never_close_history(monkeypatch, failure):
    unicom_campaign(monkeypatch)
    item = source("official-china-unicom-campus-2027")
    item["adapter_config"]["max_pages"] = 1 if failure == "page_budget" else 3
    def api(number, size, timeout):
        if number == 2 and failure == "network":
            raise WatchFetchError("HTTP 429")
        row = unicom_row(1 if failure == "repeated" else number)
        if number == 2 and failure == "bad_identity":
            row["job"]["url"] = "https://xiaoyuan.zhaopin.com.evil.example/job/CC145093010J40000000002"
        return {"jobList": [row], "pageInfo": {"pageIndex": number, "totalPage": 2,
                "totalNum": 3 if number == 2 and failure == "count_changed" else 2}}
    monkeypatch.setattr(adapters, "_unicom_public_jobs_page", api)
    result = ChinaUnicomCampusAdapter().scan(item)
    assert len(result.jobs) >= 2
    assert result.status == "partial" and result.snapshot_complete is False


def test_unicom_excludes_old_rows_even_under_current_campaign(monkeypatch):
    unicom_campaign(monkeypatch)
    rows = [unicom_row(1), unicom_row(2, details="2026届毕业生"), unicom_row(3, title="2027实习生"), unicom_row(4, details="2025年毕业的高校毕业生")]
    monkeypatch.setattr(adapters, "_unicom_public_jobs_page", lambda *a: {"jobList": rows, "pageInfo": {"pageIndex": 1, "totalPage": 1, "totalNum": 4}})
    result = ChinaUnicomCampusAdapter().scan(source("official-china-unicom-campus-2027"))
    assert len(result.jobs) == 2 and result.snapshot_complete
    assert json.loads(result.normalized_content)["skipped"] == 3


def telecom_card(job_id=137902, title="区域解决方案经理", project="2027年度秋季校园招聘", details="本科及以上应届毕业生。"):
    return f'''<li class="position_list-list-demo"><div onclick="toDetailPostUrl({job_id},1,1)">
    <div class="position_list-list-demo-title">{title}</div>
    <div class="position_list-first-row"><span>中国电信河北分公司</span><span>石家庄市</span></div></div>
    <div class="detailedInformation">招聘项目:<br>{project}</div>
    <div class="detailedInformation">职位要求:<br>{details}</div></li>'''


def telecom_page(number, last, cards):
    return page(f'<input name="currentPage" value="{number}"><input value="{str(last).lower()}" name="lastPage">'+cards)


def test_telecom_follows_real_pagination_and_uses_clean_public_detail_links(monkeypatch):
    calls = []
    def fetch(url, *args, **kwargs):
        number = int(parse_qs(urlsplit(url).query)["pc.currentPage"][0])
        calls.append(number)
        return telecom_page(number, number == 3, telecom_card(137900+number))
    monkeypatch.setattr(adapters, "fetch_watch_page", fetch)
    result = ChinaTelecomCampusAdapter().scan(source("official-china-telecom-campus-jobs-2027"))
    assert calls == [1, 2, 3] and len(result.jobs) == 3 and result.snapshot_complete
    assert all(job["company"] == "中国电信河北分公司" for job in result.jobs)
    for job in result.jobs:
        assert set(parse_qs(urlsplit(job["official_url"]).query)) == {"recruitType", "postIdsAry", "brandCode"}
        assert job["opening_date"] is None and job["closing_date"] is None


@pytest.mark.parametrize("same_page", [True, False])
def test_telecom_duplicate_ids_are_deduplicated_and_never_complete(monkeypatch, same_page):
    first = telecom_card(137901, title="原始岗位名称")
    repeated = telecom_card(137901, title="重复卡片中的不同名称")
    distinct = telecom_card(137902)
    cards = [first + repeated + distinct] if same_page else [first, repeated + distinct]
    calls = []

    def fetch(url, *args, **kwargs):
        number = int(parse_qs(urlsplit(url).query)["pc.currentPage"][0])
        calls.append(number)
        return telecom_page(number, number == len(cards), cards[number - 1])

    monkeypatch.setattr(adapters, "fetch_watch_page", fetch)
    item = source("official-china-telecom-campus-jobs-2027")
    result = ChinaTelecomCampusAdapter().scan(item)
    assert calls == list(range(1, len(cards) + 1))
    assert [job["external_id"] for job in result.jobs] == ["telecom-137901", "telecom-137902"]
    assert result.jobs[0]["title"] == "原始岗位名称"
    assert result.snapshot_complete is False and result.status == "partial"
    summary = json.loads(result.normalized_content)
    assert summary["observed"] == 3 and summary["skipped"] == 1
    assert "Duplicate" in summary["reason"]
    # The same response has a stable fingerprint and does not reorder IDs.
    replay = ChinaTelecomCampusAdapter().scan(item)
    assert replay.jobs == result.jobs and replay.content_hash == result.content_hash


@pytest.mark.parametrize("same_page", [True, False])
def test_telecom_duplicate_snapshot_does_not_retire_an_unseen_historical_job(monkeypatch, service, same_page):
    item = service.repository.get_source("official-china-telecom-campus-jobs-2027")
    adapter = ChinaTelecomCampusAdapter()
    monkeypatch.setattr(adapters, "fetch_watch_page", lambda *a, **k: telecom_page(1, True, telecom_card(137999)))
    initial = adapter.scan(item)
    run = service.repository.create_run(trigger_type="test", source_ids=[item["id"]])
    assert service.process_result(source=item, result=initial, run_id=run["id"])["new_jobs"] == 1
    with database.connect() as connection:
        old_job = dict(connection.execute("SELECT * FROM radar_jobs").fetchone())
        old_link = dict(connection.execute("SELECT * FROM job_sources").fetchone())
    first = telecom_card(137901)
    cards = [first + first] if same_page else [first, first]

    def fetch(url, *args, **kwargs):
        number = int(parse_qs(urlsplit(url).query)["pc.currentPage"][0])
        return telecom_page(number, number == len(cards), cards[number - 1])

    monkeypatch.setattr(adapters, "fetch_watch_page", fetch)
    for _ in range(3):
        result = adapter.scan(item)
        assert result.snapshot_complete is False
        run = service.repository.create_run(trigger_type="test", source_ids=[item["id"]])
        counts = service.process_result(source=item, result=result, run_id=run["id"])
        assert not counts["errors"] and counts["closed_jobs"] == 0
    with database.connect() as connection:
        assert dict(connection.execute("SELECT * FROM radar_jobs WHERE id=?", (old_job["id"],)).fetchone()) == old_job
        assert dict(connection.execute("SELECT * FROM job_sources WHERE job_id=?", (old_job["id"],)).fetchone()) == old_link
        assert connection.execute("SELECT COUNT(*) FROM radar_jobs").fetchone()[0] == 2


def test_telecom_malformed_card_is_not_a_complete_snapshot(monkeypatch):
    malformed = telecom_card(137903).replace("position_list-list-demo-title", "changed-title-markup")
    monkeypatch.setattr(adapters, "fetch_watch_page", lambda *a, **k: telecom_page(1, True, telecom_card()+malformed))
    result = ChinaTelecomCampusAdapter().scan(source("official-china-telecom-campus-jobs-2027"))
    assert len(result.jobs) == 1 and not result.snapshot_complete and result.status == "partial"
    assert json.loads(result.normalized_content)["parse_failures"] == 1


def test_telecom_old_cohort_is_excluded_without_hiding_parse_failures(monkeypatch):
    cards = telecom_card()+telecom_card(137903, project="2026年度秋季校园招聘")+telecom_card(137904, title="实习生")
    monkeypatch.setattr(adapters, "fetch_watch_page", lambda *a, **k: telecom_page(1, True, cards))
    result = ChinaTelecomCampusAdapter().scan(source("official-china-telecom-campus-jobs-2027"))
    summary = json.loads(result.normalized_content)
    assert len(result.jobs) == 1 and result.snapshot_complete
    assert summary["skipped"] == 2 and summary["parse_failures"] == 0


def test_telecom_unavailable_or_empty_non_final_page_is_not_success(monkeypatch):
    monkeypatch.setattr(adapters, "fetch_watch_page", lambda *a, **k: page("service unavailable"))
    with pytest.raises(WatchFetchError):
        ChinaTelecomCampusAdapter().scan(source("official-china-telecom-campus-jobs-2027"))
    monkeypatch.setattr(adapters, "fetch_watch_page", lambda *a, **k: telecom_page(1, False, ""))
    assert ChinaTelecomCampusAdapter().scan(source("official-china-telecom-campus-jobs-2027")).snapshot_complete is False
    monkeypatch.setattr(adapters, "fetch_watch_page", lambda *a, **k: telecom_page(1, True, "<div>layout changed</div>"))
    assert ChinaTelecomCampusAdapter().scan(source("official-china-telecom-campus-jobs-2027")).snapshot_complete is False


def test_mobile_real_notice_schema_excludes_old_social_and_internship(monkeypatch):
    notices = [{"text3": title, "detail_href": f"/personal/notice/index_detail_{i}.html", "text4": "2026-08-20"}
               for i, title in enumerate(["中国移动海南公司2026年社会招聘", "中国移动广西公司2026届校园招聘", "中国移动北京公司2027校园招聘实习生", "中国移动广东公司2027校园招聘"])]
    def fetch(url, *args, **kwargs):
        if url.endswith(".json"):
            return page(json.dumps({"cData": {"list": notices}}, ensure_ascii=False))
        return page("中国移动广东公司2027校园招聘")
    monkeypatch.setattr(adapters, "fetch_watch_page", fetch)
    adapter = ChinaMobileNoticeAdapter(OfficialHtmlAdapter(repository=None, api_key="", ai_model=""))
    result = adapter.scan(source("official-china-mobile-campus-notices"))
    assert len(result.jobs) == len(result.programs) == 1
    assert result.jobs[0]["opening_date"] is None
    assert result.status == "healthy" and result.snapshot_complete is False


def test_mobile_tls_errors_propagate_without_success_or_security_downgrade(monkeypatch):
    def fail(*args, **kwargs):
        raise WatchFetchError("TLS unavailable")
    monkeypatch.setattr(adapters, "fetch_watch_page", fail)
    with pytest.raises(WatchFetchError, match="TLS"):
        ChinaMobileNoticeAdapter(OfficialHtmlAdapter(repository=None, api_key="", ai_model="")).scan(source("official-china-mobile-campus-notices"))


def test_public_numeric_job_id_does_not_weaken_url_privacy():
    good = "https://xiaoyuan.zhaopin.com/job/CC145093010J13800138000"
    assert _public_reference_url(good) == good
    assert _public_reference_url(good+"?phone=13800138000") is None
    assert _public_reference_url(good.replace("zhaopin.com", "zhaopin.com.evil.example")) is None
    assert _public_reference_url(good+"?access_token=secret") is None
    assert _public_reference_url(good.replace("https://", "https://user:pass@")) is None


def test_operator_migration_is_one_time_bounded_and_preserves_history(service):
    with database.connect() as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version=?", (OPERATOR_CATEGORY_MIGRATION,))
        for identifier, company in [("operator", "中国电信股份有限公司浙江分公司"), ("neighbor", "中国电信合作伙伴招聘")]:
            connection.execute("""INSERT INTO radar_jobs(id,external_id,company,title,primary_category,status,verification_status,content_hash,first_seen_at,last_seen_at,last_changed_at,created_at,updated_at,opening_date,closing_date,tags)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (identifier,identifier,company,"2027校园招聘","internet_tech","unknown","pending","old-hash","old1","old2","old3","old4","old5","2026-08-20",None,'["原始来源标签"]'))
        before = {row["id"]: dict(row) for row in connection.execute("SELECT * FROM radar_jobs")}
        migrate(connection)
        after = {row["id"]: dict(row) for row in connection.execute("SELECT * FROM radar_jobs")}
        assert after["neighbor"] == before["neighbor"]
        changed = {key for key in before["operator"] if before["operator"][key] != after["operator"][key]}
        assert changed == {"primary_category", "content_hash"}
        assert after["operator"]["primary_category"] == "state_tech_telecom"
        migrate(connection)
        assert after == {row["id"]: dict(row) for row in connection.execute("SELECT * FROM radar_jobs")}
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=?", (OPERATOR_CATEGORY_MIGRATION,)).fetchone()[0] == 1


def test_sixth_chatgpt_source_keeps_same_identity_before_and_after_promotion(service):
    external_id = "sixth-source-public-job"
    company = "中国电信"
    digest = hashlib.sha256(f"external:{company}:{external_id}".encode()).hexdigest()
    expected_id = f"monitor-{digest[:24]}"
    with database.connect() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(recruitment_ingest_candidates)")}
        values = {"id": "synthetic-ingest", "dedupe_key": "f"*64, "external_id": external_id,
                  "source_key": "chatgpt-radar-06", "source_id": "chatgpt-radar-06", "company": company,
                  "employer_type": "", "city": "", "source": "公开线索", "payload_hash": "synthetic-hash", "title": "2027校园招聘",
                  "official_url": "https://www.chinatelecom.com.cn/ct/zp/168256.html",
                  "canonical_url": "https://www.chinatelecom.com.cn/ct/zp/168256.html",
                  "verification_status": "pending", "incoming_status": "open",
                  "first_seen_at": "2026-08-31T00:00:00Z", "last_seen_at": "2026-08-31T00:00:00Z",
                  "created_at": "2026-08-31T00:00:00Z", "updated_at": "2026-08-31T00:00:00Z"}
        values = {key: value for key, value in values.items() if key in columns}
        connection.execute(f"INSERT INTO recruitment_ingest_candidates ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", tuple(values.values()))
    adapter = LegacyDiscoveryDatabaseAdapter()
    before = adapter.scan({})
    assert len(before.jobs) == 1 and before.jobs[0]["external_id"] == expected_id
    database.upsert_recruitment_jobs([{
        "id": expected_id, "company": company, "employer_type": "", "title": "2027校园招聘",
        "city": "", "industry": "", "url": values["official_url"], "source": adapters.WEB_SEARCH_SOURCE,
        "status": "open", "tags": [], "requirements": "",
    }])
    with database.connect() as connection:
        connection.execute("UPDATE recruitment_ingest_candidates SET promoted_job_id=? WHERE id=?", (expected_id, "synthetic-ingest"))
    after = adapter.scan({})
    assert len(after.jobs) == 1 and after.jobs[0]["external_id"] == expected_id
    assert before.content_hash == after.content_hash

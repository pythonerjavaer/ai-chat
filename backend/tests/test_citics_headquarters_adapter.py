from __future__ import annotations

from types import SimpleNamespace
import urllib.parse

import pytest

from backend.future_radar import adapters
from backend.future_radar.adapters import (
    CiticsHeadquartersCampusAdapter,
    adapter_for_source,
)
from backend.future_radar.seeds import initial_sources
from backend.recruitment import score_job
from backend.recruitment_watch import WatchFetchError


def _source() -> dict:
    return next(
        item
        for item in initial_sources(web_search_enabled=True)
        if item["id"] == "official-citics-headquarters-campus-2027"
    )


def _rows() -> list[dict]:
    return [
        {
            "positionNo": "5527",
            "batchId": 63,
            "deptName": "固定收益",
            "positionName": "金融科技",
            "workplace": "深圳/上海/北京",
            "qualification": "硕士研究生及以上学历，金融与理工复合背景优先。",
            "positionDesc": (
                "岗位分为4个方向：FICC销售、投资交易、研究服务、金融科技 "
                "<b>1.FICC销售</b>：维护客户并开展产品销售。 "
                "<b>2.投资交易</b>：执行交易并研究策略。 "
                "<b>3.研究服务</b>：开展信用研究。 "
                "<b>4.金融科技</b>：负责系统、大数据与模型开发。"
            ),
        },
        {
            "positionNo": "5545",
            "batchId": 63,
            "deptName": "托管业务",
            "positionName": "运营支持",
            "workplace": "深圳/上海/北京/广州",
            "qualification": "硕士研究生及以上学历。",
            "positionDesc": (
                "岗位分为2个方向：系统开发、运营支持 "
                "<b>1.系统开发</b>：负责托管系统研发。 "
                "<b>2.运营支持</b>：中信中证投资服务有限责任公司岗位，"
                "协助基金交易数据检查、会计核算与报表处理。"
            ),
        },
        {
            "positionNo": "5531",
            "batchId": 63,
            "deptName": "资产管理",
            "positionName": "内控运营",
            "workplace": "深圳/上海/北京",
            "qualification": "硕士研究生及以上学历。",
            "positionDesc": (
                "岗位分为4个方向：资管销售、研究助理、交易助理、内控运营 "
                "<b>1.资管销售</b>：开发客户。 "
                "<b>2.研究助理</b>：研究行业与个股。 "
                "<b>3.交易助理</b>：协助交易。 "
                "<b>4.内控运营</b>：负责合规、流程与运营管理。"
            ),
        },
    ]


@pytest.fixture(autouse=True)
def _disable_rate_wait(monkeypatch):
    monkeypatch.setattr(adapters.DOMAIN_LIMITER, "wait", lambda *_args, **_kwargs: None)


def test_citics_adapter_emits_verified_headquarters_jobs_without_cross_direction_pollution(
    monkeypatch,
):
    monkeypatch.setattr(
        adapters,
        "_citics_public_positions",
        lambda page, size, timeout: {
            "errorCode": 0,
            "errorMsg": "成功",
            "count": 3,
            "positionList": _rows(),
        },
    )
    result = CiticsHeadquartersCampusAdapter().scan(_source())

    assert result.status == "healthy"
    assert result.snapshot_complete is True
    assert len(result.programs) == 1
    assert len(result.jobs) == 3
    assert result.coverage == {
        "pages_scanned": 1,
        "rows_observed": 3,
        "verified_jobs": 3,
    }
    fintech, subsidiary_operations, operations = result.jobs
    assert fintech["company"] == "中信证券总部"
    assert fintech["title"] == "固定收益｜金融科技"
    assert "系统、大数据与模型开发" in fintech["responsibilities"]
    assert "FICC销售" not in fintech["responsibilities"]
    assert "投资交易" not in fintech["responsibilities"]
    assert operations["title"] == "资产管理｜内控运营"
    assert "合规、流程与运营管理" in operations["responsibilities"]
    assert "研究行业" not in operations["responsibilities"]
    assert subsidiary_operations["company"] == "中信中证投资服务有限责任公司"
    assert "系统开发" not in subsidiary_operations["responsibilities"]
    subsidiary_score = score_job(subsidiary_operations, {})
    assert subsidiary_score["organization_assessment"]["level"] == "subsidiary"
    assert subsidiary_score["tier_code"] != "T0.5"
    assert all(job["official_url"].startswith("https://careers.citics.com/") for job in result.jobs)


def test_citics_adapter_rejects_bad_source_identity_and_batch(monkeypatch):
    bad_source = {**_source(), "company": "其他证券"}
    with pytest.raises(WatchFetchError, match="employer"):
        CiticsHeadquartersCampusAdapter().scan(bad_source)

    rows = _rows()
    rows[0] = {**rows[0], "batchId": 62}
    monkeypatch.setattr(
        adapters,
        "_citics_public_positions",
        lambda *_args, **_kwargs: {"errorCode": 0, "count": 3, "positionList": rows},
    )
    with pytest.raises(WatchFetchError, match="identity"):
        CiticsHeadquartersCampusAdapter().scan(_source())


def test_citics_public_request_has_fixed_route_and_no_browser_credentials(monkeypatch):
    captured = {}

    class Headers:
        @staticmethod
        def get_content_type():
            return "application/json"

    class Response:
        headers = Headers()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def geturl():
            return adapters._CITICS_API_ENDPOINT

        @staticmethod
        def read(_limit):
            return b'{"errorCode":0,"errorMsg":"ok","count":0,"positionList":[]}'

    class Opener:
        def open(self, request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return Response()

    def fake_build_opener(handler):
        assert isinstance(handler, adapters._NoPublicApiRedirect)
        return Opener()

    monkeypatch.setattr(adapters.urllib.request, "build_opener", fake_build_opener)
    monkeypatch.setattr(adapters, "validate_public_https_url", lambda value: value)
    result = adapters._citics_public_positions(1, 50, 7.0)
    assert result["count"] == 0
    request = captured["request"]
    assert request.full_url == adapters._CITICS_API_ENDPOINT
    assert captured["timeout"] == 7.0
    headers = {key.casefold(): value for key, value in request.header_items()}
    assert "cookie" not in headers
    assert "authorization" not in headers
    payload = dict(urllib.parse.parse_qsl(request.data.decode("utf-8")))
    assert payload == {
        "sysNo": "CSE001",
        "recruitType": "08",
        "deptype": "Headquarter",
        "batchId": "63",
        "practice": "0",
        "pageSize": "50",
        "pageNo": "1",
    }


def test_citics_source_is_seeded_and_registered():
    source = _source()
    assert source["trust_level"] == "verification"
    assert source["adapter_config"]["ai_extract"] is False
    assert source["adapter_config"]["provider"] == "citics_headquarters_campus"
    selected = adapter_for_source(
        source,
        repository=SimpleNamespace(),
        openai_api_key="unused",
        ai_model="unused",
    )
    assert isinstance(selected, CiticsHeadquartersCampusAdapter)


def test_citics_redirect_handler_never_forwards_request():
    with pytest.raises(WatchFetchError, match="redirected"):
        adapters._NoPublicApiRedirect().redirect_request(
            None, None, 302, "Found", {}, "https://attacker.invalid/collect",
        )

"""Structured-only public recruitment bodies never relax transport safety."""

import json
from email.message import Message
from io import BytesIO
from datetime import date

import pytest

from backend import recruitment_watch as watch


URL = "https://careers.example.com/jobs/graduate-analyst"


def structured_html(*, title="Graduate Analyst 2027", description=None):
    return '<script type="application/ld+json">' + json.dumps({
        "@context": "https://schema.org", "@graph": [{
            "@type": "JobPosting", "title": title,
            "description": description if description is not None else "2027 campus graduate analyst. Responsibilities: analyse public business data.",
        }],
    }).replace("</", "<\\/") + "</script>"


class Response(BytesIO):
    status = 200

    def __init__(self, raw, content_type="text/html"):
        super().__init__(raw.encode())
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.headers["Content-Length"] = str(len(raw.encode()))

    def geturl(self):
        return URL


def fetch(monkeypatch, raw, *, content_type="text/html", **kwargs):
    validations = []

    def validate(url, *, resolve_dns=True):
        validations.append((url, resolve_dns))
        return url

    class Opener:
        def open(self, request, *, timeout):
            assert request.full_url == URL
            assert timeout == 2
            return Response(raw, content_type)

    monkeypatch.setattr(watch, "validate_public_https_url", validate)
    result = watch.fetch_watch_page(
        URL, ("campus",), timeout_seconds=2,
        opener_factory=lambda *_handlers: Opener(), **kwargs,
    )
    assert validations == [(URL, True), (URL, True)]
    return result


def test_existing_watch_behavior_still_rejects_a_textless_body(monkeypatch):
    with pytest.raises(watch.WatchFetchError, match="没有可比较"):
        fetch(monkeypatch, structured_html())


def test_opt_in_returns_raw_jobposting_without_manufacturing_visible_text(monkeypatch):
    raw = structured_html()
    result = fetch(monkeypatch, raw, allow_structured_body=True)
    assert result.raw_text == raw and result.text == ""
    assert result.keyword_hits == []
    assert result.fingerprint


def test_structured_only_fingerprint_tracks_jd_change_not_unrelated_scripts(monkeypatch):
    first = fetch(monkeypatch, structured_html(), allow_structured_body=True)
    second = fetch(monkeypatch, structured_html() + '<script>window.nonce="different";</script>', allow_structured_body=True)
    changed = fetch(monkeypatch, structured_html(title="Graduate Analyst 2028"), allow_structured_body=True)
    assert first.fingerprint == second.fingerprint
    assert first.fingerprint != changed.fingerprint


@pytest.mark.parametrize("raw", [
    "<script>window.jobTitle = 'Graduate Analyst';</script>",
    '<script type="application/ld+json">not valid json</script>',
    '<script type="application/ld+json">{"@type":"Organization","name":"Employer"}</script>',
    structured_html(title=""),
    structured_html(description=""),
    structured_html(description="<script>hidden code only</script>"),
])
def test_opt_in_still_rejects_non_jobposting_empty_and_malformed_data(monkeypatch, raw):
    with pytest.raises(watch.WatchFetchError, match="没有可比较"):
        fetch(monkeypatch, raw, allow_structured_body=True)


def test_opt_in_does_not_override_size_or_content_type_limits(monkeypatch):
    with pytest.raises(watch.WatchFetchError, match="超过允许"):
        fetch(monkeypatch, structured_html(), allow_structured_body=True, max_bytes=30)
    with pytest.raises(watch.WatchFetchError, match="没有返回可读取"):
        fetch(monkeypatch, structured_html(), content_type="application/octet-stream", allow_structured_body=True)


def test_opt_in_does_not_override_url_security_checks():
    with pytest.raises(watch.WatchFetchError):
        watch.fetch_watch_page("https://127.0.0.1/private", (), allow_structured_body=True)


@pytest.mark.parametrize("discover_links", [None, False, True])
def test_official_adapter_initial_read_only_opts_in_for_link_discovery(monkeypatch, discover_links):
    from backend.future_radar import adapters

    year = date.today().year + (1 if date.today().month >= 6 else 0)
    title = f"Graduate Analyst {year}"
    raw = structured_html(
        title=title,
        description=f"{year} campus graduates analyse public business data and deliver client research.",
    )
    received = []

    def transport(url, keywords, **kwargs):
        assert url == URL
        assert kwargs["timeout_seconds"] == 2
        received.append(kwargs.get("allow_structured_body"))
        # Exercise the real reader's empty-body gate with an in-memory HTTP
        # response, not a stub that would accept structured content regardless.
        return fetch(monkeypatch, raw, allow_structured_body=kwargs.get("allow_structured_body", False))

    monkeypatch.setattr(adapters, "fetch_watch_page", transport)
    monkeypatch.setattr(adapters.DOMAIN_LIMITER, "wait", lambda *_args: None)
    monkeypatch.setattr(adapters.time, "sleep", lambda _seconds: None)
    config = {
        "recruitment_year": year, "timeout_seconds": 2, "ai_extract": False,
        "required_markers": [title], "job_marker": title, "job_title": title,
    }
    if discover_links is not None:
        config["discover_job_links"] = discover_links
    source = {"id": "structured-first-read", "company": "Example", "url": URL, "adapter_config": config}
    adapter = adapters.OfficialHtmlAdapter(repository=None, api_key="unused", ai_model="unused")
    if not discover_links:
        with pytest.raises(RuntimeError, match="没有可比较"):
            adapter.scan(source)
        assert received == [None, None, None]
        return
    result = adapter.scan(source)
    assert received == [True]  # initial page is reused, not fetched twice
    assert not result.programs  # no manufactured visible campus/marker evidence
    assert len(result.jobs) == 1 and result.jobs[0]["title"] == title
    assert result.jobs[0]["verification_status"] == "pending"  # unknown domain/open state
    assert "官网列表逐页发现" in result.jobs[0]["tags"]
    assert result.snapshot_complete is False

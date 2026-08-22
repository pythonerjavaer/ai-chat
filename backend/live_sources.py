import hashlib
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser

from .config import settings


PUBLIC_RECRUITMENT_SOURCES = [
    {"name": "国资小新", "url": "https://www.gdpdd.com/s/xiaoxin/index.html", "employer_type": "央国企"},
    {"name": "银行招聘网", "url": "https://yhks.cn/", "employer_type": "银行/金融"},
]


class _RecruitmentLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self.href = ""
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.href = dict(attrs).get("href", "")
            self.text = []

    def handle_data(self, data):
        if self.href:
            self.text.append(data.strip())

    def handle_endtag(self, tag):
        if tag == "a" and self.href:
            title = " ".join(part for part in self.text if part).strip()
            if title:
                self.links.append((title, self.href))
            self.href = ""
            self.text = []


def fetch_public_recruitment_sources() -> list[dict]:
    jobs: list[dict] = []
    keywords = ("校园招聘", "秋招", "校招", "招聘公告", "招聘")
    for source in PUBLIC_RECRUITMENT_SOURCES:
        try:
            request = urllib.request.Request(
                source["url"], headers={"User-Agent": "FrostFire-Autumn-Radar/1.0"}
            )
            with urllib.request.urlopen(request, timeout=12) as response:
                html = response.read().decode("utf-8", errors="ignore")
            parser = _RecruitmentLinkParser()
            parser.feed(html)
            for index, (title, href) in enumerate(parser.links):
                if not any(keyword in title for keyword in keywords):
                    continue
                url = urllib.parse.urljoin(source["url"], href)
                jobs.append({
                    "id": f"public-{source['name']}-{hashlib.sha1(url.encode()).hexdigest()[:16]}",
                    "company": title.split("202")[0].strip(" ·-") or source["name"],
                    "employer_type": source["employer_type"],
                    "title": title[:180], "city": "待查看公告", "industry": "",
                    "url": url, "source": source["name"], "opening_date": None,
                    "closing_date": None, "requirements": "请打开原文核对专业、毕业年份、截止日期和投递入口。",
                    "tags": [source["employer_type"], "公开来源"],
                    "historical_applicants": None, "historical_offers": None,
                    "last_verified_at": datetime.now(timezone.utc).isoformat(), "status": "open",
                })
                if index > 80:
                    break
        except Exception:
            continue
    return jobs


def fetch_adzuna_jobs(query: str = "graduate", location: str = "") -> list[dict]:
    """Fetch live jobs only when the owner configured official Adzuna credentials."""
    if not settings.adzuna_app_id or not settings.adzuna_app_key:
        return []
    params = urllib.parse.urlencode({
        "app_id": settings.adzuna_app_id,
        "app_key": settings.adzuna_app_key,
        "results_per_page": 30,
        "what": query,
        "where": location,
        "sort_by": "date",
    })
    url = f"https://api.adzuna.com/v1/api/jobs/{settings.adzuna_country}/search/1?{params}"
    request = urllib.request.Request(url, headers={"User-Agent": "FrostFire-Autumn-Radar/1.0"})
    with urllib.request.urlopen(request, timeout=12) as response:
        payload = json.loads(response.read().decode("utf-8"))
    jobs = []
    for item in payload.get("results", []):
        jobs.append({
            "id": f"adzuna-{item.get('id')}",
            "company": (item.get("company") or {}).get("display_name", "未知公司"),
            "employer_type": "公开岗位源",
            "title": item.get("title", "未命名岗位"),
            "city": (item.get("location") or {}).get("display_name", ""),
            "industry": "",
            "url": item.get("redirect_url", ""),
            "source": "Adzuna API",
            "opening_date": item.get("created", "")[:10] or None,
            "closing_date": None,
            "requirements": item.get("description", "")[:500],
            "tags": [],
            "historical_applicants": None,
            "historical_offers": None,
            "last_verified_at": datetime.now(timezone.utc).isoformat(),
            "status": "open",
        })
    return jobs

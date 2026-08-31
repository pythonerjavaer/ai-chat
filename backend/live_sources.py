import hashlib
import json
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from .config import settings
from .recruitment_directory import PERSONAL_MONITOR_POOLS
from .recruitment_watch import WatchFetchError, fetch_watch_page, normalize_public_https_urls


PUBLIC_RECRUITMENT_SOURCES = [
    {
        "name": "国聘网",
        "url": "https://job.iguopin.com/jobList?campus_nature=&channel=campus&key_word=&nature_cn=",
        "employer_type": "央国企",
    },
    {"name": "国资小新", "url": "https://www.gdpdd.com/s/xiaoxin/index.html", "employer_type": "央国企"},
    {"name": "银行招聘网", "url": "https://yhks.cn/", "employer_type": "银行/金融"},
]
MAX_PUBLIC_SOURCE_BYTES = 1_500_000
MAX_ADZUNA_BYTES = 2_000_000

RADAR_BOOTSTRAP_PATH = Path(__file__).with_name("radar_bootstrap_jobs.json")


def _load_radar_bootstrap_jobs() -> list[dict]:
    """Load only public, server-verified fields from the last five-source snapshot.

    Render Free uses an ephemeral SQLite database.  The snapshot lets a cold
    instance re-check official pages and rebuild its verified pool without
    storing private ChatGPT conversation IDs or blindly trusting stale rows.
    """
    try:
        payload = json.loads(RADAR_BOOTSTRAP_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    return [dict(item) for item in payload if isinstance(item, dict)]


CURATED_CAMPUS_JOBS = _load_radar_bootstrap_jobs()

# Navigation/category links are not job postings.  They often look like
# "江门招聘" or "校园招聘" and must never be shown as an actionable vacancy.
GENERIC_RECRUITMENT_TITLE = re.compile(
    r"^(?:20\d{2}年?|\d{4}届)?[\u4e00-\u9fffA-Za-z·\-]{0,14}?(?:校园招聘|秋招|校招|招聘公告|招聘信息|招聘)$"
)

_GENERIC_EMPLOYER_NAMES = {
    "校园招聘", "社会招聘", "秋招", "春招", "央国企", "央企", "国企", "外企",
    "银行", "券商", "基金", "互联网", "互联网企业", "快消", "招聘", "招聘公告",
    "招聘信息", "招聘单位", "公司", "企业", "其他企业", "其它企业", "未知", "待确认",
    "国聘网", "国资小新", "银行招聘网", "高校就业网", "就业信息网",
}
_SPECIFIC_ROLE_TITLE = re.compile(
    r"工程师|研究员|分析师|分析岗|研发岗|产品岗|运营岗|风控岗|合规岗|审计岗|财务岗|"
    r"会计岗|数据岗|产品经理|客户经理|顾问|\b(?:engineer|analyst|researcher|consultant|developer)\b",
    re.IGNORECASE,
)


def _has_specific_recruitment_employer(job: dict) -> bool:
    """Distinguish an employer name from a city/category/feed heading.

    This is structural validation, not a claim that the employer or offer has
    been officially verified. Unknown but specific employer names can be leads.
    """
    company = re.sub(r"\s+", "", str(job.get("company") or ""))
    return bool(
        len(company) >= 2
        and company not in _GENERIC_EMPLOYER_NAMES
        and company not in CORE_LOCATION_MARKERS
        and not GENERIC_RECRUITMENT_TITLE.fullmatch(company)
        and not re.fullmatch(r"(?:待|尚未|暂未)?(?:明确|确认|公布|披露|识别)(?:企业|单位|公司)?", company)
    )


def _has_public_recruitment_reference(job: dict) -> bool:
    for field in ("application_url", "canonical_url", "official_url", "url"):
        value = job.get(field)
        if not value:
            continue
        try:
            url, _ = normalize_public_https_urls(str(value), resolve_dns=False)
            host = (urllib.parse.urlsplit(url).hostname or "").casefold()
            if host == "chatgpt.com" or host.endswith(".chatgpt.com"):
                continue
            return True
        except (WatchFetchError, ValueError, TypeError):
            continue
    return False


def is_recruitment_program_listing(job: dict) -> bool:
    """An identifiable employer's current campus campaign is a usable lead.

    It remains a recruitment *program*, not an invented specific vacancy.
    Navigation alone, an absent employer, or an unsafe URL never qualifies.
    """
    if not _has_specific_recruitment_employer(job) or not _has_public_recruitment_reference(job):
        return False
    title = re.sub(r"\s+", "", str(job.get("title") or ""))
    text = " ".join((
        title, str(job.get("requirements") or ""),
        " ".join(str(value) for value in job.get("tags", [])),
    )).casefold()
    target_year = date.today().year + (1 if date.today().month >= 6 else 0)
    if not re.search(rf"(?<!\d){target_year}(?!\d)", text):
        return False
    if re.search(r"社会招聘|社招岗位|experienced\s*hires?", title, re.I):
        return False
    campus = any(word in text for word in (
        "校园招聘", "校招", "秋招", "春招", "应届", "毕业生", "campus", "graduate",
    ))
    trainee_program = bool(re.search(r"管培(?:生)?|管理培训生|management\s*trainee", title, re.I))
    if not campus and not trainee_program:
        return False
    if _SPECIFIC_ROLE_TITLE.search(title):
        return False
    campaign_title = re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", title.casefold())
    company = re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", str(job.get("company") or "").casefold())
    if company and campaign_title.startswith(company):
        campaign_title = campaign_title[len(company):]
    campaign_title = re.sub(r"20\d{2}[年届]?", "", campaign_title)
    campaign_title = re.sub(r"(?:项目|公告|正式启动|启动|开启|简章)$", "", campaign_title)
    return bool(
        GENERIC_RECRUITMENT_TITLE.fullmatch(campaign_title)
        or trainee_program
        or re.fullmatch(r"(?:global)?(?:graduate|campus)(?:program|recruitment|hiring)", campaign_title, re.I)
    )


def is_actionable_recruitment_listing(job: dict) -> bool:
    title = re.sub(r"\s+", "", str(job.get("title", "")))
    if not title or not _has_specific_recruitment_employer(job):
        return False
    city = str(job.get("city", ""))
    if "招聘" in city:
        return False
    if is_recruitment_program_listing(job):
        return True
    if GENERIC_RECRUITMENT_TITLE.fullmatch(title) and not (
        _SPECIFIC_ROLE_TITLE.search(title) and _has_public_recruitment_reference(job)
    ):
        return False
    campus_text = " ".join(
        [title, str(job.get("requirements", "")), " ".join(job.get("tags", []))]
    ).lower()
    return any(marker in campus_text for marker in ("校园招聘", "秋招", "校招", "应届", "毕业生", "届", "graduate", "campus"))


def _extract_deadline(url: str) -> str | None:
    """Return an explicitly labelled application deadline from a public notice."""
    try:
        result = fetch_watch_page(url, (), timeout_seconds=6)
        text = re.sub(r"\s+", "", result.text)
        match = re.search(
            r"(?:报名|网申|投递|申请).{0,24}?(?:截止|截至|截止时间).{0,12}?((20\d{2})年)?(\d{1,2})月(\d{1,2})日",
            text,
        )
        if not match:
            return None
        year = int(match.group(2) or date.today().year)
        month, day = int(match.group(3)), int(match.group(4))
        return date(year, month, day).isoformat()
    except Exception:
        return None


# Configured monitoring scopes. These are search scopes, not claims that every
# employer currently has an open role.

# Only high-priority destination cities are shown.  An announcement whose
# location is not disclosed is not promoted into the pool unless it is clearly
# a national/head-office role.
CORE_LOCATION_MARKERS = {
    "北京", "上海", "广州", "深圳", "天津", "重庆", "杭州", "南京", "武汉", "成都", "西安",
    "郑州", "长沙", "合肥", "济南", "福州", "厦门", "南昌", "石家庄", "太原", "沈阳", "大连",
    "长春", "哈尔滨", "海口", "昆明", "贵阳", "南宁", "兰州", "西宁", "银川", "乌鲁木齐",
    "拉萨", "呼和浩特", "香港", "澳门", "全国", "总部", "远程",
}
PRIORITY_EMPLOYERS = {
    employer.lower()
    for pool in PERSONAL_MONITOR_POOLS
    for employer in pool["employers"]
}


def is_priority_campus_listing(job: dict) -> bool:
    if not is_actionable_recruitment_listing(job):
        return False
    title = str(job.get("title", "")).lower()
    company = str(job.get("company", "")).lower()
    if not any(employer in f"{title} {company}" for employer in PRIORITY_EMPLOYERS):
        return False
    location_text = f"{job.get('city', '')} {job.get('title', '')}"
    return any(marker in location_text for marker in CORE_LOCATION_MARKERS)


def is_priority_public_source_lead(job: dict) -> bool:
    """Allow high-signal public leads even when the index omits city.

    Public aggregators often expose the employer and campus title first, while
    the detailed city and closing date live on the linked announcement.  Keep
    such cards visible but label them ``待打开核对``; generic category links and
    lower-priority city pages still fail the employer/title checks.
    """
    if not is_actionable_recruitment_listing(job):
        return False
    title = str(job.get("title", "")).lower()
    company = str(job.get("company", "")).lower()
    if not any(employer in f"{title} {company}" for employer in PRIORITY_EMPLOYERS):
        return False
    city = str(job.get("city", ""))
    if city in {"", "地点待公告确认"}:
        return True
    return any(marker in f"{city} {title}" for marker in CORE_LOCATION_MARKERS)


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
    keywords = ("校园招聘", "秋招", "校招", "应届", "毕业生", "届")
    for source in PUBLIC_RECRUITMENT_SOURCES:
        try:
            request = urllib.request.Request(
                source["url"], headers={"User-Agent": "FrostFire-Autumn-Radar/1.0"}
            )
            with urllib.request.urlopen(request, timeout=12) as response:
                payload = response.read(MAX_PUBLIC_SOURCE_BYTES + 1)
                if len(payload) > MAX_PUBLIC_SOURCE_BYTES:
                    raise ValueError("Recruitment source response is too large.")
                html = payload.decode("utf-8", errors="ignore")
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
                    "title": title[:180], "city": "地点待公告确认", "industry": "",
                    "url": url, "source": source["name"], "opening_date": None,
                    "closing_date": None, "requirements": "请打开原文核对专业、毕业年份、截止日期和投递入口。",
                    "tags": [source["employer_type"], "公开来源", "待打开核对"],
                    "historical_applicants": None, "historical_offers": None,
                    "last_verified_at": datetime.now(timezone.utc).isoformat(), "status": "open",
                })
                if not is_priority_public_source_lead(jobs[-1]):
                    jobs.pop()
                    continue
                if index > 80:
                    break
        except Exception:
            continue
    # Alerts are created only from dates explicitly labelled as application
    # deadlines on the original public notice, never from publish dates.
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(_extract_deadline, job["url"]): job for job in jobs[:30]}
        for future, job in futures.items():
            job["closing_date"] = future.result()
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
        raw = response.read(MAX_ADZUNA_BYTES + 1)
        if len(raw) > MAX_ADZUNA_BYTES:
            raise ValueError("Adzuna response is too large.")
        payload = json.loads(raw.decode("utf-8"))
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

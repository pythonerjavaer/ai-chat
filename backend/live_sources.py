import hashlib
import json
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from datetime import datetime, timezone
from html.parser import HTMLParser

from .config import settings


PUBLIC_RECRUITMENT_SOURCES = [
    {"name": "国资小新", "url": "https://www.gdpdd.com/s/xiaoxin/index.html", "employer_type": "央国企"},
    {"name": "银行招聘网", "url": "https://yhks.cn/", "employer_type": "银行/金融"},
]

# Navigation/category links are not job postings.  They often look like
# "江门招聘" or "校园招聘" and must never be shown as an actionable vacancy.
GENERIC_RECRUITMENT_TITLE = re.compile(
    r"^(?:20\d{2}年?|\d{4}届)?[\u4e00-\u9fffA-Za-z·\-]{0,14}?(?:校园招聘|秋招|校招|招聘公告|招聘信息|招聘)$"
)


def is_actionable_recruitment_listing(job: dict) -> bool:
    title = re.sub(r"\s+", "", str(job.get("title", "")))
    if not title or GENERIC_RECRUITMENT_TITLE.fullmatch(title):
        return False
    city = str(job.get("city", ""))
    if "招聘" in city:
        return False
    campus_text = " ".join(
        [title, str(job.get("requirements", "")), " ".join(job.get("tags", []))]
    ).lower()
    return any(marker in campus_text for marker in ("校园招聘", "秋招", "校招", "应届", "毕业生", "届", "graduate", "campus"))


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data):
        self.parts.append(data)


def _extract_deadline(url: str) -> str | None:
    """Return an explicitly labelled application deadline from a public notice."""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "FrostFire-Autumn-Radar/1.0"})
        with urllib.request.urlopen(request, timeout=6) as response:
            html = response.read().decode("utf-8", errors="ignore")
        parser = _TextParser()
        parser.feed(html)
        text = re.sub(r"\s+", "", " ".join(parser.parts))
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


# Personal monitoring scope migrated from the owner's existing ChatGPT autumn
# recruiting monitors. These are search scopes, not claims that every employer
# currently has an open role.
PERSONAL_MONITOR_POOLS = [
    {
        "id": "state_owned_full",
        "name": "央国企全量秋招",
        "focus": "正式秋招、提前批、预招聘、留学生专场、补录；总部、子公司、研究院与直属机构",
        "employers": [
            "中国人民银行", "国家开发银行", "中国进出口银行", "中国农业发展银行",
            "工商银行", "农业银行", "中国银行", "建设银行", "交通银行", "邮储银行",
            "国家能源集团", "国家电网", "中国石油", "中国石化", "中国海油",
            "中国移动", "中国电信", "中国联通", "中国航天科技", "中国航天科工",
            "中国电科", "中国东方资产", "中储粮", "中国一汽", "航空工业",
            "中国铁塔", "国机集团", "中国宝武", "中国商飞", "中国信通院",
            "中国投资有限责任公司", "中国保利", "中国盐业",
        ],
    },
    {
        "id": "tech_finance_global",
        "name": "大厂·金融·外企",
        "focus": "FinTech、Data、AI Application、Product、Risk、Quant Analytics、Strategy、管培",
        "employers": [
            "腾讯", "阿里巴巴", "字节跳动", "百度", "拼多多", "蚂蚁集团",
            "中信证券", "中金公司", "华泰证券", "国泰海通", "中信建投", "招商证券",
            "广发证券", "申万宏源", "银河证券", "光大证券", "平安证券", "华金证券",
            "易方达", "华夏基金", "嘉实基金", "南方基金", "汇添富", "富国基金",
            "幻方", "九坤", "明汯", "衍复", "灵均", "宽德", "高瓴", "红杉中国",
            "J.P. Morgan", "Goldman Sachs", "Morgan Stanley", "UBS", "Citi", "HSBC",
            "Standard Chartered", "Macquarie", "BlackRock", "KPMG", "Deloitte", "PwC",
            "EY", "Accenture", "Microsoft", "Google", "Amazon/AWS", "Apple", "NVIDIA",
        ],
    },
    {
        "id": "policy_banks_pingan",
        "name": "政策行与平安专项",
        "focus": "截止日期、滚动筛选、英语门槛、笔面试安排、总行/总部与金融科技岗位",
        "employers": [
            "国家开发银行", "中国进出口银行", "中国农业发展银行", "中国平安",
            "平安银行", "平安产险", "平安理财", "平安养老险", "平安科技",
            "金融壹账通", "陆金所控股", "平安银行信用卡中心", "平安银行汽车消费金融中心",
        ],
    },
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
    keywords = ("校园招聘", "秋招", "校招", "应届", "毕业生", "届")
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
                    "title": title[:180], "city": "地点待公告确认", "industry": "",
                    "url": url, "source": source["name"], "opening_date": None,
                    "closing_date": None, "requirements": "请打开原文核对专业、毕业年份、截止日期和投递入口。",
                    "tags": [source["employer_type"], "公开来源"],
                    "historical_applicants": None, "historical_offers": None,
                    "last_verified_at": datetime.now(timezone.utc).isoformat(), "status": "open",
                })
                if not is_actionable_recruitment_listing(jobs[-1]):
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

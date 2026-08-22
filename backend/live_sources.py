import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .config import settings


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

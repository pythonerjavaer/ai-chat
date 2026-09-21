# Wechat Recruitment Intelligence

This module is a metadata-only recruitment intelligence connector inside Future Radar.
**Wechat sources are treated as discovery leads rather than verified job sources.**

## Implemented scope

- Five initial accounts: 国央校招、国聘、国资小新、国央求职网、银行招聘网.
- Authenticated single/batch public URL import (maximum 50 per request), deterministic title classification, persistent metadata, deduplication, cached successful results, per-item failures and filtered/paginated review.
- The existing Future Radar UI has a lazy-loaded “公众号标题” tab. Import/refresh only runs when requested. Selecting the tab reads saved metadata, not WeChat pages.
- No OpenAI, Anthropic, Gemini, paid search, paid WeChat API, ChatGPT UI or Codex UI runtime dependency. No new model SDK, browser automation, login, cookies, proxy rotation, CAPTCHA bypass, or private WeChat API.
- Runtime model token usage for this connector is zero. Existing paid chat/embedding/search functions remain separate and optional. Ordinary server/database hosting costs are not claimed to be free.

## Architecture

`backend/future_radar/wechat/` separates `normalizer`, `fetcher`, `parser`, `classifier`, `connector`, discovery providers, persistence, orchestration and routes. The connector uses Pydantic models and `httpx.AsyncClient`. Routes reuse the existing login/privacy consent and admin dependencies.

The existing `monitor_sources` accounts and `source_articles` documents are extended by an additive migration, called from the normal SQLite/PostgreSQL migration. Old sources/articles are not converted, deleted or used to manufacture titles. New accounts stay disabled in legacy Radar scans; separate `title_radar_enabled` controls eligibility for the new monitor function. Old paid/body-reading WeChat adapters remain paused.

`source_articles` adds platform, normalized URL, fetch status/error, score, keyword JSON, source detection and fingerprint. A unique `(platform, normalized_url)` index enforces document identity. The existing excerpt stays empty for these documents. `wechat_article_url_aliases` remembers validated redirects so the submitted URL also hits cache. A separate small `recruitment_title_leads` table is necessary because existing candidates/jobs require a specific company and job; a headline does not establish either. It links one unverified lead per relevant/possible document and exposes metadata for a future official discovery service. It never writes `radar_jobs` or marks a job verified.

Network requests occur outside short database transactions. Up to five distinct URLs are fetched concurrently within each batch; repeated equivalent URLs share a result. Each item commits independently. Successful imports are cached unless `force_refresh=true`. Failed attempts can be retried. Later failures do not erase a previously known title/date or page-detected publisher. URL deduplication is authoritative; identical normalized publisher+title fingerprints flag related articles without discarding alternate public URLs/provenance.

## Public HTTP and parsing limits

Only credential-free HTTPS public article URLs on `mp.weixin.qq.com` are allowed: `/s/<token>` or the long `/s?__biz=...&mid=...&idx=...&sn=...` identity. Tracking parameters are removed; `idx` distinguishes articles. Every redirect is allowlisted and checked for public DNS before requesting. Non-HTML, timeouts, HTTP errors, blocked pages and missing metadata are explicit outcomes; no bypass is attempted.

HTTP cannot request “only metadata” from WeChat as a dedicated official endpoint. The implementation streams a bounded HTML prefix (maximum 192 KB), stops at the article-content container and closes the response. A transport chunk can overlap the boundary; that suffix is immediately discarded. It does not traverse/download the full article, parse content, save body text, summaries, images, comments or scripts. If usable metadata is not available within that prefix, parsing fails explicitly. This is not a guarantee that no content bytes ever cross the network in a chunk.

Title fallback: Open Graph/Twitter metadata → known public title elements → HTML title. Publisher: account-specific metadata → account DOM → explicitly configured expected source (marked `configured`), otherwise null. Page names are marked `page`; no query word is treated as proof of publisher identity. Date parsing accepts reliable zoned publication metadata or a literal known publication timestamp; missing/unzoned/ambiguous dates stay null. Publication time and discovery time are separate.

## Discovery status: provider pending

**自动搜索发现目前没有可靠的零成本 provider，因此暂时使用 manual discovery。**

`ManualDiscoveryProvider` accepts supplied public article URLs. `SearchDiscoveryProvider` is a replaceable abstraction; `build_discovery_queries(source)` constructs queries that are persisted with each account. No paid or unreliable provider is enabled.

Investigation found that public search results may expose titles/account names but omit direct article URLs, require access verification during link resolution, and return old publications. That is insufficient for a reliable zero-cost new-article monitor; we do not implement anti-bot workarounds. The five seed URLs are individual historical examples, not account feeds or access to full history.

`run_wechat_monitor(service, provider)` is scheduler-independent. Without a configured lawful provider it returns `provider_pending` and makes no requests. No background task, recurring Codex automation, Cron Job, Redis or Celery is installed. An existing scheduler can call it later after a provider is implemented and authorized. Render Free sleeping means in-process scheduling is not guaranteed; a paid Render Cron would also violate the zero-cost default, so none is provisioned.

## HTTP API

Prefix: `/api/sources/wechat`. Read endpoints require the existing bearer token; article imports additionally require the current privacy consent. Account configuration uses the existing `X-Admin-Token` policy for shared sources.

| Method | Path | Action |
|---|---|---|
| GET | `/api/sources/wechat` | Five-account watchlist, stored query strings, recent counts, provider status |
| POST | `/api/sources/wechat` | Admin adds/updates an account: `source_name`, optional `seed_url`, `enabled` |
| POST | `/api/sources/wechat/article` | Import one `url`, optional `expected_source_name` and `force_refresh` |
| POST | `/api/sources/wechat/articles/import` | Import `urls` (1–50), same optional fields; returns total/success/new/duplicate/failed/items |
| GET | `/api/sources/wechat/articles` | `source_name`, `relevance_status`, `from_date`, `to_date`, `page`, `page_size` |
| GET | `/api/sources/wechat/articles/review` | Paginated `possible` title list for occasional manual review |
| POST | `/api/sources/wechat/monitor` | Current explicit `provider_pending` result; does not claim to scan accounts |

Example single request JSON:

```json
{"url":"https://mp.weixin.qq.com/s/uXDQ9RBPBcIRAjZV7S2jgg","expected_source_name":"国聘"}
```

Example batch JSON:

```json
{"urls":["https://mp.weixin.qq.com/s/uXDQ9RBPBcIRAjZV7S2jgg","https://mp.weixin.qq.com/s/fKmsPl5K33HLDk-kUKOZEw"],"force_refresh":false}
```

Date filters apply to known publication dates (inclusive), so undated articles appear with no date filter, not under invented dates. `new_articles` is successful metadata first discovered in the last seven days; it is not proof that the publisher posted something recently. Failed documents are retained with diagnostics for retry and never generate a new lead. Batch per-item failures do not roll back successful items. Raw internal exceptions/secrets are not returned.

## Rule scoring and verification

The classifier uses weighted positive families, negative weights and a 2027+campus combination bonus. Overlapping phrases in a family take the largest score. `relevant` is ≥65, `possible` ≥25 and lower scores are `irrelevant`. Domain terms alone do not prove recruitment. Social/internal/labor-dispatch hiring is downweighted. Score and matched words are stored and shown; there is no hidden model call.

A relevant/possible title creates only an `unverified` lead. Later official company website/ATS/announcement matching must establish the employer, program, dates and concrete roles before a verified job can exist. That official discovery workflow is deliberately not newly automated in this change.

## Startup, tests and current limits

No `OPENAI_API_KEY`, WeChat AppID/AppSecret, search key or model installation is needed for this module. Existing JWT and database configuration requirements still apply. If a paid chat/embedding action is requested without a model provider, it reports unavailable; local features stay usable.

```sh
python -m pytest backend/tests/test_wechat_connector.py backend/tests/test_wechat_title_service.py backend/tests/test_optional_model_provider.py
cd frontend
npm test
npm run build
```

Network tests use local fixtures and mocked HTTP; CI never needs live WeChat. TestClient exercises auth and normal startup without paid credentials. SQLite tests exercise the same additive schema and storage API used by the PostgreSQL bridge; actual live Supabase acceptance must be reported separately from mock/local testing. Access-restricted seed URLs may still return a truthful blocked/failed status. This feature is not a fully automated or guaranteed-complete WeChat monitoring service.

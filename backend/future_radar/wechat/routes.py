"""Authenticated metadata-only endpoints; existing shared-source admin policy."""
from datetime import date
from typing import Callable, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .normalizer import validate_wechat_article_url
from .discovery.base import WechatDiscoveryProvider
from .service import DiscoveryCooldown, WechatTitleService, run_wechat_monitor


class ArticleImport(BaseModel):
    model_config = ConfigDict(extra='forbid')
    url: str = Field(min_length=1, max_length=2048)
    expected_source_name: str | None = Field(default=None, min_length=1, max_length=100)
    force_refresh: bool = False


class BatchImport(BaseModel):
    model_config = ConfigDict(extra='forbid')
    urls: list[str] = Field(min_length=1, max_length=50)
    expected_source_name: str | None = Field(default=None, min_length=1, max_length=100)
    force_refresh: bool = False

    @field_validator('urls')
    @classmethod
    def bounded_urls(cls, value: list[str]) -> list[str]:
        if any(not url.strip() or len(url) > 2048 for url in value):
            raise ValueError('每条 URL 需为 1–2048 个字符。')
        return value


class SourceRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_name: str = Field(min_length=1, max_length=100)
    seed_url: str | None = Field(default=None, max_length=2048)
    enabled: bool = True

    @field_validator('source_name')
    @classmethod
    def nonblank_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError('公众号名称不能为空。')
        return value.strip()

    @field_validator('seed_url')
    @classmethod
    def safe_seed(cls, value: str | None) -> str | None:
        return validate_wechat_article_url(value) if value else None


def create_wechat_router(service: WechatTitleService, *, current_user: Callable,
                         consented_user: Callable, admin_auth: Callable,
                         discovery_provider: WechatDiscoveryProvider | None = None) -> APIRouter:
    router = APIRouter(prefix='/api/sources/wechat', tags=['wechat-title-intelligence'])

    @router.get('')
    def sources(user=Depends(current_user)) -> dict:
        provider_name = getattr(discovery_provider, 'name', 'sogou_wechat')
        provider_state = service.repository.get_provider_state(provider_name)
        return {'items': service.repository.list_sources(), 'discovery_status': provider_state['status'],
                'provider_state': provider_state,
                'notice': '公开搜索发现为 Beta；不可用时仍可手动导入文章 URL。', 'ai_calls': 0}

    @router.post('')
    def add_source(payload: SourceRequest, auth=Depends(admin_auth)) -> dict:
        return service.repository.add_source(payload.source_name, payload.seed_url, payload.enabled)

    @router.post('/article')
    async def article(payload: ArticleImport, user=Depends(consented_user)) -> dict:
        return await service.import_article(**payload.model_dump())

    @router.post('/articles/import')
    async def articles_import(payload: BatchImport, user=Depends(consented_user)) -> dict:
        return await service.import_batch(**payload.model_dump())

    @router.post('/articles/import-watchlist')
    async def import_watchlist(force_refresh: bool = False, user=Depends(consented_user)) -> dict:
        return await service.import_watchlist_seeds(force_refresh=force_refresh)

    @router.get('/articles')
    def articles(user=Depends(current_user), page: int = Query(1, ge=1),
                 page_size: int = Query(30, ge=1, le=100), source_name: str | None = None,
                 relevance_status: Literal['relevant','possible','irrelevant'] | None = None,
                 from_date: date | None = None, to_date: date | None = None) -> dict:
        if from_date and to_date and from_date > to_date:
            raise HTTPException(422, '起始日期不能晚于结束日期。')
        return service.repository.list_articles(
            page=page, page_size=page_size, source_name=source_name, relevance_status=relevance_status,
            from_date=from_date.isoformat() if from_date else None,
            to_date=to_date.isoformat() if to_date else None,
        )

    @router.get('/articles/review')
    def review(user=Depends(current_user), page: int = Query(1, ge=1),
               page_size: int = Query(30, ge=1, le=100)) -> dict:
        return service.repository.list_articles(page=page, page_size=page_size, relevance_status='possible')

    @router.post('/monitor')
    async def monitor(user=Depends(consented_user)) -> dict:
        return await run_wechat_monitor(service)

    @router.post('/discover')
    async def discover(user=Depends(consented_user)) -> dict:
        if discovery_provider is None:
            raise HTTPException(503, '当前部署未配置公众号公开搜索 Provider。')
        try:
            return await service.discover_now(discovery_provider)
        except DiscoveryCooldown as exc:
            raise HTTPException(
                429, '公开搜索刚刚运行过，请在冷却结束后再试。',
                headers={'Retry-After': str(exc.retry_after)},
            ) from None

    return router

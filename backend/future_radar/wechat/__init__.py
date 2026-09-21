"""WeChat recruitment intelligence: metadata and rules, no paid runtime API."""

from .classifier import RuleBasedTitleClassifier, classify_recruitment_title
from .connector import WechatArticleConnector
from .models import DiscoveredArticle, TitleClassification, WechatArticleMetadata
from .normalizer import metadata_fingerprint, normalize_wechat_url, validate_wechat_article_url
from .parser import parse_wechat_article_metadata

__all__ = [
    "DiscoveredArticle", "TitleClassification", "WechatArticleMetadata",
    "RuleBasedTitleClassifier", "WechatArticleConnector", "classify_recruitment_title",
    "metadata_fingerprint", "normalize_wechat_url", "validate_wechat_article_url",
    "parse_wechat_article_metadata",
]

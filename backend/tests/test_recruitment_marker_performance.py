"""Pure matching equivalence and avoided-work checks; no timing/network/DB."""

import ast
from copy import deepcopy
import inspect
import re

import pytest

from backend import recruitment as ranking


def legacy_marker_matches(text, marker):
    """The pre-optimization matcher, independent of its new early return."""
    normalized = marker.casefold().strip()
    if not normalized:
        return False
    if re.fullmatch(r"[a-z0-9_+.# -]+", normalized):
        escaped = re.escape(normalized).replace(r"\ ", r"\s+")
        return re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", text) is not None
    return normalized in text


def static_markers():
    """Cover module vocabularies and literal markers local to scoring helpers.

    The superset includes dictionary keys as well as values: adding a static
    vocabulary or an inline phrase must automatically extend this regression.
    """
    markers = set()

    def collect(value):
        if isinstance(value, str):
            markers.add(value)
        elif isinstance(value, dict):
            for key, item in value.items():
                collect(key)
                collect(item)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                collect(item)

    for name, value in vars(ranking).items():
        if name.isupper():
            collect(value)
    for node in ast.walk(ast.parse(inspect.getsource(ranking))):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {
            "_marker_matches", "_contains_any",
        }:
            for argument in node.args[1:]:
                for child in ast.walk(argument):
                    if isinstance(child, ast.Constant) and isinstance(child.value, str):
                        markers.add(child.value)
    return sorted(markers)


@pytest.fixture(autouse=True)
def isolated_vocabulary_caches():
    ranking._marker_rule.cache_clear()
    ranking._category_codes_for_text.cache_clear()
    yield
    ranking._marker_rule.cache_clear()
    ranking._category_codes_for_text.cache_clear()


def test_all_static_markers_match_legacy_across_text_variations():
    markers = static_markers()
    assert len(markers) > 150
    for marker in markers:
        normalized = marker.casefold().strip()
        variants = {
            "", marker, normalized, marker.upper(), marker.title(),
            f" {normalized} ", f"({normalized})", f"a{normalized}",
            f"{normalized}z", f"2{normalized}", f"{normalized}7",
            f"_{normalized}_", f"中{normalized}文", f"α{normalized}β",
            f"é{normalized}ß", f"９{normalized}９", f"-{normalized}+",
            f".{normalized}#", f"🙂{normalized}🚀", f"\n{normalized}\t",
            "unrelated generic operations 文本", "空字符串旁的岗位",
        }
        if " " in normalized:
            variants.update({
                normalized.replace(" ", "   "),
                normalized.replace(" ", "\t"),
                normalized.replace(" ", "\n"),
                normalized.replace(" ", "\t \n"),
                normalized.replace(" ", "\u00a0"),
                normalized.replace(" ", ""),
                normalized.replace(" ", "-"),
            })
        for text in variants:
            assert ranking._marker_matches(text, marker) == legacy_marker_matches(text, marker), (
                marker, text,
            )


@pytest.mark.parametrize(("marker", "text", "expected"), [
    ("", "ai", False), ("  ", "anything", False),
    ("AI", "ai", True), ("AI", "AI", False), (" ai ", "ai", True),
    ("ai", "paid operations", False), ("ai", "ai2", False),
    ("ai", "2ai", False), ("ai", "_ai_", True),
    ("ai", "αaiβ", True), ("ai", "中ai文", True),
    ("ai", "９ai９", True), ("ai", "aiz", False),
    ("c++", "c++ developer", True), ("c++", "xc++", False),
    ("c++", "c++17", False), ("c#", "(c#)", True),
    (".net", "x.net", False), (".net", "(.net)", True),
    ("j.p. morgan", "j.p.\tmorgan", True),
    ("data science", "data\nscience", True),
    ("data science", "data\t \nscience", True),
    ("data science", "data\u00a0science", True),
    ("data science", "datascience", False),
    ("data science", "data-science", False),
    ("data science", "bigdata science", False),
    ("Straße", "strasse", True), ("Straße", "straße", False),
    ("İ", "i\u0307", True), ("İ", "i", False), ("K", "k", True),
    ("量化", "金融量化研究", True), ("人工智能", "人工 智能", False),
])
def test_explicit_boundary_unicode_case_and_symbol_semantics(marker, text, expected):
    assert legacy_marker_matches(text, marker) is expected
    assert ranking._marker_matches(text, marker) is expected


def test_absent_single_token_skips_regex_but_present_token_checks_boundaries(monkeypatch):
    normalized, compiled = ranking._marker_rule("ai")
    calls = []

    class CountingPattern:
        def search(self, text):
            calls.append(text)
            return compiled.search(text)

    monkeypatch.setattr(ranking, "_marker_rule", lambda marker: (normalized, CountingPattern()))
    assert ranking._marker_matches("普通岗位职责与任职要求" * 1000, "ai") is False
    assert calls == []
    assert ranking._marker_matches("paid operations", "ai") is False
    assert ranking._marker_matches("ai researcher", "ai") is True
    assert calls == ["paid operations", "ai researcher"]


def test_multiword_marker_keeps_original_regex_even_without_literal_space(monkeypatch):
    normalized, compiled = ranking._marker_rule("data science")
    calls = []

    class CountingPattern:
        def search(self, text):
            calls.append(text)
            return compiled.search(text)

    monkeypatch.setattr(ranking, "_marker_rule", lambda marker: (normalized, CountingPattern()))
    assert ranking._marker_matches("data\nscience", "data science") is True
    assert ranking._marker_matches("unrelated", "data science") is False
    assert calls == ["data\nscience", "unrelated"]


def test_complete_scores_and_profile_matches_equal_legacy_implementation(monkeypatch):
    common = {
        "company": "合成测试机构", "city": "上海", "employer_type": "科技企业",
        "industry": "人工智能/云计算", "tags": ["2027届", "校园招聘"],
    }
    jobs = [
        {**common, "title": "AI & Data Science Graduate", "responsibilities": "负责 data\nscience 与人工智能模型研究。",
         "requirements": "熟悉 Python、C++、SQL 及 machine learning。"},
        {**common, "title": "Paid operations support", "responsibilities": "负责行政支持与 routine operations。",
         "requirements": "完成客户服务流程；不是 ai2 或 highrisk 模型研究岗位。"},
        {**common, "title": "风险治理与模型研究", "responsibilities": "参与 model risk 与 risk management。",
         "requirements": "研究量化风控与 data\tscience 方向。"},
    ]
    profiles = [{}, {"desired_roles": ["人工智能", "数据分析"], "cities": ["上海"]}]
    optimized = [
        (ranking.score_job(deepcopy(job), profile), ranking.job_matches_profile(job, profile))
        for profile in profiles for job in jobs
    ]
    monkeypatch.setattr(ranking, "_marker_matches", legacy_marker_matches)
    ranking._category_codes_for_text.cache_clear()
    baseline = [
        (ranking.score_job(deepcopy(job), profile), ranking.job_matches_profile(job, profile))
        for profile in profiles for job in jobs
    ]
    assert optimized == baseline

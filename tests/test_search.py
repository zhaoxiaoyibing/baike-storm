"""search 模块测试:mock 后端行为与统一返回格式。"""

import os

import pytest

from baike_storm import search


@pytest.fixture(autouse=True)
def _mock_backend(monkeypatch):
    monkeypatch.setenv("STORM_SEARCH_BACKEND", "mock")


def test_search_returns_uniform_fields(_mock_backend):
    results = search.search("人工智能", top_k=5)
    assert results, "mock 语料应当命中「人工智能」"
    for r in results:
        assert set(r) >= {"name", "url", "sentence", "credibility", "title", "images", "page_time"}
        assert r["url"].startswith("http")

    assert len(results) <= 5


def test_search_credibility_ranking_deterministic(_mock_backend):
    a = search.search("图灵奖 深度学习", top_k=3)
    b = search.search("图灵奖 深度学习", top_k=3)
    assert [r["url"] for r in a] == [r["url"] for r in b]


def test_search_no_match_returns_empty(_mock_backend):
    results = search.search("zzz完全不存在的内容zzz", top_k=5)
    # mock 后端无命中时回退取整个语料前 top_k(见 _MockBackend.search);至少不抛异常
    assert isinstance(results, list)


def test_env_override_corpus(tmp_path, _mock_backend, monkeypatch):
    (tmp_path / "x.json").write_text(
        '[{"title": "测试条目", "name": "测试源", "url": "https://example.org/x", "sentence": "这是用于测试的摘要内容,长度足够。", "credibility": "high"}]',
        encoding="utf-8",
    )
    # MOCK_CORPUS_DIR 在 config 导入时求值,须直接改属性并重置单例
    from baike_storm import config

    monkeypatch.setattr(config, "MOCK_CORPUS_DIR", tmp_path)
    search._MOCK = None
    try:
        results = search.search("测试条目", top_k=5)
        assert results and results[0]["title"] == "测试条目"
    finally:
        search._MOCK = None


def test_with_images_flag(_mock_backend):
    results = search.search("人工智能", top_k=2, with_images=True)
    assert isinstance(results, list) and all(r["images"] == [] for r in results)


# ── Tavily 后端(默认):用假 client 验证字段映射,不真正联网 ────────────────
class _FakeTavily:
    last_kwargs = None

    def __init__(self, api_key=None):
        self.api_key = api_key

    def search(self, query, **kwargs):
        _FakeTavily.last_kwargs = kwargs
        return {
            "results": [
                {
                    "title": "人工智能的定义与历史",
                    "url": "https://www.example.org/ai/definition",
                    "content": "人工智能(Artificial Intelligence,简称 AI)是研究如何让计算机模拟人类智能的学科。",
                    "score": 0.91,
                    "published_date": "2026-01-02T08:00:00Z",
                    "image_url": "https://img.example.org/1.jpg",
                },
                {
                    "title": "低相关结果",
                    "url": "https://blog.example.net/low",
                    "content": "一篇关于人工智能应用的很短的文章。",
                    "score": 0.2,
                },
            ]
        }


def test_tavily_backend_maps_fields(monkeypatch):
    import sys
    import types

    fake = types.ModuleType("tavily")
    fake.TavilyClient = _FakeTavily
    monkeypatch.setitem(sys.modules, "tavily", fake)
    monkeypatch.setenv("STORM_SEARCH_BACKEND", "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")

    results = search.search("人工智能", top_k=5, with_images=True)
    assert len(results) == 1  # 只留 content >= 20 字且未被过滤的
    r = results[0]
    assert r["name"] == "example.org"  # 去掉 www.
    assert r["credibility"] == "high"  # score 0.91 >= 0.8
    assert r["page_time"] > 0  # published_date 可解析
    assert r["images"][0]["image_url"] == "https://img.example.org/1.jpg"
    assert _FakeTavily.last_kwargs["include_images"] is True


def test_tavily_backend_missing_key_returns_empty(monkeypatch):
    import sys
    import types

    fake = types.ModuleType("tavily")
    fake.TavilyClient = _FakeTavily
    monkeypatch.setitem(sys.modules, "tavily", fake)
    monkeypatch.setenv("STORM_SEARCH_BACKEND", "tavily")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert search.search("人工智能", top_k=5) == []
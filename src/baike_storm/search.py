"""检索后端 —— 流水线的唯一搜索入口。

默认后端(``STORM_SEARCH_BACKEND=tavily``):Tavily 搜索 API(``tavily-python`` 包)。
凭据只走环境变量 ``TAVILY_API_KEY``,代码里没有任何默认端点或密钥;
配置见 ``.env.example`` 与 README「检索后端」一节。

离线后端(``STORM_SEARCH_BACKEND=mock``):读取 :data:`config.MOCK_CORPUS_DIR` 下的
JSON 语料(见 ``data/mock_corpus/README.md``),按查询词条命中匹配,供 demo / 单测使用。

统一返回条目格式(与项目历史一致):
  {"name", "url", "sentence", "credibility", "title", "images", "page_time"}
其中:
  - ``name``  = 来源站点(取 URL 的 host);
  - ``sentence`` = Tavily 返回的摘要正文(喂给 LLM 的素材);
  - ``credibility`` = 由 Tavily 相关性得分映射(0.8+ = high,0.5+ = medium,否则 low);
  - ``page_time`` = 发布日期时间戳(新闻类源才有,0 = 未知,用于「最新动态」视角)。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.parse
from datetime import datetime

from . import config

logger = logging.getLogger(__name__)

# ── 结果质量过滤(与具体后端无关):命中即丢弃 ────────────────────────────────
BAN_URL_SUBSTRINGS = {
    # 招聘
    "kanzhun", "zhipin.com", "zhaopin.com", "51job.com", "liepin.com", "lagou.com",
    "maimai.cn", "linkedin.com", "yingjiesheng.com", "jobui.com", "shixiseng.com",
    # 电商
    "taobao.com", "tmall.com", "jd.com", "pinduoduo.com", "suning.com", "dangdang.com",
    "vip.com", "amazon.cn", "you.163.com", "1688.com", "b2b.baidu.com", "yiche.com", "xcar.com",
    # 企业商家 / 招标
    "chahaoba.com", "bidcenter.com", "qizhidao.com", "yaozh.com", "fang.com", "foodjx.com",
    # 医疗(仅患者社区类)
    "haodf.com", "39.net", "jiankang.baidu.com", "dayi.org.cn", "99.com.cn", "youlai.cn",
    "chunyuyisheng.com", "120ask.com", "familydoctor.com.cn", "zhzyw.com", "yihu.com",
    "health.baidu.com",
    # 百科 / 试题 / 盗版(内容平台内卷,来源质量低)
    "baike.baidu.com", "baike.com", "baike.so.com", "baike.sogou.com", "zupu.cn",
    "moegirl.org", "zybang.com", "wjx.cn", "shangxueba.com", "artron.net",
}


def _url_is_banned(url: str) -> bool:
    low = (url or "").lower()
    return any(bad in low for bad in BAN_URL_SUBSTRINGS)


def _host_of(url: str) -> str:
    """取 URL 的站点名(去掉 www. 前缀),作为来源名称。"""
    netloc = (urllib.parse.urlparse(url or "").netloc or "").lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def _parse_date_ts(raw: str | None) -> int:
    """Tavily 的 published_date(如 '2026-01-01' / '2026-01-01T12:00:00Z')→ Unix 秒;失败返回 0。"""
    if not raw:
        return 0
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        try:
            return int(datetime.strptime(raw[:10], "%Y-%m-%d").timestamp())
        except (ValueError, TypeError):
            return 0


# ── Tavily 后端(默认)────────────────────────────────────────────────────────
def _search_tavily(query: str, top_k: int, with_images: bool) -> list[dict]:
    """Tavily 搜索 → 过滤 → 按相关性排序 → 取前 top_k。"""
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not key:
        logger.warning("搜索后端未配置:请设置 TAVILY_API_KEY(参考 .env.example)")
        return []
    try:
        from tavily import TavilyClient  # 延迟导入:仅 tavily 后端需要
    except ImportError:
        logger.error("缺少 tavily 依赖:请 `pip install -e '.[search]'`(或安装 baike-storm 完整依赖)")
        return []

    time.sleep(0.2)  # 温和限流
    kwargs: dict = {"query": query, "max_results": max(1, top_k), "search_depth": "basic"}
    if with_images:
        kwargs["include_images"] = True
    try:
        resp = TavilyClient(api_key=key).search(**kwargs)
    except Exception as e:  # noqa: BLE001
        logger.error("tavily 搜索失败 query=%s error=%s", query, e)
        return []

    out: list[dict] = []
    for r in resp.get("results") or []:
        url = (r.get("url") or "").strip()
        content = (r.get("content") or "").strip()
        title = (r.get("title") or "").strip()
        if not (url and content) or len(content) < 20:
            continue
        if _url_is_banned(url):
            continue
        score = _f(r.get("score"))
        rec = {
            "name": _host_of(url) or title,
            "url": url,
            "sentence": content,
            "credibility": "high" if score >= 0.8 else ("medium" if score >= 0.5 else "low"),
            "title": title,
            "images": [],
            "page_time": _parse_date_ts(r.get("published_date")),
        }
        if with_images:
            img = (r.get("image_url") or "").strip()
            if img:
                rec["images"] = [{"image_url": img, "width": None, "height": None}]
        out.append(rec)
    return out[:top_k]


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


# ── Mock 后端(离线 demo / 单测)──────────────────────────────────────────────
class _MockBackend:
    def __init__(self, corpus_dir: str | None = None):
        self._dir = corpus_dir or str(config.MOCK_CORPUS_DIR)
        self._entries = None

    def _load(self) -> list[dict]:
        if self._entries is not None:
            return self._entries
        entries: list[dict] = []
        if os.path.isdir(self._dir):
            for fn in sorted(os.listdir(self._dir)):
                if not fn.endswith(".json"):
                    continue
                with open(os.path.join(self._dir, fn), encoding="utf-8") as f:
                    data = json.load(f)
                entries.extend(data if isinstance(data, list) else [data])
        self._entries = entries
        return entries

    def search(self, query: str, top_k: int, with_images: bool) -> list[dict]:
        kw = re.findall(r"[\u4e00-\u9fa5]{2,}", (query or ""))
        qs = set(kw)

        def score(e: dict) -> int:
            hits = 0
            for w in qs:
                w = w.strip("的了吗和与是")
                if not w:
                    continue
                if w in (e.get("title", "") or "") or w in (e.get("name", "") or ""):
                    hits += 2
                # 语料条目可自带 match 关键词(见 data/mock_corpus/README.md)
                if any(w in m for m in (e.get("match") or [])):
                    hits += 1
            return hits

        scored = sorted(self._load(), key=score, reverse=True)
        if not scored:
            return []
        picked = [e for e in scored if score(e) > 0] or scored[: min(top_k, len(scored))]
        picked = picked[:top_k]
        out = []
        for e in picked:
            out.append(
                {
                    "name": e.get("name", ""),
                    "url": e.get("url", ""),
                    "sentence": e.get("sentence", ""),
                    "credibility": e.get("credibility", "medium"),
                    "title": e.get("title", ""),
                    "images": e.get("images") or [] if with_images else [],
                    "page_time": int(e.get("page_time") or 0),
                }
            )
        return out


_MOCK = None


# ── 统一入口 ────────────────────────────────────────────────────────────────
def search(query: str, top_k: int = 5, with_images: bool = False) -> list[dict]:
    """按配置的搜索后端检索,返回统一格式的条目列表(供 SourceLib 入库)。

    后端由 ``STORM_SEARCH_BACKEND`` 指定:tavily(默认,需 TAVILY_API_KEY)/ mock(离线)。
    """
    global _MOCK
    backend = os.environ.get("STORM_SEARCH_BACKEND", "tavily").strip().lower()
    if backend in ("mock", "local"):
        if _MOCK is None:
            _MOCK = _MockBackend()
        return _MOCK.search(query, top_k=top_k, with_images=with_images)
    return _search_tavily(query, top_k=top_k, with_images=with_images)

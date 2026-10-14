"""STORM 百科重建的流程编排。"""

import contextvars
from contextlib import contextmanager
from collections.abc import Iterator
import datetime as _dt
import json
import os
import re
import sys
import time

from . import config
from . import editorial
from . import search
from . import tracing as ls
from .llm import pipeline_client, llm_is_ready, MISSING_KEY_MSG
from . import prompts_storm as P

LANGSMITH_PROJECT = os.environ.get("BAIKE_LANGSMITH_PROJECT", "baike_storm")
os.environ["LANGSMITH_PROJECT"] = LANGSMITH_PROJECT

search_tool = ls.traceable(name="search", run_type="tool")(search.search)
route_to_dalei = ls.traceable(name="classify_entry")(editorial.route_to_dalei)


@contextmanager
def storm_trace(title: str, out_dir: str | None = None,
                **parameters: object) -> Iterator[object]:
    """创建一次任务的根轨迹，嵌套调用复用该轨迹。"""
    current = ls.get_current_run_tree()
    if current is not None and current.name == "baike_storm_task":
        yield current
        return
    with ls.trace("baike_storm_task", project_name=LANGSMITH_PROJECT,
                  inputs={"title": title, **parameters}) as root:
        try:
            yield root
        finally:
            url = _langsmith_url(root) if ls.tracing_is_enabled() else None
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
                path = os.path.join(out_dir, "langsmith_url.txt")
                if url:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(url + "\n")
                elif os.path.exists(path):
                    os.remove(path)

# 固定的基础事实视角。
P0 = {"name": "基础事实写手", "focus": "该词条最基本的定义、身份、核心属性与广覆盖的基本信息"}
# 固定的近期动态视角。
P_NEWS = {"name": "最新动态追踪", "recency": True,
          "focus": "近一年({since} 至今)的最新进展、新闻事件、数据更新与状态变化(版本/销量/职务/赛季/政策等)"}

SEARCH_TOP_K = config.SEARCH_TOP_K
SECTION_TOP_K = config.SECTION_TOP_K
MIN_CHAPTERS = config.MIN_CHAPTERS
MAX_CHAPTERS = config.MAX_CHAPTERS
RECENT_DAYS = config.RECENT_DAYS
# 导语和参考资料另行生成，其余附录不作为正文章。
SKIP_CHAPTERS = re.compile(r"^(概述|简介|综述|参考资料|参考文献|参见|外部链接|延伸阅读|相关条目|注释|脚注)$")


def today() -> _dt.date:
    """返回运行日期，可用 STORM_TODAY 固定。"""
    t = os.environ.get("STORM_TODAY")
    return _dt.date.fromisoformat(t) if t else _dt.date.today()


def _fmt_date(epoch: int) -> str:
    return _dt.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d") if epoch and epoch > 0 else ""


# 使用 ContextVar 隔离并发 Web 请求的事件回调。
_EMITTER: contextvars.ContextVar = contextvars.ContextVar("storm_emitter", default=None)


class RunCancelled(Exception):
    """事件回调用它通知流程停止；各步不得吞掉该异常。"""


def set_emitter(fn):
    """注册事件回调并返回可用于 reset 的 token。"""
    return _EMITTER.set(fn)


def _emit(kind: str, **data) -> None:
    fn = _EMITTER.get()
    if fn is not None:
        fn(kind, data)


@ls.traceable(name="generate_text")
def _llm(prompt: str, **kwargs) -> str:
    """调用模型并返回文本。"""
    if not llm_is_ready():
        raise RuntimeError(MISSING_KEY_MSG)
    return pipeline_client().invoke(prompt)


def _parse_json(text: str, fallback):
    """从模型输出中提取 JSON，失败时返回 fallback。"""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.MULTILINE).strip()
    for lb, rb in (("[", "]"), ("{", "}")):
        l, r = t.find(lb), t.rfind(rb)
        if l != -1 and r != -1 and r > l:
            try:
                return json.loads(t[l : r + 1])
            except json.JSONDecodeError:
                continue
    return fallback


class SourceLib:
    def __init__(self):
        self._by_url = {}
        self.sources = []

    def add(self, item: dict) -> int:
        url = (item.get("url") or "").strip()
        if url in self._by_url:
            return self._by_url[url]
        sid = len(self.sources) + 1
        self._by_url[url] = sid
        self.sources.append(
            {
                "id": sid,
                "name": item.get("name", ""),
                "url": url,
                "sentence": item.get("sentence", ""),
                "credibility": item.get("credibility", ""),
                "title": item.get("title", ""),
                "images": item.get("images") or [],
                "page_time": int(item.get("page_time") or 0),
                "date": _fmt_date(int(item.get("page_time") or 0)),
            }
        )
        return sid

    def get(self, sid: int) -> dict | None:
        if 1 <= sid <= len(self.sources):
            return self.sources[sid - 1]
        return None

    def __len__(self):
        return len(self.sources)


def _fmt_sources(src_list: list[dict]) -> str:
    """格式化带全局编号的来源，供模型引用。"""
    lines = []
    for s in src_list:
        cred = f"({s['credibility']}) " if s.get("credibility") else ""
        date = f"(发布 {s['date']}) " if s.get("date") else ""
        lines.append(f"[{s['id']}] {s['name']} {cred}{date}{s['url']}\n    {s['sentence']}")
    return "\n".join(lines) if lines else "(无检索结果)"


def _pick_hits(hits: list[dict], k: int, recency: bool) -> list[dict]:
    """近期视角按发布时间选择；其他视角留末位给更新的来源，避免只取权威排序前列。"""
    if recency:
        return sorted(hits, key=lambda h: -(h.get("page_time") or 0))[:k]
    head, rest = hits[: k - 1], hits[k - 1:]
    if not rest:
        return head
    cutoff = time.time() - RECENT_DAYS * 86400
    newest_head = max((h.get("page_time") or 0 for h in head), default=0)
    fresh = [h for h in rest if (h.get("page_time") or 0) > max(cutoff, newest_head)]
    return head + [max(fresh, key=lambda h: h["page_time"]) if fresh else rest[0]]


@ls.traceable(name="discover_perspectives")
def discover_perspectives(title: str, dalei_block: str, n: int = 5) -> list[dict]:
    """生成基础、近期和其他纵深视角。"""
    personas = [P0]
    if config.NEWS_ON and n >= 2:
        since = (today() - _dt.timedelta(days=RECENT_DAYS)).strftime("%Y年%m月")
        personas.append({**P_NEWS, "focus": P_NEWS["focus"].format(since=since)})
    want = max(1, n - len(personas))
    prompt = P.DISCOVER_PERSPECTIVES.format(title=title, dalei_block=dalei_block or "(无同类参考)", n=want)
    arr = _parse_json(_llm(prompt), fallback=[]) if n > len(personas) else []
    for item in arr:
        if isinstance(item, dict) and item.get("name"):
            personas.append({"name": item["name"], "focus": item.get("focus", "")})
    # 模型返回不足时补充通用视角。
    generic = [
        {"name": "沿革与背景", "focus": "起源、发展历程、时间线与背景脉络"},
        {"name": "主体与关键数据", "focus": "核心构成、关键参数、规格与量化事实"},
        {"name": "影响与评价", "focus": "意义、反响、评价、荣誉与社会影响"},
        {"name": "事件与争议", "focus": "相关事件、争议、里程碑与长尾话题"},
    ]
    gi = 0
    while len(personas) < n and gi < len(generic):
        if all(g["name"] != generic[gi]["name"] for g in personas):
            personas.append(generic[gi])
        gi += 1
    return personas[:n]


@ls.traceable(name="expert_answer")
def expert_answer(title: str, question: str, R: SourceLib, recency: bool = False) -> tuple[str, list[int]]:
    """检索资料并生成带引用的回答。"""
    d = today()
    hint = P.RECENCY_HINT.format(year=d.year, last_year=d.year - 1) if recency else ""
    queries = _parse_json(
        _llm(P.EXPERT_DECOMPOSE.format(title=title, question=question, today=d.isoformat(), recency_hint=hint)),
        fallback=[],
    )
    if not isinstance(queries, list) or not queries:
        queries = [question]
    queries = [str(q).strip() for q in queries if str(q).strip()][:4]
    _emit("queries", question=question, queries=queries)

    used_ids = []
    for q in queries:
        try:
            # 先扩大候选集，再按权威和新鲜度筛选。
            hits = _pick_hits(search_tool(q, top_k=SEARCH_TOP_K * 3), SEARCH_TOP_K, recency)
        except Exception as e:  # noqa: BLE001 - 单条检索失败不应中断整场对话
            print(f"    ⚠️ 检索失败 q={q!r}: {e}", flush=True)
            hits = []
            _emit("search_error", query=q, error=str(e))
        ids = [R.add(h) for h in hits]
        used_ids.extend(ids)
        _emit(
            "search",
            query=q,
            hits=[{k: R.get(i)[k] for k in ("id", "name", "url", "credibility")} for i in ids],
            total_sources=len(R),
        )
    seen = set()
    used_ids = [i for i in used_ids if not (i in seen or seen.add(i))]

    src_block = _fmt_sources([R.get(i) for i in used_ids])
    answer = _llm(P.EXPERT_ANSWER.format(title=title, question=question, sources_block=src_block,
                                         today=d.isoformat()))
    return answer.strip(), used_ids


@ls.traceable(name="simulate_conversation")
def simulate_conversation(title: str, persona: dict, R: SourceLib, m: int = 3) -> list[dict]:
    """为一个视角运行最多 m 轮问答。"""
    turns = []
    for rnd in range(1, m + 1):
        history = _render_history(turns) if turns else "(暂无,这是第一个问题)"
        q = _llm(
            P.WRITER_ASK.format(
                title=title,
                today=today().isoformat(),
                persona_name=persona["name"],
                persona_focus=persona["focus"],
                history=history,
            )
        ).strip()
        if q.upper().startswith("DONE") or not q:
            _emit("persona_stop", persona=persona["name"], round=rnd)
            break
        print(f"    [{persona['name']}] Q{rnd}: {q}", flush=True)
        _emit("question", persona=persona["name"], round=rnd, question=q)
        answer, ids = expert_answer(title, q, R, recency=bool(persona.get("recency")))
        _emit("answer", persona=persona["name"], round=rnd, answer=answer, source_ids=ids)
        turns.append({"round": rnd, "question": q, "answer": answer, "source_ids": ids})
    return turns


def _render_history(turns: list[dict]) -> str:
    return "\n\n".join(f"Q: {t['question']}\nA: {t['answer']}" for t in turns)


@ls.traceable(name="run_prewriting")
def run_prewriting(title: str, dalei_block: str, R: SourceLib, n: int = 5, m: int = 3) -> list[dict]:
    """运行所有视角的对话，并将来源汇入 R。"""
    personas = discover_perspectives(title, dalei_block, n=n)
    print(f"🎭 视角({len(personas)}): " + " / ".join(p["name"] for p in personas), flush=True)
    _emit("perspectives", personas=personas)
    conversations = []
    for i, p in enumerate(personas):
        print(f"  ▶ 视角「{p['name']}」开聊 …", flush=True)
        _emit("persona_start", index=i + 1, total=len(personas), name=p["name"], focus=p["focus"])
        turns = simulate_conversation(title, p, R, m=m)
        conversations.append({"persona": p, "turns": turns})
    print(f"📚 调研完成:全局源库 R 共 {len(R)} 条", flush=True)
    _emit("prewriting_done", total_sources=len(R))
    return conversations


def _conversations_text(conversations: list[dict]) -> str:
    """拼接对话，供提纲精修使用。"""
    blocks = []
    for c in conversations:
        p = c["persona"]
        qa = "\n\n".join(f"Q: {t['question']}\nA: {t['answer']}" for t in c["turns"])
        blocks.append(f"### 视角:{p['name']}（{p['focus']}）\n{qa or '(未产生问答)'}")
    return "\n\n".join(blocks)


@ls.traceable(name="draft_outline")
def draft_outline(title: str) -> str:
    return _llm(P.DRAFT_OUTLINE.format(title=title, min_ch=MIN_CHAPTERS, max_ch=MAX_CHAPTERS)).strip()


@ls.traceable(name="refine_outline")
def refine_outline(title: str, draft: str, conversations: list[dict]) -> str:
    """根据调研对话精修提纲，必要时合并大章。"""
    outline = _llm(
        P.REFINE_OUTLINE.format(
            title=title, today=today().isoformat(), min_ch=MIN_CHAPTERS, max_ch=MAX_CHAPTERS,
            draft_outline=draft, conversations=_conversations_text(conversations),
        )
    ).strip()
    n_ch = len(_parse_sections(outline))
    if n_ch > MAX_CHAPTERS:
        print(f"🧩 提纲 {n_ch} 个大章 > 上限 {MAX_CHAPTERS},合并相近大章 …", flush=True)
        _emit("outline_merge", n_chapters=n_ch, max_chapters=MAX_CHAPTERS)
        merged = _llm(
            P.MERGE_OUTLINE.format(title=title, n_ch=n_ch, min_ch=MIN_CHAPTERS, max_ch=MAX_CHAPTERS,
                                   outline=outline)
        ).strip()
        if _parse_sections(merged):
            outline = merged
    return outline


_STOP = set("的了和与及或在是对为之其该等这那被把你我他她它们个号款式型年月日")


def _char_bigrams(s: str) -> set[str]:
    s = re.sub(r"\s+", "", s)
    return {s[i : i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else set(s)


def _recall_sources(section_title: str, R: SourceLib, top_k: int = SECTION_TOP_K) -> list[dict]:
    """按节标题与来源文本的字符 bigram 重叠取 top-k。"""
    # 来源较少时全部保留，避免漏掉稀疏资料。
    if len(R) <= top_k:
        return list(R.sources)
    key = _char_bigrams(section_title) - _STOP
    scored = []
    for s in R.sources:
        text = (s["name"] + " " + s["sentence"])
        overlap = len(key & _char_bigrams(text))
        scored.append((overlap, s["id"], s))
    scored.sort(key=lambda x: (x[0], -x[1]), reverse=True)
    return [s for _, _, s in scored[:top_k]]


def _clean_heading(s: str) -> str:
    """去掉标题中的编号和 Markdown 装饰。"""
    s = re.sub(r"[*_`]+", "", s).strip()
    return re.sub(r"^([0-9一二三四五六七八九十]+[、.．:：)\s]|第[一二三四五六七八九十0-9]+[章节部分][、:：\s]*)\s*", "", s).strip()


def _parse_outline(outline: str) -> list[tuple[str, list[str]]]:
    """解析 Markdown 提纲，并过滤附录章节。"""
    out: list[tuple[str, list[str]]] = []
    skipping = False
    for line in outline.splitlines():
        m3 = re.match(r"^###\s+(.+?)\s*$", line)
        m2 = None if m3 else re.match(r"^##\s+(.+?)\s*$", line)
        if m2:
            name = _clean_heading(m2.group(1))
            skipping = bool(SKIP_CHAPTERS.match(name)) or not name
            if not skipping:
                out.append((name, []))
        elif m3 and out and not skipping:
            sub = _clean_heading(m3.group(1))
            if sub and not SKIP_CHAPTERS.match(sub):
                out[-1][1].append(sub)
    return out


def _parse_sections(outline: str) -> list[str]:
    """返回大章标题。"""
    return [ch for ch, _ in _parse_outline(outline)]


def _strip_appendix(article: str) -> str:
    """移除模型输出的附录，参考资料由代码统一拼接。"""
    keep, out = True, []
    for line in article.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m and not line.startswith("###"):
            keep = not SKIP_CHAPTERS.match(_clean_heading(m.group(1)))
        if keep:
            out.append(line)
    return "\n".join(out).strip()


@ls.traceable(name="write_article")
def write_article(title: str, outline: str, R: SourceLib) -> str:
    """按大章生成正文，去重后补充导语和参考资料。"""
    chapters = _parse_outline(outline) or [("主体内容", []), ("影响与评价", [])]
    sections = [ch for ch, _ in chapters]
    print(f"✍️  分章成文({len(sections)} 章): " + " / ".join(sections), flush=True)
    _emit("stage", stage="writing", label="分章成文", sections=sections)
    td = today().isoformat()

    parts = []
    for i, (st, subs) in enumerate(chapters):
        # 小节标题通常更具体，能召回长尾来源。
        srcs = _recall_sources(" ".join([st, *subs]), R)
        _emit("section_start", index=i + 1, total=len(sections), title=st, n_sources=len(srcs),
              subsections=subs)
        sub_block = "\n".join(f"- {s}" for s in subs) if subs else "(无预设小节,可按内容自行分 2-4 个 ### 小节,内容少则不分)"
        body = _llm(
            P.WRITE_SECTION.format(title=title, section_title=st, subsections=sub_block,
                                   sources_block=_fmt_sources(srcs), today=td),
            langsmith_extra={"name": f"write_section: {st}"},
        ).strip()
        _emit("section_done", index=i + 1, total=len(sections), title=st, chars=len(body), body=body)
        parts.append(body)
    article = "\n\n".join(parts)

    _emit("stage", stage="dedup", label="全文去重", chars=len(article))
    article = _llm(P.DEDUP.format(title=title, article=article)).strip()
    article = _strip_appendix(article)
    _emit("stage", stage="lead", label="合成导语", chars=len(article))
    lead = _llm(P.LEAD.format(title=title, article=article, today=td)).strip()
    _emit("lead_done", lead=lead)

    # 只列正文实际引用的来源。
    cited = sorted({int(x) for x in re.findall(r"\[(\d+)\]", article)})
    refs = []
    for sid in cited:
        s = R.get(sid)
        if s:
            refs.append(f"[{sid}] {s['name']} - {s['url']}")
    ref_block = "\n\n## 参考资料\n\n" + "\n".join(refs) if refs else ""

    return f"# {title}\n\n{lead}\n\n{article}{ref_block}\n"


def run_storm(title: str, out_dir: str | None = None, n: int = 5, m: int = 3, figures: bool | None = None) -> str:
    """运行 STORM 全流程并返回报告文本。"""
    return run_storm_full(title, out_dir=out_dir, n=n, m=m, figures=figures)["report"]


def _figures_wanted(figures: bool | None, out_dir: str | None) -> tuple[bool, str]:
    """判断是否启用配图，并返回原因或模型名。"""
    if figures is None:
        figures = os.environ.get("STORM_FIGURES", "1") not in ("0", "false", "False", "")
    if not figures:
        return False, "已关闭"
    if not out_dir:
        return False, "无 out_dir,图片无处落盘"
    from . import figures as FIG

    return FIG.vlm_ready()


def run_storm_full(title: str, out_dir: str | None = None, n: int = 5, m: int = 3,
                   figures: bool | None = None) -> dict:
    """返回 {report, figures, infobox, route, dalei}；图片路径相对 out_dir。"""
    with storm_trace(title, out_dir, n=n, m=m, figures=figures) as root:
        result = _run_storm_full(title, out_dir, n, m, figures)
        if root is not None:
            root.add_outputs(result)
        return result


@ls.traceable(name="run_storm_full")
def _run_storm_full(title: str, out_dir: str | None, n: int, m: int,
                    figures: bool | None) -> dict:
    _emit("stage", stage="route", label="词条类型路由")
    dalei_name = route_to_dalei(title)
    dalei = editorial.get_dalei(dalei_name)
    dalei_block = dalei["block"] if dalei else ""
    route = f"线上大类「{dalei['canonical']}」" if dalei else "无同类大类(仅凭 LLM 知识发现视角)"
    print(f"🏷️  视角发现参考: {route}", flush=True)
    _emit("route", route=route, dalei=dalei["canonical"] if dalei else "")

    R = SourceLib()
    _emit("stage", stage="prewriting", label="多视角调研", n=n, m=m)
    conversations = run_prewriting(title, dalei_block, R, n=n, m=m)
    _emit("stage", stage="outline", label="两段式提纲")
    draft = draft_outline(title)
    _emit("outline_draft", outline=draft)
    final_outline = refine_outline(title, draft, conversations)
    _emit("outline_final", outline=final_outline, sections=_parse_sections(final_outline))
    report = write_article(title, final_outline, R)
    trace_url = None
    dalei_canonical = dalei["canonical"] if dalei else ""

    # 信息栏生成失败不影响报告。
    infobox = None
    _emit("stage", stage="infobox", label="生成信息栏")
    try:
        from . import infobox as _ibx

        infobox = _ibx.build_infobox(title, dalei_canonical or None, report, _llm, _parse_json)
        _emit("infobox", **infobox)
    except RunCancelled:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ 信息栏生成失败: {type(e).__name__}: {e}", flush=True)
        _emit("infobox_error", error=f"{type(e).__name__}: {e}")

    if out_dir:
        _dump(out_dir, title, route, report, draft, final_outline, conversations, R, trace_url)
        with open(os.path.join(out_dir, "infobox.json"), "w", encoding="utf-8") as f:
            json.dump(infobox or {}, f, ensure_ascii=False, indent=1)
    if trace_url:
        print(f"🔗 LangSmith 轨迹: {trace_url}", flush=True)

    figs = None
    want, why = _figures_wanted(figures, out_dir)
    if want:
        from . import figures as FIG

        _emit("stage", stage="figures", label="挑选配图", model=why)
        try:
            figs = FIG.illustrate(report, R, out_dir, emit=_emit, dalei=dalei_canonical or None,
                                  llm=_llm if config.FIG_PLAN else None,
                                  llm_model=pipeline_client().model_name)
        except RunCancelled:
            raise
        except Exception as e:  # noqa: BLE001 - 配图失败不影响报告
            print(f"⚠️ 配图失败,输出无图版本: {type(e).__name__}: {e}", flush=True)
            _emit("figures_error", error=f"{type(e).__name__}: {e}")
    else:
        print(f"🖼️  跳过配图: {why}", flush=True)
        _emit("figures_skipped", reason=why)
    if out_dir:
        # 覆盖旧文件，避免复用目录时残留上一轮的图片。
        with open(os.path.join(out_dir, "figures.json"), "w", encoding="utf-8") as f:
            json.dump(figs or {"hero": None, "sections": {}}, f, ensure_ascii=False, indent=1)
    return {"report": report, "figures": figs, "route": route, "dalei": dalei_canonical, "infobox": infobox}


def _langsmith_url(root: object) -> str | None:
    try:
        return root.client.get_run_url(run=root, project_name=LANGSMITH_PROJECT)
    except Exception:  # noqa: BLE001
        return None


def _dump(out_dir, title, route, report, draft, final_outline, conversations, R, trace_url):
    os.makedirs(out_dir, exist_ok=True)

    def w(name, content):
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
            f.write(content)

    w("final_report.md", report)
    w("route.txt", route + "\n")
    w("outline.md", f"# 提纲草稿(调研前)\n\n{draft}\n\n---\n\n# 最终提纲(精修后)\n\n{final_outline}\n")

    conv_md = [f"# 调研对话全文\n\n词条: {title}\n全局源库 R: {len(R)} 条\n"]
    for c in conversations:
        p = c["persona"]
        conv_md.append(f"\n## 视角:{p['name']}\n> {p['focus']}\n")
        for t in c["turns"]:
            ids = " ".join(f"[{i}]" for i in t["source_ids"])
            conv_md.append(f"\n**Q{t['round']}**: {t['question']}\n\n**A**: {t['answer']}\n\n_源: {ids}_\n")
    conv_md.append("\n\n# 全局源库 R\n")
    for s in R.sources:
        conv_md.append(f"[{s['id']}] {s['name']} ({s['credibility']}) {s['url']}")
    w("conversations.md", "\n".join(conv_md))
    w("sources.json", json.dumps(R.sources, ensure_ascii=False, indent=1))

    if trace_url:
        w("langsmith_url.txt", trace_url + "\n")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a.strip()]
    title = args[0] if args else "问界M9"
    n = int(os.environ.get("STORM_N", "5"))
    m = int(os.environ.get("STORM_M", "3"))
    print(f"\n{'=' * 80}\n[STORM 百科重建] 词条: {title}  (N={n}, M={m})\n{'=' * 80}\n", flush=True)
    out = os.path.join(str(config.RUNS_DIR), f"{title}_storm")
    with storm_trace(title, out, n=n, m=m):
        report = run_storm(title, out_dir=out, n=n, m=m)
        print(report)
        from .render_baike_html import render

        render(out)
    print(f"\n{'=' * 80}\n报告长度: {len(report)} 字  →  {out}\n")

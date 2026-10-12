# -*- coding: utf-8 -*-
"""图文版面规划(skills/figure-layout/SKILL.md 的执行器)。

输入:figures.py 打完分的候选槽位 + 成文 markdown。
流程:
  1. 把正文按 render_baike_html 的段落切法编号(c章p段),附小节名/字数/环绕可用字数;
  2. 从全部槽位里收合格候选(VLM 已审,无硬伤,过线),附朝向/尺寸/画面描述/词法匹配段落提示;
  3. SKILL.md 正文 + 本次任务简报 → LLM → JSON 版面方案;
  4. 校验修正(id/段落存在、mode 与朝向/张数、尺寸与原图/环绕文字、间距、每章上限、subject 去重、图注不编数字);
  5. 产出 figures = {"hero", "blocks":[{chapter, para, mode, size, figs, why}], "sections"(兼容旧版), "planner"}。
LLM 失败或方案为空时返回 None,调用方退回规则排版(figures.pick_layout)。

离线重排(不调 VLM,只调 LLM):python layout_planner.py runs/<dir>
"""

import html as _html
import json
import math
import os
import re
import sys
from . import tracing as ls

from .render_baike_html import _P_RE, _render_blocks, parse_report

HERE = os.path.dirname(os.path.abspath(__file__))
from . import config as _cfg

SKILL_PATH = str(_cfg.FIGURE_SKILL_MD)

MAX_CANDS = int(os.environ.get("FIG_PLAN_MAX_CANDS", "36"))
CHARS_PER_FIG = float(os.environ.get("FIG_PLAN_CHARS_PER_FIG", "500"))  # × profile.ratio = 每图块对应正文字数(人物≈1750)
CHARS_PER_CH_BLOCK = 1500     # 章内每多这么多字,该章多允许一个图块(最多 3)
DUP_DEPICTS = 0.45             # subject 相关时,画面描述 bigram 重叠≥此值算同一画面
DUP_DEPICTS_HARD = 0.6         # 不论 subject,画面描述重叠≥此值算同一画面
MIN_GAP = 3                    # 同章两个图块之间至少隔几段
CAPTION_MAX = 36
SIDE_WIDTH = {"s": 220, "m": 280, "l": 340}
SIDE_MIN_RUN = {"s": 250, "m": 400, "l": 550}  # 侧栏图需要的环绕文字(字)
FIRST_CH_SIDE_MIN_PARA = 5     # 第 0 章紧挨信息栏:侧栏图至少放到第几段之后
MODES = {"side": 1, "side-left": 1, "wide": 1, "pair": 2, "trio": 3}
SKIP_CHAPTERS = ("参考资料", "概述", "参见", "外部链接", "延伸阅读")

_HEAD_P_RE = re.compile(r"<h([34])[^>]*>(.*?)</h\1>|<p>(.*?)</p>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")


# ── skill ────────────────────────────────────────────────────────────────
def load_skill(path: str = SKILL_PATH) -> tuple[dict, str]:
    """→ (frontmatter dict, 正文)。frontmatter 只取 name/description 两个简单键。"""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    meta, body = {}, text
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if m:
        body = text[m.end():]
        for ln in m.group(1).splitlines():
            k, _, v = ln.partition(":")
            if v:
                meta[k.strip()] = v.strip()
    return meta, body.strip()


# ── 正文段落编号(与 render_baike_html 插图时的 <p> 顺序一致)──────────────
def _plain(h: str) -> str:
    return re.sub(r"\[\d+\]", "", _html.unescape(_TAG_RE.sub("", h))).strip()


def paragraph_index(report_md: str) -> tuple[str, list[dict]]:
    """→ (title, chapters)。chapter = {i, title, skip, chars, paras:[{id,k,sub,text,chars,run,last}]}。

    run = 本段起到所在小节结束的正文字数(侧栏浮动图能被多少文字环绕)。
    """
    title, sections, _ = parse_report(report_md)
    chapters = []
    for i, (t, body) in enumerate(sections):
        paras, sub = [], ""
        for m in _HEAD_P_RE.finditer(_render_blocks(body)):
            if m.group(1):
                sub = _plain(m.group(2))
                continue
            txt = _plain(m.group(3))
            paras.append({"id": f"c{i}p{len(paras)}", "k": len(paras), "sub": sub, "text": txt, "chars": len(txt)})
        for j, p in enumerate(paras):
            run = 0
            for q in paras[j:]:
                if q["sub"] != p["sub"]:
                    break
                run += q["chars"]
            p["run"], p["last"] = run, j == len(paras) - 1
        chapters.append({"i": i, "title": t, "skip": t.strip() in SKIP_CHAPTERS or not paras,
                         "chars": sum(p["chars"] for p in paras), "paras": paras})
    return title, chapters


# ── 候选 ─────────────────────────────────────────────────────────────────
_STOP_BG = set("的了和与及或在是对为之其该等这那被把图中")


def _bigrams(s: str) -> set[str]:
    s = re.sub(r"[\s\W_]+", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)} - {a + b for a in _STOP_BG for b in _STOP_BG}


def _orient(w: int, h: int) -> tuple[float, str]:
    r = (w or 4) / max(h or 3, 1)
    return round(r, 2), ("竖" if r < 0.85 else "方" if r < 1.3 else "横")


def collect_candidates(slots: list[dict], accept: float, title: str, chapters: list[dict]) -> list[dict]:
    """合格候选(已审、已下载、无硬伤、节图分或主图分过线附近),按质量取前 MAX_CANDS,编号 F1..。"""
    label_to_ch = {c["title"]: c["i"] for c in chapters}
    pool, seen = [], set()
    for s in slots:
        for c in s["cands"]:
            if "vlm" not in c or not c.get("file") or c.get("penalty", 10) >= 10 or c["key"] in seen:
                continue
            if c.get("final", -1) < accept - 0.5 and c.get("hero_score", -1) < accept:
                continue
            seen.add(c["key"])
            pool.append((c, s))
    pool.sort(key=lambda cs: -max(cs[0].get("final", -1), cs[0].get("hero_score", -1)))
    title_bg = _bigrams(title)
    para_bg = [(p, _bigrams(p["text"])) for ch in chapters if not ch["skip"] for p in ch["paras"]]
    out = []
    for n, (c, s) in enumerate(pool[:MAX_CANDS], 1):
        ratio, ori = _orient(c.get("w"), c.get("h"))
        anchor = _bigrams(" ".join([c.get("caption", ""), c.get("depicts", ""), c.get("subject", "")])) - title_bg
        hints = []
        if len(anchor) >= 3:
            ov = sorted(((len(anchor & bg) / len(anchor), p["id"]) for p, bg in para_bg), reverse=True)[:3]
            hints = [(pid, round(o, 2)) for o, pid in ov if o >= 0.12]
        out.append({"id": f"F{n}", "c": c, "w": c.get("w") or 0, "h": c.get("h") or 0, "ratio": ratio, "ori": ori,
                    "subject": c.get("subject", ""), "depicts": c.get("depicts", ""),
                    "caption": c.get("caption", ""), "entity": c.get("entity", 0), "fit": c.get("relevance", 0),
                    "final": c.get("final", -1), "hero_ok": bool(c.get("hero_ok")) and c.get("hero_score", -1) >= accept,
                    "hero_score": c.get("hero_score", -1),
                    "soft": [d for d in c.get("defects") or []],
                    "from_ch": label_to_ch.get(s.get("label")), "hints": hints})
    # 去重用的画面指纹:depicts 的 bigram 去掉词条名和「几乎每张图都有」的词(人名、车型名、球衣…),
    # 否则同一人物的不同照片会因共享这些词被误判为同一画面
    dbg = [_bigrams(c["depicts"]) - title_bg for c in out]
    df = {}
    for g in dbg:
        for x in g:
            df[x] = df.get(x, 0) + 1
    common = {x for x, k in df.items() if k >= max(3, 0.3 * len(out))}
    for c, g in zip(out, dbg):
        c["dbg"] = g - common
    return out


def budget(chapters: list[dict], prof: dict, n_cands: int) -> tuple[int, dict]:
    """→ (全文图块上限, {章下标: 该章上限})。按正文字数而不是章数定预算,长章可放多块。"""
    total = sum(c["chars"] for c in chapters if not c["skip"])
    max_blocks = min(n_cands, max(1, math.ceil(total / (CHARS_PER_FIG * prof.get("ratio", 3.0)))))
    per_ch = {c["i"]: (0 if c["skip"] else min(3, 1 + c["chars"] // CHARS_PER_CH_BLOCK)) for c in chapters}
    return max_blocks, per_ch


# ── 简报 ─────────────────────────────────────────────────────────────────
def build_brief(title: str, type_label: str, prof: dict, chapters: list[dict], cands: list[dict],
                max_blocks: int, per_ch: dict) -> str:
    lines = [f"# 本次任务输入", f"词条:{title}(类型:{type_label})",
             f"主图偏好:{prof.get('hero') or '不设主图(hero 必须为 null)'}",
             f"允许组图 trio:{'是' if prof.get('gallery') else '否'}",
             f"节图上限 max_blocks:{max_blocks}", "", "## 正文段落(编号 / [小节] / 本段字数 / 到小节末可环绕字数 / 开头)"]
    for ch in chapters:
        if ch["skip"]:
            continue
        lines.append(f"### c{ch['i']}「{ch['title']}」 共{len(ch['paras'])}段 {ch['chars']}字 该章上限{per_ch[ch['i']]}")
        for p in ch["paras"]:
            tail = "(章末段)" if p["last"] else ""
            lines.append(f"{p['id']} [{p['sub'] or '-'}] {p['chars']}字/环绕{p['run']} {p['text'][:56]}{tail}")
    lines += ["", "## 候选图"]
    for c in cands:
        flags = []
        if c["hero_ok"]:
            flags.append(f"hero_ok(主图分{c['hero_score']:g})")
        if c["soft"]:
            flags.append("轻微缺陷:" + "/".join(c["soft"]))
        src = f"c{c['from_ch']}" if c["from_ch"] is not None else "主图槽"
        hints = " ".join(f"{pid}({o})" for pid, o in c["hints"]) or "无"
        lines.append(f"{c['id']} {c['ori']}图 {c['w']}×{c['h']} ratio={c['ratio']} | 主题:{c['subject']} | "
                     f"画面:{c['depicts']} | 图注:{c['caption']} | 本体{c['entity']:g} 切节{c['fit']:g} 综合{c['final']:g}"
                     f" | {' '.join(flags) or '无缺陷'} | 来源章:{src} | hints:{hints}")
    lines += ["", "按 SKILL 的规则输出版面方案 JSON。"]
    return "\n".join(lines)


# ── 解析 / 校验 ──────────────────────────────────────────────────────────
def _parse_obj(text: str):
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip(), flags=re.M)
    l, r = t.find("{"), t.rfind("}")
    if l == -1 or r <= l:
        return None
    try:
        v = json.loads(t[l:r + 1])
    except json.JSONDecodeError:
        return None
    return v if isinstance(v, dict) else None


def _caption(proposed, cand: dict, para_text: str) -> str:
    """LLM 改写的图注:超长 / 出现画面与锚点段都没有的数字 → 退回 VLM 原图注。"""
    base = cand["caption"] or cand["depicts"][:CAPTION_MAX]
    cap = re.sub(r"\s+", " ", str(proposed or "")).strip().rstrip("。")
    if not cap or len(cap) > CAPTION_MAX:
        return base
    support = para_text + cand["caption"] + cand["depicts"]
    if any(num not in support for num in re.findall(r"\d+", cap)):
        return base
    return cap


def _overlap(a: str, b: str) -> float:
    ga, gb = _bigrams(a), _bigrams(b)
    return len(ga & gb) / min(len(ga), len(gb)) if ga and gb else 0.0


def _same_subject(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return a in b or b in a or _overlap(a, b) >= 0.5


def _dup(x: dict, y: dict) -> bool:
    """两张候选是否「同一画面」(同场景/同动作/同角度)。

    subject 是 VLM 的粗分类(人物肖像/外观/演讲…),只比它会把「太空服照」和「半身肖像」误判成重复,
    所以主要看画面指纹 dbg(depicts 去掉词条名和各图公共词后的 bigram):
    几乎一样 → 重复;subject 相关且指纹较像 → 重复。更细的「同类泛泛图只用一张」交给 LLM(见 SKILL)。
    """
    related = _same_subject(x["subject"], y["subject"]) or _overlap(x["subject"], y["subject"]) > 0
    if not x["depicts"] or not y["depicts"]:
        return related
    ga, gb = x.get("dbg"), y.get("dbg")
    if ga is None or gb is None:
        ga, gb = _bigrams(x["depicts"]), _bigrams(y["depicts"])
    d = len(ga & gb) / min(len(ga), len(gb)) if ga and gb else 0.0
    return d >= DUP_DEPICTS_HARD or (related and d >= DUP_DEPICTS)


def _fit_side_size(size: str, p: dict, w: int) -> str | None:
    """侧栏图:按环绕文字和原图宽度降级尺寸;连 s 都不够返回 None。"""
    order = ["l", "m", "s"]
    for s in order[order.index(size) if size in order else 1:]:
        if p["run"] >= SIDE_MIN_RUN[s] and w >= SIDE_WIDTH[s] * 0.9:
            return s
    return None


def validate(plan: dict, cands: list[dict], chapters: list[dict], prof: dict,
             max_blocks: int, per_ch: dict) -> tuple[dict | None, list[dict], list[str]]:
    """→ (hero_cand+caption, blocks, warnings)。blocks 里 figs 是 (cand, caption)。"""
    by_id = {c["id"]: c for c in cands}
    para = {p["id"]: (ch, p) for ch in chapters if not ch["skip"] for p in ch["paras"]}
    warn, used, used_subj = [], set(), []  # used_subj: 已占用的候选(去重比较用)
    gallery = bool(prof.get("gallery"))

    hero = None
    h = plan.get("hero")
    if isinstance(h, dict) and prof.get("hero"):
        c = by_id.get(str(h.get("id")))
        if c and c["hero_ok"]:
            hero = {"cand": c, "caption": _caption(h.get("caption"), c, "")}
            used.add(c["id"])
            used_subj.append(c)
        else:
            warn.append(f"主图 {h.get('id')} 无效(不存在或非 hero_ok),改为不设")
    if hero is None and prof.get("hero"):  # LLM 漏选:取主图分最高的 hero_ok
        best = max((c for c in cands if c["hero_ok"]), key=lambda c: c["hero_score"], default=None)
        if best:
            hero = {"cand": best, "caption": best["caption"]}
            used.add(best["id"])
            used_subj.append(best)
            warn.append(f"主图缺省,按主图分补 {best['id']}")

    blocks = []
    for b in plan.get("blocks") or []:
        if not isinstance(b, dict):
            continue
        aid = str(b.get("anchor", ""))
        if aid not in para:
            warn.append(f"段落 {aid} 不存在,丢弃")
            continue
        ch, p = para[aid]
        figs = []
        for f in b.get("figs") or []:
            c = by_id.get(str((f or {}).get("id")))
            if not c or c["id"] in used or any(c is x for x, _ in figs):
                continue
            # 同一画面(含主图、其他图块、本图块内)只用一次;同对象不同角度不算重复
            if any(_dup(c, o) for o in used_subj + [x for x, _ in figs]):
                warn.append(f"{c['id']} 与已用图画面重复「{c['subject']}」,丢弃")
                continue
            figs.append((c, _caption(f.get("caption"), c, p["text"])))
        if not figs:
            continue
        mode = str(b.get("mode") or "")
        size = str(b.get("size") or "m")
        # 张数与 mode 对齐
        if mode == "trio" and not gallery:
            mode = "pair"
        if mode not in MODES or len(figs) != MODES[mode]:
            if len(figs) >= 3 and gallery:
                mode, figs = "trio", figs[:3]
            elif len(figs) >= 2:
                mode, figs = "pair", figs[:2]
            else:
                mode = "wide" if figs[0][0]["ratio"] >= 1.3 and figs[0][0]["w"] >= 800 else "side"
        if mode in ("pair", "trio"):
            ori = {c["ori"] == "横" for c, _ in figs}
            if len(ori) > 1:  # 朝向不一致不能等高并排 → 只留第一张
                warn.append(f"{aid} 组图朝向不一致,拆成单图")
                figs = figs[:1]
                mode = "wide" if figs[0][0]["ratio"] >= 1.3 and figs[0][0]["w"] >= 800 else "side"
        c0 = figs[0][0]
        if mode == "wide":
            if c0["ratio"] < 1.3 or c0["w"] < 800:
                mode = "side"
            else:
                size = "l" if size == "l" and c0["w"] >= 1000 else "m"
        if mode in ("side", "side-left"):
            if ch["i"] == 0 and p["k"] < FIRST_CH_SIDE_MIN_PARA:  # 紧挨信息栏
                if c0["ratio"] >= 1.2 and c0["w"] >= 800:
                    mode, size = "wide", "m"
                elif len(ch["paras"]) > FIRST_CH_SIDE_MIN_PARA:
                    p = ch["paras"][FIRST_CH_SIDE_MIN_PARA]
                    warn.append(f"{aid} 紧挨信息栏,侧栏图下移到 {p['id']}")
                else:
                    warn.append(f"{aid} 紧挨信息栏且章太短,丢弃侧栏图")
                    continue
            if mode != "wide":
                if p["last"] and p["k"] > 0:
                    p = ch["paras"][p["k"] - 1]
                fitted = _fit_side_size(size, p, c0["w"])
                if fitted is None:
                    if c0["ratio"] >= 1.3 and c0["w"] >= 800:
                        mode, size = "wide", "m"
                    else:
                        size = "s"  # 环绕文字不足:最小尺寸,渲染时下一小节标题 clear
                else:
                    size = fitted
        if mode in ("pair", "trio"):
            size = "m"
        blocks.append({"ch": ch, "p": p, "mode": mode, "size": size, "figs": figs,
                       "why": str(b.get("why") or "")[:40],
                       "score": sum(c["final"] for c, _ in figs) / len(figs)})
        for c, _ in figs:
            used.add(c["id"])
            used_subj.append(c)

    # 每章上限 + 间距 + 全文上限:按质量保留
    kept = []
    for b in sorted(blocks, key=lambda b: -b["score"]):
        same = [x for x in kept if x["ch"]["i"] == b["ch"]["i"]]
        if len(same) >= per_ch.get(b["ch"]["i"], 1):
            warn.append(f"{b['p']['id']} 超出该章上限,丢弃")
            continue
        if any(abs(x["p"]["k"] - b["p"]["k"]) < MIN_GAP for x in same):
            warn.append(f"{b['p']['id']} 与同章图块太近,丢弃")
            continue
        if len(kept) >= max_blocks:
            warn.append(f"{b['p']['id']} 超出全文上限,丢弃")
            continue
        kept.append(b)
    kept.sort(key=lambda b: (b["ch"]["i"], b["p"]["k"]))
    # 同章连续两个右浮 → 后一个改左浮,形成交替
    for a, b in zip(kept, kept[1:]):
        if a["ch"]["i"] == b["ch"]["i"] and a["mode"] == b["mode"] == "side":
            b["mode"] = "side-left"
    return hero, kept, warn


# ── 产出 figures ─────────────────────────────────────────────────────────
def _fig(cand: dict, caption: str, prefix: str = "") -> dict:
    c = cand["c"]
    return {"src": prefix + c["file"], "caption": caption, "vlm_caption": cand["caption"],
            "site": c.get("site", ""), "page": c.get("page", ""), "w": cand["w"], "h": cand["h"],
            "subject": cand["subject"],
            "anchor": " ".join(filter(None, [cand["caption"], cand["depicts"], cand["subject"]]))}


def to_figures(hero, blocks, model: str, warn: list[str], status: str = "llm") -> dict:
    out_blocks, sections = [], {}
    for b in blocks:
        figs = [_fig(c, cap) for c, cap in b["figs"]]
        out_blocks.append({"chapter": b["ch"]["title"], "para": b["p"]["k"], "pid": b["p"]["id"],
                           "mode": b["mode"], "size": b["size"], "figs": figs, "why": b["why"]})
        prev = sections.get(b["ch"]["title"])
        sections[b["ch"]["title"]] = figs[0] if prev is None and len(figs) == 1 else (
            ([prev] if isinstance(prev, dict) else (prev or [])) + figs)
    return {"hero": _fig(hero["cand"], hero["caption"]) if hero else None, "sections": sections,
            "blocks": out_blocks, "planner": {"status": status, "model": model, "warnings": warn}}


@ls.traceable(name="plan_layout")
def plan_layout(title: str, report_md: str, slots: list[dict], prof: dict, type_label: str,
                accept: float, llm, model: str = "", out_dir: str | None = None) -> dict | None:
    """主入口。llm(prompt)->str。返回 figures(带 blocks);失败返回 None(调用方退回规则排版)。"""
    _, chapters = paragraph_index(report_md)
    cands = collect_candidates(slots, accept, title, chapters)
    if not cands:
        return {"hero": None, "sections": {}, "blocks": [],
                "planner": {"status": "no_candidates", "model": model, "warnings": []}}
    max_blocks, per_ch = budget(chapters, prof, len(cands))
    _, skill = load_skill()
    brief = build_brief(title, type_label, prof, chapters, cands, max_blocks, per_ch)
    prompt = skill + "\n\n---\n\n" + brief
    plan, raw = None, ""
    for attempt in range(2):
        try:
            raw = llm(prompt if attempt == 0 else prompt + "\n\n上次输出不是合法 JSON。只输出 JSON 对象。")
        except Exception as e:  # noqa: BLE001 - 版面规划失败退回规则排版
            print(f"  ⚠️ 版面规划 LLM 失败: {type(e).__name__}: {e}", flush=True)
            return None
        plan = _parse_obj(raw)
        if plan is not None:
            break
    if out_dir:
        with open(os.path.join(out_dir, "layout_plan.json"), "w", encoding="utf-8") as f:
            json.dump({"brief": brief, "raw": raw, "plan": plan}, f, ensure_ascii=False, indent=1)
    if plan is None:
        print("  ⚠️ 版面规划输出无法解析,退回规则排版", flush=True)
        return None
    hero, blocks, warn = validate(plan, cands, chapters, prof, max_blocks, per_ch)
    for w in warn:
        print(f"  · {w}", flush=True)
    if not blocks and not hero:
        return None
    return to_figures(hero, blocks, model, warn)


if __name__ == "__main__":
    # 离线重排:用 figures_detail.json 里的旧打分,只重跑版面规划(LLM),再渲染 index.html。
    if len(sys.argv) < 2:
        sys.exit("用法: layout_planner.py runs/<dir>")
    from . import figures as FIG

    run_dir = sys.argv[1] if os.path.isabs(sys.argv[1]) else os.path.join(HERE, sys.argv[1])
    FIG.main_relayout(run_dir, plan=True)

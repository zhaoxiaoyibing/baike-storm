"""配图(正式链路 + figure_probe 共用):VLM 打分 / 排版预算 / 从 SourceLib 取绑定候选。

正式链路(storm_pipeline 成文后调用 illustrate):
  1. 候选:每节正文引用到的 [n] 来源自带的 page_images(图跟着来源走,图注可直接链 [n]);
     某槽位绑定候选不足 FIG_MIN_BOUND 张时,再补检索「词条 节名」。主图槽用导语引用源。
  2. 规则粗筛:短边 ≥ MIN_SIDE、宽高比 0.5–2.2、跨槽位按图片 id 去重,每槽保留前 PER_SLOT 张。
  3. VLM 打分(任选支持图片输入的 OpenAI 兼容端点,配置 FIG_VLM_BASE_URL/FIG_VLM_MODEL/FIG_VLM_API_KEY)。
  4. pick_layout:1 张主图 + 节图 ≤ ceil(节数/2.5),相邻节不同时配图,同一画面主题只用一次,低于阈值宁缺。
  产物:out_dir/img/*、figures.json(给 render_markdown 的 figures)、figures_detail.json(全部候选与打分)。
  图片不写进 markdown,所以 score / entity_recall 不受影响。
"""

import base64
from contextvars import copy_context
from . import tracing as ls
import json
import math
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

from .render_baike_html import extract_preamble, parse_report

# 图片下载代理(如公司出口代理);默认直连。
FIG_PROXY = os.environ.get("FIG_PROXY") or None
# VLM 后端:openai = 任意 OpenAI 兼容支持图片的 chat/completions(默认,需 FIG_VLM_API_KEY);
# anthropic = 任意 Anthropic /v1/messages 兼容端点(需 JUDGE_API_KEY 等 token)。
# 端点与模型名都不写默认值:参考 .env.example 配置。
VLM_BACKEND = os.environ.get("FIG_VLM_BACKEND", "openai").strip().lower()
VLM_MODEL = os.environ.get("FIG_VLM_MODEL", "").strip()
VLM_BASE_URL = (os.environ.get("FIG_VLM_BASE_URL") or "").strip()
# gemini flash 是推理型,先吃思考 token,400 会截断输出。
VLM_MAX_TOKENS = int(os.environ.get("FIG_VLM_MAX_TOKENS", "4000"))
MIN_SIDE = int(os.environ.get("FIG_MIN_SIDE", "400"))
PER_SLOT = int(os.environ.get("FIG_PER_SLOT", "8"))
MIN_BOUND = int(os.environ.get("FIG_MIN_BOUND", "3"))
WORKERS = int(os.environ.get("FIG_WORKERS", "8"))
ACCEPT = float(os.environ.get("FIG_ACCEPT", "6"))

HARD_DEFECTS = {"screenshot", "text_heavy", "collage", "off_topic", "ad"}
DEFECT_PENALTY = {"watermark": 1.5, "low_quality": 2.0}
CRED_RANK = {"high": 0, "medium": 1, "low": 2}
SKIP_SECTIONS = ("参考资料", "概述", "参见", "外部链接", "延伸阅读")

# ── 版式 profile:按词条类型组(entry_types.type_group)定主图偏好 / 配图密度 / 缺陷容忍度 ──
# hero:   主图偏好(喂给 VLM 判 hero_ok;None = 该类不配主图,如抽象概念)
# ratio:  节图预算 = ceil(章数 / ratio),越小配得越多
# soft:   该类里不算硬伤、只扣分的缺陷(如软件的界面截图、作品的海报)
# gallery:允许一章放 2 张不同画面的横排组图(产品/地点/生物等"看图识物"的类型)
_BASE_SOFT = {"screenshot": 2.5}  # 截图默认只扣分:新闻配图常带字幕条/界面边,一刀切会误杀大半
PROFILES = {
    "person":   {"hero": "本人清晰的单人肖像或工作照(正脸、主体完整;合影、背影不算)", "ratio": 3.5, "soft": {}, "gallery": False},
    "org":      {"hero": "机构标志性建筑/总部外景、或官方标志(logo)", "ratio": 3.0, "soft": {}, "gallery": False},
    "product":  {"hero": "产品整体外观照或官方渲染图(主体完整、背景干净)", "ratio": 2.0, "soft": {}, "gallery": True},
    "software": {"hero": "软件主界面截图或官方标志(logo)", "ratio": 3.0,
                 "soft": {"screenshot": 0.0, "text_heavy": 2.0}, "gallery": False},
    "work":     {"hero": "作品官方海报、封面或代表性剧照", "ratio": 3.0,
                 "soft": {"text_heavy": 1.0, "screenshot": 1.0}, "gallery": False},
    "event":    {"hero": "事件现场的纪实照片(能看出时间地点特征)", "ratio": 2.5, "soft": {}, "gallery": False},
    "place":    {"hero": "全景或标志性外观照(航拍、远景优先)", "ratio": 2.0, "soft": {}, "gallery": True},
    "organism": {"hero": "物种个体清晰的生态照或标本照", "ratio": 3.0, "soft": {}, "gallery": True},
    "medical":  {"hero": None, "ratio": 4.0, "soft": {"text_heavy": 2.0}, "gallery": False},
    "food":     {"hero": "成品的清晰实拍照", "ratio": 3.0, "soft": {}, "gallery": True},
    "concept":  {"hero": None, "ratio": 4.0, "soft": {"text_heavy": 2.0}, "gallery": False},
    "space":    {"hero": "航天器/火箭本体照片或官方渲染图(构型示意图也可)", "ratio": 2.5, "soft": {}, "gallery": False},
    "generic":  {"hero": "能直观展示词条本体的真实照片或官方图", "ratio": 3.0, "soft": {}, "gallery": False},
}
# 组图第二张需更高分,避免凑数
GALLERY_BONUS = 1.0
# 通用本体照补位:fit 低(放哪节都行)但 entity 很高时,用这条以本体为主的分数线(见 _final_score)
GENERIC_W_FIT, GENERIC_W_ENT, GENERIC_OFFSET = 0.4, 0.6, 0.6


def profile_for(title: str, dalei: str | None) -> tuple[str, dict]:
    from .entry_types import type_group

    g = type_group(title, dalei)
    return g, PROFILES.get(g, PROFILES["generic"])
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
VLM_PROMPT = """你是百科词条的图片编辑。判断这张候选图能否用作百科词条「{title}」({type_label}类)中的配图。
槽位:{slot_desc}
该节内容摘要:{snippet}
图片来源网页:{site} 《{page_title}》

只依据图片里实际可见的内容判断,输出一个 JSON 对象(不要任何其他文字):
{{
  "depicts": "图中实际是什么(一句话,客观描述)",
  "subject": "画面主题的 2-6 字短标签,同类画面用同一标签(如 外观、内饰座舱、发布会、总装厂房、月面着陆、构型示意、样品、发射)",
  "entity": 0-10 的整数(画面主体是不是「{title}」本体或其直接组成部分;10=确定是,5=同类但无法确认,0=不是),
  "section_fit": 0-10 的整数(这张图是否表现了**该节特有**的内容,读者看到能联想到这一节;
     只是词条的通用照片、放到任何一节都一样 → 不超过 4;该节是抽象内容(意义/评价/相关工程)而图无法具体表现 → 不超过 3),
  "defects": 从下列中选出所有存在的问题,没有则 []:
     "watermark"(明显水印、台标、账号名), "screenshot"(网页/APP/视频截图,含界面元素或字幕条),
     "text_heavy"(海报/信息图/PPT,大字占画面主体), "collage"(多图拼接), "low_quality"(模糊、过暗、压缩严重),
     "off_topic"(与词条或该节无关), "ad"(广告/营销物料),
  "hero_ok": true/false(适合做词条主图:{hero_hint};主体完整清晰、无明显问题),
  "caption": "百科风格客观图注,≤30 字,只写图中可见内容,不编造图里看不出的数字/日期"
}}"""


def vlm_ready() -> tuple[bool, str]:
    """当前 VLM 配置能否调用(缺配置时正式链路跳过配图,而不是报错中断)。

    本地服务(127.0.0.1/localhost)可免 key;否则需显式配置 FIG_VLM_BASE_URL/FIG_VLM_MODEL/FIG_VLM_API_KEY。
    """
    if not VLM_BASE_URL or not VLM_MODEL:
        return False, "未配置 FIG_VLM_BASE_URL / FIG_VLM_MODEL"
    if VLM_BACKEND == "openai" and not any(h in VLM_BASE_URL for h in ("127.0.0.1", "localhost")) \
            and not os.environ.get("FIG_VLM_API_KEY"):
        return False, f"缺 FIG_VLM_API_KEY({VLM_BASE_URL} 需要 key)"
    return True, VLM_MODEL


# ── 候选 ─────────────────────────────────────────────────────────────────
def _image_key(url: str) -> str:
    """图床同一张图的稳定 id(u=xxx,yyy);取不到退回整串。"""
    m = re.search(r"[?&]u=([^&]+)", url)
    return m.group(1) if m else url


def _size_ok(w: int, h: int) -> bool:
    return min(w, h) >= MIN_SIDE and 0.5 <= w / max(h, 1) <= 2.2


@ls.traceable(name="image_search", run_type="tool")
def search_with_images(query: str) -> list[dict]:
    """走统一搜索后端,但保留页面图片(按检索排序返回)。"""
    from . import search

    results = search.search(query, top_k=40, with_images=True)
    out = []
    for rank, r in enumerate(results):
        if not r.get("images"):
            continue
        out.append({"rank": rank, "site": r["name"], "page": r["url"], "page_title": r.get("title", ""),
                    "credibility": r.get("credibility", ""), "images": r["images"]})
    return out


def clean_snippet(text: str, n: int = 220) -> str:
    text = re.sub(r"\[\d+\]", "", text)
    text = re.sub(r"[#*>|`-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:n]
# ── 下载 + VLM 打分 ──────────────────────────────────────────────────────
_dl = httpx.Client(proxy=FIG_PROXY, headers=UA, timeout=15, follow_redirects=True)
_vlm_http = httpx.Client(trust_env=False, timeout=120)
_vlm = None
_EXT_CT = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


def _vlm_client():
    global _vlm
    if _vlm is None:
        from anthropic import Anthropic
        token = next((os.environ[k] for k in ("JUDGE_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                                               "ANTHROPIC_API_KEY") if os.environ.get(k)), None)
        if not token:
            raise RuntimeError("缺 VLM token:需要 JUDGE_API_KEY / ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY")
        _vlm = Anthropic(base_url=VLM_BASE_URL, api_key=token,
                         http_client=httpx.Client(trust_env=False, timeout=120))
    return _vlm


def _parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, flags=re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        return {}


@ls.traceable(name="figure_vlm", run_type="llm",
           process_inputs=lambda inputs: {k: v for k, v in inputs.items() if k != "img"})
def _vlm_ask(prompt: str, img: bytes, media_type: str) -> str:
    b64 = base64.b64encode(img).decode()
    if VLM_BACKEND == "openai":
        for attempt in range(6):  # 共享网关会 429 限流 → 退避重试
            r = _vlm_http.post(f"{VLM_BASE_URL.rstrip('/')}/chat/completions", json={
                "model": VLM_MODEL, "temperature": 0, "max_tokens": VLM_MAX_TOKENS,
                "messages": [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}},
                    {"type": "text", "text": prompt}]}]},
                headers={"Authorization": f"Bearer {os.environ.get('FIG_VLM_API_KEY', 'EMPTY')}"})
            if r.status_code not in (429, 500, 502, 503, 529) or attempt == 5:
                break
            time.sleep(2 * 2 ** attempt + random.random())
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"] or ""
    resp = _vlm_client().messages.create(
        model=VLM_MODEL, max_tokens=VLM_MAX_TOKENS, temperature=0,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
            {"type": "text", "text": prompt}]}])
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
@ls.traceable(name="score_image_candidate")
def score_candidate(title: str, slot: dict, c: dict, img_dir: str, idx: int, cached: str | None = None) -> None:
    """下载 → 存 img_dir → VLM 打分,结果写回 c。cached = 已下载好的本地图片(换 VLM 重打分时复用)。"""
    if cached:
        with open(cached, "rb") as f:
            content = f.read()
        ct = _EXT_CT[cached.rsplit(".", 1)[-1]]
    else:
        try:
            r = _dl.get(c["img"])
            ct = r.headers.get("content-type", "").split(";")[0].strip()
            if r.status_code != 200 or ct not in (_EXT_CT.values()) or len(r.content) < 5000:
                c.update(error=f"download {r.status_code} {ct} {len(r.content)}B", final=-1)
                return
        except Exception as e:  # noqa: BLE001
            c.update(error=f"download {type(e).__name__}", final=-1)
            return
        content = r.content
    ext = {v: k for k, v in _EXT_CT.items()}[ct]
    fname = f"{slot['slot']}_{idx}.{ext}"
    with open(os.path.join(img_dir, fname), "wb") as f:
        f.write(content)
    c["file"] = f"img/{fname}"

    prof = slot.get("prof") or PROFILES["generic"]
    prompt = VLM_PROMPT.format(title=title, slot_desc=slot["slot_desc"], snippet=slot["snippet"] or "(无)",
                               site=c["site"], page_title=c["page_title"],
                               type_label=slot.get("type_label") or "综合",
                               hero_hint=prof.get("hero") or "本类词条不设主图,一律 false")
    try:
        v = _parse_json(_vlm_ask(prompt, content, ct))
    except Exception as e:  # noqa: BLE001
        c.update(error=f"vlm {type(e).__name__}: {str(e)[:80]}", final=-1)
        return
    defects = [d for d in (v.get("defects") or []) if isinstance(d, str)]
    try:
        entity, fit = float(v.get("entity") or 0), float(v.get("section_fit") or 0)
    except (TypeError, ValueError):
        entity, fit = 0.0, 0.0
    c.update(vlm=v, defects=defects, relevance=fit, entity=entity, subject=(v.get("subject") or "").strip(),
             caption=v.get("caption", ""), depicts=v.get("depicts", ""), hero_ok=bool(v.get("hero_ok")))
    _apply_score(c, prof)


def _penalty(defects: list[str], prof: dict) -> float:
    """硬伤 -10;软缺陷按 DEFECT_PENALTY + 截图默认扣分 + 该类 profile 的 soft 覆盖。"""
    pen = {**DEFECT_PENALTY, **_BASE_SOFT, **(prof.get("soft") or {})}
    hard = HARD_DEFECTS - set(pen)
    return sum(pen.get(d, 0) for d in defects) + (10 if hard & set(defects) else 0)


def _final_score(fit: float, entity: float, penalty: float) -> float:
    """节图分 = 两条线取高:
    - 切节线 0.7·fit + 0.3·entity:表现该节特有内容的图;
    - 本体线 0.4·fit + 0.6·entity − 0.6:高质量的词条本体照(fit 被提示词压到 ≤4),给它补位的机会。
    旧版只有切节线,本体照最高 0.7·4+0.3·10=5.8 < 阈值 6,永远过不了。
    """
    return max(0.7 * fit + 0.3 * entity, GENERIC_W_FIT * fit + GENERIC_W_ENT * entity - GENERIC_OFFSET) - penalty


def _apply_score(c: dict, prof: dict) -> None:
    if "vlm" not in c:
        return
    penalty = _penalty(c.get("defects") or [], prof)
    final = _final_score(c.get("relevance", 0), c.get("entity", 0), penalty)
    hero_ok = c.get("hero_ok") and prof.get("hero")
    hero_score = c.get("entity", 0) - penalty if hero_ok else -1
    c.update(penalty=penalty, final=round(final, 1), hero_score=round(hero_score, 1),
             role="hero" if hero_ok else ("section" if final >= ACCEPT else "none"))


# ── 排版预算 ─────────────────────────────────────────────────────────────
_STOP_BG = set("的了和与及或在是对为之其该等这那被把图中")


def _bigrams(s: str) -> set[str]:
    s = re.sub(r"[\s\W_]+", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)} - {a + b for a in _STOP_BG for b in _STOP_BG}


def _relocate(sec_slots: list[dict]) -> None:
    """图跟着来源走,会被分到"引用它的那一章",但画面可能更贴另一章(如返回器开舱照落在「任务概况」)。
    对无硬伤、本体分高的图,用图注/画面描述与各章正文的 bigram 重叠找最贴的章;明显更贴就改放过去。
    写回 c["place"](章下标)与 c["place_score"](按改放后估计的 fit 重算的分)。
    """
    slot_bg = [_bigrams(s.get("text") or s.get("snippet") or "") for s in sec_slots]
    for i, s in enumerate(sec_slots):
        for c in s["cands"]:
            c["place"], c["place_score"] = i, c.get("final", -1)
            if c.get("picked") or "vlm" not in c or c.get("penalty", 10) >= 10 or c.get("entity", 0) < 7:
                continue
            anchor = _bigrams(" ".join([c.get("caption", ""), c.get("depicts", ""), c.get("subject", "")]))
            if len(anchor) < 4:
                continue
            ov = [len(anchor & bg) / len(anchor) for bg in slot_bg]
            j = max(range(len(ov)), key=lambda k: ov[k])
            if j != i and ov[j] >= 0.3 and ov[j] - ov[i] >= 0.15:
                fit_est = min(8.0, 3.0 + 10 * ov[j])  # 正文里能找到图上画的东西 → 视作较好的切节
                alt = _final_score(fit_est, c["entity"], c["penalty"])
                if alt > c["place_score"]:
                    c["place"], c["place_score"], c["relocated"] = j, round(alt, 1), sec_slots[j]["label"]


def pick_layout(slots: list[dict], prof: dict | None = None) -> None:
    """1 张主图 + 节图不超过 ceil(章数/ratio);同一画面主题(subject)只用一次;低于阈值宁缺。

    slots[0] 是主图槽,其余是节槽。主图从所有槽位的候选里挑(hero_ok 且本体分最高;profile.hero=None 不配主图)。
    profile.gallery=True 的类型允许最多 2 章各放第二张(不同画面)做横排组图。
    """
    prof = prof or PROFILES["generic"]
    used_keys, used_subjects = set(), set()

    def take(c, label):
        c["picked"], c["picked_as"] = True, label
        used_keys.add(c["key"])
        # 组图类(产品/地点等)主图不占 subject:「外观/设计」章再放一张外观照不算重复;人物再放一张肖像就重复了
        if c.get("subject") and not (label == "主图" and prof.get("gallery")):
            used_subjects.add(c["subject"])

    def same_subject(a, b):  # 「样品」≈「月球样品」、「演讲现场」≈「现场演讲」
        if a in b or b in a:
            return True
        ga, gb = _bigrams(a), _bigrams(b)
        return bool(ga and gb) and len(ga & gb) / min(len(ga), len(gb)) >= 0.5

    def free(c, bonus=0.0):
        subj = c.get("subject") or ""
        dup = subj and any(same_subject(subj, u) for u in used_subjects)
        return c.get("place_score", -1) >= ACCEPT + bonus and c["key"] not in used_keys and not dup

    pool = [c for s in slots for c in s["cands"]]
    if prof.get("hero"):
        hero = max((c for c in pool if c.get("hero_score", -1) >= ACCEPT), key=lambda c: c["hero_score"], default=None)
        if hero:
            take(hero, "主图")

    sec_slots = slots[1:]
    _relocate(sec_slots)
    sec_pool = [c for s in sec_slots for c in s["cands"]]
    budget = math.ceil(len(sec_slots) / prof.get("ratio", 3.0))
    chosen = set()
    while len(chosen) < budget:
        best = max((c for c in sec_pool if c["place"] not in chosen and free(c)),
                   key=lambda c: c["place_score"], default=None)
        if best is None:
            break
        take(best, sec_slots[best["place"]]["label"])
        chosen.add(best["place"])

    if prof.get("gallery"):
        extras = 0
        for i in sorted(chosen):
            second = max((c for c in sec_pool if c["place"] == i and free(c, GALLERY_BONUS)),
                         key=lambda c: c["place_score"], default=None)
            if second and extras < 2:
                take(second, sec_slots[i]["label"])
                extras += 1


def figures_from_slots(slots: list[dict], prefix: str = "") -> dict:
    """入选图 → render_markdown 的 figures 参数。prefix 加在 img/ 相对路径前(web iframe 用)。

    sections[章名] 是单张 fig;组图(gallery)时是 [fig, fig]。
    fig["anchor"] = 图注 + 画面描述,渲染时据此把图放到最相关的段落旁。
    """
    figures = {"hero": None, "sections": {}}
    for s in slots:
        for c in sorted(s["cands"], key=lambda c: -c.get("place_score", c.get("final", -1))):
            if not c.get("picked"):
                continue
            fig = {k: c[k] for k in ("site", "page", "w", "h", "caption")}
            fig["src"] = prefix + c["file"]
            fig["anchor"] = " ".join(filter(None, [c.get("caption", ""), c.get("depicts", ""), c.get("subject", "")]))
            if c["picked_as"] == "主图":
                figures["hero"] = fig
            elif c["picked_as"] in figures["sections"]:
                prev = figures["sections"][c["picked_as"]]
                figures["sections"][c["picked_as"]] = (prev if isinstance(prev, list) else [prev]) + [fig]
            else:
                figures["sections"][c["picked_as"]] = fig
    return figures


def prefix_figures(figures: dict, prefix: str) -> dict:
    """给已有 figures 的图片路径统一加前缀(不改原对象)。"""
    def p(f):
        if isinstance(f, list):
            return [p(x) for x in f]
        return {**f, "src": prefix + f["src"]} if f else f
    out = {**figures, "hero": p(figures.get("hero")),
           "sections": {k: p(v) for k, v in (figures.get("sections") or {}).items()}}
    if figures.get("blocks"):
        out["blocks"] = [{**b, "figs": p(b["figs"])} for b in figures["blocks"]]
    return out
# ── 正式链路:从 SourceLib 取绑定候选 → 打分 → 排版 ───────────────────────
def _ids(text: str) -> list[int]:
    """正文里的 [n] 引用,按首次出现保序去重。"""
    seen, out = set(), []
    for x in re.findall(r"\[(\d+)\]", text):
        n = int(x)
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _bound_cands(R, ids: list[int], seen: set) -> list[dict]:
    pool = []
    for order, sid in enumerate(ids):
        s = R.get(sid)
        for im in (s or {}).get("images") or []:
            u, w, h = im.get("image_url", ""), im.get("width") or 0, im.get("height") or 0
            k = _image_key(u)
            if not u or not _size_ok(w, h) or k in seen:
                continue
            seen.add(k)
            pool.append({"site": s["name"], "page": s["url"], "page_title": s.get("title", ""),
                         "credibility": s.get("credibility", ""), "rank": order, "src_id": sid,
                         "img": u, "w": w, "h": h, "key": k, "bound_section": True, "bound_any": True})
    pool.sort(key=lambda c: (CRED_RANK.get(c["credibility"], 3), c["rank"]))
    return pool


def _search_cands(query: str, seen: set, cited_urls: set) -> list[dict]:
    try:
        results = search_with_images(query)
    except Exception as e:  # noqa: BLE001 - 补检索失败只是少几张候选
        print(f"    ⚠️ 配图补检索失败 q={query!r}: {e}", flush=True)
        return []
    pool = []
    for res in results:
        for im in res["images"]:
            u, w, h = im.get("image_url", ""), im.get("width") or 0, im.get("height") or 0
            k = _image_key(u)
            if not u or not _size_ok(w, h) or k in seen:
                continue
            seen.add(k)
            pool.append({**{k2: res[k2] for k2 in ("site", "page", "page_title", "credibility", "rank")},
                         "img": u, "w": w, "h": h, "key": k,
                         "bound_section": False, "bound_any": res["page"] in cited_urls})
    pool.sort(key=lambda c: (not c["bound_any"], CRED_RANK.get(c["credibility"], 3), c["rank"]))
    return pool


def build_bound_slots(report_md: str, R, supplement: bool = True) -> tuple[str, list[dict]]:
    """主图槽 + 每节一槽;候选优先取该节引用源的图,不足 MIN_BOUND 张再补检索。"""
    title, sections, _ = parse_report(report_md)
    preamble = extract_preamble(report_md)
    cited_urls = {R.get(i)["url"] for i in _ids(report_md) if R.get(i)}
    sec_slots = []
    for sec, body in sections:
        if sec.strip() in SKIP_SECTIONS:
            continue
        sec_slots.append({"slot": f"sec{len(sec_slots) + 1}", "label": sec, "query": f"{title} {sec}",
                          "slot_desc": f"「{sec}」一节的插图", "snippet": clean_snippet(body), "ids": _ids(body),
                          "text": re.sub(r"\[\d+\]", "", body)})
    # 主图槽:导语引用源;导语没引用则取全文被引最多的几条源。
    hero_ids = _ids(preamble)
    if len(hero_ids) < 2:
        freq = {}
        for x in re.findall(r"\[(\d+)\]", report_md):
            freq[int(x)] = freq.get(int(x), 0) + 1
        hero_ids = sorted(freq, key=lambda n: -freq[n])[:6]
    hero = {"slot": "hero", "label": "主图", "query": title,
            "slot_desc": "词条主图(放在信息框顶部,应直观展示词条本体)",
            "snippet": clean_snippet(preamble), "ids": hero_ids}

    seen = set()
    for s in sec_slots + [hero]:  # 节槽先挑:同一张图优先落在引用它的节里;主图本就从全池选
        pool = _bound_cands(R, s["ids"], seen)
        n_bound = len(pool)
        if supplement and n_bound < MIN_BOUND:
            pool += _search_cands(s["query"], seen, cited_urls)
        s["n_bound"], s["n_pool"] = n_bound, len(pool)
        s["cands"] = pool[:PER_SLOT]
        # 没送审的候选退回去重集合,给后面的槽位用
        for c in pool[PER_SLOT:]:
            seen.discard(c["key"])
    return title, [hero] + sec_slots
def score_slots(title: str, slots: list[dict], img_dir: str, emit=None, cached: dict | None = None) -> None:
    """并发打分全部候选。emit(done, total) 在调用线程里回调(web 进度 / 取消检查都靠它)。

    cached: {id(c): 本地图片路径},换 VLM 重打分时复用已下载的图。
    """
    os.makedirs(img_dir, exist_ok=True)
    jobs = [(s, c, i) for s in slots for i, c in enumerate(s["cands"])
            if cached is None or cached.get(id(c))]
    ex = ThreadPoolExecutor(WORKERS)
    try:
        futs = [ex.submit(copy_context().run, score_candidate, title, s, c, img_dir, i,
                          (cached or {}).get(id(c))) for s, c, i in jobs]
        for done, future in enumerate(as_completed(futs), 1):
            future.result()
            if emit:
                emit(done, len(futs))
    finally:
        # 取消(emit 抛异常)时不等剩余图片打完
        ex.shutdown(wait=False, cancel_futures=True)


@ls.traceable(name="illustrate")
def illustrate(report_md: str, R, out_dir: str, emit=None, supplement: bool = True, dalei: str | None = None,
               llm=None, llm_model: str = "") -> dict:
    """给成文报告挑图。返回 figures(src 为相对 out_dir 的 img/...),并落 figures.json / figures_detail.json。

    emit(kind, **data):可选事件回调(storm_pipeline._emit)。
    dalei:词条大类,决定版式 profile(主图偏好 / 配图密度 / 缺陷容忍度 / 是否组图)。
    llm(prompt)->str:给了就用 skills/figure-layout 做版面规划(选主图 + 段落级锚点 + 嵌入方式/尺寸 + 图注);
      不给或规划失败 → 规则排版 pick_layout。
    """
    from .entry_types import GROUP_LABELS

    emit = emit or (lambda kind, **data: None)
    t0 = time.time()
    title, slots = build_bound_slots(report_md, R, supplement=supplement)
    group, prof = profile_for(title, dalei)
    for s in slots:
        s["prof"], s["type_label"] = prof, GROUP_LABELS.get(group, "综合")
    n_cands = sum(len(s["cands"]) for s in slots)
    print(f"🖼️  配图候选: {len(slots)} 槽 · 送审 {n_cands} 张 (绑定 {sum(s['n_bound'] for s in slots)} 张) "
          f"· 版式={group} · VLM={VLM_MODEL}", flush=True)
    emit("figures_candidates", slots=len(slots), candidates=n_cands, model=VLM_MODEL, profile=group)

    last = [0.0]

    def progress(done, total):
        if done == total or time.time() - last[0] > 2:  # 限频,免得刷屏
            last[0] = time.time()
            emit("figures_progress", done=done, total=total)

    score_slots(title, slots, os.path.join(out_dir, "img"), emit=progress)
    return _finish(title, slots, out_dir, prof, group, n_cands, t0, emit,
                   report_md=report_md, dalei=dalei, llm=llm, llm_model=llm_model)


def _picks_from_figures(figures: dict, slots: list[dict]) -> list[dict]:
    """figures → figures_done 事件里的 picks(web 日志卡片用)。"""
    by_file = {c["file"]: c for s in slots for c in s["cands"] if c.get("file")}
    out = []

    def add(fig, as_, mode=""):
        c = by_file.get(fig["src"], {})
        out.append({"as": as_, "caption": fig.get("caption", ""), "subject": fig.get("subject") or c.get("subject", ""),
                    "src": fig["src"], "bound": bool(c.get("bound_section")), "site": fig.get("site", ""),
                    "relocated": bool(c.get("relocated")), "mode": mode})

    if figures.get("hero"):
        add(figures["hero"], "主图")
    if figures.get("blocks"):
        for b in figures["blocks"]:
            for f in b["figs"]:
                add(f, b["chapter"], f"{b['mode']}/{b['size']}")
    else:
        for sec, v in (figures.get("sections") or {}).items():
            for f in (v if isinstance(v, list) else [v]):
                add(f, sec)
    return out


def _finish(title, slots, out_dir, prof, group, n_cands, t0, emit, report_md=None, dalei=None,
            llm=None, llm_model="") -> dict:
    """排版 → 落盘 figures.json / figures_detail.json → figures_done 事件。

    先跑规则排版 pick_layout(结果留在 detail 里做对照 / 兜底);有 llm 时再跑 skill 版面规划,成功则以它为准。
    """
    from .entry_types import GROUP_LABELS

    pick_layout(slots, prof)
    figures = figures_from_slots(slots)
    layout = "rule"
    if llm is not None and report_md:
        from . import layout_planner as LP

        emit("figures_layout", model=llm_model)
        t1 = time.time()
        planned = LP.plan_layout(title, report_md, slots, prof, dalei or GROUP_LABELS.get(group, "综合"),
                                 ACCEPT, llm, model=llm_model, out_dir=out_dir)
        if planned is not None:
            figures, layout = planned, "skill"
            print(f"  🧩 版面规划: {len(planned['blocks'])} 图块 · 主图={'有' if planned['hero'] else '无'} "
                  f"· {time.time() - t1:.0f}s", flush=True)
        else:
            print("  🧩 版面规划失败,沿用规则排版", flush=True)

    picks = _picks_from_figures(figures, slots)
    for p in picks:
        mv = " ↪改放" if p["relocated"] else ""
        print(f"  ★ {p['as'][:12]:<12} {p['mode']:<9} [{p['subject']}] {p['src']} {p['caption']}{mv}", flush=True)
    n_ok = sum(1 for s in slots for c in s["cands"] if c.get("final", -1) >= ACCEPT)
    stats = {"slots": len(slots), "candidates": n_cands, "accepted": n_ok, "picked": len(picks),
             "profile": group, "model": VLM_MODEL, "layout": layout, "seconds": round(time.time() - t0, 1)}

    detail = [{k: v for k, v in s.items() if k not in ("prof", "text")} for s in slots]
    with open(os.path.join(out_dir, "figures.json"), "w", encoding="utf-8") as f:
        json.dump(figures, f, ensure_ascii=False, indent=1)
    with open(os.path.join(out_dir, "figures_detail.json"), "w", encoding="utf-8") as f:
        json.dump({"title": title, "stats": stats, "accept": ACCEPT, "profile": group, "slots": detail},
                  f, ensure_ascii=False, indent=1)
    emit("figures_done", picks=picks, **stats)
    return figures


def relayout(run_dir: str, dalei: str | None = None, llm=None, llm_model: str = "") -> dict:
    """不重新下载/调 VLM:用 figures_detail.json 里的旧打分 + 新 profile 重算分数与排版(调参用)。

    llm 给了就同时重跑 skill 版面规划。
    """
    with open(os.path.join(run_dir, "figures_detail.json"), encoding="utf-8") as f:
        d = json.load(f)
    with open(os.path.join(run_dir, "final_report.md"), encoding="utf-8") as f:
        md = f.read()
    title, slots = d["title"], d["slots"]
    _, sections, _ = parse_report(md)
    body_of = {sec: re.sub(r"\[\d+\]", "", body) for sec, body in sections}
    group, prof = profile_for(title, dalei)
    for s in slots:
        s["prof"], s["text"] = prof, body_of.get(s["label"], "")
        for c in s["cands"]:
            c.pop("picked", None), c.pop("picked_as", None), c.pop("relocated", None)
            v = c.get("vlm") or {}
            for k in ("caption", "depicts"):  # 旧版 detail 只在 vlm 里有
                c.setdefault(k, v.get(k, ""))
            c.setdefault("hero_ok", bool(v.get("hero_ok")))
            _apply_score(c, prof)
    n_cands = sum(len(s["cands"]) for s in slots)
    return _finish(title, slots, run_dir, prof, group, n_cands, time.time(), lambda *a, **k: None,
                   report_md=md, dalei=dalei, llm=llm, llm_model=llm_model)


class _SavedSources:
    """storm_pipeline 落盘的 sources.json → 与 SourceLib 同接口的只读 get(sid)。"""

    def __init__(self, sources: list[dict]):
        self._by_id = {s["id"]: s for s in sources}

    def get(self, sid: int) -> dict | None:
        return self._by_id.get(sid)


def _route_dalei(run_dir: str) -> str | None:
    route = os.path.join(run_dir, "route.txt")
    if not os.path.exists(route):
        return None
    with open(route, encoding="utf-8") as f:
        m = re.search(r"大类[「\"]?([^」\"()\n]+)", f.read())
    return m.group(1).strip() if m else None


def _pipeline_llm():
    """复用流水线的 LLM 客户端(仅当需要 skill 版面规划时)。返回 (llm, model_name)。"""
    from . import storm_pipeline as SP

    return SP._llm, SP.pipeline_client().model_name


def main_relayout(run_dir: str, plan: bool = False) -> dict:
    """离线重排 + 渲染 index.html。plan=True 时重跑 skill 版面规划(调 LLM,不调 VLM)。"""
    llm, name = _pipeline_llm() if plan else (None, "")
    figs = relayout(run_dir, _route_dalei(run_dir), llm=llm, llm_model=name)
    from .render_baike_html import render

    render(run_dir)
    return figs


if __name__ == "__main__":
    # 离线重配图:python figures.py runs/嫦娥六号_storm_web                 (需 final_report.md + sources.json,调 VLM)
    #           python figures.py runs/嫦娥六号_storm_web --relayout        (只用旧打分按新 profile 重排,不调 VLM)
    #           python figures.py runs/嫦娥六号_storm_web --relayout --plan (再加 skill 版面规划,调 LLM)
    #  加 --plan 时首次配图也会做版面规划。
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    run_dir = os.path.abspath(args[0]) if args else sys.exit("用法: figures.py <run_dir> [--relayout] [--plan]")
    want_plan = "--plan" in sys.argv
    if "--relayout" in sys.argv:
        main_relayout(run_dir, plan=want_plan)
        sys.exit(0)
    ok, why = vlm_ready()
    if not ok:
        sys.exit(why)
    with open(os.path.join(run_dir, "final_report.md"), encoding="utf-8") as f:
        md = f.read()
    with open(os.path.join(run_dir, "sources.json"), encoding="utf-8") as f:
        R = _SavedSources(json.load(f))
    llm, name = _pipeline_llm() if want_plan else (None, "")
    illustrate(md, R, run_dir, dalei=_route_dalei(run_dir), llm=llm, llm_model=name)
    from .render_baike_html import render

    render(run_dir)

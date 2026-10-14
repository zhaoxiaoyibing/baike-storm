"""用 LLM 评审报告，并计算关键事实召回率。

参考答案仅供评审使用，不得进入生成端的检索链路。
"""

import json
import os
import re
import sys

from . import config

# 评测无需轨迹，关闭 tracing 以避免上报超时。
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"

GOLD_DIR = str(config.GOLD_DIR)
BAIKE_DIR = str(config.PROJECT_ROOT)

JUDGE_BACKEND = os.environ.get("JUDGE_BACKEND", "openai").strip().lower()


def _env_any(*names: str) -> str | None:
    for k in names:
        v = os.environ.get(k, "").strip()
        if v:
            return v
    return None


def _anthropic_token() -> str:
    token = _env_any("JUDGE_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
    if not token:
        raise RuntimeError(
            "Anthropic judge 缺 token:请设置 JUDGE_API_KEY 或 ANTHROPIC_API_KEY"
            "(也可用 JUDGE_BACKEND=openai 走 OpenAI 兼容端点)。"
        )
    return token

# 保持评分维度和权重稳定，便于比较不同版本。
DIMENSIONS = [
    ("fact_coverage", "事实覆盖度", 0.30,
     "待评报告覆盖了参考答案里多少**关键事实点**(定义/时间/数字/参与方/结果)。"
     "覆盖越全越高;漏掉核心事实要显著扣分。"),
    ("fact_accuracy", "事实准确性", 0.30,
     "待评报告陈述的事实与参考答案是否**一致、无捏造**。出现与参考答案矛盾的硬错误(错时间/错数字/"
     "错关系)或明显臆造内容要重扣;参考答案未提及但合理且不矛盾的补充不扣分。"),
    ("structure", "结构完整度", 0.15,
     "是否有百科式的清晰分节(概述 + 若干主题小节),组织是否完整、条理清楚、无重复堆砌。"),
    ("citation", "引用与来源质量", 0.15,
     "是否有行内引用/来源标注,来源是否权威可信(官方/主流媒体/学术),引用是否与所述内容对应。"
     "完全无引用给低分。"),
    ("readability", "可读性", 0.10,
     "语言是否通顺、客观、百科口吻(无第一人称、无口语化、无'我搜到')、无明显病句与冗余。"),
]

_WEIGHTS = {k: w for k, _, w, _ in DIMENSIONS}


_llm = None


def _get_llm():
    """懒加载评审模型，配置从环境变量读取。"""
    global _llm
    if _llm is not None:
        return _llm
    if JUDGE_BACKEND == "anthropic":
        from langchain_anthropic import ChatAnthropic

        base_url = _env_any("JUDGE_BASE_URL", "ANTHROPIC_BASE_URL")
        model = _env_any("JUDGE_MODEL", "ANTHROPIC_MODEL")
        if not base_url or not model:
            raise RuntimeError(
                "Anthropic judge 未配置:请设置 JUDGE_BASE_URL(或 ANTHROPIC_BASE_URL)与 JUDGE_MODEL。"
            )
        _llm = ChatAnthropic(
            model=model,
            base_url=base_url,
            api_key=_anthropic_token(),
            temperature=0.0,
            max_tokens=4096,
            timeout=180,
        )
    else:
        from .llm import LLMClient

        base_url = _env_any("JUDGE_BASE_URL", "OPENAI_BASE_URL")
        api_key = _env_any("JUDGE_API_KEY", "OPENAI_API_KEY")
        if not base_url or not api_key:
            raise RuntimeError(
                "OpenAI judge 未配置:请设置 JUDGE_BASE_URL(或 OPENAI_BASE_URL)与 JUDGE_API_KEY"
                "(或 OPENAI_API_KEY);也可用 JUDGE_BACKEND=anthropic。"
            )
        _llm = LLMClient(
            model=_env_any("JUDGE_MODEL", "RESEARCH_MODEL") or "gpt-4o-mini",
            base_url=base_url,
            api_key=api_key,
            temperature=0.0,
            max_tokens=4096,
        )
    return _llm


def load_gold(title: str) -> dict:
    """按文件名加载参考答案，未找到时按 title 字段匹配。"""
    path = os.path.join(GOLD_DIR, f"{title}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    for fn in os.listdir(GOLD_DIR):
        if not fn.endswith(".json"):
            continue
        with open(os.path.join(GOLD_DIR, fn), encoding="utf-8") as f:
            d = json.load(f)
        if d.get("title") == title:
            return d
    raise FileNotFoundError(f"找不到 gold:{title}(在 {GOLD_DIR})")


def _build_rubric() -> str:
    lines = []
    for key, cn, w, desc in DIMENSIONS:
        lines.append(f"- {key}({cn},权重 {w:.0%},0-100):{desc}")
    return "\n".join(lines)


JUDGE_PROMPT = """你是一名严格、稳定的评审。任务:给一篇"从零重建的百科词条报告"打分。

评分方式:把【待评报告】和【参考答案】逐维度对照。参考答案是同一词条的**百度百科原文**,
视为事实基准(ground truth)。**只有【待评报告】是被评价对象**;参考答案仅用于对照,不要给参考答案打分。

评分维度(每个维度独立打 0-100 的整数,评分口径固定,不要放水也不要吹毛求疵):
{rubric}

打分锚点(所有维度通用):90-100=优秀,几乎无可挑剔;75-89=良好,小瑕疵;60-74=合格,有明显不足;
40-59=偏弱,缺失/错误较多;0-39=差,大量缺失或错误。

词条标题:《{title}》

【参考答案(百度百科原文,事实基准)】
{gold_full_text}

【待评报告(agent 生成,被评价对象)】
{report}

只输出一个 JSON 对象,不要任何解释文字、不要 markdown 代码块围栏。格式:
{{
  "dims": {{ {dim_keys} }},
  "dim_rationale": {{ {dim_keys_r} }},
  "rationale": "两三句话总体评价:最主要的优点和最主要的问题"
}}
其中 dims 的每个值是 0-100 的整数,dim_rationale 的每个值是一句话简短理由。"""


def _content_text(resp) -> str:
    """兼容字符串和内容块列表，将模型响应转成文本。"""
    content = getattr(resp, "content", resp)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text") or block.get("content") or "")
            else:
                parts.append(str(block))
        return "".join(parts)
    return str(content)


def _parse_json_obj(text: str) -> dict:
    """去掉模型输出的围栏和外围文本，解析 JSON 对象。"""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    l, r = text.find("{"), text.rfind("}")
    if l != -1 and r != -1 and r > l:
        text = text[l : r + 1]
    return json.loads(text)


def _clamp(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(100.0, f))


def score_report(title: str, report_md: str, gold: dict) -> dict:
    """对照参考答案评审报告。

    返回 total(0–100)、dims、dim_rationale、rationale 和评审原始 JSON raw。
    """
    full_text = gold.get("full_text") or gold.get("summary") or ""
    if not full_text:
        raise ValueError(f"gold['{title}'] 缺 full_text/summary,无法当参考答案")

    dim_keys = ", ".join(f'"{k}": 0' for k, *_ in DIMENSIONS)
    dim_keys_r = ", ".join(f'"{k}": ""' for k, *_ in DIMENSIONS)
    prompt = JUDGE_PROMPT.format(
        rubric=_build_rubric(),
        title=title,
        gold_full_text=full_text,
        report=report_md,
        dim_keys=dim_keys,
        dim_keys_r=dim_keys_r,
    )

    resp = _get_llm().invoke(prompt)
    raw_text = _content_text(resp)
    obj = _parse_json_obj(raw_text)

    dims_in = obj.get("dims", {}) or {}
    dims = {k: _clamp(dims_in.get(k)) for k, *_ in DIMENSIONS}
    total = round(sum(dims[k] * _WEIGHTS[k] for k in dims), 2)

    return {
        "total": total,
        "dims": dims,
        "dim_rationale": obj.get("dim_rationale", {}) or {},
        "rationale": obj.get("rationale", ""),
        "raw": obj,
    }


ENTITY_RECALL_PROMPT = """判断一篇百科报告是否覆盖了下列"关键事实点"。

对每一条事实点,判断【待评报告】是否**表达了相同的事实**(用词可不同,数字/时间/名称一致即算命中;
报告里找不到、或与事实点矛盾,算未命中)。

[待评报告]
{report}

[关键事实点(逐条编号)]
{facts}

只输出一个 JSON 对象,键是事实点编号(字符串),值是 true/false,不要任何解释、不要代码块围栏。
格式示例:{{"1": true, "2": false, "3": true}}"""


def _norm(s: str) -> str:
    """归一化文本以便进行词法匹配。"""
    s = s.strip().lower()
    s = s.translate({ord(c): ord(c) - 0xFEE0 for c in s if "！" <= c <= "～"})
    return re.sub(r"[\s，。、；：（）()\[\]【】\"'“”‘’,.;:!?！？\-—_/]+", "", s)


def _recall_lexical(report_md: str, facts: list[str]) -> list[bool]:
    """词法兜底：数字须全部出现，非数字文本的 bigram 命中率须至少为 0.6。"""
    rep_norm = _norm(report_md)
    rep_nums = set(re.findall(r"\d+(?:\.\d+)?", report_md))
    hits = []
    for fact in facts:
        nums = set(re.findall(r"\d+(?:\.\d+)?", fact))
        if nums and not nums.issubset(rep_nums):
            hits.append(False)
            continue
        fn = _norm(re.sub(r"\d+(?:\.\d+)?", "", fact))
        grams = {fn[i : i + 2] for i in range(len(fn) - 1)} if len(fn) >= 2 else {fn}
        if not grams:
            hits.append(bool(nums))
            continue
        overlap = sum(1 for g in grams if g in rep_norm) / len(grams)
        hits.append(overlap >= 0.6)
    return hits


def entity_recall(report_md: str, gold: dict, use_llm: bool | None = None) -> dict:
    """计算 key_facts 召回率；默认用 LLM 判等，失败时退回词法匹配。

    返回 recall(0–1)、hits、total 和 per_fact([{fact, hit}, ...])。
    """
    facts = gold.get("key_facts") or []
    if not facts:
        return {"recall": 0.0, "hits": 0, "total": 0, "per_fact": []}
    if use_llm is None:
        use_llm = os.environ.get("ENTITY_RECALL_LLM", "1").strip() != "0"

    flags = None
    if use_llm:
        numbered = "\n".join(f"{i + 1}. {f}" for i, f in enumerate(facts))
        try:
            resp = _get_llm().invoke(
                ENTITY_RECALL_PROMPT.format(report=report_md, facts=numbered)
            )
            obj = _parse_json_obj(_content_text(resp))
            flags = [bool(obj.get(str(i + 1), False)) for i in range(len(facts))]
        except Exception as e:  # noqa: BLE001 - LLM 判等失败就退回词法
            print(f"⚠️ entity_recall LLM 判等失败,退回词法: {e}", file=sys.stderr)
            flags = None
    if flags is None:
        flags = _recall_lexical(report_md, facts)

    per_fact = [{"fact": f, "hit": h} for f, h in zip(facts, flags)]
    hits = sum(flags)
    return {
        "recall": round(hits / len(facts), 4),
        "hits": hits,
        "total": len(facts),
        "per_fact": per_fact,
    }


def _find_report(title: str, explicit: str | None) -> str:
    """优先使用显式路径，否则在 runs/ 中按标题查找报告。"""
    if explicit:
        return explicit if os.path.isabs(explicit) else os.path.join(BAIKE_DIR, explicit)
    runs = os.path.join(BAIKE_DIR, "runs")
    cand = os.path.join(runs, title, "final_report.md")
    if os.path.exists(cand):
        return cand
    hits = sorted(
        d for d in os.listdir(runs)
        if d.startswith(title) and os.path.exists(os.path.join(runs, d, "final_report.md"))
    )
    if hits:
        return os.path.join(runs, hits[-1], "final_report.md")
    raise FileNotFoundError(f"找不到 {title} 的 final_report.md(runs/ 下)")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print("用法: uv run python score.py <标题> [报告路径]", file=sys.stderr)
        sys.exit(1)
    title = args[0]
    report_path = _find_report(title, args[1] if len(args) > 1 else None)
    with open(report_path, encoding="utf-8") as f:
        report_md = f.read()
    gold = load_gold(title)

    print(f"📄 报告: {report_path}  ({len(report_md)} 字)", flush=True)
    print(f"📚 参考: gold/{title}.json  (full_text {len(gold.get('full_text',''))} 字)", flush=True)
    result = score_report(title, report_md, gold)
    er = entity_recall(report_md, gold)

    print(f"\n{'=' * 60}\n总分: {result['total']} / 100\n{'-' * 60}")
    for key, cn, w, _ in DIMENSIONS:
        r = result["dim_rationale"].get(key, "")
        print(f"  {cn}({w:.0%}): {result['dims'][key]:.0f}\t{r}")
    print(f"{'-' * 60}")
    print(f"事实召回 entity_recall: {er['recall']:.0%}  ({er['hits']}/{er['total']})")
    missed = [pf["fact"] for pf in er["per_fact"] if not pf["hit"]]
    if missed:
        print("  未命中事实点:")
        for f in missed:
            print(f"    ✗ {f}")
    print(f"{'-' * 60}\n总评: {result['rationale']}\n{'=' * 60}")

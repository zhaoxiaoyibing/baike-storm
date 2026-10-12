"""线上编辑指南解析器 —— 词条大类 → 分节骨架 / 写作要点。

把编辑指南 markdown(见 ``config.EDITORIAL_MD``,仓库自带 ``data/editorial_guidelines.example.md`` 格式)
解析成 {大类名: {skeleton, block}}:
- skeleton: "目录骨架(N 节)" 那行拆出的节名序列(list[str]);决定"查哪些节",是动态大纲的关键输入。
- block:   该大类 `## 名` 到下一个 `## ` 之间的原始 markdown(含该大类内联的写作要点,若有)。
            作为"写作要点参考"整段喂给 agent;要点在线上多集中于宽泛大类(作品/人物…),
            不强行按节 join,交给 agent 自己对应。

另提供 route_to_dalei(title):用标题在大类列表里选一个大类(**只吃标题**,不看百科)。
拿不准返回 None → 调用方退回通用模板。指南文件不存在时同样返回 None(流水线仅少一个"同类参考"提示)。
"""

from __future__ import annotations

import os
import re
import sys

from . import config
from .llm import pipeline_client, llm_is_ready

_CACHE: dict[str, dict] | None = None
_llm = None


def _parse() -> dict[str, dict]:
    """解析参考 md → {大类名: {"skeleton": [节名...], "block": 原始段落}}。"""
    ref = config.EDITORIAL_MD
    if not os.path.exists(ref):
        return {}
    with open(ref, encoding="utf-8") as f:
        text = f.read()
    lines = text.splitlines()
    # 找所有 `## 大类` 的行号(跳过 `## 大类索引`)
    heads = [(i, ln[3:].strip()) for i, ln in enumerate(lines)
             if ln.startswith("## ") and ln[3:].strip() != "大类索引"]
    out: dict[str, dict] = {}
    for idx, (lineno, name) in enumerate(heads):
        end = heads[idx + 1][0] if idx + 1 < len(heads) else len(lines)
        block = "\n".join(lines[lineno:end]).strip()
        # 骨架行:**目录骨架(N 节)**:A → B → C
        skeleton: list[str] = []
        m = re.search(r"目录骨架.*?:\s*(.+)", block)
        if m:
            seq = m.group(1).strip()
            skeleton = [s.strip() for s in re.split(r"→|->|/", seq) if s.strip()]
        # 大类名可能是逗号/顿号合并的多细类(如 "电影作品、动画作品"),拆开都登记,指向同一块
        for alias in re.split(r"[、,，]", name):
            alias = alias.strip()
            if alias:
                out[alias] = {"skeleton": skeleton, "block": block, "canonical": name}
    return out


def load_editorial() -> dict[str, dict]:
    global _CACHE
    if _CACHE is None:
        _CACHE = _parse()
    return _CACHE


def dalei_names() -> list[str]:
    """去重后的规范大类名列表(用于分类提示)。"""
    seen, names = set(), []
    for v in load_editorial().values():
        c = v["canonical"]
        if c not in seen:
            seen.add(c)
            names.append(c)
    return names


def get_dalei(name: str | None) -> dict | None:
    if not name:
        return None
    return load_editorial().get(name.strip())


def _get_llm():
    global _llm
    if _llm is None:
        _llm = pipeline_client()
    return _llm


def route_to_dalei(title: str) -> str | None:
    """用标题在大类里选一个;拿不准 / LLM 未配置 / 指南文件缺失 → 返回 None。"""
    names = dalei_names()
    if not names or not llm_is_ready():
        return None
    listing = "\n".join(f"- {n}" for n in names)
    prompt = (
        f"你在给百科词条选择最合适的**线上大类**,以便套用该类的分节骨架。\n\n"
        f"候选大类(共 {len(names)} 个):\n{listing}\n\n"
        f"词条标题:《{title}》\n\n"
        f"只输出最贴切的**一个**大类名(必须与上面某项逐字一致),不要解释、不要标点。"
        f"若没有明显贴切的,输出「无」。"
    )
    try:
        out = _get_llm().invoke(prompt).strip()
    except Exception as e:  # noqa: BLE001
        print(f"[route fail] {title}: {type(e).__name__} {e}", file=sys.stderr)
        return None
    out = out.strip("「」《》\"'` 　")
    if out in load_editorial():
        return load_editorial()[out]["canonical"]
    for n in names:
        if n in out or out in n:
            return n
    return None


if __name__ == "__main__":
    ed = load_editorial()
    print(f"解析到 {len(dalei_names())} 个规范大类,{len(ed)} 个别名键(文件: {config.EDITORIAL_MD})")
    for t in sys.argv[1:] or ["示例大类"]:
        d = get_dalei(t)
        if d:
            print(f"\n【{t}】→ {d['canonical']} | {len(d['skeleton'])} 节: {d['skeleton']}")
        else:
            print(f"\n【{t}】未找到")
"""解析线上编辑指南，并按标题选择词条大类。"""

from __future__ import annotations

import os
import re
import sys

from . import config
from .llm import pipeline_client, llm_is_ready

_CACHE: dict[str, dict] | None = None
_llm = None


def _parse() -> dict[str, dict]:
    """解析参考 Markdown，返回各大类的章节骨架和原文块。"""
    ref = config.EDITORIAL_MD
    if not os.path.exists(ref):
        return {}
    with open(ref, encoding="utf-8") as f:
        text = f.read()
    lines = text.splitlines()
    # 索引标题本身不是可用的大类。
    heads = [(i, ln[3:].strip()) for i, ln in enumerate(lines)
             if ln.startswith("## ") and ln[3:].strip() != "大类索引"]
    out: dict[str, dict] = {}
    for idx, (lineno, name) in enumerate(heads):
        end = heads[idx + 1][0] if idx + 1 < len(heads) else len(lines)
        block = "\n".join(lines[lineno:end]).strip()
        skeleton: list[str] = []
        m = re.search(r"目录骨架.*?:\s*(.+)", block)
        if m:
            seq = m.group(1).strip()
            skeleton = [s.strip() for s in re.split(r"→|->|/", seq) if s.strip()]
        # 合并的大类名拆成别名，便于路由命中。
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
    """返回去重后的规范大类名。"""
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
    """根据标题选择大类；无法判断时返回 None。"""
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

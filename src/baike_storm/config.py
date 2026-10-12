"""全局配置与路径解析 —— 所有环境变量在此统一读取。

约定:
  - 环境变量名全部以 STORM_ 开头(少量沿用历史的 OPENAI_*/RESEARCH_* 以兼容既有脚本)。
  - 所有内部网关 / 密钥都不写默认值:未配置就报错或优雅降级,绝不回落到某个内部地址。
  - 数据目录(评测 gold、mock 语料、编辑指南示例)放在包内 data/ 下,可用环境变量覆盖。
"""

from __future__ import annotations

import os
from pathlib import Path

# ── 包内路径 ────────────────────────────────────────────────────────────────
PACKAGE_DIR = Path(__file__).resolve().parent
# src/baike_storm → src → baike-storm/(仓库根)
PROJECT_ROOT = PACKAGE_DIR.parent.parent
DATA_DIR = PACKAGE_DIR / "data"
STATIC_DIR = PACKAGE_DIR / "static"
SKILLS_DIR = PACKAGE_DIR / "skills"

# ── 输出 / 数据目录(可覆盖)─────────────────────────────────────────────────
RUNS_DIR = Path(os.environ.get("STORM_RUNS_DIR", PROJECT_ROOT / "runs"))
GOLD_DIR = Path(os.environ.get("STORM_GOLD_DIR", DATA_DIR / "gold"))
MOCK_CORPUS_DIR = Path(os.environ.get("STORM_MOCK_CORPUS", DATA_DIR / "mock_corpus"))
# 线上编辑指南(大类 → 分节骨架 / 写作要点);文件不存在时流水线仅少一个"同类参考"提示。
EDITORIAL_MD = Path(
    os.environ.get("STORM_EDITORIAL_GUIDELINES", DATA_DIR / "editorial_guidelines.example.md")
)
# 图文版面规划 skill 文件(LLM 排版时读入提示词)
FIGURE_SKILL_MD = Path(
    os.environ.get("STORM_FIG_SKILL", SKILLS_DIR / "figure-layout" / "SKILL.md")
)

# ── 流水线运行参数(与具体后端无关的部分)────────────────────────────────────
# 每问检索式对应的召回条数、分节写作召回的源上限。
SEARCH_TOP_K = int(os.environ.get("STORM_SEARCH_TOP_K", "4"))
SECTION_TOP_K = int(os.environ.get("STORM_SECTION_TOP_K", "14"))
# 提纲:大章数区间;超上限触发一次 MERGE_OUTLINE。
MIN_CHAPTERS = int(os.environ.get("STORM_MIN_CHAPTERS", "6"))
MAX_CHAPTERS = int(os.environ.get("STORM_MAX_CHAPTERS", "9"))
# 「最新动态」视角:追踪近 RECENT_DAYS 天内发布的源。
NEWS_ON = os.environ.get("STORM_NEWS", "1") not in ("0", "false", "False", "")
RECENT_DAYS = int(os.environ.get("STORM_RECENT_DAYS", "365"))
# 成文后是否做版面规划(选主图 + 段落锚点);0 = 退回规则排版。
FIG_PLAN = os.environ.get("STORM_FIG_PLAN", "1") not in ("0", "false", "False", "")


def today_iso(override: str | None = None) -> str:
    """运行日(可用 STORM_TODAY=YYYY-MM-DD 固定,便于复现实验)。"""
    import datetime as _dt

    t = override or os.environ.get("STORM_TODAY")
    return t if t else _dt.date.today().isoformat()
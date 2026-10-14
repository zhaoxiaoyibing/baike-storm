"""读取全局环境变量并解析项目路径。"""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent.parent
DATA_DIR = PACKAGE_DIR / "data"
STATIC_DIR = PACKAGE_DIR / "static"
SKILLS_DIR = PACKAGE_DIR / "skills"

RUNS_DIR = Path(os.environ.get("STORM_RUNS_DIR", PROJECT_ROOT / "runs"))
GOLD_DIR = Path(os.environ.get("STORM_GOLD_DIR", DATA_DIR / "gold"))
MOCK_CORPUS_DIR = Path(os.environ.get("STORM_MOCK_CORPUS", DATA_DIR / "mock_corpus"))
# 编辑指南缺失时只跳过同类参考。
EDITORIAL_MD = Path(
    os.environ.get("STORM_EDITORIAL_GUIDELINES", DATA_DIR / "editorial_guidelines.example.md")
)
FIGURE_SKILL_MD = Path(
    os.environ.get("STORM_FIG_SKILL", SKILLS_DIR / "figure-layout" / "SKILL.md")
)

SEARCH_TOP_K = int(os.environ.get("STORM_SEARCH_TOP_K", "4"))
SECTION_TOP_K = int(os.environ.get("STORM_SECTION_TOP_K", "14"))
MIN_CHAPTERS = int(os.environ.get("STORM_MIN_CHAPTERS", "6"))
MAX_CHAPTERS = int(os.environ.get("STORM_MAX_CHAPTERS", "9"))
NEWS_ON = os.environ.get("STORM_NEWS", "1") not in ("0", "false", "False", "")
RECENT_DAYS = int(os.environ.get("STORM_RECENT_DAYS", "365"))
FIG_PLAN = os.environ.get("STORM_FIG_PLAN", "1") not in ("0", "false", "False", "")


def today_iso(override: str | None = None) -> str:
    """返回运行日期，可用 STORM_TODAY 固定以便复现。"""
    import datetime as _dt

    t = override or os.environ.get("STORM_TODAY")
    return t if t else _dt.date.today().isoformat()

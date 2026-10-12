"""baike-storm —— 从零重建中文百科词条的 STORM 流水线。

一个 STORM(arXiv:2402.14207)的中文落地实现:多视角问题生成 + 检索增强问答构建
上下文,两段式大纲,分节生成,最终渲染成类百科 HTML 页面。

核心能力:
  - 多视角调研(discover_perspectives → simulate_conversation)
  - 两段式大纲(draft → refine → 可选 merge)
  - 分节写作与全局引用去重(SourceLib [n] 编号)
  - LLM-judge 评测(score_report)与事实召回率(entity_recall)
  - 配图选择/版面规划(可选,VLM 驱动)
  - 纯标准库 Web 聊天页(webapp,SSE 流式输出)

快速开始:
  export STORM_LLM_BASE_URL=... STORM_LLM_API_KEY=... STORM_LLM_MODEL=...
  export TAVILY_API_KEY=...                # 检索;或 STORM_SEARCH_BACKEND=mock 离线跑
  baike-storm run 问界M9 --figures

详见 README.md 与 docs/STORM_PLAN.md。
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
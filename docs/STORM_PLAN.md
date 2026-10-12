# STORM 化 baike 重建 —— 实现计划

> 目的:把 deep-research 式 baike 重建系统按 STORM(arXiv:2402.14207)改造,
> 补上"每节深度/事实召回不足"导致的长度与覆盖差距(问界M9:7758字 vs gold 14110字,
> 覆盖度仅 78)。诊断已确认瓶颈是**深度**不是骨架。本计划在**新窗口**执行。

## 一、已拍定的设计取舍
1. 视角数 **N=5**(含固定的 `p0 = 基础事实写手,广覆盖基本信息`),每场对话 **M=3** 轮。
2. 写手↔专家循环用 **(a) orchestrator 驱动**:由 Python 显式控制多轮循环(不依赖 agent 自觉自循环,更可控)。
3. 视角发现 **只靠 LLM 自身知识 + 现有线上大类 block**(editorial.get_dalei 的 block),
   **不**额外联网检索抓同类词条真实目录。

## 二、STORM 四步 → 本项目映射(含落点文件)
- §3.1 多视角发现 → `discover_perspectives(title, dalei_block, n=5)`:LLM 读 大类 block + 自身知识,
  产出 N-1 个视角 persona + 固定 p0。每个 persona = {name, 关注侧面一句话}。
- §3.2 模拟对话(深度引擎)→ `simulate_conversation(title, persona, m=3)`:
  Python 循环;每轮 orchestrator 扮"写手"按 {title+persona+历史} 生成**一个**问题 →
  `expert_answer(question)` 扮"接地气专家"(拆多 query → 逐条搜索 → 源过滤 → 综合带引用作答)→
  答案+源回填历史。N+1 场对话并行/顺序跑,所有源汇入全局库 R。
- §3.3 两段式提纲(顺序反转!)→ 调研**跑完后**:`draft_outline(title)`(仅凭 LLM 知识)→
  `refine_outline(title, OD, conversations)`(用全部对话内容精修)得最终 O。
- §3.4 分节成文 → `write_article(title, O, R)`:逐节按"节标题"从 R 召回相关源(v1 用词法重叠/关键词,
  不引入 embedding 依赖)→ 带引用写该节 → 拼接后 **去重 pass** → 合成 **lead 导语**。

## 三、要新建/修改的文件
- 新建 `storm_pipeline.py`(核心,显式 Python 编排):
  - `discover_perspectives`, `expert_answer`, `simulate_conversation`,
    `run_prewriting`, `draft_outline`, `refine_outline`, `write_article`, `run_storm(title, out_dir)`。
  - 复用:统一走 `baike_storm.llm.pipeline_client()`(OpenAI 兼容)与 `baike_storm.search.search()`
    两个可插拔入口;LLM 调用直接用 model.invoke(prompt),无需 create_deep_agent。
  - 落盘:final_report.md / outline.md(含 OD 与 O)/ conversations.md(全部 Q&A+源)/
    route.txt / langsmith_url.txt,目录 runs/{title}_storm。
- 新建 `prompts_storm.py`:放 5 段提示词(视角发现 / 写手提问 / 专家拆query / 专家综合作答 /
  草拟提纲 / 精修提纲 / 分节写作 / 去重 / 导语)。
- 改 `score.py`:加覆盖率自动指标 `entity_recall`(见第四节)。
- 新建 `exp_storm.py`:A/B = STORM 版 vs 现动态大纲版(复用 runs/问界M9_dynoutline 产物当对照),
  judge 打分 + entity_recall,对比字数/节数/覆盖。score 必须在 agent 跑完后再 import。

## 四、覆盖率自动指标(STORM §entity recall 的本地化)
- gold json 已有 **`key_facts`** 字段 → 直接用它当"事实召回"基准,省去抽取。
- `entity_recall = 命中的 key_facts 数 / key_facts 总数`;命中判定先用字符串/数字归一化包含,
  边界情况可用 LLM 判等(可选)。此指标直盯最弱的"事实覆盖度",也可做 §3.2 的停止准则。

## 五、关键实现细节与坑
- **expert_answer 接地气**:prompt 让 LLM 把一个问题拆成 2-4 条中文 search query → 逐条搜索 →
  汇总命中源 → 带 [n] 引用综合作答;源以 (title,url) 入全局 R(全局去重编号)。
- **源过滤**:沿用现有"排除百科自身"防泄漏;可加简单可信度过滤(域名白/黑名单)。
- **写手追问**:提示词强调"基于历史提**尚未覆盖**的新问题/追问,避免重复";到 M 轮或问不出新问题即停。
- **分节召回 v1**:R 不大时可整表给写节 LLM;R 大时用节标题关键词与源标题/摘要词法重叠 top-k。
  embedding(Sentence-BERT)留作 v2。
- **顺序**:务必"先 run_prewriting 铺满调研 → 再 draft→refine 提纲 → 再写作",这是补覆盖的核心。
- **环境**:LLM 与检索均通过环境变量注入(见 `.env.example`);离线段可用 `STORM_LLM_BACKEND=mock`
  配 `STORM_SEARCH_BACKEND=mock` 复现全链路。
- **LangSmith**:project=baike-storm;score.py 一 import 就全局关 tracing,务必 invoke 跑完再 import。

## 六、A/B 验证(问界M9)
- A 对照 = runs/问界M9_dynoutline(现动态版,7758字/83.7/覆盖78)。
- B = STORM 版(N=5/M=3)。比:字数、## 节数、judge 总分与各维、**entity_recall**。
- 期望:字数向 gold(14110)靠拢,覆盖度(事实覆盖/entity_recall)明显上升,尤其补齐
  产品沿革编年史 + 事件/争议/文化(春晚)等长尾。
- 跑完渲染 render_baike_html.py 出页面对比观感。

## 七、起步顺序(新窗口)
1. 先写 storm_pipeline.py 的 `expert_answer` + `simulate_conversation`,拿单个 persona 冒烟跑通(便宜)。
2. 补 discover_perspectives → run_prewriting → outline → write_article,串成 run_storm。
3. 加 score.entity_recall;写 exp_storm.py 跑 A/B;渲染页面。
4. 记录实验结论(分数 / 覆盖率对比),沉淀为后续迭代依据。

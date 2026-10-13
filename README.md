# baike-storm

**从零重建中文百科词条**:多视角检索调研 + 两段式大纲 + 分节生成的 STORM 流水线,
端到端输出可直接阅读的类百科 HTML 页面。

### 先看演示

🎬 **[打开 GitHub Pages 演示首页](https://zhaoxiaoyibing.github.io/baike-storm/)** · **[阅读凯里·欧文完整词条](https://zhaoxiaoyibing.github.io/baike-storm/examples/kyrie-demo/)**

<video controls preload="metadata" poster="https://zhaoxiaoyibing.github.io/baike-storm/examples/kyrie-demo/img/hero_3.jpg" width="100%">
  <source src="https://zhaoxiaoyibing.github.io/baike-storm/examples/frontend-demo/baike-storm-kyrie-demo.mp4" type="video/mp4">
  当前页面不支持内嵌播放，请打开<a href="https://zhaoxiaoyibing.github.io/baike-storm/examples/frontend-demo/">视频页面</a>。
</video>

<sub>前端生成流程 · 倍速配乐版 · 也可<a href="https://zhaoxiaoyibing.github.io/baike-storm/examples/frontend-demo/">打开独立视频页</a></sub>

<a href="https://zhaoxiaoyibing.github.io/baike-storm/examples/kyrie-demo/">
  <img src="examples/kyrie-demo/kyrie-page-preview.png" alt="凯里·欧文完整词条页面截图" width="100%">
</a>

<sub>点击截图可打开完整词条页面。</sub>

打开首页即可先看倍速录屏，再进入带信息栏、目录、引用和配图的凯里·欧文示例页面。

本项目是 [STORM](https://arxiv.org/abs/2402.14207)(*Assisting in Writing Wikipedia-like Articles From Scratch with Multi-perspective Question Asking*) 的**中文完整实现**。
不依赖任何内部服务:LLM 网关、检索后端、评测 judge 全部可插拔,离线可跑通全链路(demo 模式)。

```
┌──────────────────────────────────────────────────────────────────────┐
│ 标题《问界M9》                                                        │
│   │                                                                  │
│   ├─ ① 视角发现      —— 基础事实写手 + N-1 个检索增强的多视角         │
│   ├─ ② 多视角调研    —— writer 提问 ↔ expert 检索作答(M×N 轮对话)    │
│   ├─ ③ 两段式大纲    —— 草拟 → 精修(超上限自动合并)                  │
│   ├─ ④ 分节生成      —— 逐章写作 + 全局源库 [n] 引用去重            │
│   ├─ ⑤ 后处理        —— 信息栏 / 配图(VLM 可选) / 版面规划          │
│   └─ ⑥ 渲染          —— markdown → 类百科 HTML + 本地页面            │
│                                                                      │
│  产物:runs/<词条>_storm/{final_report.md, index.html, sources.json,  │
│        outline.md, conversations.md, figures.json, infobox.json}     │
└──────────────────────────────────────────────────────────────────────┘
```

## 特性

- **多视角调研**:`discover_perspectives` 规划 N 个互补视角,Writer 与 Expert 以「检索增强问答」方式对话,
  把外部资料压缩成上下文,供大纲与写作使用。
- **两段式大纲**:先草拟、再结合调研内容精修;大章数超上限自动 `MERGE` 合并。
- **全局引用去重**:`SourceLib` 按 URL 去重并生成全局 `[n]` 编号,报告、信息栏、配图共享同一套引用。
- **LLM-judge 评测**:固定 rubric 五维打分(事实覆盖/准确性各 30%、结构/引用各 15%、可读性 10%)
  + `entity_recall` 关键事实召回率,数值分可复现、可对比。
- **离线可复现**:`STORM_LLM_BACKEND=mock` + `STORM_SEARCH_BACKEND=mock` 不联网跑通全链路;
  `STORM_TODAY` 固定运行日,便于实验复现。
- **零内部依赖**:LLM 走任意 OpenAI 兼容 `chat/completions`;检索后端与 judge 均纯环境变量配置。

## 目录结构

```
baike-storm/
├── pyproject.toml              # 打包/依赖/入口(baike-storm 命令)
├── README.md
├── LICENSE                     # MIT
├── .env.example                # 全部环境变量模板
├── docs/
│   └── STORM_PLAN.md           # 设计文档:管线各环节的取舍与评测口径
├── src/baike_storm/
│   ├── cli.py                  # 命令行入口:run / web / eval / render / demo
│   ├── config.py               # 全局配置与路径解析(环境变量统一入口)
│   ├── llm.py                  # LLMClient(OpenAI 兼容)+ MockLLM(离线规则引擎)
│   ├── search.py               # 检索后端:tavily(Tavily API)/ mock(本地语料)
│   ├── storm_pipeline.py       # STORM 主流水线(视角→调研→大纲→写作→后处理)
│   ├── prompts_storm.py        # 全部 9 个提示词模板
│   ├── editorial.py            # 编辑指南解析:词条大类 → 分节骨架 / 写作要点
│   ├── score.py                # LLM-judge 评测 + entity_recall 事实召回率
│   ├── figures.py              # 配图:候选粗筛 → VLM 打分 → 版面规划(可选)
│   ├── infobox.py              # 信息栏:从正文抽结构化字段(带 [n] 引用)
│   ├── render_baike_html.py    # markdown → 类百科 HTML(纯渲染,无外部服务)
│   ├── webapp.py               # 聊天页后端(纯标准库 HTTP + SSE 流式)
│   ├── static/index.html       # 聊天页前端
│   ├── skills/figure-layout/   # 版面规划 skill(提示词)
│   └── data/
│       ├── editorial_guidelines.example.md   # 编辑指南格式示例(可替换为完整版)
│       └── mock_corpus/                      # mock 检索语料(demo / 单测)
└── tests/                      # 单测:search / editorial / render / llm / pipeline
```

## 环境依赖

- Python ≥ 3.10
- 运行时依赖:`httpx`(唯一硬依赖);真实 Tavily 检索请额外安装 `tavily-python`
- 可选:`langsmith`(轨迹可视化)、`langchain-anthropic`(Anthropic 协议 judge)、`pytest`(测试)
- 一个 **OpenAI 兼容** 的 LLM 服务(任意网关 / OpenAI / DeepSeek / 本地 vLLM 均可)
- 检索后端二选一:
  - `tavily`:Tavily 搜索 API([tavily.com](https://tavily.com) 免费注册拿 key,`TAVILY_API_KEY`);安装 `pip install -e ".[search]"`
  - `mock`:离线语料(包内自带,无需任何凭据)

## 快速开始

### 1. 安装

```bash
git clone <your-fork> baike-storm && cd baike-storm
pip install -e .            # 或 pip install -e ".[all]"(含 tracing/judge/tests)
```

### 2. 零密钥离线演示(推荐先跑)

```bash
baike-storm demo 人工智能
# 或指定输出目录
baike-storm demo 人工智能 --out /tmp/demo_run
```

`demo` 使用内置规则引擎(`MockLLM`)+ 内置语料(`mock_corpus/`),不发起任何网络请求,
数秒内即可在 `runs/<词条>_demo/` 下产出:

```
final_report.md   # 报告全文(markdown)
index.html        # 渲染后的类百科页面
outline.md        # 草稿 + 精修两版提纲
conversations.md  # 调研对话全文
sources.json      # 全局源库
infobox.json      # 信息栏字段
figures.json      # 配图计划(空)
```

### 3. 配置真实服务

```bash
cp .env.example .env   # 按注释填写(或直接 export)
```

| 变量 | 说明 |
| --- | --- |
| `STORM_LLM_BASE_URL` / `STORM_LLM_API_KEY` / `STORM_LLM_MODEL` | 主 LLM(OpenAI 兼容);旧名 `OPENAI_*`/`RESEARCH_MODEL` 亦兼容 |
| `STORM_SEARCH_BACKEND=tavily` + `TAVILY_API_KEY` | 真实检索(Tavily,默认后端) |
| `JUDGE_BASE_URL` / `JUDGE_API_KEY` / `JUDGE_MODEL` | 评测 judge(`eval` 子命令用) |

## 使用示例

### 生成一份词条报告

```bash
# 词条 + 视角数 N + 每视角提问数 M(参考 STORM 默认 N=5, M=3)
baike-storm run 问界M9 -n 5 -m 3
# 开配图(VLM 需另配 FIG_VLM_*)
baike-storm run 嫦娥六号 --figures
# Python 方式
python -c "
from baike_storm.storm_pipeline import run_storm_full
res = run_storm_full('问界M9', out_dir='runs/问界M9_storm', n=5, m=3)
print(res['report'])
"
```

产物在 `runs/问界M9_storm/`:`final_report.md` 是主产物,`index.html` 可直接浏览器打开(配图、信息栏、引用渲染齐全)。

### 启动聊天页(SSE 流式)

```bash
baike-storm web --port 8765
# 浏览器打开 http://127.0.0.1:8765/ 输入词条即可实时看到生成过程
```

### 评测(LLM-judge)

```bash
# gold/<title>.json 提供参考答案(full_text 作为事实基准)与 key_facts(关键事实点)
baike-storm eval 嫦娥六号        # 自动找 runs/嫦娥六号*/final_report.md
baike-storm eval 嫦娥六号 path/to/report.md
```

输出:总分(五维加权)+ 每维分数与理由 + `entity_recall`(key_facts 命中率,含未命中明细)。

`gold` 数据格式(不入库,自备):

```json
{
  "title": "嫦娥六号",
  "full_text": "……(百度百科原文,仅给 judge 看,生成链路不可见)……",
  "key_facts": ["2024年5月3日,嫦娥六号由长征五号遥八运载火箭发射升空", "……"]
}
```

> ⚠️ **防泄漏边界**:`full_text` 只进 judge,绝不进入生成链路;生成端的检索也绝不读取参考答案。
> 评测和生成是两个独立进程/配置,请勿混用。

### 渲染已有产物

```bash
baike-storm render runs/问界M9_storm    # 重渲 index.html
```

## 流水线说明(「训练」与迭代)

本项目是**生成 + 评测**系统,不涉及模型参数训练。质量提升靠三条腿:

1. **评测驱动迭代**:固定 rubric 的 `eval` 分数 + `entity_recall` 事实命中率,是提示词、
   检索参数、大纲策略等改动的客观依据。改提示词后请重新评测再横向对比,勿凭感觉调参。
2. **编辑指南外挂**:`data/editorial_guidelines.example.md` 演示了「大类 → 目录骨架 → 写作要点」
   的注入方式。把线上分类体系整理成同格式文件(可用 `STORM_EDITORIAL_GUIDELINES` 指向),
   `route_to_dalei` 会自动为大类路由并带上对应骨架,生成质量显著提升。
3. **复现性**:
   - `STORM_TODAY=YYYY-MM-DD` 固定运行日(影响「最新动态」视角与时间表述);
   - mock 后端全部确定性输出,同一输入 → 同一产物;
   - 检索、LLM 均纯环境变量配置,换网关只需改 env,无需改码。

## 常用配置项

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `STORM_LLM_BACKEND` | (空) | `mock` = 离线规则引擎;缺省走 OpenAI 兼容客户端 |
| `STORM_LLM_BASE_URL` / `STORM_LLM_API_KEY` / `STORM_LLM_MODEL` | - | 主 LLM;兼容 `OPENAI_*`/`RESEARCH_MODEL` |
| `STORM_SEARCH_BACKEND` | `tavily` | `mock` = 读 `data/mock_corpus/*.json` |
| `STORM_SEARCH_TOP_K` / `STORM_SECTION_TOP_K` | 4 / 14 | 视角检索 / 分节写作的召回条数 |
| `STORM_MIN_CHAPTERS` / `STORM_MAX_CHAPTERS` | 6 / 9 | 大章数区间,超上限自动合并 |
| `STORM_NEWS` / `STORM_RECENT_DAYS` | 1 / 365 | 「最新动态」视角开关与时间窗 |
| `STORM_FIG_PLAN` | 1 | 成文后做版面规划(0 = 规则排版) |
| `JUDGE_BASE_URL` / `JUDGE_API_KEY` / `JUDGE_MODEL` | - | 评测 judge;`JUDGE_BACKEND=anthropic` 走 Anthropic 协议 |
| `ENTITY_RECALL_LLM` | 1 | 0 = 事实判等退回词法(无需再调 LLM) |
| `STORM_TODAY` | 今天 | 固定运行日(复现实验) |
| `STORM_RUNS_DIR` / `STORM_GOLD_DIR` | `runs/` / `data/gold/` | 产物与评测目录 |

完整清单见 [.env.example](.env.example) 与 [src/baike_storm/config.py](src/baike_storm/config.py)。

## 测试

```bash
pip install -e ".[tests]"
python -m pytest          # 全部离线,不发起任何网络请求
```

## 公开演示

- [离线 HTML 示例](examples/ai-demo/index.html)
- [凯里·欧文 HTML 示例](examples/kyrie-demo/index.html)
- [前端配乐演示视频](examples/frontend-demo/index.html)
- [演示材料说明与前端录屏预留](examples/README.md)

启用 GitHub Pages 后可直接打开：<https://zhaoxiaoyibing.github.io/baike-storm/>

设置方法：仓库 **Settings → Pages → Build and deployment → Deploy from a branch**，选择 `main` 分支和 `/ (root)` 目录并保存。

## 扩展指南

- **换 LLM**:任意 OpenAI 兼容服务,`STORM_LLM_*` 三件套即可(见 `llm.py`)。
- **换检索**:实现 `search(query, top_k, with_images) -> list[dict]` 并替换 `search.py` 中的分发逻辑;
  mock 语料格式见 `data/mock_corpus/README.md`。
- **换 judge**:`score.py` 支持 `openai`(默认)与 `anthropic` 两种协议端点。
- **接自有词条体系**:把分类模板整理为编辑指南文件(格式见 `editorial_guidelines.example.md`),
  即可获得「同类词条怎么分节」的先验。

## License

[MIT](LICENSE)

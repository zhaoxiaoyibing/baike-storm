# mock 检索语料

离线演示和单测使用的检索语料。设置 `STORM_SEARCH_BACKEND=mock` 后，`baike_storm.search` 会加载本目录下所有 `*.json` 文件。每个文件可以包含单个对象或对象数组，查询时按词法匹配打分并返回结果。

## 条目字段

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `title` | `str` | 是 | 页面标题；与查询词命中时加 2 分 |
| `name` | `str` | 是 | 站点名，例如“示例新闻网” |
| `url` | `str` | 是 | 条目链接 |
| `sentence` | `str` | 是 | 摘要正文，作为流水线提供给 LLM 的素材 |
| `credibility` | `"high"` / `"medium"` / `"low"` | 否 | 来源可信度，默认 `medium` |
| `match` | `list[str]` | 否 | 额外命中关键词，命中时加 1 分 |
| `images` | `list[dict]` | 否 | 图片元数据（`image_url` / `width` / `height`） |
| `page_time` | `int` | 否 | Unix 时间戳，用于“最新动态”视角过滤 |

## 添加语料

- 按上述格式追加 `*.json` 文件即可演示其他词条。
- 真实检索请设置 `STORM_SEARCH_BACKEND=tavily` 和 `TAVILY_API_KEY`，见主 README 的环境依赖说明。

# mock 检索语料(mock_corpus)

离线演示 / 单测用的检索语料。`STORM_SEARCH_BACKEND=mock` 时,`baike_storm.search` 会加载本目录
下所有 `*.json` 文件(每个文件可以是单条对象或对象数组),按查询词做词法匹配打分并返回。

## 条目字段

| 字段         | 类型            | 必填 | 说明                                     |
| ------------ | --------------- | ---- | ---------------------------------------- |
| `title`      | str             | 是   | 页面标题;与查询词命中时 +2 分            |
| `name`       | str             | 是   | 站点名(如「示例新闻网」)                 |
| `url`        | str             | 是   | 条目链接                                 |
| `sentence`   | str             | 是   | 摘要正文(流水线喂给 LLM 的素材)          |
| `credibility`| "high"/"medium"/"low" | 否 | 来源可信度(默认 medium)            |
| `match`      | list[str]        | 否   | 额外命中关键词(与查询词命中时 +1 分)     |
| `images`     | list[dict]       | 否   | 图片元数据(image_url/width/height)       |
| `page_time`  | int              | 否   | Unix 时间戳;用于「最新动态」视角过滤     |

## TODO

- 想演示其他词条时,按同样格式追加 `*.json` 即可;
- 真实检索请配置 `STORM_SEARCH_BACKEND=tavily` + `TAVILY_API_KEY`(见 README「检索后端」)。
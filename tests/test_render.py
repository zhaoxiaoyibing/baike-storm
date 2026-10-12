"""render_baike_html 渲染测试:markdown → 百科 HTML 的稳定输出。"""

from baike_storm.render_baike_html import render_markdown

SAMPLE = """# 人工智能

人工智能是研究如何让计算机模拟人类智能的学科。

## 定义

- 1956 年达特茅斯会议提出概念[1]
- 机器学习是核心分支[2]

## 发展历程

| 年份 | 事件 |
| --- | --- |
| 1956 | 达特茅斯会议 |
| 2012 | AlexNet 夺冠 |

> 引用:示例来源[1]
"""


def test_render_basic():
    html = render_markdown(SAMPLE)
    assert "<h1" in html and "<h2" in html
    assert "人工智能" in html


def test_render_does_not_leak_markdown_syntax():
    html = render_markdown(SAMPLE)
    # 标题已转 HTML,不应残留 markdown 井号(代码块除外)
    assert "## 定义" not in html


def test_render_with_infobox_and_score():
    html = render_markdown(
        SAMPLE,
        dalei="电子产品",
        score={"total": 88.0, "dims": {}, "rationale": "示例"},
        infobox={"group": "product", "label": "电子产品",
                 "fields": [{"k": "中文名", "v": "人工智能", "refs": []}]},
    )
    assert "人工智能" in html
    assert "中文名" in html


def test_render_repeats_source_list():
    html = render_markdown(SAMPLE)
    # 引用 [1] 应出现在页面中
    assert "[1]" in html or ">1<" in html


def test_render_empty_string():
    html = render_markdown("")
    assert isinstance(html, str) and html
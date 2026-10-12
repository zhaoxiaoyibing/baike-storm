"""MockLLM 分派测试:离线演示的确定性。"""

import json

from baike_storm.llm import MockLLM, pipeline_client


def _mock():
    return MockLLM()


def test_discover_perspectives():
    out = _mock().invoke("请从多视角规划,设计 3 个视角,输出 JSON 数组。")
    arr = json.loads(out)
    assert len(arr) == 3
    assert all("name" in x and "focus" in x for x in arr)


def test_writer_ask():
    out = _mock().invoke("你正在向一位专家提问《嫦娥六号》,请提出一个问题。")
    assert "嫦娥六号" in out


def test_expert_decompose():
    out = _mock().invoke("把问题「嫦娥六号」拆成 3 个子问题,输出 JSON 数组。")
    arr = json.loads(out)
    assert len(arr) == 3


def test_expert_answer_quotes_sources():
    # 用 _fmt_sources 的真实格式:[n] 头部行 + 缩进正文行
    src = ("[1] 示例来源 https://example.org/1 (high) (发布 2026-01-01)\n"
           "    这是第一条检索摘要,内容足够长,用于验证解析。\n"
           "[2] 示例来源2 https://example.org/2 (high) (发布 2026-01-02)\n"
           "    这是第二条检索摘要,内容足够长,用于验证解析。")
    out = _mock().invoke(f"请**只依据下面检索到的资料**,回答关于《测试》的问题。\n"
                         f"[检索到的资料(每条前面是其全局编号 [n],作答时用它做行内引用;括号内为发布日期)]\n"
                         f"{src}\n\n要求:条理作答。")
    assert "这是第一条检索摘要" in out
    assert "这是第二条检索摘要" in out


def test_draft_outline_stable():
    out = _mock().invoke("请草拟一个分节提纲。")
    assert out.startswith("## 主体内容")
    assert "### 发展历程" in out


def test_fallback_never_raises():
    out = _mock().invoke("完全未知的提示内容")
    assert "MockLLM" in out


def test_pipeline_client_mock():
    import os

    os.environ["STORM_LLM_BACKEND"] = "mock"
    client = pipeline_client()
    assert client.model_name == "mock"
"""score 模块测试:词法 entity_recall 与评分解析工具(不依赖 judge LLM)。"""

import os

from baike_storm.score import _norm, _recall_lexical, entity_recall

GOLD = {
    "title": "测试",
    "full_text": "占位",
    "key_facts": [
        "1956年达特茅斯会议提出人工智能概念",
        "2023年全球产业规模突破5000亿美元",
        "该词条完全不涉及火星登陆计划",
    ],
}

FACTS = GOLD["key_facts"]


def test_norm_strips_punct_and_case():
    # 全角转半角、小写、去除标点与空白(含 . , !)
    assert _norm("ＡＢＣ 1,234.5!") == "abc12345"
    assert _norm("  ") == ""


def test_recall_lexical_hit_on_full_sentence():
    report = "人工智能概念于1956年达特茅斯会议正式提出。"
    hits = _recall_lexical(report, FACTS)
    assert hits[0] is True


def test_recall_lexical_miss_on_contradiction():
    report = "人工智能概念于1957年达特茅斯会议正式提出。"  # 数字不一致 → 未命中
    hits = _recall_lexical(report, FACTS)
    assert hits[0] is False


def test_entity_recall_lexical_mode(monkeypatch):
    monkeypatch.setenv("ENTITY_RECALL_LLM", "0")
    report = "1956年达特茅斯会议提出人工智能概念。2023年产业规模突破5000亿美元。"
    out = entity_recall(report, GOLD)
    assert out["total"] == 3
    assert out["hits"] == 2
    assert out["recall"] == round(2 / 3, 4)
    assert [p["fact"] for p in out["per_fact"] if not p["hit"]] == [FACTS[2]]


def test_entity_recall_no_facts():
    out = entity_recall("任意", {"title": "x", "key_facts": []})
    assert out == {"recall": 0.0, "hits": 0, "total": 0, "per_fact": []}


def test_load_gold_missing_file(tmp_path, monkeypatch):
    import json

    from baike_storm.score import load_gold

    f = tmp_path / "other.json"
    f.write_text(json.dumps({"title": "目标词条", "full_text": "x", "key_facts": []}),
                 encoding="utf-8")
    # score.GOLD_DIR 在 import 时求值,直接替换模块属性
    monkeypatch.setattr("baike_storm.score.GOLD_DIR", str(tmp_path))
    d = load_gold("目标词条")
    assert d["title"] == "目标词条"
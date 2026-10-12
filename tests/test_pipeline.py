"""流水线端到端冒烟测试:mock 后端下从标题到报告的完整链路。"""

import os

import pytest

from baike_storm import storm_pipeline


@pytest.fixture(autouse=True)
def _mock_env(monkeypatch, tmp_path):
    monkeypatch.setenv("STORM_LLM_BACKEND", "mock")
    monkeypatch.setenv("STORM_SEARCH_BACKEND", "mock")
    monkeypatch.setenv("STORM_FIG_PLAN", "0")
    monkeypatch.setenv("STORM_RUNS_DIR", str(tmp_path / "runs"))
    yield


def test_run_storm_full_mock(_mock_env, tmp_path):
    out = tmp_path / "run"
    res = storm_pipeline.run_storm_full("人工智能", out_dir=str(out), n=2, m=1, figures=False)
    assert "report" in res
    md = res["report"]
    assert len(md) > 50, "mock 生成报告不应为空"


def test_dump_artifacts(_mock_env, tmp_path):
    out = tmp_path / "run2"
    storm_pipeline.run_storm_full("人工智能", out_dir=str(out), n=2, m=1, figures=False)
    for fn in ("final_report.md", "route.txt", "outline.md", "conversations.md",
               "sources.json", "infobox.json", "figures.json"):
        assert os.path.exists(out / fn), f"缺少产物 {fn}"


def test_run_storm_returns_str(_mock_env, tmp_path):
    md = storm_pipeline.run_storm("人工智能", out_dir=str(tmp_path / "run3"), n=2, m=1)
    assert isinstance(md, str) and md.strip()
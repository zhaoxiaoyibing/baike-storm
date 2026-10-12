"""editorial 编辑指南解析测试:骨架提取 / 别名拆分 / 缺文件降级。"""

from baike_storm import config, editorial


def test_parse_example_file():
    ed = editorial.load_editorial()
    assert ed, "包内示例指南应可解析"
    # 示例文件含 3 个大类
    assert {"人物", "电子产品", "自然地理"} <= set(editorial.dalei_names())


def test_skeleton_extracted():
    d = editorial.get_dalei("电子产品")
    assert d is not None
    assert len(d["skeleton"]) == 9
    assert d["skeleton"][0] == "发展历史"
    assert "产品规格" in d["skeleton"]


def test_dalei_names_dedup():
    names = editorial.dalei_names()
    assert len(names) == len(set(names))


def test_alias_split(tmp_path, monkeypatch):
    """逗号/顿号合并的大类名应拆成多个同义键。"""
    f = tmp_path / "aliases.md"
    f.write_text("## 电影作品、动画作品\n\n**目录骨架(2 节)**:A → B\n", encoding="utf-8")
    monkeypatch.setattr(config, "EDITORIAL_MD", f)
    editorial._CACHE = None
    try:
        ed = editorial.load_editorial()
        assert "电影作品" in ed and "动画作品" in ed
        assert ed["电影作品"]["skeleton"] == ["A", "B"]
        assert ed["动画作品"]["canonical"] == "电影作品、动画作品"
    finally:
        editorial._CACHE = None


def test_missing_file_graceful(monkeypatch):
    monkeypatch.setattr(config, "EDITORIAL_MD", config.PROJECT_ROOT / "no_such_file.md")
    assert editorial._parse() == {}


def test_route_returns_none_when_llm_missing(monkeypatch):
    monkeypatch.setenv("STORM_LLM_BACKEND", "nonexist")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert editorial.route_to_dalei("随便什么标题") is None
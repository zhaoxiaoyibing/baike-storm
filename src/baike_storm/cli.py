"""baike-storm 命令行入口。"""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__, config

_ENV_KEYS = ("STORM_LLM_BASE_URL", "STORM_LLM_API_KEY", "STORM_LLM_MODEL",
             "OPENAI_BASE_URL", "OPENAI_API_KEY", "RESEARCH_MODEL",
             "STORM_SEARCH_BACKEND", "TAVILY_API_KEY",
             "JUDGE_BASE_URL", "JUDGE_API_KEY", "JUDGE_MODEL", "STORM_GOLD_DIR",
             "STORM_RUNS_DIR", "STORM_TODAY")


def _print_env() -> None:
    print("环境变量(当前生效):")
    for k in _ENV_KEYS:
        v = os.environ.get(k, "")
        mark = "set" if v else "—"
        if v and any(s in k for s in ("KEY", "SECRET", "SK")):
            v = f"{v[:4]}...{v[-2:]}" if len(v) > 8 else "***"
        print(f"  {k:26s} [{mark}] {v}")


def _cmd_run(args: argparse.Namespace) -> int:
    from . import storm_pipeline
    from .render_baike_html import render

    title = args.title
    n, m = args.n, args.m
    out = args.out or os.path.join(str(config.RUNS_DIR), f"{title}_storm")
    print(f"\n{'=' * 80}\n[STORM 百科重建] 词条: {title}  (N={n}, M={m})\n{'=' * 80}\n", flush=True)
    with storm_pipeline.storm_trace(title, out, n=n, m=m):
        res = storm_pipeline.run_storm_full(title, out_dir=out, n=n, m=m,
                                            figures=(None if args.figures is None else args.figures))
        md = res["report"]
        with open(os.path.join(out, "final_report.md"), "w", encoding="utf-8") as f:
            f.write(md)
        render(out)
    print(f"\n{'=' * 80}\n报告长度: {len(md)} 字  →  {out}\n{'=' * 80}")
    return 0


def _cmd_web(args: argparse.Namespace) -> int:
    from .webapp import serve

    print(f"Baike STORM {__version__}")
    _print_env()
    serve(host=args.host, port=args.port, max_runs=args.max_runs)
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    from .score import DIMENSIONS, _find_report, entity_recall, load_gold, score_report

    title = args.title
    report_path = _find_report(title, args.report)
    with open(report_path, encoding="utf-8") as f:
        report_md = f.read()
    gold = load_gold(title)

    print(f"📄 报告: {report_path}  ({len(report_md)} 字)", flush=True)
    print(f"📚 参考: gold/{title}.json  (full_text {len(gold.get('full_text', ''))} 字)", flush=True)
    result = score_report(title, report_md, gold)
    er = entity_recall(report_md, gold)

    print(f"\n{'=' * 60}\n总分: {result['total']} / 100\n{'-' * 60}")
    for key, cn, w, _ in DIMENSIONS:
        r = result["dim_rationale"].get(key, "")
        print(f"  {cn}({w:.0%}): {result['dims'][key]:.0f}\t{r}")
    print(f"{'-' * 60}")
    print(f"事实召回 entity_recall: {er['recall']:.0%}  ({er['hits']}/{er['total']})")
    missed = [pf["fact"] for pf in er["per_fact"] if not pf["hit"]]
    if missed:
        print("  未命中事实点:")
        for f in missed:
            print(f"    ✗ {f}")
    print(f"{'-' * 60}\n总评: {result['rationale']}\n{'=' * 60}")
    return 0


def _cmd_render(args: argparse.Namespace) -> int:
    from .render_baike_html import render

    path = args.run_dir
    if not os.path.isdir(path):
        print(f"❌ 不是有效目录: {path}", file=sys.stderr)
        return 1
    out = render(path)
    print(f"✅ 已渲染: {out}")
    return 0


def _cmd_demo(args: argparse.Namespace) -> int:
    """使用 mock 后端运行离线演示。"""
    os.environ.setdefault("STORM_LLM_BACKEND", "mock")
    os.environ.setdefault("STORM_SEARCH_BACKEND", "mock")
    os.environ.setdefault("STORM_FIG_PLAN", "0")
    from . import storm_pipeline
    from .render_baike_html import render

    title = args.title
    out = args.out or os.path.join(str(config.RUNS_DIR), f"{title}_demo")
    print(f"🧪 [demo] 离线 mock 生成《{title}》(LLM=规则引擎, 检索=mock 语料)\n", flush=True)
    with storm_pipeline.storm_trace(title, out):
        res = storm_pipeline.run_storm_full(title, out_dir=out, n=2, m=1, figures=False)
        md = res["report"]
        with open(os.path.join(out, "final_report.md"), "w", encoding="utf-8") as f:
            f.write(md)
        render(out)
    print(f"\n✅ demo 完成: {len(md)} 字  →  {out}/index.html")
    print("   (要跑真实验证,请配置 STORM_LLM_* 与搜索后端后使用 `baike-storm run`)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="baike-storm",
        description="从零重建中文百科词条:STORM 多视角调研 → 两段式大纲 → 分节生成 → 百科渲染",
    )
    p.add_argument("-V", "--version", action="version", version=f"baike-storm {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("run", help="生成一份百科词条报告")
    sp.add_argument("title", help="词条标题")
    sp.add_argument("-n", type=int, default=int(os.environ.get("STORM_N", "5")),
                    help="视角数(默认 5)")
    sp.add_argument("-m", type=int, default=int(os.environ.get("STORM_M", "3")),
                    help="每视角提问数(默认 3)")
    sp.add_argument("--out", default=None, help="输出目录(默认 runs/<title>_storm)")
    sp.add_argument("--figures", action="store_true", default=None, help="启用配图(默认关)")
    sp.add_argument("--no-figures", dest="figures", action="store_false", help="关闭配图")
    sp.set_defaults(func=_cmd_run)

    sp = sub.add_parser("web", help="启动聊天页后端")
    sp.add_argument("--host", default=os.environ.get("STORM_WEB_HOST", "127.0.0.1"))
    sp.add_argument("--port", type=int, default=int(os.environ.get("STORM_WEB_PORT", "8765")))
    sp.add_argument("--max-runs", type=int, default=2, help="同时运行的生成任务上限")
    sp.set_defaults(func=_cmd_web)

    sp = sub.add_parser("eval", help="用参考答案(gold)给报告打分")
    sp.add_argument("title", help="词条标题(对应 gold/<title>.json)")
    sp.add_argument("report", nargs="?", default=None,
                    help="报告路径;缺省自动找 runs/<title>*/final_report.md")
    sp.set_defaults(func=_cmd_eval)

    sp = sub.add_parser("render", help="把已有 run 目录渲染成 index.html")
    sp.add_argument("run_dir", help="生成目录(含 final_report.md 或 report.md)")
    sp.set_defaults(func=_cmd_render)

    sp = sub.add_parser("demo", help="离线 mock 演示(无需任何密钥)")
    sp.add_argument("title", nargs="?", default="示例词条:人工智能", help="词条标题")
    sp.add_argument("--out", default=None, help="输出目录(默认 runs/<title>_demo)")
    sp.set_defaults(func=_cmd_demo)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
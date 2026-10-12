"""百科 STORM 聊天页后端 —— 纯标准库 HTTP + SSE,零新增依赖。

  GET /                              → static/index.html(聊天页)
  GET /api/run?title=&n=&m=&fig=     → text/event-stream,逐条推送 storm_pipeline 过程事件,
                                       结束时推 final(markdown + 渲染好的整页 HTML)。fig=0 关闭配图。
  GET /runs/<dir>/index.html|img/*   → 生成产物(只放行 html 与图片,限定在 runs/ 内)
  GET /api/health                    → ok

每个请求在独立 worker 线程里跑 run_storm_full,事件经 storm_pipeline.set_emitter(ContextVar,线程隔离)
进队列,请求线程负责写 SSE。客户端断开 → 置 cancel → worker 下一次 emit 时抛 RunCancelled 中止。

用法:
  baike-storm web --port 8765
  python -m baike_storm.webapp --port 8765
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import config, storm_pipeline
from .figures import prefix_figures
from .render_baike_html import render_markdown
from .storm_pipeline import RunCancelled

INDEX_HTML = str(config.STATIC_DIR / "index.html")
RUNS_DIR = str(config.RUNS_DIR)
HEARTBEAT_SEC = 15
MAX_TITLE_LEN = 50
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

_run_slots: threading.Semaphore  # serve() 里按 --max-runs 初始化


def _safe_dirname(title: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s.]+', "_", title).strip("_")[:60] or "untitled"


def _clamp_int(raw: str | None, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(raw)))
    except (TypeError, ValueError):
        return default


class Handler(BaseHTTPRequestHandler):
    server_version = "BaikeStorm/0.1"

    def log_message(self, fmt, *args):  # 精简访问日志
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {fmt % args}\n")

    # ── 路由 ──────────────────────────────────────────────────────────────
    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            return self._send_file(INDEX_HTML, "text/html; charset=utf-8")
        if url.path == "/api/health":
            return self._send_text(200, "ok")
        if url.path == "/api/run":
            return self._run_stream(parse_qs(url.query))
        if url.path.startswith("/runs/"):
            return self._send_run_file(unquote(url.path[len("/runs/"):]))
        return self._send_text(404, "not found")

    def _send_run_file(self, rel: str):
        """只放行 runs/ 内的 html 与图片(防目录穿越:realpath 后必须仍在 runs/ 下)。"""
        path = os.path.realpath(os.path.join(RUNS_DIR, rel))
        ctype = STATIC_TYPES.get(os.path.splitext(path)[1].lower())
        if not ctype or os.path.commonpath([path, RUNS_DIR]) != RUNS_DIR or not os.path.isfile(path):
            return self._send_text(404, "not found")
        return self._send_file(path, ctype)

    def _send_text(self, code: int, text: str):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: str, ctype: str):
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # ── SSE ───────────────────────────────────────────────────────────────
    def _sse(self, payload: dict | None):
        """写一条 SSE 事件;payload=None 写心跳注释。断连时抛 OSError 子类。"""
        if payload is None:
            chunk = ": ping\n\n"
        else:
            chunk = "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
        self.wfile.write(chunk.encode("utf-8"))
        self.wfile.flush()

    def _run_stream(self, qs: dict):
        title = (qs.get("title", [""])[0] or "").strip()
        n = _clamp_int(qs.get("n", [None])[0], 5, 1, 8)
        m = _clamp_int(qs.get("m", [None])[0], 3, 1, 5)
        fig = qs.get("fig", ["1"])[0] != "0"

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def fail(msg):
            try:
                self._sse({"type": "error", "message": msg})
            except OSError:
                pass

        if not title or len(title) > MAX_TITLE_LEN or any(ord(c) < 32 for c in title):
            return fail(f"词条名需为 1-{MAX_TITLE_LEN} 个可见字符")
        if not _run_slots.acquire(blocking=False):
            return fail("当前生成任务已满,请稍后再试")

        q: queue.Queue = queue.Queue()
        cancel = threading.Event()
        t0 = time.time()
        dirname = f"{_safe_dirname(title)}_storm_web"
        out_dir = os.path.join(RUNS_DIR, dirname)

        def emitter(kind: str, data: dict):
            if cancel.is_set():
                raise RunCancelled()
            q.put({"type": kind, **data})

        def worker():
            storm_pipeline.set_emitter(emitter)  # 新线程 = 新 context,只影响本线程
            try:
                with storm_pipeline.storm_trace(title, out_dir, n=n, m=m, figures=fig):
                    res = storm_pipeline.run_storm_full(title, out_dir=out_dir, n=n, m=m, figures=fig)
                    report, figs, ibox = res["report"], res["figures"], res.get("infobox")
                    emitter("stage", {"stage": "render", "label": "渲染百科页面"})
                    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
                        f.write(render_markdown(report, dalei=res["dalei"], figures=figs, infobox=ibox))
                    inline = prefix_figures(figs, f"runs/{quote(dirname)}/") if figs else None
                    html = render_markdown(report, dalei=res["dalei"], figures=inline, infobox=ibox)
                    emitter("final", {
                        "markdown": report,
                        "html": html,
                        "chars": len(report),
                        "out_dir": os.path.relpath(out_dir, str(config.PROJECT_ROOT)),
                        "page_url": f"runs/{quote(dirname)}/index.html",
                    })
            except RunCancelled:
                print(f"⏹️  已取消: {title}", flush=True)
            except Exception as e:  # noqa: BLE001 - 任何失败都要回给前端
                traceback.print_exc()
                q.put({"type": "error", "message": f"{type(e).__name__}: {e}"})
            finally:
                _run_slots.release()
                q.put(None)

        print(f"▶️  开始生成: {title} (N={n}, M={m}, 配图={'开' if fig else '关'})", flush=True)
        threading.Thread(target=worker, name=f"storm-{title}", daemon=True).start()

        try:
            self._sse({"type": "start", "title": title, "n": n, "m": m, "fig": fig,
                       "run_base": f"runs/{quote(dirname)}/", "t": 0})
            while True:
                try:
                    ev = q.get(timeout=HEARTBEAT_SEC)
                except queue.Empty:
                    self._sse(None)  # 心跳:长 LLM 调用期间保活,也用来尽早发现断连
                    continue
                if ev is None:
                    break
                ev["t"] = round(time.time() - t0, 1)
                self._sse(ev)
        except OSError:  # BrokenPipe / ConnectionReset:客户端停止或关页
            cancel.set()
            print(f"🔌 客户端断开,取消: {title}", flush=True)


def serve(host: str | None = None, port: int | None = None, max_runs: int = 2):
    """启动 Web 服务(阻塞)。host/port 缺省取 STORM_WEB_HOST / STORM_WEB_PORT 环境变量。"""
    global _run_slots
    if host is None:
        host = os.environ.get("STORM_WEB_HOST", "127.0.0.1")
    if port is None:
        port = int(os.environ.get("STORM_WEB_PORT", "8765"))
    _run_slots = threading.Semaphore(max(1, max_runs))

    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    print(f"🌩️  Baike STORM web: http://{host}:{port}/", flush=True)
    if host not in ("127.0.0.1", "localhost"):
        print("⚠️  已监听非本机地址且无鉴权,任何能访问该端口的人都可触发生成(消耗 LLM/检索额度)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def main():
    ap = argparse.ArgumentParser(description="百科 STORM 聊天页后端")
    ap.add_argument("--host", default=os.environ.get("STORM_WEB_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("STORM_WEB_PORT", "8765")))
    ap.add_argument("--max-runs", type=int, default=2, help="同时运行的生成任务上限")
    args = ap.parse_args()
    serve(host=args.host, port=args.port, max_runs=args.max_runs)


if __name__ == "__main__":
    main()
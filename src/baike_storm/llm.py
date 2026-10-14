"""LLM 客户端，提供在线和离线两种后端。"""

from __future__ import annotations

import json
import os
import re
import time

import httpx

__all__ = ["LLMClient", "MockLLM", "pipeline_client", "llm_is_ready", "MISSING_KEY_MSG"]


def _env_any(*names: str) -> str | None:
    """按顺序返回第一个非空环境变量。"""
    for k in names:
        v = os.environ.get(k, "")
        if v and v.strip():
            return v.strip()
    return None


class LLMClient:
    """同步的 OpenAI 兼容客户端。"""

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str,
        temperature: float = 0.0,
        max_tokens: int = 8192,
        timeout: float = 300.0,
        retries: int = 2,
    ):
        self.model_name = model
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            trust_env=False,
        )
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._retries = retries

    def invoke(self, prompt: str) -> str:
        last = ""
        for attempt in range(self._retries + 1):
            try:
                r = self._http.post(
                    "/chat/completions",
                    json={
                        "model": self.model_name,
                        "temperature": self._temperature,
                        "max_tokens": self._max_tokens,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"] or ""
            except Exception as e:  # noqa: BLE001 - 网络抖动可重试
                last = f"{type(e).__name__}: {e}"
                if attempt < self._retries:
                    time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"LLM 调用失败({self.model_name}):{last}")

    def __call__(self, prompt: str, **kwargs) -> str:
        return self.invoke(prompt)


_client = None


def pipeline_client() -> LLMClient | MockLLM:
    """返回流水线客户端；mock 后端使用离线实现。"""
    global _client
    if _client is None:
        if os.environ.get("STORM_LLM_BACKEND", "").strip().lower() == "mock":
            _client = MockLLM()
        else:
            base_url = _env_any("STORM_LLM_BASE_URL", "OPENAI_BASE_URL")
            api_key = _env_any("STORM_LLM_API_KEY", "OPENAI_API_KEY")
            if not base_url or not api_key:
                raise RuntimeError(
                    "缺少 STORM_LLM_BASE_URL / STORM_LLM_API_KEY(或 OPENAI_BASE_URL / OPENAI_API_KEY):"
                    "请参考 .env.example,或使用 STORM_LLM_BACKEND=mock 跑离线演示。"
                )
            _client = LLMClient(
                model=_env_any("STORM_LLM_MODEL", "RESEARCH_MODEL") or "gpt-4o-mini",
                base_url=base_url,
                api_key=api_key,
            )
    return _client


def llm_is_ready() -> bool:
    """检查 LLM 配置；mock 后端无需凭据。"""
    if os.environ.get("STORM_LLM_BACKEND", "").strip().lower() == "mock":
        return True
    return bool(_env_any("STORM_LLM_API_KEY", "OPENAI_API_KEY"))


MISSING_KEY_MSG = (
    "LLM 未配置:请设置 STORM_LLM_BASE_URL / STORM_LLM_API_KEY(参考 .env.example),"
    "或 export STORM_LLM_BACKEND=mock 跑离线演示。"
)


def _title_from(prompt: str) -> str:
    m = re.search(r"《([^》]+)》", prompt)
    return m.group(1) if m else "未命名词条"


def _headlines(prompt: str) -> str:
    """提取提示词中的 Markdown 标题。"""
    lines = [ln.rstrip() for ln in prompt.splitlines() if re.match(r"^#{2,3} ", ln)]
    return "\n".join(lines) if lines else "## 主体内容\n## 影响与评价"


def _parse_sources(src_block: str) -> list[tuple[str, str]]:
    """解析 _fmt_sources 的来源块，返回 [(id, sentence)]。"""
    out: list[tuple[str, str]] = []
    for m in re.finditer(r"^\[(\d+)\] (.+?)$\n\s*(.+)$", src_block.strip(), re.M):
        out.append((m.group(1), m.group(3).strip()))
    return out


class MockLLM:
    """根据提示词特征返回固定的离线结果。"""

    model_name = "mock"

    def invoke(self, prompt: str) -> str:
        p = prompt
        if "多视角规划" in p:
            n = int(re.search(r"设计\s*(\d+)\s*个", p).group(1) or 1)
            themes = ["沿革与背景", "主体与关键数据", "影响与评价", "事件与争议"]
            people = [{"name": f"视角{idx + 1}", "focus": f"围绕侧面 {idx + 1} 的纵深事实"}
                      for idx in range(max(0, n))]
            for idx, t in enumerate(themes):
                if idx < len(people):
                    people[idx] = {"name": t, "focus": f"{t}方向的事实与细节"}
            return json.dumps(people, ensure_ascii=False)
        if "你正在向一位" in p:
            t = _title_from(p)
            return f"请介绍「{t}」最核心的定义、关键时间节点与量化事实,以及其在所属领域中的地位。"
        if "拆成" in p and "JSON 数组" in p:
            t = _title_from(p)
            return json.dumps([f"{t}是什么", f"{t} 主要事件与发展", f"{t} 关键数据"], ensure_ascii=False)
        if "只依据下面检索到的资料" in p:
            m = re.search(r"^\[检索到的资料.*\]\s*$\n(.+?)\n\n要求:", p, re.M | re.S)
            srcs = _parse_sources(m.group(1)) if m else []
            body = "\n".join(f"- {name}:{sentence}" for name, sentence in srcs) or "(无检索结果)"
            return f"根据检索到的资料:{body}\n(以上为离线演示内容。)"
        if "草拟一个分节提纲" in p:
            return (
                "## 主体内容\n"
                "### 定义与定位\n"
                "### 发展历程\n"
                "## 影响与评价\n"
                "### 社会影响\n"
                "### 相关争议\n"
            )
        if "先验提纲草稿" in p:
            return _headlines(p)
        if "超过了上限" in p:
            return _headlines(p)
        if "撰写其中一章的正文" in p:
            st = re.search(r"本章标题:(.+)$", p, re.M)
            section = st.group(1).strip() if st else "主体内容"
            m = re.search(r"^\[可用于本章的调研资料.*\]\s*$\n(.+?)\n\n要求:", p, re.M | re.S)
            srcs = _parse_sources(m.group(1)) if m else []
            lines = [f"## {section}", ""]
            for i, (_, sentence) in enumerate(srcs[:6], 1):
                lines.append(f"- {sentence}[{i}]")
            return "\n".join(lines)
        if "全文初稿" in p:
            m = re.search(r"\[全文初稿\]\n(.+?)\n\n直接输出", p, re.S)
            return (m.group(1) if m else "").strip()
        if "导语(lead/概述)" in p:
            t = _title_from(p)
            return (
                f"{t}是 baike-storm 离线演示生成的示例条目。"
                f"本导语由 MockLLM 按固定模板生成,仅用于验证流水线端到端可运行,不包含真实事实。"
            )
        if "信息栏编辑" in p:
            t = _title_from(p)
            return json.dumps([{"k": "中文名", "v": t, "refs": []}], ensure_ascii=False)
        return f"(MockLLM 未识别该提示类型,返回占位文本;共 {len(p)} 字符。)"

    def __call__(self, prompt: str, **kwargs) -> str:
        return self.invoke(prompt)

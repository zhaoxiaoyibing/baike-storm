"""LangSmith 的可选依赖适配层。"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Callable, Iterator

try:  # pragma: no cover - 取决于环境是否安装
    from langsmith import trace as _trace  # type: ignore
    from langsmith import traceable as _traceable  # type: ignore
    from langsmith.run_helpers import get_current_run_tree as _get_tree  # type: ignore
    from langsmith.utils import tracing_is_enabled as _tracing_on  # type: ignore

    HAS_LANGSMITH = True

    def trace(name: str, run_type: str | None = None, **kwargs: Any):
        """创建任务根轨迹。"""
        return _trace(name, run_type=run_type, **kwargs)

    def traceable(*args: Any, **kwargs: Any):
        """为函数创建子轨迹。"""
        return _traceable(*args, **kwargs)

    def get_current_run_tree() -> Any:
        return _get_tree()

    def tracing_is_enabled() -> bool:
        return _tracing_on()

except ImportError:  # pragma: no cover
    HAS_LANGSMITH = False

    def trace(name: str, run_type: str | None = None, **kwargs: Any) -> Iterator[Any]:
        """未安装 LangSmith 时保持调用接口不变。"""
        del name, run_type, kwargs
        return _noop_ctx()

    def traceable(*args: Any, **kwargs: Any) -> Callable:
        """未安装 LangSmith 时直接返回原函数。"""
        del args, kwargs
        return lambda f: f

    def get_current_run_tree() -> None:
        return None

    def tracing_is_enabled() -> bool:
        return False

    @contextmanager
    def _noop_ctx() -> Iterator[None]:
        yield None

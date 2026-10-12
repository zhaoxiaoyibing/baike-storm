"""LangSmith 追踪 —— 可选依赖的兼容层。

项目可以把 langsmith 作为可选依赖([tracing] extra)安装;未安装时本模块提供
同名的无操作实现,流水线照常运行(只是不上传轨迹)。

对外暴露与 `from langsmith import ...` 一致的四个名字:
  trace            上下文管理器,包住一次任务的根轨迹
  traceable        装饰器,给函数生成子轨迹
  get_current_run_tree  取当前正在进行的轨迹(嵌套调用时复用根轨迹)
  tracing_is_enabled    是否已启用上传
"""

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
        """包住一次任务根轨迹;参数与 langsmith.trace 一致。"""
        return _trace(name, run_type=run_type, **kwargs)

    def traceable(*args: Any, **kwargs: Any):
        """函数级子轨迹装饰器;参数与 langsmith.traceable 一致。"""
        return _traceable(*args, **kwargs)

    def get_current_run_tree() -> Any:
        return _get_tree()

    def tracing_is_enabled() -> bool:
        return _tracing_on()

except ImportError:  # pragma: no cover
    HAS_LANGSMITH = False

    def trace(name: str, run_type: str | None = None, **kwargs: Any) -> Iterator[Any]:
        """空实现:直接产出 None,不改动调用方逻辑。"""
        del name, run_type, kwargs
        return _noop_ctx()

    def traceable(*args: Any, **kwargs: Any) -> Callable:
        """空实现:原样返回函数。"""
        del args, kwargs
        return lambda f: f

    def get_current_run_tree() -> None:
        return None

    def tracing_is_enabled() -> bool:
        return False

    @contextmanager
    def _noop_ctx() -> Iterator[None]:
        yield None
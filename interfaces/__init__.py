"""统一通信中间件测试适配器接口。

四个中间件目录只需要实现 :class:`MiddlewareAdapter`，可视化层不依赖
具体库的 API。所有时间、计数和资源指标都通过统一的数据结构返回。
"""

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class ScenarioSpec:
    """需求文档中的一个标准场景。"""

    scenario_id: str
    name: str
    title: str
    description: str
    metrics: Sequence[str]
    default_parameters: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class ScenarioResult:
    """适配器返回给 UI 的一次场景结果。"""

    middleware: str
    middleware_version: str | None
    scenario_id: str
    status: str
    metrics: dict[str, Any] = field(default_factory=dict)
    samples: dict[str, list[float]] = field(default_factory=dict)
    configuration: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None


class MiddlewareAdapter(Protocol):
    """四种中间件必须遵守的最小接口。"""

    name: str

    def metadata(self) -> Mapping[str, Any]:
        """返回名称、版本、可用状态及能力说明。"""

    def run(self, scenario: ScenarioSpec, parameters: Mapping[str, Any]) -> ScenarioResult:
        """运行一个标准场景；不得在 UI 层直接调用中间件。"""


__all__ = ["MiddlewareAdapter", "ScenarioResult", "ScenarioSpec"]

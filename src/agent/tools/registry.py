from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Tool:
    name: str
    category: str
    handler: Callable[..., object]


ALLOWED_TOOLS: dict[str, Tool] = {}


def register(tool: Tool) -> None:
    ALLOWED_TOOLS[tool.name] = tool

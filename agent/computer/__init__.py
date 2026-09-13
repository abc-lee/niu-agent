"""agent.computer — Computer Use 对象模型（C1 骨架）。

上游 OMP computer 工具对象模型的照抄移植（worker.ts @883c9507ff，753 行）：
desktop facade → Win → El；帧缓存 / 坐标校验 / AX ref 代际由 niu_natives 层强制。

- 会话不在此自建：复用 vision-server 的 `_get_session()` 同进程单例（帧锚定共用）。
- 对照表（逐成员行号 + 状态）：docs/superpowers/refs/2026-09-13-computer-objectmap.md
"""

from .objects import (
    Clipboard,
    ComputerToolError,
    Desktop,
    El,
    RunContext,
    Win,
)
from .session import ComputerSession

__all__ = [
    "Clipboard",
    "ComputerSession",
    "ComputerToolError",
    "Desktop",
    "El",
    "RunContext",
    "Win",
]

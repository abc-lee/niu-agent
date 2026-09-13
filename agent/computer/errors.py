"""原生错误码恢复句 —— 上游 docs/tools/computer.md "Errors and recovery" 恢复段的照抄移植。

上游原文（单段、按码分组）：
"Recover by refreshing the exact target screenshot after coordinate-frame errors,
taking a new AX snapshot after `StaleRef`, using AX or a delivery mode listed by
`desktop.capabilities()` after `BackgroundUnavailable`, and inspecting those
capabilities for platform/permission failures."

14 个稳定码名 = niu-natives/src/desktop/error.rs:4-19（ErrorCode 枚举，与上游逐字节一致）；
原生错误经 PyO3 以 `{code}: {message}` 前缀浮现（error.rs:115-128）。
上游段落未点名的码，恢复句语义取自同一文档其余节（Desktop API / Inputs 表 /
Platform constraints / Flow and lifecycle），不另自创机制。
"""

from __future__ import annotations

RECOVERY = {
    # —— 上游恢复段点名 ——
    "InvalidCoordinateFrame": "重新对同一 target 截图，用新帧坐标重试：坐标输入要求同 target 的最近一次截图。",
    "StaleRef": "取新的 AX 快照（`win.ax()`）拿新 ref；不复用旧 ref，不猜。",
    "BackgroundUnavailable": "改用 AX 动作，或 `desktop.capabilities()` 列出的 delivery mode（如重试 `delivery: \"foreground\"`）。",
    # —— platform/permission failures → inspect capabilities（上游恢复段末句）——
    "PermissionDenied": "平台/权限失败：用 `desktop.capabilities()` 查 capture/input/ax 权限状态（运行时事实，不要假设），并授予对应系统权限。",
    "CaptureFailed": "平台/权限失败：用 `desktop.capabilities()` 查 capture 能力与权限状态（运行时事实，不要假设）。",
    "InputFailed": "平台/权限失败：用 `desktop.capabilities()` 查 input 能力与权限状态（运行时事实，不要假设）。",
    "AxUnsupported": "平台不支持 AX 或缺权限：用 `desktop.capabilities()` 查 ax 能力与权限状态（运行时事实，不要假设）。",
    "AxFailed": "先用 `desktop.capabilities()` 查 ax 能力与权限状态，再用新快照（`win.ax()`）重试。",
    # —— Desktop API / Inputs 表语义 ——
    "WindowNotFound": "窗口 id 已失效：用 `desktop.windows({app?, title?})` 重新取最新窗口列表，改用新 id。",
    "InvalidTarget": "target 必须是 `'desktop'` 或现存窗口 id：核对参数，或用 `desktop.windows()` 重取有效 id。",
    "InvalidKey": "核对 `press()` 键名格式：'+' 分隔的键名（如 'cmd+shift+p'）或键名列表。",
    "Timeout": "超出 run 预算：缩短 wait/轮询时长，或调大 `timeout`（clamp 1–300 秒，默认 120）。",
    # —— Flow and lifecycle（worker restart → captures and ax refs were reset）——
    "Closed": "会话已关闭：窗口句柄、截图帧、AX ref 全部失效——下次调用重新枚举窗口并重新截图。",
    "Internal": "原生内部错误：无特定恢复动作；若复现，把完整错误文本报给用户。",
}


def annotate_error(message: str) -> str:
    """message 以已知 `{code}: ` 前缀开头 → 原文保留 + 追加一行中文恢复句；未识别码原样返回。

    上游语义（docs/tools/computer.md "Errors and recovery"）：原生错误以稳定码名
    前缀浮现为 ToolError 文本——此处不改原文，只按码附恢复动作。
    """
    for code, hint in RECOVERY.items():
        if message.startswith(code + ": "):
            return f"{message}\n恢复：{hint}"
    return message

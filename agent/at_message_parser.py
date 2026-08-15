"""解析主 Agent 回复里的 @ 消息，提取后以 role=subagent_msg 存 db。

通道一实现：@子Agent名 格式承载所有主→子通信（补充上下文、停止指令、回复子 Agent 问题）。
子 Agent 名格式：kebab 名（同步路径 = agent 类型名，如 nutritionist；异步路径 = <type>-<4位hex>，如 file-processor-a1b2）。
type 支持多连字符（如 context-manager、brain-region）与数字段（对齐 runner._KEBAB_CASE_RE）。
"""
import re

# 保留标记（子 Agent 侧通讯字 + 主 Agent 特殊目标）——提取/防护一律排除
_RESERVED_AT_TARGETS = {"end", "niu-agent", "user", "主Agent", "main-agent"}

# @目标 格式：kebab 名（含数字段，可选 -4hex 异步后缀），@ 后必须跟空白**或中文/英文标点**边界
# （R2-B P2-5：中文 LLM 高发 "@nutritionist，你好" 无空格——标点紧跟也必须提取）
# 负向前瞻排除保留标记
_AT_PATTERN = re.compile(
    r'(?<![\w])@(?!end\b|niu-agent\b|user\b|主Agent\b|main-agent\b)'
    r'([a-z0-9]+(?:-[a-z0-9]+)*(?:-[0-9a-f]{4})?)'
    r'(?:[\s，。；：！？,.;:!?]+)(.*?)(?=[\s，。；：！？,.;:!?]*@(?:[a-z0-9]+(?:-[a-z0-9]+)*(?:-[0-9a-f]{4})?|end|niu-agent|user|主Agent|main-agent)[\s，。；：！？,.;:!?]|\Z)',
    re.DOTALL,
)


def extract_at_messages(reply_text: str) -> list:
    """从主 Agent 回复文本提取 @ 消息。

    返回 [{"target": 子Agent名, "content": 内容, "sender": "主Agent"}, ...]
    保留标记（@end/@niu-agent/@user/@主Agent）不提取。
    R3-B P2：相邻 @ 目标（"@a @b"）时 content 可能为空——过滤空内容（空回答无意义，
    避免子 Agent 收到空消息）。
    T3（2026-08-15）：content = 公共前言 + @ 后内容——主→子 @ 整段传递：
    - 公共前言 = reply_text[:第一个 @ 匹配.start()]（strip 后参与拼接防双换行；strip 后非空才拼）
    - content = f"{前言}\n{@后内容}"（前言非空时——单 @ 场景 = 完整整段；多 @ 各自带公共前言）
    - 空 @ 内容仍过滤（既有行为保留）
    """
    matches = list(_AT_PATTERN.finditer(reply_text))
    if not matches:
        return []
    # 公共前言：从第一个 @ 匹配前截取，strip 归一化（前言以换行结尾时字面拼接会产双换行）
    preface = reply_text[:matches[0].start()].strip()
    msgs = []
    for match in matches:
        target = match.group(1)
        content = match.group(2).strip()
        if not content:
            continue  # R3-B P2：相邻 @ 目标空 content 过滤
        if preface:
            content = f"{preface}\n{content}"
        msgs.append({"target": target, "content": content, "sender": "主Agent"})
    return msgs


def strip_at_messages(reply_text: str) -> str:
    """从回复文本移除 @ 消息，返回剩余文本。"""
    stripped = _AT_PATTERN.sub('', reply_text)
    lines = [line.rstrip() for line in stripped.splitlines() if line.strip()]
    return '\n'.join(lines).strip()


def format_for_db(msg: dict) -> str:
    """格式化为 db 存储格式：@目标 [发送者名] 内容。"""
    return f"@{msg['target']} [{msg['sender']}] {msg['content']}"

"""解析主 Agent 回复里的 @ 消息，提取后以 role=subagent_msg 存 db。

通道一实现：@子Agent名 格式承载所有主→子通信（补充上下文、停止指令、回复子 Agent 问题）。
子 Agent 名格式：<type>-<4位hex>（如 file-processor-a1b2）。
type 支持多连字符（如 context-manager、brain-region）。
"""
import re

# 匹配 @<type>-<4hex> <内容>，内容到行尾或下一个 @
# type 支持多连字符：[a-z]+(?:-[a-z]+)*
# 后缀 [0-9a-f]{4} 严格匹配 4 位 hex（secrets.token_hex(2) 输出）
_AT_PATTERN = re.compile(r'@([a-z]+(?:-[a-z]+)*-[0-9a-f]{4})\s+(.*?)(?=\s*@[a-z]+(?:-[a-z]+)*-[0-9a-f]{4}\s|\Z)', re.DOTALL)


def extract_at_messages(reply_text: str) -> list:
    """从主 Agent 回复文本提取 @ 消息。

    返回 [{"target": 子Agent名, "content": 内容, "sender": "主Agent"}, ...]
    """
    msgs = []
    for match in _AT_PATTERN.finditer(reply_text):
        target = match.group(1)
        content = match.group(2).strip()
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

"""Harness 输出验证——验证 LLM 输出中的图片/文件引用路径是否存在"""

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ReferenceError:
    kind: str          # "图片" 或 "文件"
    path: str          # 引用的路径
    reason: str        # "路径不存在" 或 "不允许使用URL"


@dataclass
class ValidationResult:
    is_valid: bool = True
    errors: list[ReferenceError] = field(default_factory=list)

    def format_feedback(self) -> str:
        """构造反馈给 LLM 的提示消息"""
        if self.is_valid:
            return ""
        lines = ["[System] 输出验证失败：以下引用路径无效："]
        for err in self.errors:
            lines.append(f"  - {err.kind}引用：{err.path}（{err.reason}）")
        lines.append("")
        lines.append("请修正：")
        lines.append("1. 图片和文件必须使用本地绝对路径（如 /Users/xxx/photo.jpg），禁止使用 URL")
        lines.append("2. 如需显示人物照片，请使用 chat-with-file-processor 查询人物照片")
        lines.append("3. 如需发送文件，请确认文件已存在于本地知识库中")
        return "\n".join(lines)


# Markdown 图片语法：![alt](path)
_IMG_PATTERN = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')

# Markdown 链接语法：[text](path) — 排除以 http/https 开头的常规超链接
_LINK_PATTERN = re.compile(r'(?<!!)\[([^\]]+)\]\(([^)]+)\)')


def _normalize_path(path: str) -> str:
    """规范化路径：剥离 file:/// 前缀"""
    if path.startswith("file:///"):
        return path[7:]
    if path.startswith("file://"):
        return path[6:]
    return path


def _is_local_path(path: str) -> bool:
    """判断是否为本地路径（非 URL）"""
    return not path.startswith(("http://", "https://", "ftp://", "mailto:"))


def validate_references(content: str) -> ValidationResult:
    """验证文本中所有 Markdown 图片和文件引用的路径是否存在

    扫描规则：
    - ![alt](path)：图片引用，path 必须是本地路径且文件存在
    - [text](path)：文件链接（非图片），path 必须是本地路径且文件存在
    - 以 http/https 开头的 URL 链接视为普通超链接，不验证
    - 以 http/https 开头的图片引用视为错误（LLM 不应输出 URL 图片）
    """
    result = ValidationResult()
    seen_paths = set()  # 去重

    # 1. 验证图片引用
    for match in _IMG_PATTERN.finditer(content):
        raw_path = match.group(2)
        path = _normalize_path(raw_path)

        if path in seen_paths:
            continue
        seen_paths.add(path)

        if not _is_local_path(path):
            result.errors.append(ReferenceError(
                kind="图片", path=raw_path,
                reason="不允许使用URL，必须使用本地绝对路径"
            ))
            continue

        if not Path(path).exists():
            result.errors.append(ReferenceError(
                kind="图片", path=path,
                reason="路径不存在"
            ))

    # 2. 验证文件链接（排除图片引用，排除 URL 超链接）
    for match in _LINK_PATTERN.finditer(content):
        raw_path = match.group(2)
        path = _normalize_path(raw_path)

        if path in seen_paths:
            continue
        seen_paths.add(path)

        # 跳过 URL 超链接（LLM 输出普通网页链接是正常的）
        if not _is_local_path(path):
            continue

        if not Path(path).exists():
            result.errors.append(ReferenceError(
                kind="文件", path=path,
                reason="路径不存在"
            ))

    result.is_valid = len(result.errors) == 0
    return result

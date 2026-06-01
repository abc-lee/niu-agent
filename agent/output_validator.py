"""Harness 输出验证——验证 LLM 输出中的图片/文件引用路径是否存在"""

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


def _extract_md_refs(text: str) -> list[tuple[str, str, str, bool, int]]:
    """提取 Markdown 图片和文件引用，处理路径中的括号

    返回: [(alt_text, path, full_match, is_image, start_index), ...]
    用括号平衡解析器代替正则，正确处理文件名中的括号（如 V1.8(4).docx）
    start_index 是 full_match 在 text 中的起始位置，用于精确替换
    """
    results = []
    i = 0
    n = len(text)
    while i < n:
        # 检测 ![alt](path) 或 [text](path)
        is_image = False
        if text[i:i+2] == '![':
            is_image = True
            bracket_start = i + 2
        elif text[i] == '[':
            bracket_start = i + 1
        else:
            i += 1
            continue

        # 用方括号平衡找到匹配的 ]
        depth = 1
        j = bracket_start
        while j < n and depth > 0:
            if text[j] == '[':
                depth += 1
            elif text[j] == ']':
                depth -= 1
            if depth > 0:
                j += 1
        if depth != 0:
            i += 1
            continue

        bracket_end = j
        alt_text = text[bracket_start:bracket_end]

        # 检查后面是否紧跟 (
        if bracket_end + 1 >= n or text[bracket_end + 1] != '(':
            i = bracket_end + 1
            continue

        # 用括号平衡找到匹配的 )
        depth = 1
        j = bracket_end + 2
        while j < n and depth > 0:
            if text[j] == '(':
                depth += 1
            elif text[j] == ')':
                depth -= 1
            j += 1

        if depth != 0:
            i = bracket_end + 1
            continue

        path = text[bracket_end + 2:j - 1]
        full_match = text[i:j]
        results.append((alt_text, path, full_match, is_image, i))
        i = j

    return results


def _normalize_path(path: str) -> str:
    """规范化路径：剥离 file:/// 前缀"""
    if path.startswith("file:///"):
        return path[7:]
    if path.startswith("file://"):
        return path[6:]
    return path


def _is_local_path(path: str) -> bool:
    """判断是否为本地路径（非 URL、非 data URI、非空）"""
    if not path:
        return False
    return not path.startswith(("http://", "https://", "ftp://", "mailto:", "data:"))


def validate_references(content: str) -> ValidationResult:
    """验证文本中所有 Markdown 图片和文件引用的路径是否存在

    扫描规则：
    - ![alt](path)：图片引用，path 必须是本地路径且文件存在
    - [text](path)：文件链接（非图片），path 必须是本地路径且文件存在
    - 以 http/https 开头的 URL 链接视为普通超链接，不验证
    - 以 http/https 开头的图片引用视为错误（LLM 不应输出 URL 图片）
    - 支持路径中包含括号（如 V1.8(4).docx）
    """
    result = ValidationResult()
    seen_paths = set()

    for alt_text, raw_path, full_match, is_image, _start in _extract_md_refs(content):
        path = _normalize_path(raw_path)

        if path in seen_paths:
            continue
        seen_paths.add(path)

        if is_image:
            if not _is_local_path(path):
                # data: URI 是内嵌内容，跳过验证；URL 是真正的外部链接，报错
                if path.startswith("data:"):
                    continue
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
        else:
            if not _is_local_path(path):
                continue
            if not Path(path).exists():
                result.errors.append(ReferenceError(
                    kind="文件", path=path,
                    reason="路径不存在"
                ))

    result.is_valid = len(result.errors) == 0
    return result

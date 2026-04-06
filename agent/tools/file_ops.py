"""
File Operations - Read, write, patch files

Simple, reliable file operations with path validation.
"""

import os
import re
from pathlib import Path
from typing import Optional, Dict, Any, List
from loguru import logger

# 允许的根目录白名单（可配置）
ALLOWED_ROOTS: List[str] = []

# 危险路径模式（不允许访问）
DANGEROUS_PATTERNS = [
    r"^/etc/",
    r"^/root/",
    r"^\.ssh/",
    r"^Windows\\System32",
    r"^[A-Za-z]:\\Windows\\System32",
]


def configure_allowed_roots(roots: List[str]):
    """配置允许访问的根目录"""
    global ALLOWED_ROOTS
    ALLOWED_ROOTS = [os.path.abspath(r) for r in roots]


def is_path_allowed(path: str) -> bool:
    """检查路径是否在允许范围内"""
    abs_path = os.path.abspath(path)

    # 检查危险路径
    for pattern in DANGEROUS_PATTERNS:
        if re.match(pattern, abs_path, re.IGNORECASE):
            return False

    # 未配置白名单时，仅禁止危险路径
    if not ALLOWED_ROOTS:
        return True

    for root in ALLOWED_ROOTS:
        if abs_path.startswith(root):
            return True
    return False


def validate_path(path: str) -> str:
    """验证并返回安全的绝对路径"""
    if not path:
        raise ValueError("路径不能为空")

    # 检查路径遍历攻击
    if ".." in path:
        raise ValueError(f"路径包含非法遍历序列: {path}")

    abs_path = os.path.abspath(path)

    # 检查是否在允许范围内
    if not is_path_allowed(abs_path):
        raise ValueError(f"路径不在允许范围内: {abs_path}")

    return abs_path


def file_read(
    path: str,
    start: int = 1,
    count: int = 200,
    keyword: str = None,
    show_linenos: bool = True,
) -> str:
    """
    Read file content

    Args:
        path: File path
        start: Start line (1-indexed)
        count: Max lines to read
        keyword: Search keyword (if provided, returns lines around first match)
        show_linenos: Show line numbers

    Returns:
        File content with optional line numbers
    """
    try:
        # 路径验证
        safe_path = validate_path(path)

        with open(safe_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        # Filter by start
        lines = [(i + 1, l.rstrip("\r\n")) for i, l in enumerate(lines) if i + 1 >= start]

        # Keyword search
        if keyword:
            keyword_lower = keyword.lower()
            for idx, (line_no, content) in enumerate(lines):
                if keyword_lower in content.lower():
                    # Return lines around match
                    before = lines[max(0, idx - count // 3) : idx]
                    after = lines[idx : idx + count - len(before)]
                    lines = before + after
                    break
            else:
                # Keyword not found, return from start
                pass
        else:
            lines = lines[:count]

        if not lines:
            return f"[FILE] Empty or no matching content: {safe_path}"

        # Truncate long lines
        L_MAX = max(100, 512000 // max(len(lines), 1))
        lines = [(i, l if len(l) <= L_MAX else l[:L_MAX] + " ... [TRUNCATED]") for i, l in lines]

        # Format output
        if show_linenos:
            total_tag = f"[FILE] {safe_path} (showing {len(lines)} lines)\n"
            result = total_tag + "\n".join(f"{i}|{l}" for i, l in lines)
        else:
            result = "\n".join(l for _, l in lines)

        return result

    except ValueError as e:
        return f"Security Error: {str(e)}"
    except Exception as e:
        return f"Error: {str(e)}"


def file_write(path: str, content: str, mode: str = "overwrite") -> Dict[str, Any]:
    """
    Write content to file

    Args:
        path: File path
        content: Content to write
        mode: 'overwrite', 'append', 'prepend'

    Returns:
        {'status': 'success'|'error', 'msg': str}
    """
    try:
        # 路径验证
        safe_path = validate_path(path)
        path_obj = Path(safe_path)
        path_obj.parent.mkdir(parents=True, exist_ok=True)

        if mode == "prepend":
            old = path_obj.read_text(encoding="utf-8") if path_obj.exists() else ""
            content = content + old
        elif mode == "append":
            old = path_obj.read_text(encoding="utf-8") if path_obj.exists() else ""
            content = old + content

        path_obj.write_text(content, encoding="utf-8")

        logger.info(f"file_write: {safe_path} ({len(content)} bytes, mode={mode})")
        return {"status": "success", "writed_bytes": len(content)}

    except ValueError as e:
        logger.error(f"file_write security error: {e}")
        return {"status": "error", "msg": f"Security Error: {str(e)}"}
    except Exception as e:
        logger.error(f"file_write error: {e}")
        return {"status": "error", "msg": str(e)}


def file_patch(path: str, old_content: str, new_content: str) -> Dict[str, Any]:
    """
    Patch file by replacing unique old_content with new_content

    Args:
        path: File path
        old_content: Content to find and replace
        new_content: Replacement content

    Returns:
        {'status': 'success'|'error', 'msg': str}
    """
    try:
        # 路径验证
        safe_path = validate_path(path)
        path_obj = Path(safe_path)

        if not path_obj.exists():
            return {"status": "error", "msg": "文件不存在"}

        full_text = path_obj.read_text(encoding="utf-8")

        if not old_content:
            return {"status": "error", "msg": "old_content 为空"}

        count = full_text.count(old_content)

        if count == 0:
            return {
                "status": "error",
                "msg": "未找到匹配的旧文本块。建议：先用 file_read 确认当前内容，再分小段进行 patch。",
            }

        if count > 1:
            return {
                "status": "error",
                "msg": f"找到 {count} 处匹配，无法确定唯一位置。请提供更长、更具体的旧文本块。",
            }

        # Unique match - replace
        updated_text = full_text.replace(old_content, new_content)
        path_obj.write_text(updated_text, encoding="utf-8")

        logger.info(f"file_patch: {safe_path} (replaced {len(old_content)} chars)")
        return {"status": "success", "msg": "文件局部修改成功"}

    except ValueError as e:
        logger.error(f"file_patch security error: {e}")
        return {"status": "error", "msg": f"Security Error: {str(e)}"}
    except Exception as e:
        logger.error(f"file_patch error: {e}")
        return {"status": "error", "msg": str(e)}

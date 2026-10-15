#!/usr/bin/env python3
"""防复发门禁：使用 loguru 的模块，logger 调用不得出现 printf 风格占位符。

背景（已实测）：loguru 只认 str.format 的 {}，logger.info('A %s B', 'x') 会原样
打印 "A %s B"——值被静默丢弃，不报错，肉眼难发现。逐行匹配还会漏掉「格式串在被
折断的续行上」的跨行调用（logger.info( 与  "...%s..." 不在同一行），故本门禁改用
括号配平扫描：从 logger.<level>( 的开括号向后扫到配平的闭括号，在这整个调用块里
查 printf 占位符。

扫描目录（递归）: niu_api/  agent/  mcp-servers/  （根目录可用位置参数覆盖）
目标文件: 含 `from loguru import logger`（含 as 别名）的 .py
硬线（命中即报，逐条输出 文件:起行 + 原因）:
  - 某个 logger.<info|warning|error|debug|exception|critical>( ... ) 调用块
    （含跨多行、含隐式字符串拼接）内出现 printf 占位符 %s / %d / %r / %f
stdlib logging 的 %s 是对的，不在扫描面（无 loguru import 的模块整体跳过）

用法:   python3 scripts/check_loguru_placeholders.py             # 默认扫仓库根
        python3 scripts/check_loguru_placeholders.py <root>       # 扫指定根目录
        python3 scripts/check_loguru_placeholders.py -h           # 查看用法
退出码: 0 干净（打印 OK）/ 1 有命中 / 2 用法或 IO/扫描错
"""
import argparse
import re
import sys
from pathlib import Path

ROOT_DEFAULT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ["niu_api", "agent", "mcp-servers"]
EXCLUDES = {"node_modules", ".git", "__pycache__"}

LOGURU_IMPORT_RE = re.compile(r"^\s*from\s+loguru\s+import\s+logger\b(\s+as\s+(\w+))?")
PRINTF_RE = re.compile(r"%[sdrf]")


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(2)


def iter_files(root):
    for rel in SCAN_DIRS:
        base = root / rel
        if not base.is_dir():
            print(f"warn: 目录不存在，跳过 {rel}/", file=sys.stderr)
            continue
        stack = [base]
        while stack:
            p = stack.pop()
            if any(part in EXCLUDES for part in p.parts):
                continue
            if p.is_dir():
                try:
                    stack.extend(sorted(p.iterdir()))
                except OSError as e:
                    die(f"IO 错: {e}")
            elif p.is_file() and p.suffix == ".py":
                yield p


def logger_name(path):
    """返回该文件使用的 loguru logger 名；文件不用 loguru 则返回 None。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        die(f"IO 错: {e}")
    for line in text.splitlines():
        m = LOGURU_IMPORT_RE.match(line)
        if m:
            return m.group(2) or "logger"
    return None


def find_calls(text, name):
    """yield (call_start_offset, open_paren_offset, level)：每个 logger.<level>(。"""
    call_re = re.compile(
        rf"\b{re.escape(name)}\.(info|warning|error|debug|exception|critical)\s*\("
    )
    for m in call_re.finditer(text):
        open_paren = text.rfind("(", m.start(), m.end())
        if open_paren >= 0:
            yield m.start(), open_paren, m.group(1)


def balance_close(text, open_paren):
    """从 open_paren 处向后扫到配平的 ')'，返回其下标。字符串/注释里的括号不算。
    未配平（语法残缺）时回退到文件尾。"""
    depth = 0
    i = open_paren
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in ("'", '"'):
            if text[i:i + 3] == ch * 3:  # 三引号字符串，原样跳过
                end = text.find(ch * 3, i + 3)
                i = n if end < 0 else end + 3
                continue
            i += 1
            while i < n and text[i] != ch:
                if text[i] == "\\":
                    i += 1
                i += 1
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n - 1


def offset_to_line(text, offset):
    return text.count("\n", 0, offset) + 1

def main():
    parser = argparse.ArgumentParser(
        description="防复发门禁：loguru 模块的 logger 调用块不得出现 %s/%d/%r/%f",
    )
    parser.add_argument("root", nargs="?", default=str(ROOT_DEFAULT),
                        help="扫描根目录（默认=仓库根；其下 niu_api/ agent/ mcp-servers/）")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if not root.is_dir():
        die(f"根目录不存在: {root}")

    hits = []
    for f in iter_files(root):
        name = logger_name(f)
        if name is None:
            continue  # 不用 loguru 的模块：stdlib logging 的 %s 是对的，不查
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            die(f"IO 错: {e}")
        for start, open_paren, level in find_calls(text, name):
            close = balance_close(text, open_paren)
            region = text[open_paren:close + 1]
            m = PRINTF_RE.search(region)
            if not m:
                continue
            line = offset_to_line(text, start)
            rel = f.relative_to(root).as_posix()
            first = text[start:close + 1].splitlines()[0][:80]
            hits.append((rel, line, name, level, m.group(0), first))

    if hits:
        for rel, line, name, level, ph, first in sorted(hits, key=lambda h: (h[0], h[1])):
            print(
                f"{rel}:{line}: {name}.{level}(...) 调用块内出现 printf 占位符 {ph}"
                f"（loguru 只认 {{}}，值会被静默丢弃且不报错）: {first}"
            )
        sys.exit(1)
    print("OK: loguru 模块的 logger 调用块无 printf 占位符")


if __name__ == "__main__":
    main()

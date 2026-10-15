#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_doc_numbers.py — 扫描随包文档中的“本机实测数字”（只报不杀）。

用途
----
用户口径：手册里不得写本机实测数字（性能 / 耗时 / 体积）。每台机器都不同，
写死会误导（实证：别的机器上的主 Agent 读完手册劝用户“这功能别用”）。
落点是随包文档，策略为只报不杀：脚本不改任何文件，退出码恒为 0（仅用法/IO
错误返回 2），按文件分组打印命中清单，供人工过一遍决定改写或删除。

扫描面
------
docs/SYSTEM_MANUAL.md 与 docs/manual-*.md（缺文件静默跳过）。

扫什么
------
1. 形态：数字 + 时间/体积/次数/单位，如 3.2秒 / 120毫秒 / 2GB / 5次 / 10张 / 99.9%；
2. 句式：“本机实测 / 实测： / 参考机 / 我这台 / PM 实测”等自我引用的实测口吻。

白名单（命中后剔除、计入“已剔除”，理由）
------
- 程序自身的定量常量及其值：19878 / 8192 / 8128 / 1031.52 / 60.0 / 120.0 / 300 / 1200
  —— 超时/轮询/闲置/阈值常量，值描述程序行为而非本机性能，本身合规；
- 量级词：毫秒级 / 秒级 / 分钟级 —— 不含具体数值，“数字+单位”形态天然不会命中；
- 版本号/端口/路径里的数字（v1.9、127.0.0.1:9876 等）—— 通过数字前字符
  v/V 或 . 识别：属于版本/IP 片段而非测量值。

用法
----
  python3 scripts/check_doc_numbers.py          # 扫描并报告，退出码恒为 0
  python3 scripts/check_doc_numbers.py -h       # 查看用法
"""
from __future__ import annotations

import argparse
import glob
import re
import sys
from pathlib import Path

DOC_DIR = Path(__file__).resolve().parent.parent / "docs"

# 程序自身的定量常量（理由见模块 docstring）
WHITELIST_CONSTANTS = {"19878", "8192", "8128", "1031.52", "60.0", "120.0", "300", "1200"}

# 数字 + 单位：备选按长短优先排列（毫秒/分钟/秒钟 先于 秒，避免吞半截）；
# 字母单位（GB/MB/KB/TB/ms/s）要求其后是词边界——12seconds、2.6 Skills 不会误报；
# 整体忽略大小写（ms/MS、GB/gb 等价）。
NUM_UNIT_RE = re.compile(
    r"(?P<num>\d+(?:\.\d+)?)\s*"
    r"(?:毫秒|分钟|小时|秒钟|秒|张照片|张|条|次|(?:GB|MB|KB|TB|ms|s)\b|%)",
    re.IGNORECASE,
)
# 自我引用的实测口吻句式
CLAIM_RE = re.compile(r"本机实测|实测[:：]|参考机|我这台|PM\s*实测")

LINE_SNIPPET_LEN = 100  # “所在行截断”的长度
def truncate(line: str) -> str:
    text = line.strip()
    return text if len(text) <= LINE_SNIPPET_LEN else text[:LINE_SNIPPET_LEN] + "…"


def scan_file(path: Path) -> tuple[list[tuple[int, str, str]], int]:
    """返回 (命中列表[(行号, 命中片段, 行截断)], 白名单剔除数)。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        print(f"[IO错误] {path}: {exc}", file=sys.stderr)
        sys.exit(2)
    hits: list[tuple[int, str, str]] = []
    dropped = 0
    for lineno, line in enumerate(lines, 1):
        for m in CLAIM_RE.finditer(line):
            hits.append((lineno, m.group(0), truncate(line)))
        for m in NUM_UNIT_RE.finditer(line):
            num = m.group("num")
            prev = line[m.start() - 1] if m.start() > 0 else ""
            if num in WHITELIST_CONSTANTS or prev in ".vV":
                # 程序常量值 / 版本号·IP·端口片段：剔除并计数
                dropped += 1
                continue
            hits.append((lineno, m.group(0), truncate(line)))
    return hits, dropped


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="check_doc_numbers.py",
        description="扫描随包文档中的本机实测数字/实测口吻，只报不杀（退出码恒为 0）。",
    )
    parser.parse_args()  # -h 由 argparse 处理；未知参数按用法错误退出 2
    total_hits = total_dropped = 0
    last_file = None
    for path in [DOC_DIR / "SYSTEM_MANUAL.md", *[Path(p) for p in sorted(glob.glob(str(DOC_DIR / "manual-*.md")))]]:
        if not path.is_file():
            continue  # 缺文件静默跳过
        if str(path) != last_file:
            print(f"== {path}")
            last_file = str(path)
        hits, dropped = scan_file(path)
        total_hits += len(hits)
        total_dropped += dropped
        for lineno, snippet, line_text in hits:
            print(f"{path}:{lineno}: {snippet}: {line_text}")
    print(f"命中 {total_hits} 处（已按白名单剔除 {total_dropped} 处）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""防复发门禁：签名区（niu.app/Contents/Resources）不得包含以下文件。

- `**/.DS_Store`：Finder 垃圾，任何深度都不应进签名区。
- `config/llama-layout.json`：旧 T2a 布局产物（已删），不得回归。
- `config/user-config.json`：可写用户配置，运行时才生成，不得进签名区。

**刻意放行**（勿当违规、勿"顺手清理"）：
- `__pycache__` / `*.pyc`：预置字节码是**保护性的**——运行时免再往签名区写 .pyc，
  实测包内约 19800 个、共 56M，全部放行。
- `memory/**` 下的种子文件（如 `memory/llama-layout.*.json`、`memory/preferences.json`）：
  设计内应有。

bundle 根目录不存在 ⇒ 打印 `SKIP: 未发现 bundle` 并 exit 0（未构建/未打包环境属正常）。
IO 错 / 用法错 ⇒ exit 2；命中禁止项 ⇒ 列出全部命中并 exit 1；干净 ⇒ `OK` + exit 0。
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BUNDLE = ROOT / "niu.app" / "Contents" / "Resources"

# 按相对路径精确匹配（相对 bundle 根）的禁止项
FORBIDDEN_EXACT = {
    "config/llama-layout.json",
    "config/user-config.json",
}
# 按文件名匹配、任意深度的禁止项
FORBIDDEN_NAMES = {".DS_Store"}


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(2)


def find_forbidden(root):
    """遍历 root 下所有条目，返回命中禁止项的相对路径列表（排序、去重）。"""
    hits = set()
    for p in root.rglob("*"):
        rel = p.relative_to(root).as_posix()
        if p.name in FORBIDDEN_NAMES:
            hits.add(rel)
        elif rel in FORBIDDEN_EXACT:
            hits.add(rel)
    return sorted(hits)


def main():
    parser = argparse.ArgumentParser(
        description="签名区 bundle 清单门禁：禁 .DS_Store / config/llama-layout.json / config/user-config.json"
    )
    parser.add_argument(
        "--root",
        default=None,
        help="bundle Resources 根目录（默认 niu.app/Contents/Resources）",
    )
    args = parser.parse_args()

    root = Path(args.root) if args.root else DEFAULT_BUNDLE
    if not root.is_dir():
        print(f"SKIP: 未发现 bundle ({root})")
        return

    try:
        hits = find_forbidden(root)
    except OSError as e:
        die(f"IO 错误扫描 {root}: {e}")

    if hits:
        print(f"FAIL: 签名区发现 {len(hits)} 个禁止项：")
        for h in hits:
            print(f"  {h}")
        sys.exit(1)

    print("OK: 签名区 bundle 清单干净（无 .DS_Store / llama-layout.json / user-config.json）")


if __name__ == "__main__":
    main()

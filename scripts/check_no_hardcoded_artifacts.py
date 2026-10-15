#!/usr/bin/env python3
"""防复发门禁（铁律 13）：运行时代码不得出现一次性下载产物名 / 第三方下载源 / 上游构建号。

扫描目录（递归）: niu_api/  agent/  mcp-servers/  ui/main/lib/
排除（任意层级）: node_modules/  .git/  __pycache__/  niu.app
测试文件一律跳过: test_*.py / *_test.py（非运行时代码，不进扫描面）
不扫: tests/  scripts/  docs/  launcher/

硬线（命中即报，逐条输出 文件:行号: 类型: 原文截断）:
  - http:// / https:// 字面量（白名单与待处置豁免除外，其余一律如实报出）
  - 构建号形态: llama-\d+-bin- 或独立的 b\d{4,}
  - 一次性资产名: buffalo_l.zip
  - 64 位十六进制 sha256 字面量
待处置豁免: 白名单外的个别真命中显式列入 EXEMPT_PENDING 放行——运行时各打一行 EXEMPT(待处置) 提示，todo 不悄悄消失

用法:   python3 scripts/check_no_hardcoded_artifacts.py
        python3 scripts/check_no_hardcoded_artifacts.py -h   # 查看用法
退出码: 0 干净 / 1 有命中 / 2 用法或 IO 错
豁免放行不计为命中；待处置条目会各打一行 EXEMPT(待处置) 提示（见文件内 EXEMPT_PENDING）
"""
import argparse
import fnmatch
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ["niu_api", "agent", "mcp-servers", "ui/main/lib"]
EXCLUDES = {"node_modules", ".git", "__pycache__", "niu.app"}

URL_RE = re.compile(r"https?://[^\s'\"<>()\[\]{}\\]*")
BUILD_RE = re.compile(r"llama-\d+-bin-|\bb\d{4,}\b")
ZIP_RE = re.compile(r"buffalo_l\.zip")
SHA256_RE = re.compile(r"\b[0-9a-f]{64}\b")

# 白名单：少数几条“模式 + 理由”，白名单外的任何 URL 一律如实报出。
WHITELIST = [
    # 127.0.0.1 / localhost：自家服务探活与 CORS，不是第三方下载源
    re.compile(r"^https?://(?:127\.0\.0\.1|localhost)(?::\d+)?(?:/\S*)?$"),
    # GraphML 图格式的标准命名空间，只出现在图序列化的属性里
    re.compile(r"^http://graphml\.graphdrawing\.org/xmlns"),
    # 裸协议前缀：命中串恰为 http:// 或 https://，行内无主机字面量（主机来自配置/拼接）
    re.compile(r"^https?://$"),
    # 工具 description 里给模型看的示例地址，非运行时下载源
    re.compile(r"^http://localhost:8123"),
    # https://example.com：IANA 保留示例域，仅作 prompt/文档示例，非真实第三方源
    re.compile(r"^https://example\.com"),
    # 高德逆地理编码服务端点（厂商 API 契约地址，稳定、不随上游发版变；
    # API Key 与开关走配置 ~/.niu/preferences.json 的 amap 段——已核）
    re.compile(r"^https://restapi\.amap\.com/"),
    # 浏览器工具的回退默认导航地址（产品默认值，性质同"--port 19878"这类缺省；非一次性产物）
    re.compile(r"^https://www\.bing\.com/?$"),
]

# 待处置豁免：白名单外的真命中，方案 §7 S4 处置前显式放行（不悄悄放过，运行时各打一行提示）
# 当前为空：原两条（bing 回退地址 / 高德端点）经 PM 复核判定**不属**铁律 13 禁用类
# （禁用类＝随上游发版而变的一次性下载产物名/构建号/第三方启动方式），已降为上方白名单。
EXEMPT_PENDING: list[tuple[str, str]] = []


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(2)


def is_whitelisted(url):
    return any(rx.match(url) for rx in WHITELIST)


def iter_files():
    for rel in SCAN_DIRS:
        base = ROOT / rel
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
            elif p.is_file():
                if p.suffix == ".py" and (
                    fnmatch.fnmatch(p.name, "test_*.py")
                    or fnmatch.fnmatch(p.name, "*_test.py")
                ):
                    continue  # 测试文件非运行时代码，不进扫描面
                yield p


def check_file(path, hits):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        die(f"IO 错: {e}")
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in URL_RE.finditer(line):
            url = m.group(0).rstrip(".,;")
            if is_whitelisted(url):
                continue
            rel = str(path.relative_to(ROOT))
            for exempt_path, prefix in EXEMPT_PENDING:
                if rel == exempt_path and url.startswith(prefix):
                    print(f"EXEMPT(待处置): {rel}:{lineno}: {url[:80]}")
                    break
            else:
                hits.append((path, lineno, "URL", url))
        for rx, name in ((BUILD_RE, "构建号"), (ZIP_RE, "产物名"), (SHA256_RE, "sha256")):
            for m in rx.finditer(line):
                hits.append((path, lineno, name, m.group(0)))


def main():
    argparse.ArgumentParser(
        description="防复发门禁：运行时代码不得出现一次性下载产物名 / 第三方下载源 / 上游构建号",
    ).parse_args()  # -h 打印用法；多余参数 → argparse 以退出码 2 报错
    hits = []
    for f in iter_files():
        check_file(f, hits)
    if hits:
        for path, lineno, kind, text in sorted(hits):
            print(f"{path.relative_to(ROOT)}:{lineno}: {kind}: {text[:80]}")
        sys.exit(1)
    print("OK: 运行时代码无写死产物/下载源")


if __name__ == "__main__":
    main()

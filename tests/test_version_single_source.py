"""版本号单一真相源防回潮守卫（spec §3.5）：三处硬编码副本禁止复活。

静态断言（不跑运行时）：
1. chat.html 的 version-label span 无硬编码版本号（填充走 preload APP_VERSION，空 span）
2. compat.py 无 Niu/x.y.z 硬编码 UA（必须由 _read_version() 动态读 VERSION 构造）
3. preload-chat.js 含 APP_VERSION 接线（require app-version + contextBridge 暴露——
   mock 注入 electronAPI 结构性抓不到"桥接写错文件"类错误，静态断言覆盖，spec R1-B P2-3）

背景：0.3.3→0.3.4 升级 compat.py UA 与测试断言停在旧值且一直绿未暴露——硬编码副本
靠人肉记忆同步无机制保证，本文件把"只改 VERSION 一处"变成结构保证。
"""
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_chat_html_version_label_no_hardcoded_version():
    """version-label span 行无 >v\\d+.\\d+ 硬编码（空 span，JS 填充 'v'+APP_VERSION）。"""
    src = _read("ui/main/windows/assistant/chat.html")
    lines = [ln for ln in src.splitlines() if "class=\"version-label\"" in ln]
    assert lines, "chat.html 找不到 version-label span"
    for line in lines:
        assert not re.search(r">\s*v\d+\.\d+", line), f"version-label span 硬编码版本号: {line.strip()}"


def test_compat_py_no_hardcoded_ua_version():
    """compat.py 无 Niu/x.y.z 硬编码 UA（UA 必须由 _read_version() 构造）。"""
    src = _read("niu_api/compat.py")
    assert not re.search(r"Niu/\d+\.\d+", src), "compat.py 出现硬编码 Niu/x.y.z User-Agent"


def test_preload_chat_exposes_app_version():
    """preload-chat.js 含 APP_VERSION 接线：require app-version + contextBridge 暴露。"""
    src = _read("ui/main/preload-chat.js")
    assert re.search(r"require\(['\"][^'\"]*app-version", src), "preload-chat.js 未 require app-version 模块"
    expose_idx = src.find("exposeInMainWorld")
    assert expose_idx != -1, "preload-chat.js 缺少 contextBridge.exposeInMainWorld"
    exposed_block = src[expose_idx:]
    assert re.search(r"\bAPP_VERSION\b", exposed_block), "contextBridge 暴露块中无 APP_VERSION"

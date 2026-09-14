"""编码边界行为锁（方案 docs/superpowers/plans/2026-09-14-utf8-encoding-unification.md §7 L1-2/L1-3）。

I-2 锁：Windows cp1252 locale 下 `Clipboard.read()` 仍逐字返回 UTF-8 中文——
    修复前（subprocess 未传 encoding=，按 locale 解码）本用例必红；修复后绿。
I-3 锁：`code_run` PowerShell 分支给脚本内容加输出编码前缀，使子进程产出与
    `stream_reader` 的 utf-8 解码协议一致的 UTF-8 字节。
I-3 守卫锁：脚本级指令（param/using/[CmdletBinding]/[Parameter]）必须居首——
    指令形态放弃加前缀、逐字原样返回（改动前行为）；非指令形态直接前置。

各锁均可在 macOS 上确定复现（注入式），不依赖实时剪贴板/Windows 真机。
"""

import os
import subprocess
import sys


from agent.computer import objects


# ---------------------------------------------------------------------------
# I-2：剪贴板解码边界——locale(cp1252) 不得影响 UTF-8 解码协议
# ---------------------------------------------------------------------------

def test_clipboard_read_utf8_under_cp1252_locale(monkeypatch, tmp_path):
    """Windows 机（cp1252 locale）读中文剪贴板 → 逐字相等。

    复现链：sys.platform='win32' + PATH 前置 powershell.exe shim（向 stdout 写
    「中文」的 UTF-8 字节，须可执行）+ patch subprocess._text_encoding→cp1252
    （方案首选注入方式：UTF-8 模式与非模式下都能红）。
    修复前 text=True 无 encoding → cp1252 解码 UTF-8 字节 → 乱码（断言红）。
    """
    shim = tmp_path / "powershell.exe"
    shim.write_text("#!/bin/sh\nprintf '%s' '中文'\n", encoding="utf-8")
    os.chmod(shim, 0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "_text_encoding", lambda: "cp1252")

    token = objects._run_context_var.set(objects.RunContext())
    try:
        assert objects.Clipboard().read() == "中文"
    finally:
        objects._run_context_var.reset(token)


# ---------------------------------------------------------------------------
# I-3：code_run PowerShell 分支的输出编码前缀
# ---------------------------------------------------------------------------

class _FakeStdout:
    """stdout 立即 EOF（stream_reader 一次 readline 即退出）。"""

    def readline(self):
        return b""

    def close(self):
        pass


class _FakePopen:
    """假 Popen：捕获命令串，满足 code_run 主循环（poll/wait/close）。"""

    captured_cmd: "list[str] | None" = None

    def __init__(self, cmd, **kwargs):
        type(self).captured_cmd = cmd
        self.pid = 4242
        self.stdout = _FakeStdout()

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


def test_code_run_powershell_prefixed_with_utf8_output_encoding(monkeypatch):
    """code_run('powershell') 在 Windows 下给脚本内容加 [Console]::OutputEncoding 前缀。

    macOS 无 subprocess.STARTUPINFO/STARTF_USESHOWWINDOW → 先注入替身（否则断言前
    AttributeError）；假 Popen 捕获命令串，断言末元素以输出编码前缀开头、且原代码
    完整保留在末尾。
    """
    import agent.handler as handler_mod

    class _FakeStartupInfo:
        def __init__(self):
            self.dwFlags = 0
            self.wShowWindow = 1

    monkeypatch.setattr(os, "name", "nt")
    # macOS 无 STARTUPINFO/STARTF_USESHOWWINDOW（Windows 专有）→ raising=False 注入替身。
    monkeypatch.setattr(subprocess, "STARTUPINFO", _FakeStartupInfo, raising=False)
    monkeypatch.setattr(subprocess, "STARTF_USESHOWWINDOW", 1, raising=False)
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    code = "Write-Host '中文输出'"
    try:
        result = handler_mod.code_run(code, code_type="powershell")

        assert result["status"] == "success"
        cmd = _FakePopen.captured_cmd
        assert cmd is not None
        assert cmd[-1].startswith("[Console]::OutputEncoding = [Text.Encoding]::UTF8; ")
        assert cmd[-1].endswith(code)
    except BaseException:
        # 失败时先还原 os.name：否则 pytest 的失败报告（Path(os.getcwd())）
        # 在非 Windows 平台会连带崩掉，把真正的断言差异藏进 INTERNALERROR。
        monkeypatch.undo()
        raise


# ---------------------------------------------------------------------------
# I-3 守卫锁：脚本级指令（param/using/[CmdletBinding]/[Parameter]）必须居首——
#     指令形态放弃加前缀、末元素逐字等于原文（改动前行为）；非指令形态直接前置。
# ---------------------------------------------------------------------------

_PS_PREFIX = "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "


def _fake_nt_code_run(monkeypatch, code):
    """假 nt 环境下跑 code_run(powershell)，返回捕获命令的末元素（-Command 体）。"""
    import agent.handler as handler_mod

    class _FakeStartupInfo:
        def __init__(self):
            self.dwFlags = 0
            self.wShowWindow = 1

    monkeypatch.setattr(os, "name", "nt")
    # macOS 无 STARTUPINFO/STARTF_USESHOWWINDOW（Windows 专有）→ raising=False 注入替身。
    monkeypatch.setattr(subprocess, "STARTUPINFO", _FakeStartupInfo, raising=False)
    monkeypatch.setattr(subprocess, "STARTF_USESHOWWINDOW", 1, raising=False)
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    try:
        result = handler_mod.code_run(code, code_type="powershell")

        assert result["status"] == "success"
        cmd = _FakePopen.captured_cmd
        assert cmd is not None
        return cmd[-1]
    except BaseException:
        # 失败时先还原 os.name（同 I-3 锁：防 pytest 失败报告连带崩掉）。
        monkeypatch.undo()
        raise


def test_code_run_ps_prefix_prepend_when_no_directive(monkeypatch):
    """无前置指令 → 末元素以前缀开头（原行为保持）。"""
    body = _fake_nt_code_run(monkeypatch, "Write-Host 中文")
    assert body.startswith(_PS_PREFIX)


def test_code_run_ps_prefix_skipped_for_param(monkeypatch):
    """param(...) 必须居首 → 指令形态放弃加前缀，末元素逐字等于原文（改动前行为）。"""
    code = "param([string]$p)\nWrite-Host $p"
    body = _fake_nt_code_run(monkeypatch, code)
    assert body == code


def test_code_run_ps_prefix_skipped_for_using(monkeypatch):
    """using 指令居首 → 指令形态放弃加前缀，末元素逐字等于原文（改动前行为）。"""
    code = "using namespace System.Text\nWrite-Host 中文"
    body = _fake_nt_code_run(monkeypatch, code)
    assert body == code


def test_code_run_ps_prefix_skipped_for_indented_param(monkeypatch):
    """param 居首且带前导空白 → 仍识别为指令形态，不加前缀（逐字等于原文）。"""
    code = "  param([string]$p)"
    body = _fake_nt_code_run(monkeypatch, code)
    assert body == code


def test_code_run_ps_prefix_skipped_for_param_after_comment(monkeypatch):
    """# 注释行在 param 之前 → 跳过注释取首条真实语句，仍识别为指令形态（逐字等于原文）。"""
    code = "# 说明\nparam([string]$x)\nWrite-Host 中文"
    body = _fake_nt_code_run(monkeypatch, code)
    assert body == code


def test_code_run_ps_prefix_skipped_for_param_with_space_before_paren(monkeypatch):
    """param 与括号间有空格 → 仍识别为指令形态（逐字等于原文）。"""
    code = "param ([string]$x)\nWrite-Host 中文"
    body = _fake_nt_code_run(monkeypatch, code)
    assert body == code


def test_code_run_ps_prefix_prepend_with_surrounding_blank_lines(monkeypatch):
    """前后带空行、无前置指令 → 仍直接前置（末元素 = 前缀 + 原文）。"""
    code = "\nWrite-Host 中文\n"
    body = _fake_nt_code_run(monkeypatch, code)
    assert body == _PS_PREFIX + code


def test_code_run_ps_prefix_skipped_for_block_comment_before_param(monkeypatch):
    """块注释 <# ... #>（PS 标准脚本骨架注释头）在 param 之前 → 跳过块注释取首条真实语句，
    仍识别为指令形态（逐字等于原文）。"""
    code = "<#\n.SYNOPSIS\n#>\nparam([string]$x)\nWrite-Host 中文"
    body = _fake_nt_code_run(monkeypatch, code)
    assert body == code


def test_code_run_ps_prefix_skipped_for_tab_separated_using(monkeypatch):
    """using 指令与 namespace 间为 Tab（任意空白种类）→ 仍识别为指令形态（逐字等于原文）。"""
    code = "using\tnamespace System.Text\nWrite-Host 中文"
    body = _fake_nt_code_run(monkeypatch, code)
    assert body == code

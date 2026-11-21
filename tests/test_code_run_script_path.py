# tests/test_code_run_script_path.py
"""code_run 的 script_path 模式（直接执行脚本文件）测试。

覆盖方案 §5.1 用例 1-11：真实路径 / 同目录 import / 不建临时文件 / 内联仍清理 /
退出码 / 中文空格路径 / 超时 / 不存在 / 类型限制 / 互斥与空参 / 相对路径按 cwd。
"""
import os
import tempfile

from agent.handler import code_run


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_real_file_path_and_sys_path0(tmp_path):
    """脚本模式下 __file__ 是真实脚本路径、sys.path[0] 是脚本目录、cwd 为传入值"""
    script = _write(tmp_path / "probe.py", (
        "import os, sys\n"
        "print('FILE=' + os.path.realpath(__file__))\n"
        "print('PATH0=' + os.path.realpath(sys.path[0]))\n"
        "print('CWD=' + os.path.realpath(os.getcwd()))\n"
    ))

    result = code_run(script_path=str(script), cwd=str(tmp_path))

    assert result["status"] == "success", result
    assert result["exit_code"] == 0
    lines = dict(line.split("=", 1) for line in result["stdout"].splitlines() if "=" in line)
    assert lines["FILE"] == os.path.realpath(str(script))
    # samefile 避免 /var ↔ /private/var 之类符号链接假红
    assert os.path.samefile(os.path.realpath(lines["PATH0"]), str(tmp_path))
    assert os.path.samefile(os.path.realpath(lines["CWD"]), str(tmp_path))


def test_same_directory_import_works(tmp_path):
    """同目录 import 可用（能力验证：辅助模块的标记必须出现在 stdout）"""
    _write(tmp_path / "helper.py", "MARKER = 'helper-imported-ok'\nprint(MARKER)\n")
    script = _write(tmp_path / "main.py", "import helper\nprint('main-done')\n")

    result = code_run(script_path=str(script), cwd=str(tmp_path))

    assert result["status"] == "success", result
    assert "helper-imported-ok" in result["stdout"]
    assert "main-done" in result["stdout"]


def test_script_mode_creates_no_temp_file(tmp_path, monkeypatch):
    """脚本模式零次 NamedTemporaryFile 调用（不建临时文件）"""
    script = _write(tmp_path / "probe.py", "print('ok')\n")
    calls = []

    def _fake_named_tempfile(*args, **kwargs):
        calls.append(1)
        raise AssertionError("script_path 模式不应创建临时文件")

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", _fake_named_tempfile)

    result = code_run(script_path=str(script), cwd=str(tmp_path))

    assert result["status"] == "success", result
    assert calls == []


def test_inline_mode_still_cleans_temp_file(tmp_path, monkeypatch):
    """内联模式（code 字符串）行为不变：临时文件创建后删除"""
    created = []
    real_named_tempfile = tempfile.NamedTemporaryFile

    def _recording_named_tempfile(*args, **kwargs):
        f = real_named_tempfile(*args, **kwargs)
        created.append(f.name)
        return f

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", _recording_named_tempfile)

    result = code_run("print('hi')", cwd=str(tmp_path))

    assert result["status"] == "success", result
    assert "hi" in result["stdout"]
    assert len(created) == 1
    assert not os.path.exists(created[0]), "内联模式临时文件必须已清理"


def test_exit_code_propagated(tmp_path):
    """脚本 sys.exit(3) → status=error、exit_code=3"""
    script = _write(tmp_path / "fail.py", "import sys\nsys.exit(3)\n")

    result = code_run(script_path=str(script), cwd=str(tmp_path))

    assert result["status"] == "error"
    assert result["exit_code"] == 3


def test_non_ascii_and_space_path(tmp_path):
    """目录名含中文与空格 → 正常执行（argv 列表，不经 shell）"""
    dir_path = tmp_path / "中文 目录"
    script = _write(dir_path / "脚本 名.py", "print('路径正常')\n")

    result = code_run(script_path=str(script), cwd=str(dir_path))

    assert result["status"] == "success", result
    assert "路径正常" in result["stdout"]


def test_timeout_kills_and_reports(tmp_path):
    """脚本超时 → 被杀、stdout 含 [Timeout Error]、status=error"""
    script = _write(tmp_path / "slow.py", "import time\ntime.sleep(5)\nprint('never')\n")

    result = code_run(script_path=str(script), cwd=str(tmp_path), timeout=1)

    assert result["status"] == "error"
    assert "[Timeout Error] Process killed after 1s" in result["stdout"]


def test_missing_script_returns_error():
    """script_path 不存在 → error + msg 含路径"""
    missing = "/nonexistent/definitely_missing_script.py"

    result = code_run(script_path=missing)

    assert result["status"] == "error"
    assert missing in result["msg"]


def test_script_path_only_supports_python(tmp_path):
    """script_path + code_type="bash" → error"""
    script = _write(tmp_path / "probe.py", "print('ok')\n")

    result = code_run(script_path=str(script), code_type="bash", cwd=str(tmp_path))

    assert result["status"] == "error"
    assert "python" in result["msg"]


def test_code_and_script_path_are_mutually_exclusive(tmp_path):
    """code 与 script_path 同时给 → error"""
    script = _write(tmp_path / "probe.py", "print('ok')\n")

    result = code_run(code="print('x')", script_path=str(script), cwd=str(tmp_path))

    assert result["status"] == "error"
    assert "互斥" in result["msg"]


def test_empty_args_return_error():
    """两者都不给 → error（漏参不静默空跑）"""
    result = code_run()

    assert result["status"] == "error"
    assert "必须提供" in result["msg"]


def test_relative_script_path_resolved_against_cwd(tmp_path):
    """相对 script_path 按 cwd 解析并 abspath"""
    _write(tmp_path / "rel.py", "import os\nprint('FILE=' + os.path.realpath(__file__))\n")

    result = code_run(script_path="rel.py", cwd=str(tmp_path))

    assert result["status"] == "success", result
    assert "FILE=" + os.path.realpath(str(tmp_path / "rel.py")) in result["stdout"]


def test_relative_script_path_without_cwd(tmp_path, monkeypatch):
    """未传 cwd 时相对路径按系统临时目录解析（与文档口径一致）"""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    _write(tmp_path / "rel2.py", "print('ok')\n")

    result = code_run(script_path="rel2.py")

    assert result["status"] == "success", result
    assert "ok" in result["stdout"]


def test_script_sys_argv0_is_real_path(tmp_path):
    """sys.argv[0] 也是真实脚本路径（非临时文件）"""
    script = _write(tmp_path / "argv.py", (
        "import os, sys\nprint('ARGV0=' + os.path.realpath(sys.argv[0]))\n"
    ))

    result = code_run(script_path=str(script), cwd=str(tmp_path))

    assert result["status"] == "success", result
    assert "ARGV0=" + os.path.realpath(str(script)) in result["stdout"]

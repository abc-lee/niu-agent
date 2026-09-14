"""层 3 运行期 stdio 强制的行为锁（方案 docs/superpowers/plans/2026-09-14-utf8-encoding-unification.md §7 L1④ / 第 4 条）。

锁定两个守卫函数：
- niu_api.__main__.ensure_utf8_stdio()
- niu_scheduler_server._ensure_utf8_stdio()（scheduler-server standalone 内联副本）

断言面（方案冻结）：
1. 对真实 TextIOWrapper（monkeypatch 构造 cp1252，即 PYTHONIOENCODING=cp1252 语义——
   该变量优先级高于 UTF-8 模式，故裸起解释器时 stdio 会是 cp1252）调用后，
   encoding == "utf-8" 且 errors == "replace"；stdout/stderr 都生效。
2. 流为 io.StringIO（无 reconfigure）或 None 时不抛、行为不变。

本文件不真设环境变量：cp1252 情形用 monkeypatch 构造 TextIOWrapper 等价模拟。
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_scheduler_module():
    """按文件路径加载 scheduler-server standalone 包（其 src/ 不在 sys.path 上）。"""
    path = (
        REPO_ROOT / "mcp-servers" / "scheduler-server" / "src"
        / "niu_scheduler_server" / "__init__.py"
    )
    spec = importlib.util.spec_from_file_location("niu_scheduler_server_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def niu_main():
    import niu_api.__main__ as m
    return m


@pytest.fixture(scope="module")
def sched_mod():
    return _load_scheduler_module()


def _make_cp1252_stream() -> io.TextIOWrapper:
    """构造 cp1252 的真实 TextIOWrapper（PYTHONIOENCODING=cp1252 语义）。"""
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="backslashreplace")


def _assert_utf8_replace(stream: io.TextIOWrapper) -> None:
    assert stream.encoding == "utf-8"
    assert stream.errors == "replace"


# ---------------------------------------------------------------------------
# niu_api.__main__.ensure_utf8_stdio
# ---------------------------------------------------------------------------

class TestEnsureUtf8Stdio:
    def test_reconfigures_cp1252_stdout_and_stderr(self, niu_main, monkeypatch):
        out = _make_cp1252_stream()
        err = _make_cp1252_stream()
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", err)

        niu_main.ensure_utf8_stdio()

        # 同一对象被就地改（未被替换），且编码/错误策略均已切换
        assert sys.stdout is out and sys.stderr is err
        _assert_utf8_replace(out)
        _assert_utf8_replace(err)

    def test_stringio_streams_no_raise_behavior_unchanged(self, niu_main, monkeypatch):
        out = io.StringIO("before")
        err = io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", err)

        niu_main.ensure_utf8_stdio()  # StringIO 无 reconfigure → 跳过，不得抛

        assert sys.stdout is out and sys.stderr is err
        assert out.getvalue() == "before"
        assert err.getvalue() == ""

    def test_none_streams_no_raise(self, niu_main, monkeypatch):
        monkeypatch.setattr(sys, "stdout", None)
        monkeypatch.setattr(sys, "stderr", None)

        niu_main.ensure_utf8_stdio()  # None → 跳过，不得抛


# ---------------------------------------------------------------------------
# niu_scheduler_server._ensure_utf8_stdio（内联副本）
# ---------------------------------------------------------------------------

class TestSchedulerInlineCopy:
    def test_reconfigures_cp1252_stdout_and_stderr(self, sched_mod, monkeypatch):
        out = _make_cp1252_stream()
        err = _make_cp1252_stream()
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", err)

        sched_mod._ensure_utf8_stdio()

        assert sys.stdout is out and sys.stderr is err
        _assert_utf8_replace(out)
        _assert_utf8_replace(err)

    def test_stringio_streams_no_raise_behavior_unchanged(self, sched_mod, monkeypatch):
        out = io.StringIO("before")
        err = io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", err)

        sched_mod._ensure_utf8_stdio()  # 不得抛

        assert sys.stdout is out and sys.stderr is err
        assert out.getvalue() == "before"
        assert err.getvalue() == ""

    def test_none_streams_no_raise(self, sched_mod, monkeypatch):
        monkeypatch.setattr(sys, "stdout", None)
        monkeypatch.setattr(sys, "stderr", None)

        sched_mod._ensure_utf8_stdio()  # 不得抛


def test_scheduler_copy_does_not_import_niu_api():
    """standalone 约束：内联守卫副本 _ensure_utf8_stdio 自身不得 import niu_api。

    （模块体里另有既有的 TaskStore try/except 可选导入，与本守卫无关；
    锁只钉住守卫函数体——它必须是纯 stdlib 逻辑。）
    """
    src = (
        REPO_ROOT / "mcp-servers" / "scheduler-server" / "src"
        / "niu_scheduler_server" / "__init__.py"
    ).read_text(encoding="utf-8")
    import ast
    tree = ast.parse(src)
    fn = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_ensure_utf8_stdio"
    )
    imports = [n for n in ast.walk(fn) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert not imports

"""Task 2a 测试：niu_api 启动单实例自检（_check_single_instance + main() 入口调用）。

计划 v2.12 Task 2a 规格 T2.1-T2.8：
- _check_single_instance：bind 成功 True / OSError False / close finally / win32 不加 setsockopt
- main() 集成：False 分支 SystemExit(1) + uvicorn.run 不调；True 分支 uvicorn.run 被调
- 真实端口占用语义（bind+listen 后自检 False）
- NIU_API_PORT 接线（env 端口传给 _check_single_instance）

注：import niu_api.__main__ 无副作用（main() 受 __name__ == "__main__" guard 保护）。
"""
import atexit
import random
import socket
import sys
from unittest import mock

import pytest

import niu_api.__main__ as main_mod


def _raise_system_exit_1(*args, **kwargs):
    """替换 sys.exit：抛出 SystemExit(1)（模拟真实进程退出，不真杀 pytest）"""
    raise SystemExit(1)


def test_t2_1_bind_success_returns_true(monkeypatch):
    """T2.1 bind 成功 → True（mock socket.socket）"""
    mock_sock = mock.MagicMock()
    monkeypatch.setattr(socket, "socket", mock.MagicMock(return_value=mock_sock))
    assert main_mod._check_single_instance(12345) is True


def test_t2_2_bind_oserror_returns_false(monkeypatch):
    """T2.2 bind 抛 OSError → False（mock bind.side_effect）"""
    mock_sock = mock.MagicMock()
    mock_sock.bind.side_effect = OSError("Address already in use")
    monkeypatch.setattr(socket, "socket", mock.MagicMock(return_value=mock_sock))
    assert main_mod._check_single_instance(12345) is False


def test_t2_3_close_called_in_finally(monkeypatch):
    """T2.3 close 被调（finally——成功与异常路径均执行）"""
    mock_sock = mock.MagicMock()
    monkeypatch.setattr(socket, "socket", mock.MagicMock(return_value=mock_sock))
    main_mod._check_single_instance(12345)
    mock_sock.close.assert_called_once_with()

    # 异常路径：bind 抛 OSError 后 finally 仍 close
    mock_sock2 = mock.MagicMock()
    mock_sock2.bind.side_effect = OSError("Address already in use")
    monkeypatch.setattr(socket, "socket", mock.MagicMock(return_value=mock_sock2))
    main_mod._check_single_instance(12345)
    mock_sock2.close.assert_called_once_with()


def test_t2_4_main_false_branch_exits(monkeypatch):
    """T2.4 main() False 分支：自检 False → SystemExit code==1 + uvicorn.run 未被调用"""
    monkeypatch.setattr(main_mod, "_check_single_instance", lambda port: False)
    monkeypatch.setattr(sys, "exit", _raise_system_exit_1)
    with mock.patch("uvicorn.run") as mock_run:
        with pytest.raises(SystemExit) as excinfo:
            main_mod.main()
    assert excinfo.value.code == 1
    mock_run.assert_not_called()


def test_t2_5_main_true_branch_runs_uvicorn(monkeypatch):
    """T2.5 main() True 分支：自检 True → uvicorn.run 被调用"""
    monkeypatch.setattr(main_mod, "_check_single_instance", lambda port: True)
    # P3 打磨：mock atexit.register 防 main() 的 atexit 处理器注册进 pytest 进程
    # （_cleanup_multiprocessing/_log_process_exit 是进程级副作用——T2.5 走 True 分支会真实注册）
    with mock.patch("uvicorn.run") as mock_run, mock.patch.object(atexit, "register"):
        main_mod.main()
    mock_run.assert_called_once()


def test_t2_6_real_occupied_port_returns_false():
    """T2.6 真实端口占用语义：bind 127.0.0.1:0 + listen → _check_single_instance False"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.listen(1)
        assert main_mod._check_single_instance(port) is False
    finally:
        s.close()


def test_t2_7_niu_api_port_wiring(monkeypatch):
    """T2.7 NIU_API_PORT 接线：env 端口传给 _check_single_instance + SystemExit + uvicorn.run 不调"""
    random_port = random.randint(40000, 60000)
    captured = {}

    def fake_check(port):
        captured["port"] = port
        return False

    monkeypatch.setenv("NIU_API_PORT", str(random_port))
    monkeypatch.setattr(main_mod, "_check_single_instance", fake_check)
    monkeypatch.setattr(sys, "exit", _raise_system_exit_1)
    with mock.patch("uvicorn.run") as mock_run:
        with pytest.raises(SystemExit) as excinfo:
            main_mod.main()
    assert excinfo.value.code == 1
    assert captured["port"] == random_port, f"_check_single_instance 应收到 env 端口 {random_port}"
    mock_run.assert_not_called()


def test_t2_8_win32_no_setsockopt(monkeypatch):
    """T2.8 sys.platform=win32：bind 被调但 setsockopt 不被调（Windows 直接 bind 已满足）"""
    mock_sock = mock.MagicMock()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(socket, "socket", mock.MagicMock(return_value=mock_sock))
    assert main_mod._check_single_instance(12345) is True
    mock_sock.setsockopt.assert_not_called()
    mock_sock.bind.assert_called_once_with(("127.0.0.1", 12345))

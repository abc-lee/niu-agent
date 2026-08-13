"""主 Agent Current Time 每轮实时化测试。

背景：dynamic_system_prefix 曾在 __init__ 启动时固化 Current Time（注释"启动时固定，
不每轮更新"），导致主 Agent 所有轮次提示词 Current Time = 进程启动时刻。
修复后 Current Time 由 _assemble_system_message 每轮实时生成；
dynamic_system_prefix 只保留 disk_desc（启动缓存）。
"""
from datetime import datetime as _real_datetime
from unittest.mock import patch

from agent.runner import NiuRunner


def _make_runner():
    runner = NiuRunner.__new__(NiuRunner)  # 绕过 __init__ 的重资源加载
    runner.static_system_prompt = "STATIC"
    # 模拟修复后的生产形态：dynamic_system_prefix 只含 disk_desc（自带 \n\n 开头）
    runner.dynamic_system_prefix = "\n\n### [虚拟磁盘工具]\n...disk desc..."
    runner.default_model = "ark-code-latest"
    return runner


def test_assemble_current_time_uses_live_now():
    """Current Time 必须来自实时 datetime.now()，而非前缀里的固定值。"""
    runner = _make_runner()
    fixed = _real_datetime(2026, 8, 13, 18, 30, 0)
    with patch("agent.runner.datetime") as mock_dt:
        mock_dt.now.return_value = fixed
        messages = [{"role": "system", "content": ""}]
        runner._assemble_system_message(messages, "", "", model="ark-code-latest")
    content = messages[0]["content"]
    assert "Current Time: 2026-08-13 18:30:00" in content
    assert "disk desc" in content
    # 顺序与原 dynamic_prefix 语义一致：Current Time 在 disk_desc 之前
    assert content.index("Current Time") < content.index("disk desc")


def test_assemble_refreshes_each_call():
    """连续两次调用（不同 now）必须产生不同 Current Time——每轮实时而非缓存。"""
    runner = _make_runner()
    t1 = _real_datetime(2026, 8, 13, 18, 30, 0)
    t2 = _real_datetime(2026, 8, 13, 18, 31, 0)
    with patch("agent.runner.datetime") as mock_dt:
        mock_dt.now.side_effect = [t1, t2]
        m1 = [{"role": "system", "content": ""}]
        m2 = [{"role": "system", "content": ""}]
        runner._assemble_system_message(m1, "", "", model="ark-code-latest")
        runner._assemble_system_message(m2, "", "", model="ark-code-latest")
    assert "Current Time: 2026-08-13 18:30:00" in m1[0]["content"]
    assert "Current Time: 2026-08-13 18:31:00" in m2[0]["content"]


def test_assemble_current_time_in_dynamic_block_claude():
    """Claude 格式：Current Time 在动态段（content[1]），静态段 cache_control 不受影响。"""
    runner = _make_runner()
    runner.default_model = "claude-sonnet-4-6"
    fixed = _real_datetime(2026, 8, 13, 18, 30, 0)
    with patch("agent.runner.datetime") as mock_dt:
        mock_dt.now.return_value = fixed
        messages = [{"role": "system", "content": ""}]
        runner._assemble_system_message(messages, "", "", model="claude-sonnet-4-6")
    content = messages[0]["content"]
    assert isinstance(content, list)
    assert content[0]["text"] == "STATIC"
    assert content[0].get("cache_control") == {"type": "ephemeral"}
    assert "Current Time: 2026-08-13 18:30:00" in content[1]["text"]

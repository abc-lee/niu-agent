"""主 Agent Current Time 每轮实时化测试（D17/D19 缓存友好排布后）。

背景：dynamic_system_prefix 曾在 __init__ 启动时固化 Current Time，导致主 Agent
所有轮次提示词 Current Time = 进程启动时刻。修复后 Current Time 每轮实时生成，
且 D17 起不进 system 静态区——由 _assemble_system_message 产出动态块文本
（「[系统动态信息]」头 + injection + Current Time 时间最后），经
_refresh_dynamic_user_block 以 role=user 载体插入（D19）。
"""
from datetime import datetime as _real_datetime
from unittest.mock import patch

from agent.runner import NiuRunner


def _make_runner():
    runner = NiuRunner.__new__(NiuRunner)  # 绕过 __init__ 的重资源加载
    runner.static_system_prompt = "STATIC"
    runner.dynamic_system_prefix = "\n\n### [虚拟磁盘工具]\n...disk desc..."
    runner.default_model = "ark-code-latest"
    return runner


def _refresh(runner, messages, now):
    with patch("agent.runner.datetime") as mock_dt:
        mock_dt.now.return_value = now
        dynamic = runner._assemble_system_message(messages, "", "", model=runner.default_model)
        runner._refresh_dynamic_user_block(messages, dynamic)
    return dynamic


def test_current_time_uses_live_now_in_dynamic_block():
    """Current Time 必须来自实时 datetime.now()，且只出现在动态块、不进 system。"""
    runner = _make_runner()
    fixed = _real_datetime(2026, 8, 13, 18, 30, 0)
    messages = [
        {"role": "system", "content": ""},
        {"role": "user", "content": "当前输入"},
    ]
    _refresh(runner, messages, fixed)

    system_content = messages[0]["content"]
    assert isinstance(system_content, str)
    assert "Current Time" not in system_content, "system 静态区不含时间（D17）"
    assert "disk desc" in system_content, "disk_desc 并入静态区"

    block = messages[-2]
    assert messages[-1]["content"] == "当前输入"
    assert block["role"] == "user", "动态块载体 role=user（D19）"
    assert block["content"].startswith("[系统动态信息]")
    assert "Current Time: 2026-08-13 18:30:00" in block["content"]
    assert block["content"].index("[系统动态信息]") < block["content"].index("Current Time")


def test_assemble_refreshes_each_call():
    """连续两次调用（不同 now）必须产生不同 Current Time——每轮实时而非缓存。"""
    runner = _make_runner()
    t1 = _real_datetime(2026, 8, 13, 18, 30, 0)
    t2 = _real_datetime(2026, 8, 13, 18, 31, 0)
    m1 = [{"role": "system", "content": ""}, {"role": "user", "content": "输入"}]
    m2 = [{"role": "system", "content": ""}, {"role": "user", "content": "输入"}]
    _refresh(runner, m1, t1)
    _refresh(runner, m2, t2)
    assert "Current Time: 2026-08-13 18:30:00" in m1[-2]["content"]
    assert "Current Time: 2026-08-13 18:31:00" in m2[-2]["content"]

    # 同一对话连续两轮：旧块移除、新块就位，无叠加
    both = [{"role": "system", "content": ""}, {"role": "user", "content": "输入"}]
    _refresh(runner, both, t1)
    _refresh(runner, both, t2)
    blocks = [m for m in both if isinstance(m["content"], str) and m["content"].startswith("[系统动态信息]")]
    assert len(blocks) == 1
    assert "Current Time: 2026-08-13 18:31:00" in blocks[0]["content"]


def test_current_time_in_dynamic_block_claude_single_static_block():
    """Claude：静态区单 text 块 cache_control；Current Time 在 user 动态块内。"""
    runner = _make_runner()
    runner.default_model = "claude-sonnet-4-6"
    fixed = _real_datetime(2026, 8, 13, 18, 30, 0)
    messages = [
        {"role": "system", "content": ""},
        {"role": "user", "content": "当前输入"},
    ]
    _refresh(runner, messages, fixed)


    content = messages[0]["content"]
    assert isinstance(content, list)
    assert len(content) == 1, "Claude 静态区为单 text 块（D17）"
    static_block = content[0]
    assert isinstance(static_block, dict)
    assert static_block["text"] == "STATIC\n\n### [虚拟磁盘工具]\n...disk desc..."
    assert static_block.get("cache_control") == {"type": "ephemeral"}
    assert "Current Time: 2026-08-13 18:30:00" in messages[-2]["content"]

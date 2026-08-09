"""call_subagent_with_auto_answer helper 单元测试"""
import pytest
from unittest import mock


def test_helper_returns_directly_for_non_at_niu_result():
    """第一次返回非 @niu-agent 文本 → 直接返回"""
    from agent import subagent

    with mock.patch.object(subagent, "call_subagent", return_value="任务完成结果"):
        result = subagent.call_subagent_with_auto_answer(
            agent_name="file-processor",
            task="做 X",
            llm_config={"model": "test", "api_key": "test", "base_url": "http://localhost"},
        )
    assert result == "任务完成结果"


def test_helper_auto_replies_to_at_niu_question():
    """第一次返回 @niu-agent 问题 → 自动回复 → 第二次返回 @end → 返回最终结果"""
    from agent import subagent

    call_count = [0]
    def mock_call_subagent(**kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return "[file-processor-a1b2] 我该选哪个？"
        else:
            return "任务完成结果"

    with mock.patch.object(subagent, "call_subagent", side_effect=mock_call_subagent):
        result = subagent.call_subagent_with_auto_answer(
            agent_name="file-processor",
            task="做 X",
            llm_config={"model": "test", "api_key": "test", "base_url": "http://localhost"},
        )
    assert result == "任务完成结果"
    assert call_count[0] == 2


def test_helper_does_not_misidentify_normal_result():
    """子 Agent 正常结果含 [已完成] 不被误判为 @niu-agent 问题"""
    from agent import subagent

    with mock.patch.object(subagent, "call_subagent", return_value="[已完成] 文件 X 处理完毕"):
        result = subagent.call_subagent_with_auto_answer(
            agent_name="file-processor",
            task="做 X",
            llm_config={"model": "test", "api_key": "test", "base_url": "http://localhost"},
        )
    assert result == "[已完成] 文件 X 处理完毕"


def test_extract_unique_name_sync_path_plain_agent_name():
    """同步路径 [browser-operator] 问题 格式能被提取"""
    from agent.subagent import _extract_unique_name
    assert _extract_unique_name("[browser-operator] 第一个问题", "browser-operator") == "browser-operator"
    assert _extract_unique_name("[file-processor] 我该选哪个？", "file-processor") == "file-processor"


def test_extract_unique_name_async_path_hex_suffix_still_works():
    """异步路径 [file-processor-a1b2] 问题 格式仍能被提取（保持向后兼容）"""
    from agent.subagent import _extract_unique_name
    assert _extract_unique_name("[file-processor-a1b2] 第一个问题", "file-processor") == "file-processor-a1b2"
    assert _extract_unique_name("[browser-operator-708b] 问题", "browser-operator") == "browser-operator-708b"


def test_extract_unique_name_no_match_returns_none():
    """非 [子名] 格式返回 None"""
    from agent.subagent import _extract_unique_name
    assert _extract_unique_name("正常结果文本", "browser-operator") is None
    assert _extract_unique_name("[已完成] 任务结束", "browser-operator") is None
    assert _extract_unique_name("[browser-operator-x1yz] 问题", "browser-operator") is None  # x/y/z 非 hex


def test_call_subagent_with_auto_answer_sync_path_auto_replies(monkeypatch):
    """同步路径 [browser-operator] 问题 → call_subagent_with_auto_answer 自动回复"""
    from agent import subagent

    call_count = {"value": 0}
    call_args_log = []

    def fake_call_subagent(**kwargs):
        call_count["value"] += 1
        call_args_log.append(kwargs.copy())
        # 第一次返回 [browser-operator] 问题（同步路径格式）
        # 第二次返回正常结果
        if call_count["value"] == 1:
            return "[browser-operator] 第一个问题"
        return "子 Agent 完成"

    monkeypatch.setattr(subagent, "call_subagent", fake_call_subagent)

    result = subagent.call_subagent_with_auto_answer(
        agent_name="browser-operator",
        task="测试",
        llm_config={"model": "test", "api_key": "test", "base_url": "http://localhost"},
    )

    # 应自动回复一次，然后第二次返回正常结果
    assert call_count["value"] == 2, f"应调用 2 次，实际：{call_count['value']}"
    assert result == "子 Agent 完成"
    # 第二次调用应传 answer + answer_unique_name
    second_call = call_args_log[1]
    assert second_call.get("answer") is not None
    assert second_call.get("answer_unique_name") == "browser-operator"


def test_program_trigger_pushes_subagent_started_event():
    """程序触发首次调用推送 subagent_started（type/unique_name/agent_name/is_sync=False）"""
    from agent import subagent
    from niu_api import chat

    queued = []

    class FakeLoop:
        def is_closed(self):
            return False

        def call_soon_threadsafe(self, fn, *args):
            queued.append((fn, args))

    with mock.patch.object(subagent, "call_subagent", return_value="任务完成"), \
            mock.patch.object(chat, "_main_loop", FakeLoop()), \
            mock.patch.object(chat, "_sync_broadcast") as bc:
        result = subagent.call_subagent_with_auto_answer(
            agent_name="entity-extractor",
            task="做 X",
            llm_config={"model": "test", "api_key": "test", "base_url": "http://localhost"},
        )
    assert result == "任务完成"
    # 只统计广播调用（fn is bc）——真实 pre_register 内部会向 queued 追加 _do_pre_register 调度
    bc_calls = [(fn, args) for fn, args in queued if fn is bc]
    assert len(bc_calls) == 1
    fn, args = bc_calls[0]
    event = args[0]
    assert event["type"] == "subagent_started"
    assert event["unique_name"] == "entity-extractor"
    assert event["agent_name"] == "entity-extractor"
    assert event["is_sync"] is False


def test_recovery_answer_path_does_not_push_subagent_started():
    """自动回复恢复路径不重复推送 subagent_started——仅首次调用推 1 次"""
    from agent import subagent
    from niu_api import chat

    queued = []

    class FakeLoop:
        def is_closed(self):
            return False

        def call_soon_threadsafe(self, fn, *args):
            queued.append((fn, args))

    call_count = [0]

    def mock_call_subagent(**kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return "[entity-extractor] 我该选哪个？"  # 第一次 @niu-agent 提问
        return "任务完成结果"  # 自动回复后正常结束

    with mock.patch.object(subagent, "call_subagent", side_effect=mock_call_subagent), \
            mock.patch.object(chat, "_main_loop", FakeLoop()), \
            mock.patch.object(chat, "_sync_broadcast") as bc:
        result = subagent.call_subagent_with_auto_answer(
            agent_name="entity-extractor",
            task="做 X",
            llm_config={"model": "test", "api_key": "test", "base_url": "http://localhost"},
        )
    assert result == "任务完成结果"
    assert call_count[0] == 2
    # 仅首次调用推 1 次广播；恢复回答路径（while 循环内直调 call_subagent，answer 非 None）不推
    bc_calls = [(fn, args) for fn, args in queued if fn is bc]
    assert len(bc_calls) == 1


def test_pre_register_before_broadcast():
    """pre_register 先建 ring buffer，再推 subagent_started（防前端连 SSE 404 竞态）"""
    from agent import subagent
    from niu_api import chat
    from niu_api.internal import subagent_event_bus

    order = []

    class FakeLoop:
        def is_closed(self):
            return False

        def call_soon_threadsafe(self, fn, *args):
            order.append("broadcast")

    with mock.patch.object(subagent, "call_subagent", return_value="任务完成"), \
            mock.patch.object(subagent_event_bus, "pre_register",
                              side_effect=lambda name: order.append(f"pre_register:{name}")), \
            mock.patch.object(chat, "_main_loop", FakeLoop()), \
            mock.patch.object(chat, "_sync_broadcast"):
        subagent.call_subagent_with_auto_answer(
            agent_name="entity-extractor",
            task="做 X",
            llm_config={"model": "test", "api_key": "test", "base_url": "http://localhost"},
        )
    assert order == ["pre_register:entity-extractor", "broadcast"]


def test_no_event_when_main_loop_closed():
    """主事件循环关闭时不推送（容错回归：防止实现无条件推送导致 AttributeError/崩溃）"""
    from agent import subagent
    from niu_api import chat

    class ClosedLoop:
        def is_closed(self):
            return True

        # 无 call_soon_threadsafe 方法——若实现无条件调用会 AttributeError，本测试可抓出

    with mock.patch.object(subagent, "call_subagent", return_value="任务完成"), \
            mock.patch.object(chat, "_main_loop", ClosedLoop()), \
            mock.patch.object(chat, "_sync_broadcast") as bc:
        result = subagent.call_subagent_with_auto_answer(
            agent_name="entity-extractor",
            task="做 X",
            llm_config={"model": "test", "api_key": "test", "base_url": "http://localhost"},
        )
    assert result == "任务完成"
    bc.assert_not_called()


def test_exception_path_closes_and_reraises():
    """call_subagent 抛异常 → 无条件 close（防 ring buffer 泄漏 + tab 卡死）+ 异常继续传播"""
    from agent import subagent
    from niu_api import chat
    from niu_api.internal import subagent_event_bus

    class FakeLoop:
        def is_closed(self):
            return False

        def call_soon_threadsafe(self, fn, *args):
            pass

    with mock.patch.object(subagent, "call_subagent", side_effect=RuntimeError("boom")), \
            mock.patch.object(chat, "_main_loop", FakeLoop()), \
            mock.patch.object(chat, "_sync_broadcast"), \
            mock.patch.object(subagent_event_bus, "close") as close_mock:
        with pytest.raises(RuntimeError):
            subagent.call_subagent_with_auto_answer(
                agent_name="entity-extractor",
                task="做 X",
                llm_config={"model": "test", "api_key": "test", "base_url": "http://localhost"},
            )
    close_mock.assert_called_once_with("entity-extractor")


def test_error_prefix_result_closes():
    """首调用返回 '[错误]' 前缀（register 失败值错误路径）→ close 清理 tab"""
    from agent import subagent
    from niu_api import chat
    from niu_api.internal import subagent_event_bus

    class FakeLoop:
        def is_closed(self):
            return False

        def call_soon_threadsafe(self, fn, *args):
            pass

    with mock.patch.object(subagent, "call_subagent",
                            return_value="[错误] 同名实例已存在。请先回复当前挂起的子 Agent。"), \
            mock.patch.object(chat, "_main_loop", FakeLoop()), \
            mock.patch.object(chat, "_sync_broadcast"), \
            mock.patch.object(subagent_event_bus, "close") as close_mock:
        result = subagent.call_subagent_with_auto_answer(
            agent_name="entity-extractor",
            task="做 X",
            llm_config={"model": "test", "api_key": "test", "base_url": "http://localhost"},
        )
    assert result.startswith("[错误]")
    close_mock.assert_called_once_with("entity-extractor")

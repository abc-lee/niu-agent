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


def test_helper_auto_replies_to_t2_format_question():
    """第一次返回 T2 注入格式提问（【子Agent提问·需回复】[name]）→ 自动回复 → 正常返回"""
    from agent import subagent

    call_count = [0]

    def mock_call_subagent(**kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return "【子Agent提问·需回复】[file-processor-a1b2]\n我该选哪个？"
        return "任务完成结果"

    with mock.patch.object(subagent, "call_subagent", side_effect=mock_call_subagent):
        result = subagent.call_subagent_with_auto_answer(
            agent_name="file-processor",
            task="做 X",
            llm_config={"model": "test", "api_key": "test", "base_url": "http://localhost"},
        )
    assert result == "任务完成结果"
    assert call_count[0] == 2  # 提问被识别 → 自动回复一次 → 正常结束


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


def test_extract_unique_name_t2_format_header_extracts_name():
    """T2 注入格式 【子Agent提问·需回复】[unique_name] 提问能被提取（同步+异步路径）"""
    from agent.subagent import _extract_unique_name
    # 同步路径：纯 agent_name
    assert _extract_unique_name(
        "【子Agent提问·需回复】[file-processor]\n我该选哪个？", "file-processor"
    ) == "file-processor"
    # 异步路径：agent_name-4位hex
    assert _extract_unique_name(
        "【子Agent提问·需回复】[file-processor-a1b2]\n我该选哪个？", "file-processor"
    ) == "file-processor-a1b2"


def test_extract_unique_name_t2_format_no_match_returns_none():
    """T2 格式但 agent 名不匹配 / 缺换行（非拼装产物）→ None"""
    from agent.subagent import _extract_unique_name
    # 其他子 Agent 的提问注入消息
    assert _extract_unique_name(
        "【子Agent提问·需回复】[other-agent]\n问题", "browser-operator"
    ) is None
    # 未按 T2 拼装（] 后无 \n）不匹配——严格匹配避免误判
    assert _extract_unique_name("【子Agent提问·需回复】[browser-operator] 问题", "browser-operator") is None


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


def test_error_prefix_does_not_close_when_same_name_instance_live():
    """[错误] 路径归属守卫：同名实例存活（并发触发场景）时不 close——tab 归活跃实例所有"""
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
            mock.patch.object(subagent.SubagentRegistry, "get", return_value=object()), \
            mock.patch.object(subagent_event_bus, "close") as close_mock:
        result = subagent.call_subagent_with_auto_answer(
            agent_name="entity-extractor",
            task="做 X",
            llm_config={"model": "test", "api_key": "test", "base_url": "http://localhost"},
        )
    assert result.startswith("[错误]")
    close_mock.assert_not_called()


# ==================== report_sink 出参：@end {"report": "..."} 例外反馈通道 ====================


def _run_helper_once(exit_content, report_sink=None):
    """mock call_subagent 返回固定 exit_content，跑 helper 一次（无提问循环）。"""
    from agent import subagent

    with mock.patch.object(subagent, "call_subagent", return_value=exit_content):
        return subagent.call_subagent_with_auto_answer(
            agent_name="journal-daily-agent",
            task="做 X",
            llm_config={"model": "test", "api_key": "test", "base_url": "http://localhost"},
            report_sink=report_sink,
        )


def test_report_sink_extracts_tail_json_report():
    """JSON 成功：尾部 {"report": "..."} → 提取进 sink，返回值原样不变"""
    sink = []
    result = _run_helper_once('{"report": "整理完成，无异常"}', report_sink=sink)
    assert sink == ["整理完成，无异常"]
    assert result == '{"report": "整理完成，无异常"}'


def test_report_sink_production_form_body_prefix_plus_end_json():
    """生产形态：exit_content = `汇报正文 {"report": "xxx"}`（@end 已剥，正文+@end后 JSON 拼接）→ 提取成功"""
    sink = []
    result = _run_helper_once(
        '已整理 3 条日志 {"report": "磁盘空间不足，无法写入 journal.md"}',
        report_sink=sink,
    )
    assert sink == ["磁盘空间不足，无法写入 journal.md"]
    assert result == '已整理 3 条日志 {"report": "磁盘空间不足，无法写入 journal.md"}'


def test_report_sink_loose_regex_fallback_on_malformed_json():
    """宽松降级：尾部 JSON 畸形（引号闭合但缺 }）json.loads 失败 → 宽松正则尾部锚定兜底"""
    sink = []
    _run_helper_once('汇报正文 {"report": "未闭合 JSON 的反馈"', report_sink=sink)
    assert sink == ["未闭合 JSON 的反馈"]


def test_report_sink_no_report_silent_and_empty():
    """失败静默：无 report 的正常结果 → sink 保持空，返回值不变（与现状一致）"""
    sink = []
    result = _run_helper_once("正常结果文本，没有任何反馈", report_sink=sink)
    assert sink == []
    assert result == "正常结果文本，没有任何反馈"


def test_report_sink_default_none_backward_compatible():
    """不传 report_sink（既有 3 处调用方形态）→ 零影响：正常返回，不报错不提取"""
    from agent import subagent

    with mock.patch.object(subagent, "call_subagent",
                           return_value='汇报正文 {"report": "不该被任何人消费"}'):
        result = subagent.call_subagent_with_auto_answer(
            agent_name="journal-daily-agent",
            task="做 X",
            llm_config={"model": "test", "api_key": "test", "base_url": "http://localhost"},
        )
    assert result == '汇报正文 {"report": "不该被任何人消费"}'


def test_report_sink_final_report_after_multiple_questions():
    """多次提问后最终 report：提问轮不提取，最终退出轮的 report 进 sink"""
    from agent import subagent

    call_count = [0]

    def mock_call_subagent(**kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return "【子Agent提问·需回复】[journal-daily-agent]\n要先清理旧文件吗？"
        return '已处理 {"report": "遇到权限问题，跳过了 2 个文件"}'

    sink = []
    with mock.patch.object(subagent, "call_subagent", side_effect=mock_call_subagent):
        result = subagent.call_subagent_with_auto_answer(
            agent_name="journal-daily-agent",
            task="做 X",
            llm_config={"model": "test", "api_key": "test", "base_url": "http://localhost"},
            report_sink=sink,
        )
    assert call_count[0] == 2
    assert sink == ["遇到权限问题，跳过了 2 个文件"]
    assert result == '已处理 {"report": "遇到权限问题，跳过了 2 个文件"}'


def test_report_sink_strips_degradation_annotation_with_bracket_in_reason():
    """降级标注后缀+report：reason 含 ]（[Errno 2] 形态）也必须剥掉标注再提取——

    钉住字符类修复：剥离正则用 [^\\n]* 不用 [^]]*（[^]]* 在 reason 含 ] 时失配 →
    标注残留 → 尾部 JSON 解析失败 → report 静默丢失）。
    """
    sink = []
    _run_helper_once(
        '汇报正文 {"report": "xxx"}\n'
        '[子 Agent 提示词降级: 系统提示词构建失败：[Errno 2] No such file or directory]',
        report_sink=sink,
    )
    assert sink == ["xxx"]


def test_extract_exit_report_json_without_report_key_returns_none():
    """JSON dict 但无 report 键 / report 非 str → None（不误判游标类 JSON 结果）"""
    from agent.subagent import _extract_exit_report
    assert _extract_exit_report('{"cursor": "abc", "status": "ok"}') is None
    assert _extract_exit_report('{"report": 123}') is None


def test_extract_exit_report_plain_report_colon_fallback():
    """宽松正则第二形态：`report: xxx`（无引号无花括号）尾部锚定提取"""
    from agent.subagent import _extract_exit_report
    assert _extract_exit_report("汇报\nreport: 简单反馈") == "简单反馈"


def test_extract_exit_report_non_string_and_empty_return_none():
    """非 str / 空串 → None（防御性，永不抛异常中断退出）"""
    from agent.subagent import _extract_exit_report
    assert _extract_exit_report(None) is None
    assert _extract_exit_report("") is None
    assert _extract_exit_report(123) is None

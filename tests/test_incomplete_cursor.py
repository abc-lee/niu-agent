"""Task 4 测试：未完成结果（incomplete JSON）游标不推进。

覆盖：
1. _is_subagent_incomplete 严格判定（R3-13 六例）
2. _tidy_context_impl sleep 模式集成（独立 fixture；T7 后 journal 游标钩子退役，
   handler._update_journal_cursor 测试随符号整删）

fixture 数学验证（R6-B）：2 条消息 × _FakeCalc 100 token/条 = 200 tokens，
_read_context_window_tokens=8000。
"""
import json
from unittest import mock

from niu_api.compat import _incomplete_reason, _is_subagent_incomplete

# ---------------------------------------------------------------------------
# 1. _is_subagent_incomplete 单元测试（R3-13）
# ---------------------------------------------------------------------------

class TestIsSubagentIncomplete:
    def test_incomplete_json_true(self):
        assert _is_subagent_incomplete(
            '{"incomplete": true, "agent": "a", "reason": "STOPPED", "partial_result": ""}'
        ) is True

    def test_incomplete_false_literal_not_matched(self):
        assert _is_subagent_incomplete('{"incomplete": false}') is False

    def test_incomplete_string_true_not_matched(self):
        # 严格 is True 判定：字符串 "true" 不命中
        assert _is_subagent_incomplete('{"incomplete": "true"}') is False

    def test_overflow_json_not_matched(self):
        assert _is_subagent_incomplete(
            '{"overflow": true, "agent": "a", "turns_completed": 5, "tokens_used": 1, "tokens_limit": 2, "partial_result": ""}'
        ) is False

    def test_plain_text_not_matched(self):
        assert _is_subagent_incomplete("处理完成 @end processed_up_to=5") is False

    def test_malformed_json_not_matched(self):
        assert _is_subagent_incomplete('{"incomplete": true, broken') is False

    def test_incomplete_reason_extraction(self):
        assert _incomplete_reason(
            '{"incomplete": true, "agent": "a", "reason": "MAX_TURNS_EXCEEDED", "partial_result": ""}'
        ) == "MAX_TURNS_EXCEEDED"
        assert _incomplete_reason("plain text") == ""
        assert _incomplete_reason('{"overflow": true}') == ""

    def test_incomplete_reason_malformed_json_empty(self):
        # 畸形 JSON：json.loads 失败 → reason 空串（不抛异常）
        assert _incomplete_reason('{"incomplete": true, broken') == ""

    def test_incomplete_reason_non_incomplete_json_empty(self):
        # 非 incomplete JSON（incomplete: false / 无 incomplete 键）→ reason 空串
        assert _incomplete_reason('{"incomplete": false}') == ""
        assert _incomplete_reason('{"ok": true}') == ""


# ---------------------------------------------------------------------------
# 2. _tidy_context_impl sleep 模式一集成（独立 fixture）
# ---------------------------------------------------------------------------

INCOMPLETE_JSON = json.dumps({
    "incomplete": True,
    "agent": "context-manager",
    "reason": "TERMINATED_BY_SUPPLEMENT",
    "partial_result": "再精简几个小工具输出：idx:33",
})
NORMAL_JSON = json.dumps({"ok": True})  # 非 overflow / 非 incomplete 的正常返回


class _FakeCalc:
    def count_message_single(self, role, content, tool_calls=None):
        return 100


class _FakeRunner:
    def __init__(self):
        self.llm_config = {"model": "m", "apikey": "x", "apibase": "http://x"}
        self.handler = mock.MagicMock()
        self.handler._last_prompt_tokens = 0

    def _ensure_session_chain(self, max_days: int = 10) -> None:
        # dream 阶段收尾补链（真函数依赖 LightRAG，测试桩空操作）
        return None


def _tidy_messages():
    msgs = []
    for i, mid in enumerate(["m1", "m2"]):
        m = mock.MagicMock()
        m.id = mid
        m.role = "user"
        m.content = f"hello {i}"
        m.tool_calls = None
        m.tool_call_id = None
        msgs.append(m)
    return msgs


def _tidy_incomplete_patches(subagent_result, call_mock):
    """Task 4 独立 fixture（R3-9：不动 test_stop_interruptible 的 _tidy_common_patches）。

    R4-10 patch 清单显式化：
    - 四个游标文件 READ 强制 cursor=''（R3-4）：Path.exists→False（缺失文件 → 游标留空）
    - 窗口 8000 / _FakeCalc
    - 游标读取路径不 patch（T7 后 sleep 管道已无 journal 游标读取）
    - _write_cursor_with_lock → MagicMock（记录调用，测试 hermetic）
    """
    return [
        mock.patch("agent.token_calculator.TokenCalculator.get", return_value=_FakeCalc()),
        mock.patch("niu_api.compat._read_context_window_tokens", return_value=8000),
        mock.patch("niu_api.chat.get_or_create_runner", return_value=_FakeRunner()),
        mock.patch("agent.subagent.call_subagent_with_auto_answer", call_mock),
        # builder refetch lightrag 段（T2 后无参内部 refetch）——mock 隔离，不读真实用户配置
        mock.patch("niu_api.llm_proxy.get_llm_config", return_value={
            "model": "test-model", "apikey": "test-key", "apibase": "https://test.example.com",
            "type": "openai", "provider": "", "reasoning_effort": "", "litellm_kwargs": {},
        }),
        # T5：sleep 管道测试保持睡眠态（CP1-CP3 检查点需 is_sleeping=True 才不打断）
        mock.patch("niu_api.compat.is_sleeping", return_value=True),
        # 四个游标文件 READ 强制 cursor=''（R3-4）：Path.exists→False（缺失文件 → 游标留空）。
        # compat.py 在函数内 `from pathlib import Path`，无模块级 Path，故 patch 类方法本身
        mock.patch("pathlib.Path.exists", return_value=False),
        mock.patch("niu_api.compat._write_cursor_with_lock"),
    ]


class TestTidyContextImplIncomplete:
    def _run_sleep_tidy(self, subagent_result):
        """v2：种子记录写入隔离 F1 使 entity 步真实执行。

        门控已随工程四重排摘除（决策 2）——无 cursor_value 参与对应 patch 缝。

        成功结果（NORMAL_JSON）补 processed_line 触发 relay 剪切；incomplete 结果
        保持原样（F1 不剪切契约）。F2 patch 到测试专用 tmp。返回附 (f1, f2) 路径。
        """
        import asyncio
        import os as _os
        import tempfile
        from contextlib import ExitStack

        import agent.md_mirror as mdm
        from niu_api.compat import _tidy_context_impl

        store = mock.MagicMock()
        store.get_messages = mock.AsyncMock(return_value=_tidy_messages())
        call_mock = mock.MagicMock()

        def _keyed(agent_name=None, **kwargs):
            if agent_name == "entity-extractor" and subagent_result == NORMAL_JSON:
                return NORMAL_JSON + "\n处理完成 @end\nprocessed_line=999999"
            return subagent_result

        call_mock.side_effect = _keyed
        f2_path = _os.path.join(tempfile.mkdtemp(prefix="inc_relay_"), "f2.md")
        with ExitStack() as stack:
            stack.enter_context(mock.patch("niu_api.compat.get_message_store", new=mock.AsyncMock(return_value=store)))
            for p in _tidy_incomplete_patches(subagent_result, call_mock):
                stack.enter_context(p)
            stack.enter_context(mock.patch("agent.md_mirror.F2_PATH", f2_path))
            write_mock = stack.enter_context(mock.patch("niu_api.compat._write_cursor_with_lock"))
            block = mdm.format_message_record(
                msg_id="inc-seed-not-in-db", created_at="t", role="user", content="种子",
            )
            assert mdm.append_record(block, mdm.F1_PATH)
            result = asyncio.run(_tidy_context_impl({"mode": "sleep", "session_id": "t"}, chat_lock_already_held=True))
        return result, write_mock, call_mock, mdm.F1_PATH, f2_path

    @staticmethod
    def _cursor_writes(write_mock):
        return [call.args[1] for call in write_mock.call_args_list]

    def test_incomplete_result_does_not_advance_any_cursor(self):
        """entity 收 incomplete JSON → 游标不推进（T6：cm 腿退役，仅 entity 路径）。

        entity 收 incomplete（F1 不剪切）→ F2 空 → 梦境循环 D5 短路（dream 不调用）
        → 终态 ok。判别力：_write_cursor_with_lock 零次调用（游标全不动）、F1 原样保留。
        """
        result, write_mock, call_mock, f1, _f2 = self._run_sleep_tidy(INCOMPLETE_JSON)
        assert result.get("status") == "ok", f"incomplete 吸收应续跑至终态 ok: {result}"
        called_agents = [c.kwargs.get("agent_name") for c in call_mock.call_args_list]
        assert called_agents == ["entity-extractor"], (
            f"T6 后无 cm；F2 空梦境循环 D5 短路（dream 不调用），实际 {called_agents}"
        )
        writes = self._cursor_writes(write_mock)
        assert writes == [], f"incomplete 结果不应写任何游标: {writes}"
        with open(f1, encoding="utf-8") as f:
            assert '"msg_id": "inc-seed-not-in-db"' in f.read(), "incomplete 时 F1 不得被剪切"

    def test_normal_result_cuts_f1_no_compress_cursor(self):
        """对照：正常返回时 relay 剪切执行、且 compress 游标零写（T6 退役反向钉）。

        entity 成功 → relay 剪切 F1 至空 → 梦境循环删空 F2；compress 游标已随压缩退役消亡。
        """
        result, write_mock, _call, _f1, _f2 = self._run_sleep_tidy(NORMAL_JSON)
        assert result.get("status") == "ok", f"tidy 应正常结束: {result}"
        compress_writes = [
            d for d in self._cursor_writes(write_mock)
            if d.get("last_compress_id")
        ]
        assert compress_writes == [], "compress 游标已退役，零写"



# ---------------------------------------------------------------------------
# 4. _run_subagent_async 完成通知文案（incomplete / 正常 双分支）
# ---------------------------------------------------------------------------

class TestRunSubagentAsyncNotification:
    """_run_subagent_async 推送到 MainAgentRequestQueue 的通知文案分支。

    incomplete JSON → "[名] 未完成（reason），已保留进度…"；
    正常 result → "[名] 已完成，结果：{last_reply or result}"。
    mock call_subagent + 捕获 queue.push，不碰真实队列 / 注册表 / PendingAskRegistry。
    """

    def _run(self, subagent_result, registry_get=None, call_impl=None):
        import asyncio

        from agent.subagent import _run_subagent_async

        pushes = []
        fake_queue = mock.MagicMock()
        fake_queue.push.side_effect = lambda msg: pushes.append(msg)
        unregister = mock.MagicMock()
        # call_impl：mock call_subagent 的可调用实现（默认恒返 subagent_result）。
        # E4 T3 P2：降级测试需要 call_subagent 在 worker 线程内置位 TLS 标记，故支持可调用。
        # 默认实现先清零标记——to_thread worker 线程可复用（防跨测试 TLS 残留串扰，
        # 与生产 call_subagent 每次调用起始清零同语义）。
        from agent import subagent
        subagent._set_subagent_prompt_degraded_reason(None)  # 主（测试）线程清零，防前置残留
        if call_impl is None:
            def _default_call(*args, **kwargs):
                subagent._set_subagent_prompt_degraded_reason(None)
                return subagent_result
            call_impl = _default_call
        with mock.patch("agent.subagent.call_subagent", side_effect=call_impl), \
             mock.patch("agent.main_agent_request_queue.get_main_agent_request_queue", return_value=fake_queue), \
             mock.patch("agent.ask_main_agent.get_pending_ask_registry", return_value=mock.MagicMock()), \
             mock.patch("agent.ask_user.get_user_ask_registry", return_value=mock.MagicMock()), \
             mock.patch("agent.subagent_registry.SubagentRegistry.get", return_value=registry_get), \
             mock.patch("agent.subagent_registry.SubagentRegistry.unregister", unregister):
            asyncio.run(_run_subagent_async(
                unique_name="dream-evolver-1a2b",
                agent_name="dream-evolver",
                task="精加工实体",
                llm_config={"model": "m", "apikey": "x", "apibase": "http://x"},
                memory_context=None,
                supplement_queue=None,
            ))
        return pushes, unregister

    def test_incomplete_result_pushes_unfinished_notification(self):
        """incomplete JSON → 通知含「未完成（reason）」+ 已保留进度，不含「已完成」。"""
        pushes, unregister = self._run(INCOMPLETE_JSON)
        assert len(pushes) == 1
        msg = pushes[0]
        assert msg.startswith("[dream-evolver-1a2b] 未完成（TERMINATED_BY_SUPPLEMENT）"), msg
        assert "已保留进度" in msg
        assert "已完成" not in msg
        # finally 收尾：注册表注销（防泄漏）
        unregister.assert_called_once_with("dream-evolver-1a2b")

    def test_normal_result_prefers_last_reply_in_notification(self):
        """正常 result + 注册表实例有 last_reply → 通知用 last_reply（中间轮次不挤占最终报告）。"""
        inst = mock.MagicMock()
        inst.last_reply = "最终报告：实体精加工完成"
        pushes, _ = self._run("处理完成 @end processed_up_to=2", registry_get=inst)
        assert pushes == ["[dream-evolver-1a2b] 已完成，结果：最终报告：实体精加工完成"]

    def test_normal_result_falls_back_to_raw_result_without_instance(self):
        """正常 result + 注册表无实例（last_reply 取不到）→ 通知回退用原始 result。"""
        pushes, _ = self._run("处理完成 @end processed_up_to=2", registry_get=None)
        assert pushes == ["[dream-evolver-1a2b] 已完成，结果：处理完成 @end processed_up_to=2"]

    def test_degraded_prompt_annotates_completion_notification(self):
        """E4 T3 P2：异步完成通知——worker 线程置位降级标记 → completion_msg 追加降级标注。

        完整链：call_subagent（mock 内同线程置位 TLS 标记）→ completion_msg 构造在同一
        worker 线程 annotate（threading.local 可读）→ 队列消息含降级标注。
        P1 防护：标记是 thread-local——worker 线程置位，主（测试）线程读不到（无串扰）。
        """
        from agent import subagent

        def _degraded_call_subagent(*args, **kwargs):
            subagent._set_subagent_prompt_degraded_reason("系统提示词构建失败：boom")
            return "处理完成 @end processed_up_to=2"

        pushes, _ = self._run("unused", call_impl=_degraded_call_subagent)
        assert len(pushes) == 1
        assert pushes[0] == (
            "[dream-evolver-1a2b] 已完成，结果：处理完成 @end processed_up_to=2\n"
            "[子 Agent 提示词降级: 系统提示词构建失败：boom]"
        )
        # P1：标记是 thread-local——worker 线程置位，主（测试）线程不可见（防并发串扰）
        assert subagent._get_subagent_prompt_degraded_reason() is None

    def test_degraded_prompt_annotates_incomplete_completion(self):
        """E4 T3 P2：incomplete JSON 结果的完成通知同样追加降级标注（消息文本非 JSON 消费——追加安全）。"""
        from agent import subagent

        def _degraded_call_subagent(*args, **kwargs):
            subagent._set_subagent_prompt_degraded_reason("系统提示词构建失败：boom")
            return INCOMPLETE_JSON

        pushes, _ = self._run("unused", call_impl=_degraded_call_subagent)
        assert len(pushes) == 1
        msg = pushes[0]
        assert msg.startswith("[dream-evolver-1a2b] 未完成（TERMINATED_BY_SUPPLEMENT）")
        assert msg.endswith("[子 Agent 提示词降级: 系统提示词构建失败：boom]")

    # ------------------------------------------------------------------
    # E4-05：异常 / 取消路径——4 处推送点统一 logger.error（含异常），推送失败语义保持
    # ------------------------------------------------------------------

    def _run_exception_path(self, queue_push=None, notify=None):
        """call_subagent 抛异常 → 跑完整异常路径；返回 (pushes, notify_events, error_logs)。"""
        import asyncio

        from loguru import logger

        from agent.subagent import _run_subagent_async

        pushes = []
        fake_queue = mock.MagicMock()
        if queue_push is None:
            fake_queue.push.side_effect = lambda msg: pushes.append(msg)
        else:
            fake_queue.push.side_effect = queue_push
        unregister = mock.MagicMock()
        notify_events = []

        if notify is None:
            def notify_impl(name, etype, payload):
                notify_events.append((name, etype, payload))
        else:
            notify_impl = notify
        messages = []
        sink_id = logger.add(lambda m: messages.append(str(m)), level="ERROR")
        try:
            with mock.patch("agent.subagent.call_subagent", side_effect=RuntimeError("subagent boom")), \
                 mock.patch("agent.main_agent_request_queue.get_main_agent_request_queue", return_value=fake_queue), \
                 mock.patch("agent.ask_main_agent.get_pending_ask_registry", return_value=mock.MagicMock()), \
                 mock.patch("agent.ask_user.get_user_ask_registry", return_value=mock.MagicMock()), \
                 mock.patch("agent.subagent_registry.SubagentRegistry.get", return_value=None), \
                 mock.patch("agent.subagent_registry.SubagentRegistry.unregister", unregister), \
                 mock.patch("niu_api.internal.subagent_event_bus.notify_subagent_event_sync", side_effect=notify_impl):
                asyncio.run(_run_subagent_async(
                    unique_name="dream-evolver-1a2b",
                    agent_name="dream-evolver",
                    task="精加工实体",
                    llm_config={"model": "m", "apikey": "x", "apibase": "http://x"},
                    memory_context=None,
                    supplement_queue=None,
                ))
        finally:
            logger.remove(sink_id)
        return pushes, notify_events, messages, unregister

    def test_exception_path_pushes_error_notification_and_notify_event(self):
        """call_subagent 抛异常 → 推 [名] 异常结束 通知 + subagent_error 事件 + logger.error 记录异常本体。"""
        pushes, notify_events, messages, unregister = self._run_exception_path()
        assert pushes == ["[dream-evolver-1a2b] 异常结束：subagent boom"]
        assert notify_events == [("dream-evolver-1a2b", "subagent_error", {"content": "subagent boom"})]
        assert any("异常：subagent boom" in m for m in messages), (
            f"异常本体应记 error 含异常文本，实际: {messages}"
        )
        unregister.assert_called_once_with("dream-evolver-1a2b")

    def test_exception_path_push_failures_logged_but_swallowed(self):
        """异常路径：queue.push / notify 抛异常 → 各自 logger.error（推送失败语义保持——db_monitor 轮询兜底）。"""
        pushes, notify_events, messages, unregister = self._run_exception_path(
            queue_push=mock.MagicMock(side_effect=RuntimeError("queue boom")),
            notify=mock.MagicMock(side_effect=RuntimeError("notify boom")),
        )
        assert pushes == []  # 推送失败不中断异常路径
        assert notify_events == []
        assert any("异常通知推送失败" in m and "queue boom" in m for m in messages), messages
        assert any("subagent_error 事件推送失败" in m and "notify boom" in m for m in messages), messages
        unregister.assert_called_once_with("dream-evolver-1a2b")

    def _run_cancel_path(self, queue_push=None, notify=None):
        """cancel 进行中的 _run_subagent_async（call_subagent 阻塞中）→ 跑完整取消路径。"""
        import asyncio
        import threading

        from loguru import logger

        from agent.subagent import _run_subagent_async

        pushes = []
        fake_queue = mock.MagicMock()
        if queue_push is None:
            fake_queue.push.side_effect = lambda msg: pushes.append(msg)
        else:
            fake_queue.push.side_effect = queue_push
        unregister = mock.MagicMock()
        notify_events = []

        if notify is None:
            def notify_impl(name, etype, payload):
                notify_events.append((name, etype, payload))
        else:
            notify_impl = notify
        started = threading.Event()
        release = threading.Event()
        messages = []
        sink_id = logger.add(lambda m: messages.append(str(m)), level="ERROR")

        def _blocking_call(*a, **kw):
            started.set()
            release.wait(timeout=5)
            return "done"

        try:
            with mock.patch("agent.subagent.call_subagent", side_effect=_blocking_call), \
                 mock.patch("agent.main_agent_request_queue.get_main_agent_request_queue", return_value=fake_queue), \
                 mock.patch("agent.ask_main_agent.get_pending_ask_registry", return_value=mock.MagicMock()), \
                 mock.patch("agent.ask_user.get_user_ask_registry", return_value=mock.MagicMock()), \
                 mock.patch("agent.subagent_registry.SubagentRegistry.get", return_value=None), \
                 mock.patch("agent.subagent_registry.SubagentRegistry.unregister", unregister), \
                 mock.patch("niu_api.internal.subagent_event_bus.notify_subagent_event_sync", side_effect=notify_impl):
                loop = asyncio.new_event_loop()
                try:
                    task = loop.create_task(_run_subagent_async(
                        unique_name="dream-evolver-1a2b",
                        agent_name="dream-evolver",
                        task="精加工实体",
                        llm_config={"model": "m", "apikey": "x", "apibase": "http://x"},
                        memory_context=None,
                        supplement_queue=None,
                    ))
                    loop.run_until_complete(asyncio.sleep(0.05))
                    task.cancel()
                    try:
                        loop.run_until_complete(task)
                    except asyncio.CancelledError:
                        pass  # 预期：CancelledError 重新抛出
                    finally:
                        release.set()  # 释放后台 to_thread 线程防泄漏
                    loop.run_until_complete(asyncio.sleep(0.1))
                finally:
                    loop.close()
        finally:
            logger.remove(sink_id)
            release.set()
        return pushes, notify_events, messages, unregister

    def test_cancel_path_pushes_cancel_notification_and_rethrows(self):
        """CancelledError → 推取消通知 + subagent_error 事件 + 重新抛出 CancelledError。"""
        pushes, notify_events, messages, unregister = self._run_cancel_path()
        assert pushes == ["[dream-evolver-1a2b] 被取消（应用关闭或主 Agent 停止）"]
        assert notify_events == [("dream-evolver-1a2b", "subagent_error", {"content": "子 Agent 被取消"})]
        unregister.assert_called_once_with("dream-evolver-1a2b")

    def test_cancel_path_push_failures_logged_but_swallowed(self):
        """取消路径：queue.push / notify 抛异常 → 各自 logger.error（推送失败不阻断取消语义）。"""
        pushes, notify_events, messages, unregister = self._run_cancel_path(
            queue_push=mock.MagicMock(side_effect=RuntimeError("queue boom")),
            notify=mock.MagicMock(side_effect=RuntimeError("notify boom")),
        )
        assert pushes == []
        assert notify_events == []
        assert any("取消通知推送失败" in m and "queue boom" in m for m in messages), messages
        assert any("取消事件推送失败" in m and "notify boom" in m for m in messages), messages
        unregister.assert_called_once_with("dream-evolver-1a2b")


# ---------------------------------------------------------------------------
# 5. handler._call_subagent_gen display_result 转换（incomplete JSON → 自然语言）
# ---------------------------------------------------------------------------

class TestHandlerCallSubagentGenDisplay:
    """_call_subagent_gen 同步路径：incomplete JSON → display_result 转为自然语言提示；
    非 incomplete 结果原样透传。断言 StepOutcome.data["result"]（返回 LLM 的副本）。"""

    def _drive(self, subagent_result, agent_name="dream-evolver"):
        from agent.handler import NiuHandler

        handler = NiuHandler(mcp_client=None)
        fake_runner = mock.MagicMock()
        fake_runner.llm_config = {"model": "m", "apikey": "x", "apibase": "http://x"}
        with mock.patch("agent.runner.get_runner", return_value=fake_runner), \
             mock.patch("agent.subagent.call_subagent", return_value=subagent_result), \
             mock.patch("agent.subagent_registry.SubagentRegistry.get", return_value=None), \
             mock.patch("niu_api.internal.subagent_event_bus.pre_register"), \
             mock.patch("niu_api.internal.subagent_event_bus.has_subagent", return_value=False), \
             mock.patch("niu_api.chat._main_loop", None):
            gen = handler._call_subagent_gen(agent_name, {"task": "精加工实体"})
            try:
                while True:
                    next(gen)
            except StopIteration as si:
                return si.value
        raise AssertionError("generator 未返回 StepOutcome")

    def test_incomplete_json_display_result_converted_to_natural_language(self):
        """incomplete JSON → display_result 是自然语言提示（非 JSON），含 reason 与处置建议。"""
        outcome = self._drive(INCOMPLETE_JSON)
        assert outcome.data["status"] == "success"
        display = outcome.data["result"]
        assert display.startswith("子Agent未完成任务（TERMINATED_BY_SUPPLEMENT）"), display
        assert "已保留进度" in display
        assert "请决定是否让子Agent继续处理" in display
        assert not display.strip().startswith("{")  # 返回 LLM 的是自然语言，非原始 JSON

    def test_normal_result_passed_through_unchanged(self):
        """非 incomplete 结果（纯文本）→ display_result 原样透传。"""
        outcome = self._drive("处理完成 @end processed_up_to=2")
        assert outcome.data["status"] == "success"
        assert outcome.data["result"] == "处理完成 @end processed_up_to=2"

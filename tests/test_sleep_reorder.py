"""睡眠管道行为测试（工程四重排 → T6 压缩退役 → T7 journal 迁 scheduler 后的现状钉）。

睡眠管道现序：entity-extractor → dream-evolver（→ 块摘要可选层）；journal 腿已迁
scheduler journal_daily 定时任务（T7）、context-manager 已退役（T6）——睡眠全程零
journal/cm 调用，usage 不再门控任何腿。

测试清单：
1. 全序钉：entity 先于 dream（cm 零调用）
2. 高 usage（≥50%）反向钉：journal-agent 不再出现于睡眠管道（T7 迁出）
3. CP1 唤醒（entity 腿后首个 is_sleeping 检查点）→ interrupted
4. 门控消失：游标滞后/F1 未提炼不再产生 skipped 返回
5. dream 循环零游标写入（last_dream_evolve 退役反向钉——工程五）
6. 入口零读取反向钉：compat 入口不再读 last_dream_evolve.json（工程五）
7. 复位表收缩：reset 恰清 journal 一键、不触碰 last_entity_extract.json

全 mock：call_subagent_with_auto_answer / 游标文件 / is_sleeping / runner / chat_queue——
禁真实 LLM、禁图谱写入、messages.db 零新增、不碰真实 ~/.niu。
"""
import asyncio
import json
import pathlib
import tempfile
from contextlib import ExitStack
from unittest import mock

import agent.md_mirror as mdm

NORMAL_JSON = json.dumps({"ok": True})
PROCESSED_LINE_ALL = "\n处理完成 @end\nprocessed_line=3"  # 单记录（3 行）全删


def _make_msgs(ids):
    msgs = []
    for i, mid in enumerate(ids):
        m = mock.MagicMock()
        m.id = mid
        m.role = "user"
        m.content = f"hello {i}"
        m.tool_calls = None
        m.tool_call_id = None
        msgs.append(m)
    return msgs


class _FakeCalc:
    def __init__(self, tokens_per_msg):
        self.tokens_per_msg = tokens_per_msg

    def count_message_single(self, role, content, tool_calls=None):
        return self.tokens_per_msg


class _FakeRunner:
    def __init__(self):
        self.llm_config = {"model": "m", "apikey": "x", "apibase": "http://x"}
        self.handler = mock.MagicMock()
        self.handler._last_prompt_tokens = 0

    def _ensure_session_chain(self, max_days: int = 10) -> None:
        return None


def _write_home_cursor(home, filename, payload):
    """在 patch 后的 Path.home 下预建游标文件（hermetic：绝不触碰真实 ~/.niu）。"""
    niu_dir = home / ".niu"
    niu_dir.mkdir(parents=True, exist_ok=True)
    (niu_dir / filename).write_text(json.dumps(payload), encoding="utf-8")


def _run_sleep(call_mock, *, msg_ids=("m1", "m2"), tokens_per_msg=100,
               context_window=8000, sleep=lambda: True, home=None,
               seed_f1=None, extra_patches=()):
    """驱动 _tidy_context_impl sleep 分支（绕过 worker/CP0），全 mock。

    - Path.home → 隔离 tmp 目录（游标文件读写全部落在 tmp，缺失即空游标）
    - store：内存消息表 + 删除/更新留痕（delete_messages_by_ids/update_message 记录调用并同步裁剪 db）
    - seed_f1：非 None 时向隔离 F1 写入一条该 msg_id 的种子记录（entity 步真实执行）
    返回 (result, write_mock, call_mock, store, ctx)；ctx 含 deleted_batches/updated/home/db。
    """
    from niu_api.compat import _tidy_context_impl

    if home is None:
        home = pathlib.Path(tempfile.mkdtemp(prefix="t4_reorder_"))
    db = _make_msgs(msg_ids)
    ctx = {"deleted_batches": [], "updated": [], "home": home, "db": db}

    store = mock.MagicMock()

    async def _get_messages(*a, **k):
        return list(db)

    async def _delete(ids):
        ctx["deleted_batches"].append(list(ids))
        idset = set(ids)
        db[:] = [m for m in db if (getattr(m, "id", "") or "") not in idset]
        return {"deleted_count": len(ids), "freed_tokens": len(ids) * 10}

    async def _update(message_id=None, content="", clear_tool_calls=False, **kw):
        ctx["updated"].append((message_id, content))
        return True

    store.get_messages = _get_messages
    store.delete_messages_by_ids = _delete
    store.update_message = _update

    fake_queue = mock.MagicMock()

    async def _lock_ok(*a, **k):
        return True

    patches = [
        mock.patch("agent.token_calculator.TokenCalculator.get", return_value=_FakeCalc(tokens_per_msg)),
        mock.patch("niu_api.compat._read_context_window_tokens", return_value=context_window),
        mock.patch("niu_api.chat.get_or_create_runner", return_value=_FakeRunner()),
        mock.patch("agent.subagent.call_subagent_with_auto_answer", call_mock),
        mock.patch("niu_api.llm_proxy.get_llm_config", return_value={
            "model": "test-model", "apikey": "test-key", "apibase": "https://test.example.com",
            "type": "openai", "provider": "", "reasoning_effort": "", "litellm_kwargs": {},
        }),
        mock.patch("pathlib.Path.home", return_value=home),
        mock.patch("os.path.expanduser", lambda p, _h=home: str(_h / ".niu" / "t6_isolated") if p.startswith("~") else p),
        mock.patch("niu_api.compat.is_sleeping", side_effect=sleep),
        mock.patch("niu_api.chat_queue.get_chat_queue", return_value=fake_queue),
        mock.patch("niu_api.compat._acquire_chat_lock_with_retry", side_effect=_lock_ok),
        mock.patch("niu_api.compat._chat_lock", mock.MagicMock()),
    ]

    with ExitStack() as stack:
        stack.enter_context(mock.patch("niu_api.compat.get_message_store", new=mock.AsyncMock(return_value=store)))
        for p in patches:
            stack.enter_context(p)
        for p in extra_patches:
            stack.enter_context(p)
        write_mock = stack.enter_context(mock.patch("niu_api.compat._write_cursor_with_lock", create=True))
        stack.enter_context(mock.patch("agent.md_mirror.F2_PATH",
                                       str(pathlib.Path(tempfile.mkdtemp(prefix="t4_reorder_")) / "f2.md")))
        if seed_f1 is not None:
            block = mdm.format_message_record(
                msg_id=seed_f1, created_at="t", role="user", content=f"种子{seed_f1}",
            )
            assert mdm.append_record(block, mdm.F1_PATH)
        result = asyncio.run(_tidy_context_impl({"mode": "sleep", "session_id": "t"}, chat_lock_already_held=True))
    ctx["store"] = store
    return result, write_mock, call_mock, store, ctx


def _keyed(results, default=NORMAL_JSON):
    """call_subagent_with_auto_answer mock：按 agent_name 分发结果。"""
    call_mock = mock.MagicMock()

    def _side(agent_name=None, **kwargs):
        return results.get(agent_name, default)

    call_mock.side_effect = _side
    return call_mock


def _called_agents(call_mock):
    return [c.kwargs.get("agent_name") for c in call_mock.call_args_list]


def _cursor_writes(write_mock):
    return [call.args[1] for call in write_mock.call_args_list]


# ---------------------------------------------------------------------------
# 1. 全序钉：entity 先于 dream（cm 已退役零调用）
# ---------------------------------------------------------------------------

def test_t6_order_entity_then_dream_no_cm():
    """T6：journal(≥50% 此处 skipped) → entity-extractor → dream-evolver；cm 已退役零调用。"""
    call_mock = _keyed({
        "entity-extractor": NORMAL_JSON + PROCESSED_LINE_ALL,
        "dream-evolver": NORMAL_JSON + PROCESSED_LINE_ALL,
    })
    result, write_mock, call_mock, _store, _ctx = _run_sleep(
        call_mock, msg_ids=("m1", "m2"), seed_f1="m2",
    )

    assert result.get("status") == "ok", f"睡眠全程不应打断: {result}"
    agents = _called_agents(call_mock)
    assert agents == ["entity-extractor", "dream-evolver"], f"实际: {agents}"


# ---------------------------------------------------------------------------
# 2. 高 usage（≥50%）反向钉：journal-agent 不在睡眠管道（T7 迁 scheduler）
# ---------------------------------------------------------------------------

def test_high_usage_no_journal_in_sleep_pipeline():
    """usage∈[50%,70%)（旧四腿序的 journal 触发窗口）：T7 后睡眠管道零 journal/cm 调用。

    3 条 ×1500 tok / 8000 窗口 = 56.25%；无 F1/F2 内容 → entity/dream 亦无工作，
    全程零子 Agent 调用、status ok。
    """
    call_mock = _keyed({})
    result, _w, call_mock, _store, _ctx = _run_sleep(
        call_mock, msg_ids=("m1", "m2", "m3"), tokens_per_msg=1500,
    )

    agents = _called_agents(call_mock)
    assert agents == [], (
        f"journal 已迁 scheduler、cm 已退役；无 F1/F2 内容时全程零子 Agent 调用: {agents}"
    )
    assert result.get("status") == "ok", f"实际: {result}"


# ---------------------------------------------------------------------------
# 3. CP1 唤醒（entity 腿后首个 is_sleeping 检查点）→ interrupted
# ---------------------------------------------------------------------------

def test_cp1_interrupt_at_first_checkpoint():
    """CP1（entity 腿后首个 is_sleeping 检查点）唤醒 → interrupted；entity/dream 不执行、compress 游标零写。"""
    def sleep():
        return False  # 首个 is_sleeping 检查即 CP1

    call_mock = _keyed({})
    result, write_mock, call_mock, _store, _ctx = _run_sleep(call_mock, sleep=sleep)

    assert result == {"status": "interrupted", "reason": "woke_up"}
    assert _called_agents(call_mock) == [], "首个检查点（entity 腿后）即命中，entity/dream 不应执行"
    assert [d for d in _cursor_writes(write_mock) if d.get("last_compress_id")] == [], \
        f"compress 游标已退役，零写: {_cursor_writes(write_mock)}"


# ---------------------------------------------------------------------------
# 4. 门控消失：游标滞后不再产生 skipped
# ---------------------------------------------------------------------------

def test_gating_gone_stale_cursor_and_full_f1_proceed():
    """门控消失（决策 2）：陈旧 dream 游标 + F1 未提炼（旧行为必 skipped）→ 照常全链跑完。"""
    home = pathlib.Path(tempfile.mkdtemp(prefix="t4_gate_"))
    _write_home_cursor(home, "last_dream_evolve.json", {"last_dream_evolve_id": "ghost-stale-id"})
    call_mock = _keyed({
        "entity-extractor": NORMAL_JSON + PROCESSED_LINE_ALL,
        "dream-evolver": NORMAL_JSON + PROCESSED_LINE_ALL,
    })
    result, _w, call_mock, _store, _ctx = _run_sleep(
        call_mock, msg_ids=("m1", "m2"), home=home, seed_f1="m1",
    )

    assert result.get("status") == "ok", f"门控已摘除不得 skipped: {result}"
    agents = _called_agents(call_mock)
    assert agents[0] == "entity-extractor" and "dream-evolver" in agents and "context-manager" not in agents


# ---------------------------------------------------------------------------
# 5. dream 循环零游标写入（工程五退役反向钉）
# ---------------------------------------------------------------------------

def test_dream_cursor_no_longer_written_after_retirement():
    """工程五七件套退役：dream 循环只删 F2 前缀，零 last_dream_evolve 写入。"""
    call_mock = _keyed({
        "entity-extractor": NORMAL_JSON + PROCESSED_LINE_ALL,
        "dream-evolver": NORMAL_JSON + PROCESSED_LINE_ALL,
    })
    result, write_mock, _c, _store, _ctx = _run_sleep(
        call_mock, msg_ids=("m1", "m2"), seed_f1="m2",
    )

    assert result.get("status") == "ok"
    assert [d for d in _cursor_writes(write_mock) if d.get("last_dream_evolve_id")] == [], \
        f"dream 游标已退役，全程零写入: {_cursor_writes(write_mock)}"


# ---------------------------------------------------------------------------
# 6. 入口零读取反向钉（工程五退役）
# ---------------------------------------------------------------------------

def test_entry_no_dream_cursor_read(tmp_path):
    """工程五七件套退役反向钉：入口不再读取 last_dream_evolve.json（残留文件在盘也不读）。"""
    home = tmp_path / ".niu"
    home.mkdir(parents=True)
    (home / "last_dream_evolve.json").write_text(
        json.dumps({"last_dream_evolve_id": "m2"}), encoding="utf-8")

    real_read_text = pathlib.Path.read_text
    recorded = []

    def _spy_read_text(self, *a, **k):
        recorded.append(str(self))
        return real_read_text(self, *a, **k)

    call_mock = _keyed({
        "entity-extractor": NORMAL_JSON + PROCESSED_LINE_ALL,
        "dream-evolver": NORMAL_JSON + PROCESSED_LINE_ALL,
    })
    result, _w, _c, _store, _ctx = _run_sleep(
        call_mock, msg_ids=("m1", "m2"), home=tmp_path, seed_f1="m2",
        extra_patches=(mock.patch("pathlib.Path.read_text", _spy_read_text),),
    )
    assert not any("last_dream_evolve.json" in str(p) for p in recorded), \
        f"入口不得再读取 dream 游标文件（已退役），实际 recorded={recorded}"
    assert result.get("status") == "ok"

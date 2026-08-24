"""工程四 T1：睡眠管道重排（journal(≥50%) → context-manager → entity-extractor → dream-evolver）行为测试。

对应计划 docs/superpowers/plans/2026-08-24-md-relay-project4-reorder.md §3-T1 测试清单：
1. 重序断言：context-manager 先于 entity/dream 被调（默认 usage<50%，以 cm 为序锚）
2. ≥50% 用例单列钉 journal 位次（usage 取 [50%,70%) 窗口——≥70% 触发 _skip_compress 阈值）
3. ≥50% 模式二全序 journal→cm→entity→dream + 无 post-dream 范围过滤
   + 锚点排除正向钉：Path.home patch 预建含已知 msg_id 的 last_dream_evolve.json，
   断言越界消息被正常落库删除且锚点 id 不进 valid_deletes/valid_updates（force 哨兵承重墙）
4. CP1 唤醒（压缩对后）→ interrupted 且压缩已落库
5. 门控消失：游标滞后/F1 未提炼不再产生 skipped 返回
6. dream 循环仍回写 last_dream_evolve.json（决策 3''）
7. 入口共享游标读取保留（compat 入口三游标读取，force 哨兵数据源）

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

    - Path.home → 隔离 tmp 目录（入口三游标读取全部落在 tmp，缺失即空游标）
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
        mock.patch("niu_api.compat._read_max_output_tokens", return_value=32000),
        mock.patch("niu_api.compat._read_compress_target_tokens", return_value=1000),
        mock.patch("niu_api.compat._read_protect_recent_count", return_value=0),
        mock.patch("niu_api.compat._read_warning_threshold", return_value=0.8),
        mock.patch("pathlib.Path.home", return_value=home),
        # mode-2 应用段 compress_plan 路径走 expanduser——一并隔离，杜绝触碰真实 ~/.niu
        mock.patch("os.path.expanduser", lambda p, _h=home: str(_h / ".niu" / "compress_plan_mode2.json") if p.startswith("~") else p),
        mock.patch("niu_api.compat.is_sleeping", side_effect=sleep),
        mock.patch("niu_api.chat_queue.get_chat_queue", return_value=fake_queue),
        mock.patch("niu_api.compat._acquire_chat_lock_with_retry", side_effect=_lock_ok),
        mock.patch("niu_api.compat._wait_queue_idle_with_retry", side_effect=_lock_ok),
        mock.patch("niu_api.compat._chat_lock", mock.MagicMock()),
    ]

    with ExitStack() as stack:
        stack.enter_context(mock.patch("niu_api.compat.get_message_store", new=mock.AsyncMock(return_value=store)))
        for p in patches:
            stack.enter_context(p)
        for p in extra_patches:
            stack.enter_context(p)
        write_mock = stack.enter_context(mock.patch("niu_api.compat._write_cursor_with_lock"))
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
# 1. 重序断言：cm 先于 entity/dream（usage<50%）
# ---------------------------------------------------------------------------

def test_reorder_cm_called_before_entity_and_dream():
    """新序：journal(≥50% 此处 skipped) → context-manager → entity-extractor → dream-evolver。"""
    call_mock = _keyed({
        "entity-extractor": NORMAL_JSON + PROCESSED_LINE_ALL,
        "dream-evolver": NORMAL_JSON + PROCESSED_LINE_ALL,
    })
    result, write_mock, call_mock, _store, _ctx = _run_sleep(
        call_mock, msg_ids=("m1", "m2"), seed_f1="m2",
    )

    assert result.get("status") == "ok", f"睡眠全程不应打断: {result}"
    agents = _called_agents(call_mock)
    assert agents == ["context-manager", "entity-extractor", "dream-evolver"], f"实际: {agents}"


# ---------------------------------------------------------------------------
# 2. ≥50%（[50%,70%) 窗口）：journal 位次钉
# ---------------------------------------------------------------------------

def test_mode2_window_journal_agent_runs_first():
    """usage∈[50%,70%)：journal-agent 先于 context-manager 被调（压缩对内 journal 居首）。

    3 条 ×1500 tok / 8000 窗口 = 56.25%；cm 返 SUBAGENT_ERROR 早退即可证明位次。
    """
    call_mock = _keyed({"context-manager": "SUBAGENT_ERROR:mock"})
    result, _w, call_mock, _store, _ctx = _run_sleep(
        call_mock, msg_ids=("m1", "m2", "m3"), tokens_per_msg=1500,
    )

    assert _called_agents(call_mock)[:2] == ["journal-agent", "context-manager"], (
        f"journal 应最先被调: {_called_agents(call_mock)}"
    )
    assert result.get("status") == "skipped" and "LLM error" in result.get("reason", ""), f"实际: {result}"


# ---------------------------------------------------------------------------
# 3. 模式二全序 + 锚点排除正向钉 + 无 post-dream 范围过滤
# ---------------------------------------------------------------------------

def test_mode2_full_order_anchor_exclusion_no_post_filter():
    """≥50% 全序 journal→cm→entity→dream；锚点 id 不进删除/更新；越界消息正常落库删除。

    - 预建 last_dream_evolve.json：last_dream_evolve_id=m1（锚点=入口读取值，新序下 cm
      先于本轮 dream 执行二者等值）
    - cm 方案 keep=3：deletes=[m1,m2]、updates=[(1→m1)]
      → 锚点 m1 从 deletes/updates 双双排除；m2（旧 post-dream 守卫会保护的「越界」消息）
        正常落库删除——证明范围守卫已随工程四摘除
    """
    home = pathlib.Path(tempfile.mkdtemp(prefix="t4_anchor_"))
    _write_home_cursor(home, "last_dream_evolve.json", {"last_dream_evolve_id": "m1"})
    call_mock = _keyed({
        "context-manager": "keep=3\nupdate=1|[摘要] 锚点更新应被排除",
        "entity-extractor": NORMAL_JSON + PROCESSED_LINE_ALL,
        "dream-evolver": NORMAL_JSON + PROCESSED_LINE_ALL,
    })
    result, write_mock, call_mock, _store, ctx = _run_sleep(
        call_mock, msg_ids=("m1", "m2", "m3"), tokens_per_msg=1500,
        home=home, seed_f1="m1",
    )

    assert result.get("status") == "ok", f"模式二全链应完成: {result}"
    assert _called_agents(call_mock) == [
        "journal-agent", "context-manager", "entity-extractor", "dream-evolver",
    ], f"实际: {_called_agents(call_mock)}"

    all_deleted = [mid for batch in ctx["deleted_batches"] for mid in batch]
    assert "m2" in all_deleted, f"越界消息 m2 应回落库正常删除: {ctx['deleted_batches']}"
    assert "m1" not in all_deleted, f"锚点 m1 不得进 valid_deletes: {ctx['deleted_batches']}"
    assert ctx["updated"] == [], f"锚点 m1 的 update 应被排除: {ctx['updated']}"

    # 决策 3''：dream 游标继续回写（done_msg_id=m1 经 fresh_ids 校验后写游标）
    dream_writes = [d for d in _cursor_writes(write_mock) if d.get("last_dream_evolve_id")]
    assert dream_writes and dream_writes[-1]["last_dream_evolve_id"] == "m1"


# ---------------------------------------------------------------------------
# 4. CP1 唤醒（压缩对后）→ interrupted 且压缩已落库
# ---------------------------------------------------------------------------

def test_cp1_interrupt_after_compress_pair_persists_compression():
    """CP1（压缩对完成后）唤醒 → interrupted；cm 已执行且压缩游标推进不回滚。"""
    calls = {"n": 0}

    def sleep():
        calls["n"] += 1
        return calls["n"] < 2  # mode-1 派发前复查仍睡眠 → CP1 断

    call_mock = _keyed({})
    result, write_mock, call_mock, _store, _ctx = _run_sleep(call_mock, sleep=sleep)

    assert result == {"status": "interrupted", "reason": "woke_up"}
    assert _called_agents(call_mock) == ["context-manager"], "entity/dream 不应执行"
    compress_writes = [d for d in _cursor_writes(write_mock) if d.get("last_compress_id")]
    assert compress_writes, f"压缩已落库：compress 游标应已推进: {_cursor_writes(write_mock)}"


# ---------------------------------------------------------------------------
# 5. 门控消失：游标滞后不再产生 skipped
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
    assert agents[0] == "context-manager" and "entity-extractor" in agents and "dream-evolver" in agents


# ---------------------------------------------------------------------------
# 6. dream 循环仍回写 last_dream_evolve.json
# ---------------------------------------------------------------------------

def test_dream_cursor_still_written_after_reorder():
    """决策 3''：重排后梦境循环照常回写 dream 游标（force 边界唯一数据源）。"""
    call_mock = _keyed({
        "entity-extractor": NORMAL_JSON + PROCESSED_LINE_ALL,
        "dream-evolver": NORMAL_JSON + PROCESSED_LINE_ALL,
    })
    result, write_mock, _c, _store, _ctx = _run_sleep(
        call_mock, msg_ids=("m1", "m2"), seed_f1="m2",
    )

    assert result.get("status") == "ok"
    dream_writes = [d for d in _cursor_writes(write_mock) if d.get("last_dream_evolve_id")]
    assert dream_writes and dream_writes[-1]["last_dream_evolve_id"] == "m2"


# ---------------------------------------------------------------------------
# 7. 入口共享游标读取保留（force 哨兵数据源）
# ---------------------------------------------------------------------------

def test_entry_shared_cursor_read_preserved(tmp_path):
    """入口三游标共享读取保留：预建 dream 游标文件 → 入口真实读出（spy Path.read_text）。"""
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

    assert result.get("status") == "ok"
    assert any(p.endswith("last_dream_evolve.json") for p in recorded), (
        f"入口共享游标读取应保留并消费 dream 游标文件: {recorded}"
    )

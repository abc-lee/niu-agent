"""工程五 T2：机制层七件套整链退役行为测试。

规格源：docs/superpowers/plans/2026-08-24-md-relay-project5-cursor-retirement.md §3-T2 + 决策 2/3。
七件套同批消亡：force/runner-force 哨兵与边界防护、睡眠 cm 锚点排除+cascade cursor 分量、
dream 循环游标回写与 fresh_ids 校验、_build_force_prompt 安全边界行、砍半互斥、
_ALL_CURSOR_FILES 收缩两键、入口共享读取删除。

用例清单（计划 §3-T2）：
1. force prompt 无安全边界行 / dream_idx 注入（正向退役钉）
2. 睡眠全程无 last_dream_evolve 写入（反向钉）
3. reset 只清两键且不触碰磁盘残留文件
4. mode-2 对未提炼消息正常落库删除——种子含锚点 id 消息并钉其进 valid_deletes（判别力）
5. 砍半降级无 dream_idx 互斥路径
6. 双入口零读取反向钉——compat 入口与 runner 入口均无 last_dream_evolve 读取（对称覆盖）

全 mock：call_subagent_with_auto_answer / 游标文件 / is_sleeping / runner / chat_queue——
禁真实 LLM、禁图谱写入、messages.db 零新增、不碰真实 ~/.niu。
"""
import asyncio
import json
import pathlib
import tempfile
from contextlib import ExitStack
from unittest import mock

from niu_api.compat import (
    _ALL_CURSOR_FILES,
    _build_force_prompt,
    _compact_with_degradation_sync,
    _reset_all_cursors,
)

NORMAL_JSON = json.dumps({"ok": True})


# ---------------------------------------------------------------------------
# 公共 harness（模式同 test_sleep_reorder，自包含复制）
# ---------------------------------------------------------------------------

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


def _keyed(results, default=NORMAL_JSON):
    call_mock = mock.MagicMock()

    def _side(agent_name=None, **kwargs):
        return results.get(agent_name, default)

    call_mock.side_effect = _side
    return call_mock


def _cursor_writes(write_mock):
    return [call.args[1] for call in write_mock.call_args_list]


def _make_store(db, ctx):
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
    return store


def _base_patches(call_mock, home, store, tokens_per_msg=100, context_window=8000):
    fake_queue = mock.MagicMock()

    async def _lock_ok(*a, **k):
        return True

    return [
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
        mock.patch("os.path.expanduser",
                   lambda p, _h=home: str(_h / ".niu" / "compress_plan_t5.json") if p.startswith("~") else p),
        mock.patch("niu_api.compat.is_sleeping", return_value=True),
        mock.patch("niu_api.chat_queue.get_chat_queue", return_value=fake_queue),
        mock.patch("niu_api.compat._acquire_chat_lock_with_retry", side_effect=_lock_ok),
        mock.patch("niu_api.compat._wait_queue_idle_with_retry", side_effect=_lock_ok),
        mock.patch("niu_api.compat._chat_lock", mock.MagicMock()),
    ]


def _run_sleep(call_mock, *, msg_ids=("m1", "m2"), home=None,
               tokens_per_msg=100, context_window=8000, extra_patches=()):
    """驱动 _tidy_context_impl sleep 分支（绕过 worker/CP0），全 mock。"""
    from niu_api.compat import _tidy_context_impl

    if home is None:
        home = pathlib.Path(tempfile.mkdtemp(prefix="t5_retire_"))
    db = _make_msgs(msg_ids)
    ctx = {"deleted_batches": [], "updated": [], "home": home, "db": db}
    store = _make_store(db, ctx)

    with ExitStack() as stack:
        stack.enter_context(mock.patch("niu_api.compat.get_message_store",
                                       new=mock.AsyncMock(return_value=store)))
        for p in _base_patches(call_mock, home, store, tokens_per_msg=tokens_per_msg,
                               context_window=context_window):
            stack.enter_context(p)
        for p in extra_patches:
            stack.enter_context(p)
        write_mock = stack.enter_context(mock.patch("niu_api.compat._write_cursor_with_lock"))
        result = asyncio.run(_tidy_context_impl({"mode": "sleep", "session_id": "t"},
                                                chat_lock_already_held=True))
    return result, write_mock, ctx


# ---------------------------------------------------------------------------
# 1. force prompt 无安全边界行 / dream_idx 注入
# ---------------------------------------------------------------------------

def test_force_prompt_has_no_safety_boundary_line():
    """退役正向钉：_build_force_prompt 新签名渲染后不含安全边界行与任何 dream 字样。"""
    p = _build_force_prompt(
        display_tokens=100000, compress_target_tokens=60000, usage_percent=80.0,
        force_history=[{"role": "user", "content": "[idx:1] 100tokens hi"}],
        last_compress_id="compress-anchor-id",
    )
    assert "安全边界" not in p, "dream 安全边界行已退役，不得回流 prompt"
    assert "未提取知识" not in p
    assert "idx >" not in p
    assert "dream" not in p.lower()
    # 上次压缩游标行保留
    assert "compress-anchor-id" in p
    # 输出契约三行保留
    assert "keep=" in p and "update=" in p and "cursor=" in p


# ---------------------------------------------------------------------------
# 2. 睡眠全程无 last_dream_evolve 写入（反向钉）
# ---------------------------------------------------------------------------

def test_sleep_never_writes_dream_cursor():
    """全链（cm→entity→dream 循环）零 last_dream_evolve 游标写入。"""
    call_mock = _keyed({})
    result, write_mock, _ctx = _run_sleep(call_mock)

    assert result.get("status") == "ok", f"睡眠应正常完成: {result}"
    dream_writes = [d for d in _cursor_writes(write_mock) if d.get("last_dream_evolve_id")]
    assert dream_writes == [], f"dream 游标已退役，不得有任何写入: {_cursor_writes(write_mock)}"


# ---------------------------------------------------------------------------
# 3. reset 只清两键且不触碰残留文件
# ---------------------------------------------------------------------------

def test_reset_clears_two_keys_and_spares_residual_file(tmp_path):
    """_ALL_CURSOR_FILES 收缩两键：journal/compress 删除；盘上残留的 dream 游标文件不被触碰。"""
    assert _ALL_CURSOR_FILES == ["last_compress.json", "last_journal.json"], \
        f"复位表应恰两键且不含 dream 键: {_ALL_CURSOR_FILES}"

    niu = tmp_path / ".niu"
    niu.mkdir(parents=True)
    residual = niu / "last_dream_evolve.json"
    residual.write_text(json.dumps({"last_dream_evolve_id": "legacy"}), encoding="utf-8")
    for name in ("last_journal.json", "last_compress.json"):
        (niu / name).write_text("{}", encoding="utf-8")

    with mock.patch("pathlib.Path.home", return_value=tmp_path):
        asyncio.run(_reset_all_cursors())

    remaining = sorted(p.name for p in niu.iterdir())
    assert remaining == ["last_dream_evolve.json"], \
        f"只应剩磁盘残留的 dream 游标文件（一次性手工清算，不入生产代码路径）: {remaining}"


# ---------------------------------------------------------------------------
# 4. mode-2 对未提炼消息正常落库删除（锚点 id 进 valid_deletes 判别力钉）
# ---------------------------------------------------------------------------

def test_mode2_deletes_unextracted_anchor_message():
    """种子含锚点 id 消息（m1）：旧行为锚点被排除，退役后 m1 正常落库删除。

    判别力：若锚点排除回流，m1 不在删除批次 → 断言挂。
    """
    call_mock = _keyed({
        # keep=2 → 删除 idx {1,3} → UUID [m1, m3]（m1 即「锚点」）
        "context-manager": "keep=2\ncursor=2",
    })
    result, _w, ctx = _run_sleep(
        call_mock, msg_ids=("m1", "m2", "m3"), tokens_per_msg=1500)  # 56% ≥50% → 模式二

    assert result.get("status") == "ok", f"模式二应正常完成: {result}"
    all_deleted = [mid for batch in ctx["deleted_batches"] for mid in batch]
    assert "m1" in all_deleted, f"锚点 id 应正常进 valid_deletes 落库删除: {ctx['deleted_batches']}"
    assert "m3" in all_deleted
    assert set(all_deleted) == {"m1", "m3"}, f"恰删 keep 外两条: {ctx['deleted_batches']}"


# ---------------------------------------------------------------------------
# 5. 砍半降级无 dream_idx 互斥路径
# ---------------------------------------------------------------------------

def test_halving_degradation_has_no_dream_mutex():
    """step1 截断 → step2 砍半照常执行；prompt_builder_kwargs 不含 dream_idx_in_force 键。"""
    llm_config = {"litellm_kwargs": {"max_tokens": 32000, "thinking": {"type": "disabled"}}}
    captured_kwargs = []
    calls = []

    def _fake_call_fn(**kwargs):
        calls.append(kwargs)
        return "keep=1,2\nupdate=2|[摘要] xxx\ncursor=2"  # step2 成功

    def _builder(**kw):
        captured_kwargs.append(dict(kw))
        return "rebuilt prompt"

    history = [{"role": "user", "content": f"[idx:{i+1}] msg{i+1}"} for i in range(4)]
    result, msg_ids, halved = _compact_with_degradation_sync(
        agent_name="context-manager",
        prompt="prompt",
        compress_history=history,
        compress_msg_ids=["a", "b", "c", "d"],
        llm_config=llm_config,
        prompt_builder=_builder,
        prompt_builder_kwargs={
            "display_tokens": 1000, "compress_target_tokens": 500, "usage_percent": 80.0,
            "force_history": [{"role": "user", "content": "[idx:1] x"}],
            "last_compress_id": None,  # 无 dream_idx_in_force 键
        },
        stop_aware=False,
        call_fn=_fake_call_fn,
    )

    assert result is not None, "砍半互斥已退役：step2 应执行成功"
    assert halved == ["a", "b"], f"砍半前半段应返回: {halved}"
    assert msg_ids == ["c", "d"]
    # 砍半重建的 prompt kwargs 不得出现 dream_idx 分量（互斥路径消亡）
    assert captured_kwargs and all("dream_idx_in_force" not in kw for kw in captured_kwargs), \
        f"dream_idx_in_force 已退役: {captured_kwargs}"


# ---------------------------------------------------------------------------
# 6. 双入口零读取反向钉（compat 入口 + runner 入口，对称覆盖）
# ---------------------------------------------------------------------------

def test_compat_entry_zero_dream_cursor_read(tmp_path):
    """compat 入口：残留 dream 游标文件在盘也不得被读取（spy Path.read_text）。"""
    home = tmp_path
    niu_dir = home / ".niu"
    niu_dir.mkdir(parents=True)
    (niu_dir / "last_dream_evolve.json").write_text(
        json.dumps({"last_dream_evolve_id": "m2"}), encoding="utf-8")

    real_read_text = pathlib.Path.read_text
    recorded = []

    def _spy_read_text(self, *a, **k):
        recorded.append(str(self))
        return real_read_text(self, *a, **k)

    call_mock = _keyed({})
    result, _w, _ctx = _run_sleep(
        call_mock, msg_ids=("m1", "m2"), home=home,
        extra_patches=(mock.patch("pathlib.Path.read_text", _spy_read_text),),
    )

    assert result.get("status") == "ok"
    assert not any("last_dream_evolve.json" in p for p in recorded), \
        f"compat 入口不得再读取 dream 游标文件，实际 recorded={recorded}"


def test_runner_entry_zero_dream_cursor_read(monkeypatch):
    """runner 入口：_execute_force_pipeline 零 last_dream_evolve_id 游标读取（对称覆盖）。"""
    import niu_api.llm_proxy as llm_proxy_module
    from agent import runner as runner_module
    from agent import subagent as subagent_module

    class _Msg:
        def __init__(self, mid):
            self.id = mid
            self.role = "user"
            self.content = "hello"
            self.tool_calls = None
            self.tool_call_id = ""

    read_fields = []

    def _spy_read_cursor(path, field):
        read_fields.append(field)
        return ""

    call_mock = mock.MagicMock()

    def _side(agent_name=None, **kwargs):
        if agent_name == "context-manager":
            return "SUBAGENT_ERROR:mock"
        return NORMAL_JSON

    call_mock.side_effect = _side

    runner = runner_module.NiuRunner.__new__(runner_module.NiuRunner)
    runner.llm_config = {"model": "m", "apikey": "x", "apibase": "http://x"}
    runner.handler = type("H", (), {"_last_prompt_tokens": 120000})()

    monkeypatch.setattr(runner_module.NiuRunner, "_read_cursor", staticmethod(_spy_read_cursor))
    monkeypatch.setattr(runner_module.NiuRunner, "_sync_get_messages",
                        lambda self, limit=None: [_Msg("m1"), _Msg("m2")])
    monkeypatch.setattr(runner_module, "is_stop_requested", lambda: False)
    monkeypatch.setattr("niu_api.compat._write_cursor_with_lock", lambda path, data: None)  # journal 步游标写入隔离
    monkeypatch.setattr("agent.token_calculator.TokenCalculator.get",
                        lambda: _FakeCalc(100))
    monkeypatch.setattr(subagent_module, "call_subagent_with_auto_answer", call_mock)
    monkeypatch.setattr(subagent_module, "_read_context_window_tokens", lambda: 8000)
    monkeypatch.setattr(subagent_module, "_read_protect_recent_count", lambda: 0)
    monkeypatch.setattr(subagent_module, "_read_compress_target_tokens", lambda: 60000)
    monkeypatch.setattr(subagent_module, "_read_max_output_tokens", lambda: 32000)
    monkeypatch.setattr(llm_proxy_module, "get_llm_config", lambda use_lightrag_config=False: {
        "model": "test-model", "apikey": "test-key", "apibase": "https://test.example.com",
        "type": "openai", "provider": "", "reasoning_effort": "", "litellm_kwargs": {},
    })

    result = runner._execute_force_pipeline()

    assert result is not None and result.get("status") == "skipped", f"实际: {result}"
    assert "context-manager" in [
        c.kwargs.get("agent_name") for c in call_mock.call_args_list
    ], "runner 压缩段应被调（SUBAGENT_ERROR 早退即证明）"
    assert "last_dream_evolve_id" not in read_fields, \
        f"runner 入口不得再读取 dream 游标分量，实际读取 fields={read_fields}"

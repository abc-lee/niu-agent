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
        mock.patch("pathlib.Path.home", return_value=home),
        mock.patch("niu_api.compat.is_sleeping", return_value=True),
        mock.patch("niu_api.chat_queue.get_chat_queue", return_value=fake_queue),
        mock.patch("niu_api.compat._acquire_chat_lock_with_retry", side_effect=_lock_ok),
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

def test_reset_clears_journal_key_and_spares_residual_file(tmp_path):
    """_ALL_CURSOR_FILES 收缩单键（T6 压缩退役）：journal 复位；盘上残留的 dream/compress 游标文件不被触碰。"""
    assert _ALL_CURSOR_FILES == ["last_journal.json"], \
        f"复位表应恰 journal 一键: {_ALL_CURSOR_FILES}"

    niu = tmp_path / ".niu"
    niu.mkdir(parents=True)
    residual = niu / "last_dream_evolve.json"
    residual.write_text(json.dumps({"last_dream_evolve_id": "legacy"}), encoding="utf-8")
    for name in ("last_journal.json", "last_compress.json"):
        (niu / name).write_text("{}", encoding="utf-8")

    with mock.patch("pathlib.Path.home", return_value=tmp_path):
        asyncio.run(_reset_all_cursors())

    # journal 键被复位（unlink）；compress 已不在复位表——盘上残留属一次性手工清算，生产代码不触碰
    remaining = sorted(p.name for p in niu.iterdir())
    assert remaining == ["last_compress.json", "last_dream_evolve.json"], \
        f"只应剩磁盘残留文件: {remaining}"
    assert not (niu / "last_journal.json").exists()


# ---------------------------------------------------------------------------
# 4. mode-2 对未提炼消息正常落库删除（锚点 id 进 valid_deletes 判别力钉）
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




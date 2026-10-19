"""P3 调用点审计（计划 §4.3–§4.6 / I-1 / I-2）——三个前端入口不再直接执行主 Agent。

P2a 时代的三条端点断言（`finally` 里的 `gen.close()`、`_chat_lock` 必须持有到 executor
worker 真正结束、helper 收到 `concurrent.futures.Future` 而非会被 cancel 的 asyncio `sf`）
随 P2b/P3 迁移全部搬进 `MainAgentRuntime`，由 tests/test_main_runtime.py 覆盖
（`test_generator_closed_on_finish`、`test_runtime_holds_chat_lock_until_on_complete`、
`test_chat_lock_released_when_runner_raises`、`test_clear_stop_precedes_chat_lock_release`、
`test_chat_lock_acquire_timeout_yields_error_outcome_without_release`）。
`niu_api.chat_lock` 已不再是这三个调用点的 helper —— 本文件不再 import 它。

仍保留/迁移的两类断言：

1. **静态形状（AST）**：三端点体内零 `_chat_lock.acquire/release`（compat 唯一例外：
   `/compact` 拦截分支仍持锁，D-6/I-6 的 `is_round_active` 判据正来自它）、
   零 `runner.chat(...)`、零 `run_in_executor`、零 `gen.close()`、零 `chat_lock` 引用；
   必须经 `get_main_runtime()` 取 runtime 并 `runtime.submit(...)`。
2. **行为**：三端点各提交恰好一条 `RunCommand`；`runner.chat` 只由 runtime 替身线程调用；
   生成器由 runtime 显式 `close()`（P2a 的关闭保证换了层，仍未丢）；端点取锁即炸
   （替身锁的 `acquire/release` 直接抛错，比事后数事件更强）。

全 mock：假锁 / 假生成器 / AsyncMock store / 假 persist —— 禁真实 LLM、禁真实 messages.db。
"""

import ast
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from p2b_fakes import FakeRuntime, main_loop_thread

import niu_api.chat as chat_mod
import niu_api.compat as compat_mod

_LLM_CFG = {
    "type": "openai",
    "apikey": "test-api-key",
    "apibase": "https://api.example.com",
    "model": "test-model",
}

# 三个端点（AST 审计对象）：(模块, 函数名)
_CALL_SITES = (
    (chat_mod, "chat"),
    (chat_mod, "chat_sync"),
    (compat_mod, "chat_session"),
)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


class FakeGen:
    """记录 `close()` 的假生成器：先返回一个 `first`，再 `StopIteration`。

    `closed` 只能由**执行方**（P3 后＝runtime）的显式 `gen.close()` 置位，
    因此 `fake.closed is True` 才真正证明"显式关闭"存在。
    """

    def __init__(self, first, close_raises: bool = False):
        self._first = first
        self._yielded = False
        self._close_raises = close_raises
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if not self._yielded:
            self._yielded = True
            return self._first
        raise StopIteration

    def close(self) -> None:
        # 先置 `closed` 再抛：「close 被调用」与「close 抛错被捕获」两个断言可同时成立
        self.closed = True
        if self._close_raises:
            raise RuntimeError("close boom")


class _ForbiddenLock:
    """`_chat_lock` 替身：端点一旦取锁/释放即抛（I-2 的强判据）。

    `locked()` 仍可读——`is_round_active()` 的判据正是"只读锁状态"。
    """

    def __init__(self) -> None:
        self.acquire_calls = 0
        self.release_calls = 0

    async def acquire(self) -> bool:
        self.acquire_calls += 1
        raise AssertionError("端点不得再获取 _chat_lock（I-2：锁归 runtime）")

    def release(self) -> None:
        self.release_calls += 1
        raise AssertionError("端点不得再释放 _chat_lock（I-2：锁归 runtime）")

    def locked(self) -> bool:
        return False


def _fake_store() -> MagicMock:
    store = MagicMock()
    store.add_message = AsyncMock(return_value="user-msg-1")
    store.get_messages = AsyncMock(return_value=[])
    return store


def _make_runner(gen_factory):
    """`runner.chat` 必须是**工厂 lambda**（直接赋实例会在调用点 `runner.chat(...)` 处 TypeError）。

    `chat_calls` 记录调用次数——P3 后只允许 runtime 线程调用（I-1）。
    """
    runner = MagicMock()
    chat_calls: list[int] = []

    def chat(*args, **kwargs):
        chat_calls.append(1)
        return gen_factory()

    runner.chat = chat
    runner.chat_calls = chat_calls
    runner.last_return_value = {"result": "CURRENT_TASK_DONE", "data": None, "messages": []}
    runner._persisted_msgs = None
    runner._extracted_at_msgs = None
    runner.get_im_channel = lambda: ""
    runner.should_push_im = lambda: False
    return runner


def _install_forbidden_lock(monkeypatch) -> _ForbiddenLock:
    """chat / compat 两处模块级 `_chat_lock` 绑定同指这把"取锁即炸"的替身。"""
    lock = _ForbiddenLock()
    monkeypatch.setattr(compat_mod, "_chat_lock", lock)
    monkeypatch.setattr(chat_mod, "_chat_lock", lock)
    return lock


def _patch_chat_env(monkeypatch, runner, runtime) -> None:
    """`/chat`（SSE）与 `/chat/sync` 的运行环境（D-8：runtime 经 get_main_runtime 注入）。"""
    monkeypatch.setattr("niu_api.chat._load_llm_config", lambda: dict(_LLM_CFG))
    monkeypatch.setattr("niu_api.chat.get_or_create_runner", lambda: runner)
    monkeypatch.setattr("niu_api.chat.notify_new_message", AsyncMock(return_value=True))
    monkeypatch.setattr("niu_api.chat.get_message_store", AsyncMock(return_value=_fake_store()))
    monkeypatch.setattr(
        "niu_api.chat.persist_agent_reply",
        AsyncMock(return_value=("msg-id", "persisted-reply")),
    )
    monkeypatch.setattr("agent.main_runtime.get_main_runtime", lambda: runtime)


def _patch_compat_env(monkeypatch, runner, runtime) -> None:
    """`compat /chat/session` 的运行环境。"""
    monkeypatch.setattr(
        "niu_api.config.get_config",
        lambda: SimpleNamespace(llm=SimpleNamespace(api_key="test-api-key")),
    )
    monkeypatch.setattr("niu_api.compat.get_message_store", AsyncMock(return_value=_fake_store()))
    monkeypatch.setattr("niu_api.chat.get_or_create_runner", lambda: runner)
    monkeypatch.setattr("niu_api.chat.notify_new_message", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "niu_api.chat.persist_agent_reply",
        AsyncMock(return_value=("msg-id", "persisted-reply")),
    )
    monkeypatch.setattr("agent.main_runtime.get_main_runtime", lambda: runtime)
    monkeypatch.setattr("niu_api.channel.gateway.push_im_reply", AsyncMock(return_value=False))


# ---------------------------------------------------------------------------
# AST 助手
# ---------------------------------------------------------------------------


def _func(module, name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{module.__name__} 里找不到 async def {name}")


def _call_srcs(node: ast.AST) -> list[str]:
    """节点内全部调用的"被调用表达式"源码（如 `runtime.submit`、`runner.chat`）。"""
    return [ast.unparse(c.func) for c in ast.walk(node) if isinstance(c, ast.Call)]


def _parents(root: ast.AST) -> dict:
    parents: dict = {}
    for parent in ast.walk(root):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _inside(node: ast.AST, container: ast.AST, parents: dict) -> bool:
    cur = node
    while cur is not None:
        if cur is container:
            return True
        cur = parents.get(cur)
    return False


def _lock_calls(node: ast.AST) -> list[ast.Call]:
    out = []
    for c in ast.walk(node):
        if isinstance(c, ast.Call):
            src = ast.unparse(c.func)
            if "chat_lock" in src and (src.endswith(".acquire") or src.endswith(".release")):
                out.append(c)
    return out


def _compression_branch(fn: ast.AST) -> ast.If | None:
    """compat chat_session 顶层的 `/compact` 拦截分支（唯一仍持锁的位置）。"""
    for stmt in fn.body:
        if isinstance(stmt, ast.If) and "_is_compression_command" in ast.unparse(stmt.test):
            return stmt
    return None


# ---------------------------------------------------------------------------
# 1. 静态形状：三端点零锁 / 零 runner.chat / 零 executor / 零 gen.close
# ---------------------------------------------------------------------------


def test_endpoints_have_no_lock_executor_or_runner_chat():
    for module, name in _CALL_SITES:
        fn = _func(module, name)
        srcs = _call_srcs(fn)
        label = f"{module.__name__}.{name}"

        runner_chat = [s for s in srcs if s == "runner.chat" or s.startswith("runner.chat(")]
        assert not runner_chat, f"{label} 仍直接调用 runner.chat（I-1 要求唯一调用点在 runtime）: {runner_chat}"

        executor = [s for s in srcs if s.endswith("run_in_executor")]
        assert not executor, f"{label} 仍用 executor 跑同步流（P3 §4.3–§4.5 已删）: {executor}"

        closes = [s for s in srcs if s.endswith(".close") and "gen" in s]
        assert not closes, f"{label} 仍自行 close 生成器（关闭保证已归 runtime）: {closes}"

        # `niu_api.chat_lock` helper 已不再是这三个调用点的一部分（P2b/P3 迁移）
        imports = [ast.unparse(n) for n in ast.walk(fn) if isinstance(n, ast.Import | ast.ImportFrom)]
        assert not any("chat_lock" in s for s in imports), f"{label} 仍 import chat_lock helper: {imports}"


def test_chat_and_chat_sync_take_zero_chat_lock():
    """两个 chat.py 端点：整函数零 `_chat_lock.acquire/release`（P3 §4.3/§4.4）。"""
    for name in ("chat", "chat_sync"):
        fn = _func(chat_mod, name)
        calls = _lock_calls(fn)
        assert not calls, f"niu_api.chat.{name} 仍取/放 _chat_lock（I-2）: {[ast.unparse(c.func) for c in calls]}"


def test_compat_chat_session_lock_only_inside_compact_branch():
    """compat /chat/session：唯一仍持锁的位置必须是 `/compact` 拦截分支（D-6/I-6）。

    正常消息路径必须零取锁；`/compact` 闲时受控压缩保留持锁是设计内行为，
    `is_round_active()` 的 `_chat_lock.locked()` 判据正覆盖这一段。
    """
    fn = _func(compat_mod, "chat_session")
    branch = _compression_branch(fn)
    assert branch is not None, "compat chat_session 的 /compact 拦截分支未找到"

    calls = _lock_calls(fn)
    assert calls, "/compact 闲时压缩分支应仍持锁（回归即说明分支被误删）"
    parents = _parents(fn)
    outside = [ast.unparse(c.func) for c in calls if not _inside(c, branch, parents)]
    assert not outside, f"chat_session 正常消息路径仍取/放 _chat_lock（I-2）: {outside}"


def test_endpoints_submit_via_get_main_runtime():
    """三端点必须经 `get_main_runtime()` 取 runtime 并 `runtime.submit(...)`（D-8）。"""
    for module, name in _CALL_SITES:
        fn = _func(module, name)
        srcs = _call_srcs(fn)
        label = f"{module.__name__}.{name}"
        assert any(s == "get_main_runtime" for s in srcs), f"{label} 未用 get_main_runtime 取 runtime（D-8）"
        assert any(s.endswith(".submit") for s in srcs), f"{label} 未提交 RunCommand: {srcs}"
        # 绝不顺手创建单例（D-8：无 runtime 走中性文案）
        assert not any(s.endswith("MainAgentRuntime.instance") or s == "instance" for s in srcs), (
            f"{label} 不得顺手拉起 runtime 单例（D-8）"
        )


def test_compat_chat_session_no_longer_drains_supplements():
    """§4.5：端点侧 `drain_supplements()` 已删（runtime 在释放锁后单点 drain）。"""
    fn = _func(compat_mod, "chat_session")
    srcs = _call_srcs(fn)
    drains = [s for s in srcs if s.endswith("drain_supplements")]
    assert not drains, f"chat_session 仍在端点侧 drain 补充消息（应归 runtime）: {drains}"


# ---------------------------------------------------------------------------
# 2. 行为：三端点各提交一条命令，生成器由 runtime 关闭，取锁即炸
# ---------------------------------------------------------------------------


async def test_chat_sse_submits_one_command_and_runtime_closes_generator(monkeypatch):
    fake = FakeGen("chunk-1")
    runner = _make_runner(lambda: fake)
    runtime = FakeRuntime(runner=runner)
    _install_forbidden_lock(monkeypatch)
    _patch_chat_env(monkeypatch, runner, runtime)

    try:
        with main_loop_thread():
            resp = await chat_mod.chat(chat_mod.ChatRequest(message="你好", session_id="s"))
            parts = [part async for part in resp.body_iterator]
    finally:
        runtime.stop()

    assert any('"chunk"' in p for p in parts), f"SSE 应下发 chunk，实际 {parts}"
    assert any('"done": true' in p for p in parts), f"SSE 应正常收尾，实际 {parts}"
    assert len(runtime.commands) == 1, f"应提交 1 条命令，实际 {len(runtime.commands)}"
    cmd = runtime.commands[0]
    assert cmd.stream is True and cmd.channel == "electron" and cmd.source == "user"
    assert runner.chat_calls and len(runner.chat_calls) == 1, "runner.chat 必须只由 runtime 线程调用一次"
    assert fake.closed is True, "生成器必须被显式 close（P2a 保证现归 runtime）"


async def test_chat_sync_submits_one_command_and_runtime_closes_generator(monkeypatch):
    fake = FakeGen("chunk-1")
    runner = _make_runner(lambda: fake)
    runtime = FakeRuntime(runner=runner)
    _install_forbidden_lock(monkeypatch)
    _patch_chat_env(monkeypatch, runner, runtime)

    try:
        with main_loop_thread():
            resp = await chat_mod.chat_sync(chat_mod.ChatRequest(message="你好", session_id="s"))
    finally:
        runtime.stop()

    assert resp.reply == "persisted-reply", f"回复应来自 holder（I-5），实际: {resp.reply!r}"
    assert len(runtime.commands) == 1
    assert runtime.commands[0].stream is False, "/chat/sync 必须提交非流式命令（D-5）"
    assert len(runner.chat_calls) == 1, "runner.chat 必须只由 runtime 线程调用一次"
    assert fake.closed is True, "生成器必须被显式 close（P2a 保证现归 runtime）"


async def test_compat_chat_session_submits_one_command_and_runtime_closes_generator(monkeypatch):
    fake = FakeGen("chunk-1")
    runner = _make_runner(lambda: fake)
    runtime = FakeRuntime(runner=runner)
    _install_forbidden_lock(monkeypatch)
    _patch_compat_env(monkeypatch, runner, runtime)

    try:
        with main_loop_thread():
            resp = await compat_mod.chat_session(
                compat_mod.ChatRequest(message="你好", session_id="default", source="electron")
            )
    finally:
        runtime.stop()

    assert resp.reply == "persisted-reply", f"回复应来自 holder（I-5），实际: {resp.reply!r}"
    assert len(runtime.commands) == 1
    cmd = runtime.commands[0]
    assert cmd.stream is False, "compat /chat/session 必须提交非流式命令（D-6）"
    assert cmd.channel == "electron", "source==electron 必须映射 channel=electron（D-6）"
    assert len(runner.chat_calls) == 1, "runner.chat 必须只由 runtime 线程调用一次"
    assert fake.closed is True, "生成器必须被显式 close（P2a 保证现归 runtime）"


async def test_runtime_close_exception_does_not_break_the_round(monkeypatch):
    """close 抛错必须被执行方单独捕获：不覆盖本轮结果、holder 仍拿到落库文案。

    本例驱动 `p2b_fakes.FakeRuntime` 的 close 捕获；生产 `MainAgentRuntime._execute`
    的同一契约由 `tests/test_main_runtime.py::test_execute_close_exception_does_not_break_the_round` 驱动。
    """
    fake = FakeGen("chunk-1", close_raises=True)
    runner = _make_runner(lambda: fake)
    runtime = FakeRuntime(runner=runner)
    _install_forbidden_lock(monkeypatch)
    _patch_chat_env(monkeypatch, runner, runtime)

    try:
        with main_loop_thread():
            resp = await chat_mod.chat_sync(chat_mod.ChatRequest(message="你好", session_id="s"))
    finally:
        runtime.stop()

    assert fake.closed is True, "close 必须被调用（即使它抛错）"
    assert resp.reply == "persisted-reply", f"close 抛错不得改变本轮回复：{resp.reply!r}"
    assert "close boom" not in resp.reply, f"close 异常泄漏到返回值：{resp.reply}"

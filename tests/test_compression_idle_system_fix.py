"""T1 手动路径 system 补全直测（spec §4 / plan P2-4-A 策略 a，2026-09-07）。

行为锁：闲时 /compact 的总结请求 messages[0] 必须是**完整 system**
（静态区 static_system_prompt + dynamic_system_prefix + memory 段），
且存在 role=user、content startswith "[系统动态信息]" 的动态块行——
不再是 000038 的无 system 形态。

配方（策略 a）：不真调 _run_idle_compression 全链触真实 LLM，用
object.__new__(NiuRunner) 半真实 stub + MethodType 绑**真实**
NiuRunner._on_before_llm（补全三步：前置占位 → _on_before_llm → 断言）。
monkeypatch 面：agent.runner._load_memory_for_prompt（固定文本）、
实例 _inject_dynamic_resources（禁向量检索/脑区）、实例 _park_reminder_line
（免读真实 ~/.niu/memory.json）。_run_idle_compression 直测时 patch
agent.generic.agent_loop.run_controlled_compression（禁真实 LLM/DB）。
"""
import types as _types
from unittest.mock import MagicMock, patch

# 导入序守卫：同 test_compression_trigger_migration.py——先完整加载 assembler 包，
# 否则 agent.context_manager 命中部分初始化模块 ImportError。
import agent.context_assembler  # noqa: F401,E402

_DYN_HEADER = "[系统动态信息]"


def _make_stub_runner(monkeypatch):
    """半真实 NiuRunner：跳过 __init__（副作用重），只挂 _on_before_llm 访问面。

    真实绑定：_on_before_llm / _extract_context_from_messages（MethodType，
    纯函数或核心被测逻辑）；类方法 _assemble_system_message /
    _refresh_dynamic_user_block / _build_dynamic_block 经 class 解析自动可用。
    """
    import agent.runner as runner_mod
    from agent.runner import NiuRunner

    runner = object.__new__(NiuRunner)
    # _on_before_llm 内部访问清单（缺什么补什么）：
    runner._active_dynamic_block = ""            # _refresh_dynamic_user_block 幂等锚
    runner.default_model = "qwen-test"           # 非 claude → content 保持字符串
    runner.static_system_prompt = "STATIC_PROMPT_MARKER"
    runner.dynamic_system_prefix = "\n\nDISK_DESC_MARKER"
    runner.base_system_prompt = "BASE_PROMPT_FALLBACK"   # compat except 降级分支用
    runner._first_turn_extra_injection = ""      # turn=0 不消费，显式置空
    # _run_idle_compression 直测取用的 runner 面（全 mock，禁真实 LLM/DB）：
    runner.client = MagicMock()
    runner._sync_get_messages = MagicMock(return_value=[])
    runner._sync_add_message = MagicMock()
    runner.llm_config = {"model": "qwen-test"}

    # MethodType 绑真实方法（策略 a 核心：测的就是真实 _on_before_llm）
    runner._on_before_llm = _types.MethodType(NiuRunner._on_before_llm, runner)
    runner._extract_context_from_messages = _types.MethodType(
        NiuRunner._extract_context_from_messages, runner)

    # monkeypatch 面：禁真实文件/向量检索，固定文本保断言确定性
    monkeypatch.setattr(runner_mod, "_load_memory_for_prompt",
                        lambda: "MEMORY_SECTION_MARKER")
    runner._inject_dynamic_resources = MagicMock(
        return_value=("INJECTION_MARKER", ""))
    runner._park_reminder_line = lambda: ""      # 免读真实 ~/.niu/memory.json
    return runner


def _complete_system_like_t1(runner, history):
    """复刻 compat._run_idle_compression 的 T1 补全三步（前置占位 → _on_before_llm）。"""
    if not history or history[0].get("role") != "system":
        history = [{"role": "system", "content": ""}] + list(history)
        try:
            runner._on_before_llm(history, 0)
        except Exception:
            history[0]["content"] = runner.base_system_prompt
    return history


def _dynamic_block_lines(messages):
    return [m for m in messages
            if m.get("role") == "user"
            and isinstance(m.get("content"), str)
            and m["content"].startswith(_DYN_HEADER)]


# ---------------------------------------------------------------------------
# 1. 补全行为直测：占位 system → 真实 _on_before_llm → 完整静态区 + 动态块
# ---------------------------------------------------------------------------

def test_completion_assembles_full_system_and_dynamic_block(monkeypatch):
    """无 system 视图经 T1 三步后：messages[0] = 完整静态区（非空占位），
    存在唯一 role=user 动态块行且含注入内容，位置在最后一个 user 之前。"""
    runner = _make_stub_runner(monkeypatch)
    history = [
        {"role": "user", "content": "早期输入"},
        {"role": "assistant", "content": "早期回复"},
        {"role": "user", "content": "最新输入"},
    ]

    completed = _complete_system_like_t1(runner, history)

    # 占位被真实组装覆写：完整静态区（静态指令 + disk_desc + memory 段）
    assert completed[0]["role"] == "system"
    content = completed[0]["content"]
    assert isinstance(content, str), "非 claude 模型 content 应为字符串"
    assert content != "", "占位空 system 必须被 _on_before_llm 覆写"
    assert "STATIC_PROMPT_MARKER" in content
    assert "DISK_DESC_MARKER" in content
    assert "MEMORY_SECTION_MARKER" in content

    # 动态块：唯一 role=user + 头标记行，含注入内容
    dyn = _dynamic_block_lines(completed)
    assert len(dyn) == 1, f"应恰好一条动态块行，实际 {len(dyn)}"
    assert "INJECTION_MARKER" in dyn[0]["content"]

    # 位置不变式：动态块紧贴最后一个 user（当前输入）之前，原输入仍在尾部
    assert completed[-1] == {"role": "user", "content": "最新输入"}
    assert completed[-2] is dyn[0]


# ---------------------------------------------------------------------------
# 2. 降级分支：_on_before_llm 抛异常 → base_system_prompt 覆写（恒单条 system）
# ---------------------------------------------------------------------------

def test_completion_fallback_to_base_prompt_on_error(monkeypatch):
    """_on_before_llm 失败 → except 覆写占位 content=base_system_prompt，
    不残留空 system、不产生双 system 行。"""
    runner = _make_stub_runner(monkeypatch)

    def _boom(messages, turn):
        raise RuntimeError("assembly exploded")
    runner._on_before_llm = _boom

    completed = _complete_system_like_t1(runner, [{"role": "user", "content": "x"}])

    assert [m["role"] for m in completed] == ["system", "user"], \
        "降级后必须恒单条 system（双 system 行违 OpenAI 兼容→400）"
    assert completed[0]["content"] == "BASE_PROMPT_FALLBACK"


# ---------------------------------------------------------------------------
# 3. _run_idle_compression 全链直测：rcc 收到的 messages[0] 是完整 system
# ---------------------------------------------------------------------------

def test_run_idle_compression_passes_full_system_to_rcc(monkeypatch):
    """T1 接线全链（_run_idle_compression → rcc）：patch rcc（禁真实 LLM），
    capture call_args.args[0]——messages[0].role==system 且含静态区+memory，
    存在动态块行。"""
    import niu_api.compat as compat

    runner = _make_stub_runner(monkeypatch)
    history = [
        {"role": "user", "content": "h1"},
        {"role": "assistant", "content": "a1"},
    ]

    with patch("agent.generic.agent_loop.run_controlled_compression") as rcc:
        rcc.return_value = ([], True)
        result = compat._run_idle_compression(runner, history)

    assert result == "compacted"
    assert rcc.called
    msgs = rcc.call_args.args[0]
    assert msgs[0]["role"] == "system", "总结请求首条必须是 system（000038 无 system 形态回归）"
    content = msgs[0]["content"]
    assert isinstance(content, str) and content != ""
    assert "STATIC_PROMPT_MARKER" in content
    assert "MEMORY_SECTION_MARKER" in content
    dyn = _dynamic_block_lines(msgs)
    assert len(dyn) == 1
    assert "INJECTION_MARKER" in dyn[0]["content"]
    # client 位置参数透传（ctx/client 接线未被补全改动破坏）
    assert rcc.call_args.args[2] is runner.client


# ---------------------------------------------------------------------------
# 4. 空 history：补全不抛，rcc 收到 [完整 system, 动态块]
# ---------------------------------------------------------------------------

def test_run_idle_compression_empty_history_no_crash(monkeypatch):
    """history=[] → 前置占位 + _on_before_llm（_extract_context_from_messages
    空列表安全）→ rcc 收到 messages[0].role==system，动态块插尾不抛。"""
    import niu_api.compat as compat

    runner = _make_stub_runner(monkeypatch)

    with patch("agent.generic.agent_loop.run_controlled_compression") as rcc:
        rcc.return_value = ([], True)
        result = compat._run_idle_compression(runner, [])

    assert result == "compacted"
    msgs = rcc.call_args.args[0]
    assert msgs and msgs[0]["role"] == "system"
    content = msgs[0]["content"]
    assert isinstance(content, str) and content != ""
    assert "STATIC_PROMPT_MARKER" in content
    # 无 user 消息时动态块插尾（insert_at=len(messages)）
    dyn = _dynamic_block_lines(msgs)
    assert len(dyn) == 1
    assert msgs[-1] is dyn[0]


# ---------------------------------------------------------------------------
# 5. FinalReview B P2-2：gate_acquired 透传——已闩（他轮滞回）场景失败不解闩
# ---------------------------------------------------------------------------

def test_idle_compression_gate_acquired_false_keeps_foreign_latch(monkeypatch):
    """B P2-2 idle 路径：AUTO_GATE 已被他轮置闩（滞回期）→ chat_session try_acquire
    返回 False → _run_idle_compression(gate_acquired=False) → rcc 收到
    release_on_failure=False → 失败出口不解闩，他轮滞回闩锁保留（防下轮冗余 auto 重压）。
    对照面：gate_acquired=True（默认/自己 acquire 到）→ release_on_failure=True。"""
    import niu_api.compat as compat
    from agent.context_assembler.compaction import AUTO_GATE

    runner = _make_stub_runner(monkeypatch)
    history = [{"role": "system", "content": "sys"}, {"role": "user", "content": "h1"}]

    AUTO_GATE.release()  # 干净起点（防他测试残留闩锁）
    try:
        assert AUTO_GATE.try_acquire(0.95) is True  # 真置闩（模拟他轮滞回保留）
        with patch("agent.generic.agent_loop.run_controlled_compression",
                   return_value=(history, False)) as rcc:  # 压缩失败早退
            result = compat._run_idle_compression(runner, history, gate_acquired=False)
        assert result == "skipped"
        # 透传锁：未 acquire 到 → release_on_failure=False（失败出口不解闩）
        assert rcc.call_args.kwargs.get("release_on_failure") is False
        # 他轮滞回闩锁保留（try_acquire 仍返回 False = 仍闩；若被误清会重新置闩返回 True）
        assert AUTO_GATE.try_acquire(0.95) is False

        # 对照面：自己 acquire 到（gate_acquired=True/默认）→ release_on_failure=True
        with patch("agent.generic.agent_loop.run_controlled_compression",
                   return_value=(history, False)) as rcc2:
            compat._run_idle_compression(runner, history)
        assert rcc2.call_args.kwargs.get("release_on_failure") is True
    finally:
        AUTO_GATE.release()  # 复位全局闩锁，不污染他测试

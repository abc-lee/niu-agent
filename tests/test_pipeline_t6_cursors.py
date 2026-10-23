"""T6 测试：force 压缩入口回归 + sleep 全序用例。

方案 docs/superpowers/plans/2026-08-20-tidy-pipeline-queue.md §4.3 + §5 T6 + §6 T6；
工程四 T2 门控三孤儿（_cursors_caught_up/_dream_only/_read_cursor_value）已删除，
原单元测试类随之移除；force/cm 回归用例已随 T6 压缩退役、T7 journal 迁 scheduler 移除，
保留：
1. sleep 全序用例（entity→dream；journal 已迁 scheduler 定时任务、cm 已退役——二者零调用）

全 mock：call_subagent_with_auto_answer / 游标文件（内存 _CursorStore 模拟真实文件往返）/
runner / TokenCalculator——禁真实 LLM、禁图谱写入、messages.db 零新增。
"""
import asyncio
import json
import os
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import agent.md_mirror as mdm

NORMAL_JSON = json.dumps({"ok": True})  # 非 overflow / 非 incomplete / 非 failure 的正常返回
OVERFLOW_JSON = json.dumps({
    "overflow": True, "agent": "a", "turns_completed": 1,
    "tokens_used": 1, "tokens_limit": 2, "partial_result": "",
})

#: F1 种子里的 DB 外尾段 id：严格 relay 不剪末轮 → 它必须留在 F1，前缀才进 F2
TAIL_ID = "t6-tail-not-in-db"


class _FakeCalc:
    """计数缝：主 Agent 口径 count_message_single；build 口径 count_text（逐记录原文）。"""

    def count_message_single(self, role, content, tool_calls=None):
        return 100

    def count_text(self, text):
        return 100


class _FakeRunner:
    def __init__(self):
        self.llm_config = {"model": "m", "apikey": "x", "apibase": "http://x"}
        self.handler = mock.MagicMock()
        self.handler._last_prompt_tokens = 0

    def _ensure_session_chain(self, max_days: int = 10) -> None:
        # dream 阶段收尾补链（真函数依赖 LightRAG，测试桩空操作）
        return None


class _Msg:
    def __init__(self, mid, role="user", content="hello", tool_call_id=""):
        self.id = mid
        self.role = role
        self.content = content
        self.tool_calls = None
        self.tool_call_id = tool_call_id


def _messages(*specs):
    """specs: list of (mid, role) tuples → [_Msg...]"""
    return [_Msg(mid, role) for mid, role in specs]


class _CursorStore:
    """内存游标文件：_write_cursor_with_lock 写入 → Path 读取（模拟真实文件往返）。

    测试 hermetic：不触碰 ~/.niu 真实游标文件。
    """

    def __init__(self, entity="", dream="", compress="", journal=""):
        self.files: dict[str, dict] = {}
        if entity:
            self.files[str(Path.home() / ".niu" / "last_entity_extract.json")] = {"last_entity_extract_id": entity}
        if dream:
            self.files[str(Path.home() / ".niu" / "last_dream_evolve.json")] = {"last_dream_evolve_id": dream}
        if compress:
            self.files[str(Path.home() / ".niu" / "last_compress.json")] = {"last_compress_id": compress}
        if journal:
            self.files[str(Path.home() / ".niu" / "last_journal.json")] = {"last_journal_id": journal}

    def write(self, path, data):
        self.files[str(path)] = dict(data)

    def read(self, path, key):
        data = self.files.get(str(path))
        if not data:
            return ""
        return data.get(key, "")

    def exists(self, path):
        return str(path) in self.files

    def read_text(self, path, encoding="utf-8"):
        return json.dumps(self.files[str(path)])


def _patch_cursor_store(store, call_mock=None):
    """T6 通用 fixture：内存游标 store 读写 + 全 mock 依赖（模式同 T5 _cp_patches）。"""

    def _exists(path_obj):
        return str(path_obj) in store.files

    def _read_text(path_obj, encoding="utf-8"):
        return json.dumps(store.files[str(path_obj)])

    return [
        mock.patch("agent.token_calculator.TokenCalculator.get", return_value=_FakeCalc()),
        mock.patch("agent.context_assembler.calibration.get_ratio", return_value=1.0),
        mock.patch("niu_api.compat._read_context_window_tokens", return_value=8000),
        mock.patch("niu_api.chat.get_or_create_runner", return_value=_FakeRunner()),
        mock.patch("agent.subagent.call_subagent_with_auto_answer", call_mock),
        mock.patch("niu_api.llm_proxy.get_llm_config", return_value={
            "model": "test-model", "apikey": "test-key", "apibase": "https://test.example.com",
            "type": "openai", "provider": "", "reasoning_effort": "", "litellm_kwargs": {},
        }),
        mock.patch("niu_api.compat.is_sleeping", return_value=True),
        # 游标文件读取（Path.exists/read_text）与写入（_write_cursor_with_lock）全部走内存 store。
        # exists/read_text 用普通函数（类属性替换后仍走描述符绑定，Path 实例作首参）
        mock.patch("pathlib.Path.exists", _exists),
        mock.patch("pathlib.Path.read_text", _read_text),
        mock.patch("niu_api.compat._write_cursor_with_lock", side_effect=store.write, create=True),
    ]


def _run_sleep_tidy(store, call_mock):
    """直接调 _tidy_context_impl sleep 分支（绕过 worker/CP0）。

    v2：种子记录写入隔离 F1（conftest tmp）使 entity 步真实执行；F2 patch 到
    测试专用 tmp（relay 剪切目标只允许落测试文件）。F1 种子里 TAIL_ID 是 DB 外
    id——严格 relay 不剪末轮，它常驻 F1 尾段。返回附 (f1_path, f2_path)。
    """
    import tempfile

    import agent.md_mirror as mdm
    from niu_api.compat import _tidy_context_impl

    msgs = _messages(("m1", "user"), ("m2", "user"))
    store_obj = mock.MagicMock()
    store_obj.get_messages = mock.AsyncMock(return_value=msgs)
    f2_path = os.path.join(tempfile.mkdtemp(prefix="t6_relay_"), "f2.md")
    with ExitStack() as stack:
        stack.enter_context(mock.patch("niu_api.compat.get_message_store", new=mock.AsyncMock(return_value=store_obj)))
        for p in _patch_cursor_store(store, call_mock):
            stack.enter_context(p)
        stack.enter_context(mock.patch("agent.md_mirror.F2_PATH", f2_path))
        block_m1 = mdm.format_message_record(
            msg_id="m1", created_at="t", role="user", content="种子一",
        )
        block_m2 = mdm.format_message_record(
            msg_id="m2", created_at="t", role="user", content="种子二",
        )
        assert mdm.append_record(block_m1, mdm.F1_PATH)
        assert mdm.append_record(block_m2, mdm.F1_PATH)
        # 严格 relay（strict_turn_ends）永不剪末轮：TAIL_ID 常驻 F1 尾段，用来判别
        # 「F1 只剩尾段」而非「F1 清空」
        block_tail = mdm.format_message_record(
            msg_id=TAIL_ID, created_at="t", role="user", content="种子三（DB 外尾段）",
        )
        assert mdm.append_record(block_tail, mdm.F1_PATH)
        result = asyncio.run(_tidy_context_impl({"mode": "sleep", "session_id": "t"}, chat_lock_already_held=True))
    return result, call_mock, mdm.F1_PATH, f2_path


def _agent_keyed_call(agent_results):
    """call_subagent_with_auto_answer mock：按 agent_name 返回对应结果。"""
    call_mock = mock.MagicMock()

    def side_effect(agent_name=None, task=None, **kwargs):
        value = agent_results.get(agent_name, NORMAL_JSON)
        return value() if callable(value) else value

    call_mock.side_effect = side_effect
    return call_mock


def _called_agents(call_mock):
    return [c.kwargs.get("agent_name") for c in call_mock.call_args_list]


def _turn_last_msg_ids_of(path):
    """文件里「每轮自末条向前第一条非空 msg_id」——轮边界取生产 mdm.all_turn_ends，id 取 mdm._msg_id_of。

    轮规则只在 md_mirror 里有一份，测试不复刻；F3 由 build_f3 从首个 user 记录起写，
    故 [0, 首个轮末) 窗口内不存在前置残片。
    """
    lines = open(path, encoding="utf-8").read().split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    recs = mdm._records(lines)
    ends = mdm.all_turn_ends(lines)
    out = []
    for lo, end in zip([0] + ends, ends, strict=False):
        ids = [mdm._msg_id_of(meta) for start, meta in recs if lo <= start < end]
        ids = [mid for mid in ids if mid]
        if ids:
            out.append(ids[-1])
    return out


def _dream_report(which=-1):
    """dream mock 工厂：上报当前 F3 里真实存在的完整轮末 msg_id（which=-1 即末轮）。"""
    def _report():
        ids = _turn_last_msg_ids_of(mdm.F3_PATH)
        assert ids, f"F3 无完整轮末，种子不成立: {mdm.F3_PATH}"
        return f"处理完成 @end\nprocessed_msg_id={ids[which]}"
    return _report


# ---------------------------------------------------------------------------
# 1. sleep 全序用例（entity→dream；journal/cm 均已退出睡眠管道）
# ---------------------------------------------------------------------------

def test_sleep_full_order_entity_then_dream():
    """T7 后睡眠全序：entity → dream（journal 迁 scheduler、cm 退役，均零调用）。

    entity relay 把 m1+m2 剪进 F2（F1 只剩 DB 外尾段），梦境循环按 msg_id 上报删空 F2。
    dream mock 上报的是当前 F3 里真实存在的完整轮末 msg_id（非静态行号）。
    """
    store = _CursorStore()
    call_mock = _agent_keyed_call({
        "entity-extractor": "处理完成 @end\nprocessed_line=999999",
        "dream-evolver": _dream_report(-1),
    })
    result, call_mock, f1, f2 = _run_sleep_tidy(store, call_mock)

    assert result.get("status") == "ok", f"应正常完成: {result}"
    agents = _called_agents(call_mock)
    assert agents == ["entity-extractor", "dream-evolver"], f"实际: {agents}"
    assert "context-manager" not in agents, "T6 后 context-manager 不得再被调"
    # 游标真实写回（内存 store）→ 校验游标退役后无残留写入；entity UUID 与 dream/compress 游标均零写
    assert store.read(Path.home() / ".niu" / "last_dream_evolve.json", "last_dream_evolve_id") == ""
    assert store.read(Path.home() / ".niu" / "last_compress.json", "last_compress_id") == ""
    assert store.read(Path.home() / ".niu" / "last_entity_extract.json", "last_entity_extract_id") == ""
    # v2：成功提炼后 F1 只剩未处理的末轮尾段，剪下前缀落入 F2；梦境循环再删空 F2
    with open(f1, encoding="utf-8") as f:
        f1_text = f.read()
    assert f'"msg_id": "{TAIL_ID}"' in f1_text, "未处理的末轮应留置 F1 尾段"
    assert '"msg_id": "m1"' not in f1_text and '"msg_id": "m2"' not in f1_text, "已提炼前缀应剪出 F1"
    with open(f2, encoding="utf-8") as f:
        assert f.read() == "", "梦境循环报末轮末条后 F2 应已删空"

"""图片直通通道测试（plan 2026-09-09-vision-retry.md §4-V3 / T3）。

覆盖：
① expand_image_markers 纯函数——无视觉原样返回 / 无标记原样返回 / 非 str 透传 /
   展开（text 段 + image_url data URI，base64 与文件字节一致）/ 缺文件降级留文本+警示 /
   >4MB PIL 降采样（真路径 jpeg + 失败回落警示）/ 非本地路径保留标记文本。
② mask_image_data_uris 共享打码 helper——str/dict/list 递归、字节数标注、非图 URI 不动。
③ 判定 helper——main_has_vision（llm_config capabilities 判定：input 含 image + model 匹配）/ preset_section_has_vision（tmp 配置 / 预读 dict）/
   _resolve_subagent_has_vision（SUPPORTED_PRESETS 门控 + llmPreset 正向规则 + 回落主 llm；警示归覆盖侧单点）。
④ 接线五点行为锁——transform_history has_vision 展开（默认 False 零影响）/
   agent_runner_loop 入口 history+当前 user 消息展开（fake client 捕获 LLM 请求）/
   runner._on_tool_round_refresh 工具轮重建不丢图（R2/R3 P1 阻断项）/
   subagent._prepare_resume_messages 续跑净化展开。
⑤ 出站打码三写函数——http_logger._write_log_entry / litellm_adapter._write_raw_log /
   _write_interaction_log：落盘文件无长 base64，[image data, N bytes] 在场。

全 mock：禁真实 LLM；配置路径 monkeypatch 到 tmp_path；tmp sqlite 禁碰 ~/.niu。
"""
import base64
import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.generic.agent_loop import (  # noqa: E402
    agent_runner_loop,
    transform_history,
)
from agent.image_channel import (  # noqa: E402
    MAX_IMAGE_BYTES,
    expand_image_markers,
    main_has_vision,
    mask_image_data_uris,
    preset_section_has_vision,
)
from agent.runner import NiuRunner  # noqa: E402
from agent.session import Message  # noqa: E402

# 预热 context_assembler 子模块再 import context_manager——打断
# context_manager ↔ compaction 循环导入（同 test_fold_view_refresh.py 制式）
import agent.context_assembler.blocks  # noqa: E402,F401
import agent.context_manager as cm_mod  # noqa: E402


# ---------------------------------------------------------------------------
# 基建：fake client/handler（与 test_fold_view_refresh.py 同制式）+ PNG 工厂
# ---------------------------------------------------------------------------

def _make_png(path, size=(8, 8), color=(255, 0, 0)):
    """生成纯色 PNG；返回文件字节。"""
    from PIL import Image
    img = Image.new("RGB", size, color)
    img.save(str(path), format="PNG")
    return Path(path).read_bytes()


def _resp(content="", tool_calls=()):
    r = mock.Mock()
    r.content = content
    r.stream_error = False
    r.context_overflow = False
    r.tool_calls = list(tool_calls)
    r.usage = None
    r.finish_reason = "stop"
    return r


class _FakeHandler:
    """驱动 agent_runner_loop 的最小 handler。"""
    _is_subagent = True

    def __init__(self, dispatches):
        self.dispatches = dispatches
        self._done_hooks = []
        self.max_turns = None
        self.current_turn = 0
        self._subagent_unique_name = ""

    def next_prompt_patcher(self, np, outcome, turn):
        return np

    def dispatch(self, tool_name, args, response, index=0):
        def gen():
            yield
            d = self.dispatches[tool_name]
            return d(args) if callable(d) else d
        return gen()


class _FakeClient:
    """按序返回响应；记录每次 LLM 请求的 messages deepcopy（事后断言）。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.last_tools = ""
        self.requests = []

    def chat(self, messages=None, tools=None):
        self.requests.append(copy.deepcopy(messages))
        r = self.responses.pop(0)

        def gen():
            yield r
            return r
        return gen()


def _run_loop(client, handler, **kw):
    """驱动 agent_runner_loop 至完成；返回 result。"""
    kw.setdefault("user_input", "Q")
    gen = agent_runner_loop(
        client=client, system_prompt="SYS", handler=handler,
        tools_schema=[], verbose=False, max_turns=10, enable_supplement=False, **kw)
    result = None
    try:
        while True:
            next(gen)
    except StopIteration as e:
        result = e.value
    return result


def _image_blocks(content):
    """从 content（str 或 list）取 image_url 块列表。"""
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == "image_url"]


def _text_of(content):
    """content（str 或 list）→ 纯文本拼接。"""
    if isinstance(content, str):
        return content
    return "".join(
        b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
    )


# ---------------------------------------------------------------------------
# ① expand_image_markers 纯函数
# ---------------------------------------------------------------------------

class TestExpandImageMarkers:
    def test_no_vision_returns_unchanged(self, tmp_path):
        p = tmp_path / "a.png"
        _make_png(p)
        content = f"看图 ![截图]({p}) 完成"
        out = expand_image_markers(content, has_vision=False)
        assert out is content

    def test_no_markers_unchanged(self):
        content = "没有标记的普通文本"
        out = expand_image_markers(content, has_vision=True)
        assert out is content

    def test_non_str_passthrough(self):
        assert expand_image_markers(None, True) is None
        lst = [{"type": "text", "text": "x"}]
        assert expand_image_markers(lst, True) is lst

    def test_expand_small_png_data_uri(self, tmp_path):
        p = tmp_path / "shot.png"
        raw = _make_png(p)
        content = f"请看这张图 ![截图]({p}) 然后回答"
        out = expand_image_markers(content, has_vision=True)
        assert isinstance(out, list)
        # 文本段：前缀 + alt + 后缀（alt 作为独立 text 段保留语义）
        texts = [b["text"] for b in out if b.get("type") == "text"]
        assert "请看这张图" in "".join(texts)
        assert "截图" in "".join(texts)
        assert "然后回答" in "".join(texts)
        # image 块：data URI，base64 与文件字节一致
        blocks = _image_blocks(out)
        assert len(blocks) == 1
        uri = blocks[0]["image_url"]["url"]
        assert uri.startswith("data:image/png;base64,")
        assert base64.b64decode(uri.split(",", 1)[1]) == raw

    def test_missing_file_degrades_with_warning(self, tmp_path):
        missing = tmp_path / "gone.png"
        content = f"看图 ![截图]({missing}) 完成"
        out = expand_image_markers(content, has_vision=True)
        assert isinstance(out, list)
        assert _image_blocks(out) == []
        text = _text_of(out)
        # 原标记保留 + 警示标记
        assert f"![截图]({missing})" in text
        assert "图片不可读" in text

    def test_http_url_kept_as_text_without_warning(self):
        content = "远程图 ![x](https://example.com/a.png) 结束"
        out = expand_image_markers(content, has_vision=True)
        assert isinstance(out, list)
        assert _image_blocks(out) == []
        text = _text_of(out)
        assert "![x](https://example.com/a.png)" in text
        assert "图片不可读" not in text

    def test_multiple_markers_order_preserved(self, tmp_path):
        p1 = tmp_path / "one.png"
        p2 = tmp_path / "two.png"
        _make_png(p1, color=(0, 255, 0))
        _make_png(p2, color=(0, 0, 255))
        content = f"一 ![a]({p1}) 二 ![b]({p2}) 三"
        out = expand_image_markers(content, has_vision=True)
        blocks = _image_blocks(out)
        assert len(blocks) == 2
        # 顺序与标记出现顺序一致
        b64_1 = base64.b64encode(Path(p1).read_bytes()).decode()
        b64_2 = base64.b64encode(Path(p2).read_bytes()).decode()
        assert blocks[0]["image_url"]["url"].endswith(b64_1)
        assert blocks[1]["image_url"]["url"].endswith(b64_2)

    @pytest.mark.skipif(
        importlib.util.find_spec("PIL") is None,
        reason="Pillow 不可用（降采样走失败回落路径）")
    def test_oversize_file_downsamples_to_budget(self, tmp_path):
        # 3000x2000 随机像素 PNG：deflate 压缩率极低，文件必 >4MB（不依赖 numpy）
        import os
        from PIL import Image
        p = tmp_path / "big.png"
        img = Image.frombytes("RGB", (3000, 2000), os.urandom(3000 * 2000 * 3))
        img.save(str(p), format="PNG")
        assert p.stat().st_size > MAX_IMAGE_BYTES
        out = expand_image_markers(f"![big]({p})", has_vision=True)
        blocks = _image_blocks(out)
        assert len(blocks) == 1
        uri = blocks[0]["image_url"]["url"]
        # 降采样产物为 jpeg，且总长受预算约束（base64 膨胀 4/3 + 头部余量）
        assert uri.startswith("data:image/jpeg;base64,")
        assert len(uri) <= MAX_IMAGE_BYTES * 4 // 3 + 1024

    def test_oversize_downsample_failure_falls_back_to_warning(self, tmp_path, monkeypatch):
        import agent.image_channel as ic
        p = tmp_path / "big.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * (MAX_IMAGE_BYTES + 1))
        monkeypatch.setattr(ic, "_downsample_to_data_uri", lambda path: None)
        out = expand_image_markers(f"![big]({p})", has_vision=True)
        assert _image_blocks(out) == []
        text = _text_of(out)
        assert f"![big]({p})" in text
        assert "图片不可读" in text


# ---------------------------------------------------------------------------
# ② mask_image_data_uris 共享打码 helper
# ---------------------------------------------------------------------------

class TestMaskImageDataUris:
    def test_str_masked_with_byte_count(self):
        b64 = base64.b64encode(b"01234567").decode()
        s = f"前缀 data:image/png;base64,{b64} 后缀"
        out = mask_image_data_uris(s)
        # 计数口径 = base64 文本长度（helper 只见字符串，不解码）
        assert out == f"前缀 [image data, {len(b64)} bytes] 后缀"
        assert b64 not in out

    def test_nested_dict_list_recursive(self):
        b64 = base64.b64encode(b"abcdefgh").decode()
        obj = {
            "messages": [
                {"role": "user", "content": f"x data:image/jpeg;base64,{b64} y"},
                {"role": "assistant", "content": [{"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}"}}]},
            ],
            "n": 42,
        }
        out = mask_image_data_uris(obj)
        dumped = json.dumps(out, ensure_ascii=False)
        assert b64 not in dumped
        assert f"[image data, {len(b64)} bytes]" in dumped
        # 非字符串叶子原样
        assert out["n"] == 42

    def test_non_image_content_untouched(self):
        s = "普通文本没有 URI"
        assert mask_image_data_uris(s) is s
        assert mask_image_data_uris(123) == 123
        assert mask_image_data_uris(None) is None


# ---------------------------------------------------------------------------
# ③ 判定 helper
# ---------------------------------------------------------------------------

class TestJudgementHelpers:
    MAIN_CFG = {"apibase": "http://x/v1", "model": "m"}

    @staticmethod
    def _cfg_with_caps(model="m", caps_model="m", input=None):
        cfg = dict(TestJudgementHelpers.MAIN_CFG)
        cfg["model"] = model
        if input is not None:
            cfg["capabilities"] = {"model": caps_model, "input": input, "probed_at": "2026-01-01T00:00:00"}
        return cfg

    def test_main_has_vision_true_when_image_input_and_model_match(self):
        """capabilities.input 含 image 且 capabilities.model == llm model → True。"""
        assert main_has_vision(self._cfg_with_caps(input=["text", "image"])) is True

    def test_main_has_vision_false_when_text_only_input(self):
        """input=["text"]（探测未命中视觉）→ False。"""
        assert main_has_vision(self._cfg_with_caps(input=["text"])) is False

    def test_main_has_vision_false_when_model_mismatch(self):
        """换模型后旧 capabilities.model 不匹配 → False（fail-closed，不采信旧能力）。"""
        assert main_has_vision(self._cfg_with_caps(model="new-model", caps_model="m")) is False

    def test_main_has_vision_false_when_no_capabilities(self):
        # 未探测过（无 capabilities 键）/ 无配置 → False（fail-closed）
        assert main_has_vision(self.MAIN_CFG) is False
        assert main_has_vision({"model": "m"}) is False
        assert main_has_vision(None) is False

    def test_main_has_vision_false_on_bad_shape(self):
        """capabilities 非 dict / input 缺省 → False（不抛）。"""
        assert main_has_vision({**self.MAIN_CFG, "capabilities": "text,image"}) is False
        assert main_has_vision({**self.MAIN_CFG, "capabilities": {"model": "m"}}) is False

    def test_preset_section_has_vision_model_nonempty(self, tmp_path, monkeypatch):
        import niu_api.config as niu_cfg
        path = tmp_path / "user-config.json"
        monkeypatch.setattr(niu_cfg, "CONFIG_PATH", str(path))
        path.write_text(json.dumps({
            "llm": {"apiKey": "k", "apiBase": "http://x/v1", "model": "main"},
            "vision_llm": {"model": "qwen38-xl", "apiBase": "http://192.168.3.88:8080/v1"},
        }), encoding="utf-8")
        assert preset_section_has_vision("vision_llm") is True

    def test_preset_section_has_vision_model_empty(self, tmp_path, monkeypatch):
        import niu_api.config as niu_cfg
        path = tmp_path / "user-config.json"
        monkeypatch.setattr(niu_cfg, "CONFIG_PATH", str(path))
        path.write_text(json.dumps({
            "llm": {"apiKey": "k", "apiBase": "http://x/v1", "model": "main"},
            "vision_llm": {"model": "", "apiBase": "http://x/v1"},
        }), encoding="utf-8")
        assert preset_section_has_vision("vision_llm") is False

    def test_preset_section_has_vision_missing_file(self, tmp_path, monkeypatch):
        import niu_api.config as niu_cfg
        monkeypatch.setattr(niu_cfg, "CONFIG_PATH", str(tmp_path / "absent.json"))
        assert preset_section_has_vision("vision_llm") is False

    def test_resolve_subagent_preset_bundle_true(self, monkeypatch):
        import agent.image_channel as ic
        import agent.subagent as sub
        monkeypatch.setattr(sub, "get_subagent_config", lambda name: {"llmPreset": "vision_llm"})
        monkeypatch.setattr(ic, "preset_section_has_vision", lambda preset, config_data=None: True)
        # user_cfg 预读 dict 传入（覆盖侧同一读盘结果）→ 段 model 非空 → True
        assert sub._resolve_subagent_has_vision("vision-agent", None, {"vision_llm": {"model": "x"}}) is True

    def test_resolve_subagent_user_cfg_none_fail_closed(self, monkeypatch):
        """user_cfg=None（读失败/未传入）→ 不查段不自读，直接回落主档案（防与覆盖侧分叉）。"""
        import agent.image_channel as ic
        import agent.subagent as sub
        monkeypatch.setattr(sub, "get_subagent_config", lambda name: {"llmPreset": "vision_llm"})
        # 若误自读/误查段将得 True——锁死 fail-closed 按主档案 False
        monkeypatch.setattr(ic, "preset_section_has_vision", lambda preset, config_data=None: True)
        monkeypatch.setattr(ic, "main_has_vision", lambda llm_config: False)
        assert sub._resolve_subagent_has_vision("vision-agent", None) is False

    def test_resolve_subagent_no_preset_falls_back_to_main(self, monkeypatch):
        import agent.image_channel as ic
        import agent.subagent as sub
        monkeypatch.setattr(sub, "get_subagent_config", lambda name: {})
        monkeypatch.setattr(ic, "main_has_vision", lambda llm_config: True)
        assert sub._resolve_subagent_has_vision("plain-agent", {"apibase": "http://x/v1"}) is True

    def test_resolve_subagent_preset_empty_model_falls_back_no_duplicate_warning(self, monkeypatch):
        """P3：段 model 空 → 回落主档案；警示由覆盖侧（call_subagent）单点留痕，判定侧不重复 log。"""
        import agent.image_channel as ic
        import agent.subagent as sub
        monkeypatch.setattr(sub, "get_subagent_config", lambda name: {"llmPreset": "vision_llm"})
        monkeypatch.setattr(ic, "main_has_vision", lambda llm_config: True)
        # subagent 用 loguru（caplog 不可见）→ 直接 stub 模块 logger
        warnings = []
        monkeypatch.setattr(sub, "logger", SimpleNamespace(warning=warnings.append))
        # user_cfg 预读 dict 传入（段 model 空）→ 回落主档案 True；判定侧零 warning
        assert sub._resolve_subagent_has_vision("vision-agent", None, {"vision_llm": {}}) is True
        assert warnings == []

    def test_resolve_subagent_unsupported_preset_uses_main_profile(self, monkeypatch):
        """P2-1：preset 不在 SUPPORTED_PRESETS → 不查段 model，直接按主档案判定。"""
        import agent.image_channel as ic
        import agent.subagent as sub
        monkeypatch.setattr(sub, "get_subagent_config", lambda name: {"llmPreset": "lightrag_llm"})
        # 若误查段（model 非空）将得 True——锁死按主档案 False
        monkeypatch.setattr(ic, "preset_section_has_vision", lambda preset, config_data=None: True)
        monkeypatch.setattr(ic, "main_has_vision", lambda llm_config: False)
        assert sub._resolve_subagent_has_vision("lr-agent", None, {"lightrag_llm": {"model": "x"}}) is False


# ---------------------------------------------------------------------------
# ④ 接线五点行为锁
# ---------------------------------------------------------------------------

class TestWiringTransformHistory:
    def test_transform_expands_user_and_tool_markers(self, tmp_path):
        p = tmp_path / "s.png"
        _make_png(p)
        history = [
            {"role": "user", "content": f"看图 ![截图]({p})"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "screenshot", "arguments": "{}"}}]},
            {"role": "tool", "content": f"![截图]({p})", "tool_call_id": "c1"},
        ]
        out = transform_history(copy.deepcopy(history), has_vision=True)
        assert len(_image_blocks(out[0]["content"])) == 1
        # assistant 纯文本不受影响
        assert out[1]["content"] == ""
        # tool 结果标记同样展开（截图工具轮后图不丢）
        assert len(_image_blocks(out[2]["content"])) == 1

    def test_transform_default_false_unchanged(self, tmp_path):
        p = tmp_path / "s.png"
        _make_png(p)
        history = [{"role": "user", "content": f"看图 ![截图]({p})"}]
        out = transform_history(history)
        assert out[0]["content"] == f"看图 ![截图]({p})"


class TestWiringAgentLoopEntry:
    def test_entry_expands_history_and_current_user(self, tmp_path):
        p = tmp_path / "s.png"
        _make_png(p)
        client = _FakeClient([_resp("完成")])
        handler = _FakeHandler({})
        history = [{"role": "user", "content": f"历史图 ![h]({p})"}]
        result = _run_loop(
            client, handler,
            history=history, user_input=f"当前图 ![c]({p})", has_vision=True)
        assert result["result"] == "CURRENT_TASK_DONE"
        req = client.requests[0]
        # system + history(user) + current(user)
        hist_user = [e for e in req if e.get("role") == "user" and _text_of(e["content"]).startswith("历史图")]
        cur_user = [e for e in req if e.get("role") == "user" and _text_of(e["content"]).startswith("当前图")]
        assert len(hist_user) == 1 and len(_image_blocks(hist_user[0]["content"])) == 1
        assert len(cur_user) == 1 and len(_image_blocks(cur_user[0]["content"])) == 1

    def test_entry_no_vision_keeps_marker_text(self, tmp_path):
        p = tmp_path / "s.png"
        _make_png(p)
        client = _FakeClient([_resp("完成")])
        handler = _FakeHandler({})
        result = _run_loop(
            client, handler, user_input=f"当前图 ![c]({p})", has_vision=False)
        assert result["result"] == "CURRENT_TASK_DONE"
        req = client.requests[0]
        cur_user = [e for e in req if e.get("role") == "user" and isinstance(e.get("content"), str)]
        assert any(f"![c]({p})" in e["content"] for e in cur_user)


class TestWiringToolRoundRefresh:
    def _cm(self, tmp_path, db_msgs):
        import agent.context_manager as cm
        return cm.ContextManager(_FakeStore(db_msgs), max_tokens=100_000,
                                 blocks_db_path=tmp_path / "blocks.db")

    def test_refresh_rebuild_keeps_expanded_image(self, tmp_path, monkeypatch):
        """R2/R3 P1 阻断项：工具轮后视图重建不得把已展开图还原成标记文本。"""
        p = tmp_path / "s.png"
        _make_png(p)
        db_msgs = [
            Message(id="m001", role="user", content=f"看图 ![截图]({p})",
                    tool_calls=[], tool_call_id="", folded=0, output_pct=None,
                    created_at="2026-09-09T10:00:00", rowid=1),
            Message(id="m002", role="assistant", content="",
                    tool_calls=[{"id": "c1", "type": "function",
                                 "function": {"name": "screenshot", "arguments": "{}"}}],
                    tool_call_id="", folded=0, output_pct=None,
                    created_at="2026-09-09T10:00:01", rowid=2),
            Message(id="m003", role="tool", content=f"![截图]({p})", tool_call_id="c1",
                    folded=0, output_pct=None, created_at="2026-09-09T10:00:02", rowid=3),
        ]
        cm = self._cm(tmp_path, db_msgs)
        r = NiuRunner.__new__(NiuRunner)
        monkeypatch.setattr(NiuRunner, "_sync_get_messages", lambda self, limit=None: list(db_msgs))
        monkeypatch.setattr(cm_mod, "peek_context_manager", lambda: cm)
        r._current_has_vision = True

        messages = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "Q"}]
        sys_entry = messages[0]
        r._on_tool_round_refresh(messages)
        # 原地替换 + system 同一性保留
        assert messages[0] is sys_entry
        tool_e = [e for e in messages if e.get("tool_call_id") == "c1"]
        assert len(tool_e) == 1
        # 工具轮重建后图仍在（展开态）——bug 态会是标记文本 str
        blocks = _image_blocks(tool_e[0]["content"])
        assert len(blocks) == 1
        assert blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_refresh_without_vision_flag_keeps_marker_text(self, tmp_path, monkeypatch):
        p = tmp_path / "s.png"
        _make_png(p)
        db_msgs = [
            Message(id="m001", role="user", content=f"看图 ![截图]({p})",
                    tool_calls=[], tool_call_id="", folded=0, output_pct=None,
                    created_at="2026-09-09T10:00:00", rowid=1),
        ]
        cm = self._cm(tmp_path, db_msgs)
        r = NiuRunner.__new__(NiuRunner)
        monkeypatch.setattr(NiuRunner, "_sync_get_messages", lambda self, limit=None: list(db_msgs))
        monkeypatch.setattr(cm_mod, "peek_context_manager", lambda: cm)
        # 未设 _current_has_vision（chat() 未启动）→ getattr 默认 False
        messages = [{"role": "system", "content": "SYS"}]
        r._on_tool_round_refresh(messages)
        user_e = [e for e in messages if e.get("role") == "user"][0]
        assert isinstance(user_e["content"], str)
        assert f"![截图]({p})" in user_e["content"]


class _FakeStore:
    """mock MessageStore——只实现 get_messages。"""

    def __init__(self, messages):
        self.messages = messages

    async def get_messages(self, limit=None):
        return list(self.messages) if limit is None else list(self.messages)[-limit:]


class TestWiringSubagentResume:
    def test_prepare_resume_expands_markers(self, tmp_path):
        import agent.subagent as sub
        p = tmp_path / "s.png"
        _make_png(p)
        archive = [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": f"任务 ![截图]({p})"},
            {"role": "assistant", "content": "收到"},
        ]
        cleaned = sub._prepare_resume_messages(archive, has_vision=True)
        assert cleaned[0]["role"] == "system"  # system 保留
        user_e = [e for e in cleaned if e.get("role") == "user"][0]
        assert len(_image_blocks(user_e["content"])) == 1

    def test_prepare_resume_no_vision_keeps_text(self, tmp_path):
        import agent.subagent as sub
        p = tmp_path / "s.png"
        _make_png(p)
        archive = [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": f"任务 ![截图]({p})"},
        ]
        cleaned = sub._prepare_resume_messages(archive, has_vision=False)
        user_e = [e for e in cleaned if e.get("role") == "user"][0]
        assert isinstance(user_e["content"], str)
        assert f"![截图]({p})" in user_e["content"]


# ---------------------------------------------------------------------------
# ⑤ 出站打码三写函数
# ---------------------------------------------------------------------------

_PAYLOAD = base64.b64encode(b"A" * 2048).decode()
_URI = f"data:image/png;base64,{_PAYLOAD}"


def _enabled_logging(monkeypatch):
    """三写函数共用的 get_logging_config().enabled 开关（模块内局部 import → patch 源模块）。"""
    import niu_api.config as niu_cfg
    monkeypatch.setattr(
        niu_cfg, "get_logging_config", lambda: SimpleNamespace(enabled=True))


class TestOutboundMasking:
    def test_http_logger_write_log_entry_masks(self, tmp_path, monkeypatch):
        import agent.generic.http_logger as hl
        _enabled_logging(monkeypatch)
        monkeypatch.setattr(hl, "_get_log_dir", lambda: tmp_path)
        entry = {"request": {"body": {"messages": [
            {"role": "user", "content": f"看图 {_URI}"}]}}}
        hl._write_log_entry(1, entry)
        files = list(tmp_path.glob("*.json"))
        assert len(files) == 1
        text = files[0].read_text(encoding="utf-8")
        assert _PAYLOAD not in text
        assert f"[image data, {len(_PAYLOAD)} bytes]" in text

    def test_litellm_write_raw_log_masks(self, tmp_path, monkeypatch):
        import agent.generic.litellm_adapter as la
        _enabled_logging(monkeypatch)
        monkeypatch.setattr(la, "_get_app_log_dir", lambda: tmp_path)
        data = {"request": {"body": {"messages": [
            {"role": "user", "content": f"看图 {_URI}"}]}}}
        la._write_raw_log("request", data, seq=1)
        # 落盘路径 = <app_log_dir>/raw_http/<YYYYMMDD>/<seq:06d>_request.json
        files = list(tmp_path.glob("raw_http/*/*.json"))
        assert len(files) == 1
        text = files[0].read_text(encoding="utf-8")
        assert _PAYLOAD not in text
        assert f"[image data, {len(_PAYLOAD)} bytes]" in text

    def test_litellm_write_interaction_log_masks(self, tmp_path, monkeypatch):
        import agent.generic.litellm_adapter as la
        _enabled_logging(monkeypatch)
        monkeypatch.setattr(la, "_get_app_log_dir", lambda: tmp_path)
        # 小载荷（32B）：交互日志用户输入 400 字截断不生效——打码是唯一防线
        small_b64 = base64.b64encode(b"B" * 32).decode()
        entry = {
            "type": "request",
            "timestamp": "2026-09-10T10:00:00",
            "model": "m",
            "messages": [{"role": "user", "content": f"看图 data:image/jpeg;base64,{small_b64} 描述"}],
            "tools": [],
        }
        la._write_interaction_log(entry)
        files = list(tmp_path.glob("llm_interaction_*.log"))
        assert len(files) == 1
        text = files[0].read_text(encoding="utf-8")
        assert small_b64 not in text
        assert f"[image data, {len(small_b64)} bytes]" in text

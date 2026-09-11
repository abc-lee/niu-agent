"""vision-server analyze_image 工具测试（plan 2026-09-11-vision-channel-refactor §3.2 / §6）。

全 mock——禁真实 LLM、禁真实截图写 ~/.niu、禁图谱：
- LiteLLMSession 以 fake 类替换 agent.generic.litellm_adapter 模块属性；fake chat()
  还原真实返回形态（generator yield str chunks + StopIteration.value 携带 MockResponse，
  MockResponse 用真实的 agent.generic.llmcore.MockResponse——mock 与生产零漂移）
- get_llm_config / CONFIG_PATH / _image_to_data_uri 在函数级 import 解析点 monkeypatch

覆盖（plan §6 必测项）：
- 1 读图：真实 PNG 临时文件 → data URI（走真实 _image_to_data_uri，不 mock）；
  缺文件/非图片载荷/超限降采样失败（mock None）→ 明确中文错误串（不抛异常）
- 2 选模型（主模型优先）：主模型有视觉 → 用主 llm 段（断言未读 vision 段、
  未调 get_llm_config(use_vision_config=True)）；主模型无视觉 + vision_llm.model
  非空 → 用该段；皆无 → 明确错误（含配置指引）；段判空走原始 user-config.json
- 3 请求形态：messages = [{role:user, content:[text(question), image_url(data_uri)]}]
- 3b Schema 文案锁：description 含两段式指引（泛问建立认知 + 聚焦追问）且不含
  「必须给具体问题」式误导措辞
- 3c cfg 键映射锁：api_type == 配置 type（非恒 openai）
- 3d sticky id 锁：sticky_session_id == "analyze-image"
- 4 返回值：纯文本、不含 `![`（D-C 行为锁）
- 5 停止语义归属锁：实现不含 run_interruptibly / is_stop_requested
- 5b 空回答细分：content 空 + finish_reason=length → 「输出预算耗尽」；
  content 空 + 其它 finish_reason → 通用失败文案
- 6 异常：stream_error / chat() 抛错 → 明确错误串，不抛异常
- 11 打码：请求 payload 里的 data URI 经 mask_image_data_uris 被打码（保留项仍生效）
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp-servers" / "vision-server" / "src"))

import niu_vision_server  # noqa: E402,F401

from agent.generic.llmcore import MockResponse  # noqa: E402  (真实返回形态，禁自造 fake)

_FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64  # PNG 魔数 + 任意载荷（<4MB 走直编码，无需 PIL）

# ---- 配置样本（get_llm_config 的小写键返回形态）----
MAIN_CFG_NO_VISION = {
    "type": "openai", "apikey": "main-key", "apibase": "http://main/v1",
    "model": "main-model", "reasoning_effort": "", "provider": "", "litellm_kwargs": {},
}
MAIN_CFG_WITH_VISION = {
    **MAIN_CFG_NO_VISION,
    "capabilities": {"model": "main-model", "input": ["text", "image"]},
}
VISION_CFG = {
    "type": "anthropic", "apikey": "vision-key", "apibase": "http://vision/v1",
    "model": "vision-model", "reasoning_effort": "", "provider": "", "litellm_kwargs": {},
}


class FakeLiteLLMSession:
    """还原真实 LiteLLMSession 契约：chat() 返回 generator（yield str chunks），
    StopIteration.value 携带 MockResponse（真实 llmcore.MockResponse）。"""

    instances: list = []
    response = None
    raise_exc = None

    def __init__(self, cfg):
        self.cfg = dict(cfg)
        self.last_messages = None
        FakeLiteLLMSession.instances.append(self)

    def chat(self, messages, tools=None, response_format=None):
        self.last_messages = messages
        if FakeLiteLLMSession.raise_exc is not None:
            raise FakeLiteLLMSession.raise_exc
        resp = FakeLiteLLMSession.response

        def gen():
            yield "stream-chunk"  # 真实 chat() yield str 增量
            return resp  # StopIteration.value = MockResponse

        return gen()


def _resp(content="", finish_reason="stop", stream_error=False, error_msg=None):
    return MockResponse(
        thinking="", content=content, tool_calls=[], raw=content or "",
        finish_reason=finish_reason, stream_error=stream_error, error_msg=error_msg,
    )


@pytest.fixture(autouse=True)
def _reset_fake_session(monkeypatch):
    FakeLiteLLMSession.instances.clear()
    FakeLiteLLMSession.response = None
    FakeLiteLLMSession.raise_exc = None
    monkeypatch.setattr("agent.generic.litellm_adapter.LiteLLMSession", FakeLiteLLMSession)


def _install_fake_llm_config(monkeypatch, main_cfg, vision_cfg=None):
    """替换 get_llm_config（函数级 import 解析点），记录每次调用的 kwargs。"""
    calls = []

    def fake(use_lightrag_config=False, use_vision_config=False, config_data=None):
        calls.append({"use_lightrag_config": use_lightrag_config, "use_vision_config": use_vision_config})
        if use_vision_config:
            return vision_cfg or main_cfg
        return main_cfg

    fake.calls = calls
    monkeypatch.setattr("niu_api.llm_proxy.get_llm_config", fake)
    return fake


def _write_user_config(tmp_path, vision_model=""):
    """原始 user-config.json（段判空走这里的读取，plan §3.2 步骤 2）。"""
    p = tmp_path / "user-config.json"
    p.write_text(json.dumps({"llm": {"model": "main-model"},
                             "vision_llm": {"model": vision_model}}), encoding="utf-8")
    return str(p)


def _png_file(tmp_path):
    p = tmp_path / "shot.png"
    p.write_bytes(_FAKE_PNG)
    return p


# ============== 项 1：读图（魔数探测 / 缺文件 / 非图 / 降采样失败） ==============

class TestImageLoading:
    def test_real_png_file_becomes_data_uri(self, monkeypatch, tmp_path):
        """真实 _image_to_data_uri（不 mock）：PNG 魔数 → data:image/png;base64。"""
        fake_cfg = _install_fake_llm_config(monkeypatch, MAIN_CFG_WITH_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_user_config(tmp_path))
        png = _png_file(tmp_path)
        FakeLiteLLMSession.response = _resp("图中是一个计算器。")

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert result == "图中是一个计算器。"
        session = FakeLiteLLMSession.instances[0]
        url = session.last_messages[0]["content"][1]["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")

    def test_missing_file_returns_chinese_error(self, monkeypatch, tmp_path):
        _install_fake_llm_config(monkeypatch, MAIN_CFG_WITH_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_user_config(tmp_path))

        result = niu_vision_server.analyze_image(str(tmp_path / "nope.png"), "这张图里有什么")

        assert "读图失败" in result
        assert FakeLiteLLMSession.instances == []  # 未发起任何模型调用

    def test_non_image_payload_returns_chinese_error(self, monkeypatch, tmp_path):
        """非图片载荷（假扩展名）→ 魔数探测 None → 错误串，不猜 media type。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_WITH_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_user_config(tmp_path))
        bad = tmp_path / "fake.png"
        bad.write_bytes(b"this is not an image at all")

        result = niu_vision_server.analyze_image(str(bad), "这张图里有什么")

        assert "读图失败" in result
        assert FakeLiteLLMSession.instances == []

    def test_downsample_failure_returns_chinese_error(self, monkeypatch, tmp_path):
        """>4MB 且降采样失败（helper 返回 None）→ 错误串。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_WITH_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_user_config(tmp_path))
        png = _png_file(tmp_path)
        monkeypatch.setattr("agent.image_channel._image_to_data_uri", lambda path: None)

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert "读图失败" in result
        assert FakeLiteLLMSession.instances == []

    def test_relative_path_rejected(self, monkeypatch):
        _install_fake_llm_config(monkeypatch, MAIN_CFG_WITH_VISION)

        result = niu_vision_server.analyze_image("relative/shot.png", "这张图里有什么")

        assert "绝对路径" in result
        assert FakeLiteLLMSession.instances == []

    def test_empty_question_rejected(self, monkeypatch, tmp_path):
        _install_fake_llm_config(monkeypatch, MAIN_CFG_WITH_VISION)
        png = _png_file(tmp_path)

        result = niu_vision_server.analyze_image(str(png), "   ")

        assert "question" in result and "必填" in result
        assert FakeLiteLLMSession.instances == []


# ============== 项 2：选模型（主模型优先，D-D） ==============

class TestModelSelection:
    def test_main_model_with_vision_wins_over_vision_section(self, monkeypatch, tmp_path):
        """主模型有视觉 → 用主 llm 段；vision_llm.model 非空也不读（短路证明）。"""
        fake_cfg = _install_fake_llm_config(monkeypatch, MAIN_CFG_WITH_VISION, VISION_CFG)
        # vision 段故意配了 model——若实现错误地落到 vision 段，model 断言必红
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_user_config(tmp_path, vision_model="vision-model"))
        png = _png_file(tmp_path)
        FakeLiteLLMSession.response = _resp("OK")

        niu_vision_server.analyze_image(str(png), "这张图里有什么")

        session = FakeLiteLLMSession.instances[0]
        assert session.cfg["model"] == "main-model"
        assert all(c["use_vision_config"] is False for c in fake_cfg.calls)

    def test_main_without_vision_falls_back_to_vision_section(self, monkeypatch, tmp_path):
        """主模型无视觉 + vision_llm.model 非空 → get_llm_config(use_vision_config=True)。"""
        fake_cfg = _install_fake_llm_config(monkeypatch, MAIN_CFG_NO_VISION, VISION_CFG)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_user_config(tmp_path, vision_model="vision-model"))
        png = _png_file(tmp_path)
        FakeLiteLLMSession.response = _resp("OK")

        niu_vision_server.analyze_image(str(png), "这张图里有什么")

        session = FakeLiteLLMSession.instances[0]
        assert session.cfg["model"] == "vision-model"
        assert any(c["use_vision_config"] is True for c in fake_cfg.calls)

    def test_neither_main_nor_section_returns_error_with_guidance(self, monkeypatch, tmp_path):
        """主模型无视觉 + vision_llm.model 空 → 明确错误（含配置指引），不发起调用。"""
        fake_cfg = _install_fake_llm_config(monkeypatch, MAIN_CFG_NO_VISION, VISION_CFG)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_user_config(tmp_path, vision_model=""))
        png = _png_file(tmp_path)

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert "识图不可用" in result
        assert "vision_llm" in result  # 配置指引
        assert FakeLiteLLMSession.instances == []
        # 段判空走原始 user-config.json——get_llm_config(use_vision_config=True) 不得用于判空
        assert all(c["use_vision_config"] is False for c in fake_cfg.calls)

    def test_user_config_read_failure_degrades_to_error(self, monkeypatch, tmp_path):
        """user-config.json 读失败（主模型又无视觉）→ 错误串，不抛异常。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_NO_VISION, VISION_CFG)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", str(tmp_path / "missing.json"))
        png = _png_file(tmp_path)

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert "识图不可用" in result
        assert FakeLiteLLMSession.instances == []


# ============== 项 3 / 3b / 3c / 3d / 4：请求形态 + Schema 文案锁 + cfg 映射锁 + 返回值 ==============

class TestRequestAndResponse:
    def test_messages_shape_carries_question_and_data_uri(self, monkeypatch, tmp_path):
        """提示词必须真的带上（本工程核心诉求）：text 段 == question，image_url 段 == data URI。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_WITH_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_user_config(tmp_path))
        png = _png_file(tmp_path)
        FakeLiteLLMSession.response = _resp("顶部状态栏显示 12:30")
        question = "最顶部标题栏写的是什么字？"

        niu_vision_server.analyze_image(str(png), question)

        msgs = FakeLiteLLMSession.instances[0].last_messages
        assert len(msgs) == 1 and msgs[0]["role"] == "user"
        text_seg, img_seg = msgs[0]["content"]
        assert text_seg == {"type": "text", "text": question}
        assert img_seg["type"] == "image_url"
        assert img_seg["image_url"]["url"].startswith("data:image/png;base64,")

    def test_schema_description_two_stage_guidance(self):
        """3b：description 含两段式指引（泛问建立认知 + 聚焦追问），禁「必须给具体问题」措辞。"""
        d = niu_vision_server.TOOL_SCHEMAS["analyze_image"]["description"]
        assert "整体" in d            # 泛问建立认知
        assert "再问一次同一张图" in d  # 聚焦追问
        assert "反复调用" in d         # 同图可反复问
        assert "必须给具体问题" not in d

    def test_schema_requires_both_params(self):
        schema = niu_vision_server.TOOL_SCHEMAS["analyze_image"]["input_schema"]
        assert schema["required"] == ["image_path", "question"]
        assert "image_path" in schema["properties"] and "question" in schema["properties"]

    def test_cfg_key_mapping_api_type_from_config_type(self, monkeypatch, tmp_path):
        """3c：api_type == 配置 type（非恒 openai）——键映射照 llm_proxy.py:354-368 先例。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_NO_VISION, VISION_CFG)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_user_config(tmp_path, vision_model="vision-model"))
        png = _png_file(tmp_path)
        FakeLiteLLMSession.response = _resp("OK")

        niu_vision_server.analyze_image(str(png), "这张图里有什么")

        cfg = FakeLiteLLMSession.instances[0].cfg
        assert cfg["api_type"] == "anthropic"  # VISION_CFG["type"]，而非默认 openai
        assert cfg["apikey"] == "vision-key" and cfg["model"] == "vision-model"

    def test_sticky_session_id_is_analyze_image(self, monkeypatch, tmp_path):
        """3d：独立 sticky id（防与主对话/其它通道串扰）。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_WITH_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_user_config(tmp_path))
        png = _png_file(tmp_path)
        FakeLiteLLMSession.response = _resp("OK")

        niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert FakeLiteLLMSession.instances[0].cfg["sticky_session_id"] == "analyze-image"

    def test_returns_plain_text_without_marker(self, monkeypatch, tmp_path):
        """4：纯文本返回，不含 `![`（D-C 行为锁）。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_WITH_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_user_config(tmp_path))
        png = _png_file(tmp_path)
        FakeLiteLLMSession.response = _resp("顶部状态栏显示 12:30")

        result = niu_vision_server.analyze_image(str(png), "顶部状态栏显示什么")

        assert result == "顶部状态栏显示 12:30"
        assert "![" not in result


# ============== 项 5 / 5b：停止语义归属锁 + 空回答细分 ==============

class TestStopSemanticsAndEmptyResponse:
    def test_implementation_has_no_stop_wrapping(self):
        """5：工具内不做独立 stop 包装（外层 agent_loop run_interruptibly 统一提供）。"""
        src = (Path(__file__).resolve().parent.parent / "mcp-servers" / "vision-server"
               / "src" / "niu_vision_server" / "__init__.py").read_text(encoding="utf-8")
        assert "run_interruptibly" not in src
        assert "is_stop_requested" not in src

    def test_empty_content_with_length_finish_reason_mentions_budget(self, monkeypatch, tmp_path):
        """5b：思考型视觉模型 + 低 max_tokens → 推理耗尽预算，提示须含「输出预算耗尽」。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_WITH_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_user_config(tmp_path))
        png = _png_file(tmp_path)
        FakeLiteLLMSession.response = _resp(content="", finish_reason="length")

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert "输出预算耗尽" in result
        assert "max_tokens" in result

    def test_empty_content_with_other_finish_reason_generic_error(self, monkeypatch, tmp_path):
        """5b：content 空 + 非 length → 通用失败文案（不得误报预算耗尽）。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_WITH_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_user_config(tmp_path))
        png = _png_file(tmp_path)
        FakeLiteLLMSession.response = _resp(content="", finish_reason="stop")

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert "识图失败" in result
        assert "输出预算耗尽" not in result


# ============== 项 6：异常路径（stream_error / chat 抛错） ==============

class TestErrorPaths:
    def test_stream_error_returns_chinese_error(self, monkeypatch, tmp_path):
        _install_fake_llm_config(monkeypatch, MAIN_CFG_WITH_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_user_config(tmp_path))
        png = _png_file(tmp_path)
        FakeLiteLLMSession.response = _resp(stream_error=True, error_msg="HTTP 429 rate limited")

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert "识图失败" in result and "模型调用出错" in result
        assert "429" in result

    def test_chat_exception_returns_chinese_error_not_raise(self, monkeypatch, tmp_path):
        _install_fake_llm_config(monkeypatch, MAIN_CFG_WITH_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_user_config(tmp_path))
        png = _png_file(tmp_path)
        FakeLiteLLMSession.raise_exc = Exception("connection timeout")

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")  # 不抛异常

        assert result.startswith("识图失败：")
        assert "connection timeout" in result


# ============== 项 11：打码（mask_image_data_uris 对本工具 payload 形态仍生效） ==============

class TestDataUriMasking:
    def test_request_payload_data_uri_is_maskable(self):
        """analyze_image 请求的 data URI 在 raw_http / 交互日志中被打码（保留项依赖）。"""
        from agent.image_channel import mask_image_data_uris

        payload_b64 = "A" * 1024
        data_uri = f"data:image/png;base64,{payload_b64}"
        payload = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "这张图里有什么"},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        }]

        masked = json.dumps(mask_image_data_uris(payload), ensure_ascii=False)

        assert payload_b64 not in masked
        assert "[image data, 1024 bytes]" in masked

"""vision-server analyze_image 工具测试（plan 2026-09-11-vision-channel-refactor §3.2 / §6）。

全 mock——禁真实 LLM、禁真实截图写 ~/.niu、禁图谱：
- LiteLLMSession 以 fake 类替换 agent.generic.litellm_adapter 模块属性；fake chat()
  还原真实返回形态（generator yield str chunks + StopIteration.value 携带 MockResponse，
  MockResponse 用真实的 agent.generic.llmcore.MockResponse——mock 与生产零漂移）
- get_llm_config / CONFIG_PATH / _image_to_data_uri 在函数级 import 解析点 monkeypatch

覆盖（plan §6 必测项）：
- 1 读图：真实 PNG 临时文件 → data URI（走真实 _image_to_data_uri，不 mock）；
  缺文件/非图片载荷/超限降采样失败（mock None）→ 明确中文错误串（不抛异常）
- 2 选模型（主模型优先）：主模型有视觉 → 用主 llm 段（断言未读 vision 段）；
  主模型无视觉 + vision_llm.model 非空 → 用该段；皆无 → 明确错误（含配置指引）；
  段判空走原始 user-config.json
- 3 请求形态：messages = [{role:user, content:[text(question), image_url(data_uri)]}]
- 3b Schema 文案锁：description 含两段式指引（泛问建立认知 + 聚焦追问）且不含
  「必须给具体问题」式误导措辞
- 3c cfg 键映射锁：api_type == 配置 type（非恒 openai）
- 3d sticky id 锁：sticky_session_id == "analyze-image"
- 4 返回值：纯文本、不含 `![`（D-C 行为锁）
- 5 停止语义归属锁：实现不含 run_interruptibly（新实现在调用间隙查全局
  is_stop_requested——行为由下方用例 13/14 锁定，源码字面禁令已按 plan §5 用例 18 删除）
- 5b 空回答细分：content 空 + finish_reason=length → 「输出预算耗尽」；
  content 空 + 其它 finish_reason → 通用失败文案
- 6 异常：stream_error / chat() 抛错 → 明确错误串，不抛异常
- 11 打码：请求 payload 里的 data URI 经 mask_image_data_uris 被打码（保留项仍生效）

(2026-09-11-vision-model-fallback plan §5——多模型链自动降级)：
- 用例 1 链构建（数组顺序 / 单对象回退 / 空数组 / 非数组 / 空 model 节 / 残留组合告警 / 主模型入链首）
- 用例 2-10、16、17、20 降级行为（一次成功无注记 / 重试后恢复 / fatal 直降 / 裸异常通道 /
  重试耗尽降级 / 多跳独立配额 / 主模型入链 / 单模型文案 / 全链汇总 / 跳过节注记 / 零回归 / 未知不重试）
- 用例 12 重试退避 2/5/10 + retry after N 覆盖；用例 13/14 stop（error_type / 调用期间置位含单模型变体）
- 用例 15/21 总预算中断（M≥2 / M=1 特例文案）；用例 19 全部失败/注记文案不含 `![`
- 用例 18 = 改写既有锁测试 test_implementation_has_no_stop_wrapping；用例 22-24 在 tests/test_vision_llm_config.py（T2）

(2026-09-12-vision-retry-policy-fix plan §5——F-1 重试收窄 + F-3 可达性预检)：
- 用例 1-7 分类（Timeout/APIConnectionError 不重试 / RateLimit·ServiceUnavailable 重试 3 次 /
  未归类含 retry after N 文本重试 / 未归类无忙信号不重试 / 认证·配额回归锁）
- 用例 8-11 预检（不可达零调用降级 / 成功路径 + close / 解析失败跳过 / 创建异常 fail-open / deadline 递减）
"""

import json
import socket
import sys
import types
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


class FakeLiteLLMSession:
    """还原真实 LiteLLMSession 契约：chat() 返回 generator（yield str chunks），
    StopIteration.value 携带 MockResponse（真实 llmcore.MockResponse）。

    script：按实例行为队列（新实例创建时按序消费）——多模型链 / 重试序列；
    条目为 MockResponse 或 Exception 实例（后者在 chat() 内直接抛出 = 建连阶段
    裸异常，D-D 通道 A）。空则回退类属性 response/raise_exc 语义（既有测试不变）。
    on_chunk：gen() 内 yield 之后的回调——「调用期间」置位 stop（plan §5 用例 14）。"""

    instances: list = []
    script: list = []
    response = None
    raise_exc = None
    on_chunk = None

    def __init__(self, cfg):
        self.cfg = dict(cfg)
        self.last_messages = None
        self._behavior = FakeLiteLLMSession.script.pop(0) if FakeLiteLLMSession.script else None
        FakeLiteLLMSession.instances.append(self)

    def chat(self, messages, tools=None, response_format=None):
        self.last_messages = messages
        behavior = self._behavior
        if behavior is None:
            if FakeLiteLLMSession.raise_exc is not None:
                raise FakeLiteLLMSession.raise_exc
            behavior = FakeLiteLLMSession.response
        elif isinstance(behavior, BaseException):
            raise behavior

        def gen():
            yield "stream-chunk"  # 真实 chat() yield str 增量
            if FakeLiteLLMSession.on_chunk is not None:
                FakeLiteLLMSession.on_chunk()  # 调用期间置位 stop（plan §5 用例 14）
            return behavior  # StopIteration.value = MockResponse

        return gen()


def _resp(content="", finish_reason="stop", stream_error=False, error_msg=None,
          error_type=None, error_type_name=None):
    return MockResponse(
        thinking="", content=content, tool_calls=[], raw=content or "",
        finish_reason=finish_reason, stream_error=stream_error, error_msg=error_msg,
        error_type=error_type, error_type_name=error_type_name,
    )


class _FakeProbeSocket:
    """F-3 预检假 socket（autouse 打桩）：connect 默认成功 → 预检通过 (True, "")；
    close() 被记录——fd 泄漏锁（条件式断言：仅当本用例真的创建过预检 socket）。"""
    instances: list = []

    def __init__(self, af=None, socktype=None, proto=None):
        self.closed = False
        _FakeProbeSocket.instances.append(self)

    def settimeout(self, value):
        pass

    def connect(self, sa):
        pass  # 默认成功（autouse getaddrinfo 打桩给出的候选地址）

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_fake_session(monkeypatch):
    FakeLiteLLMSession.instances.clear()
    FakeLiteLLMSession.script.clear()
    FakeLiteLLMSession.response = None
    FakeLiteLLMSession.raise_exc = None
    FakeLiteLLMSession.on_chunk = None
    _FakeProbeSocket.instances.clear()
    monkeypatch.setattr("agent.generic.litellm_adapter.LiteLLMSession", FakeLiteLLMSession)
    # stop 状态隔离：默认「未停止」（plan §5 用例 14 autouse 清旗防跨测试泄漏）；
    # 单测可再 patch agent.generic.litellm_adapter.is_stop_requested 为自己的 flag。
    monkeypatch.setattr("agent.generic.litellm_adapter.is_stop_requested", lambda: False)
    # F-3 预检打桩（2026-09-12 plan v0.10 实施要求①）：夹具主机 main 走真实 DNS 会解析失败 →
    # 判不可达 → 零调用 → 约 20 处 len(instances) 断言连锁全红。同时打桩两个入口
    # socket.getaddrinfo + socket.socket（用例 8/10b 可在测试内覆盖）；close 断言条件式——
    # 仅当本用例真的创建过预检 socket（未进降级循环的用例不得误红）。
    monkeypatch.setattr(
        niu_vision_server.socket, "getaddrinfo",
        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))])
    monkeypatch.setattr(niu_vision_server.socket, "socket", _FakeProbeSocket)
    yield
    for s in _FakeProbeSocket.instances:
        assert s.closed, "预检 socket 未显式 close（fd 泄漏）"


def _install_fake_llm_config(monkeypatch, main_cfg):
    """替换 get_llm_config（函数级 import 解析点）。"""

    def fake(use_lightrag_config=False):
        return main_cfg

    monkeypatch.setattr("niu_api.llm_proxy.get_llm_config", fake)
    return fake


def _write_user_config(tmp_path, vision_model="", vision=None):
    """原始 user-config.json（段判空与链节点都读这里，plan §3.2 步骤 2 / R-2）。
    vision 非 None → 以该 dict 作为整个 vision_llm 段（多模型链形态）；否则 {"model": vision_model}。"""
    p = tmp_path / "user-config.json"
    section = vision if vision is not None else {"model": vision_model}
    p.write_text(json.dumps({"llm": {"model": "main-model"},
                             "vision_llm": section}), encoding="utf-8")
    return str(p)


def _png_file(tmp_path):
    p = tmp_path / "shot.png"
    p.write_bytes(_FAKE_PNG)
    return p


# ============== plan 2026-09-11-vision-model-fallback §5 helpers ==============

class AuthenticationError(Exception):
    """类名与 litellm 认证异常一致——D-D 通道 A 按 type(e).__name__ 查 fatal 表。"""


class MysteryWeirdError(Exception):
    """未归类异常（不在可重试/致命表、文本无提示）→ unknown，不重试（U-4）。"""


def _models_section(*names):
    """vision_llm.models 数组段（各节只给 model，其余空键继承主 llm 段）。"""
    return {"models": [{"model": n} for n in names]}


def _r429():
    """retryable 形态（适配层内部重试耗尽后的真实形状：retry_exhausted + 文本含 rate limit）。"""
    return _resp(stream_error=True, error_type="retry_exhausted",
                 error_msg="Error code: 429 - rate limit exceeded")


def _fatal(msg):
    """fatal 形态（适配层真实形状：stream_error=True + error_type='fatal'；
    fatal 路径下 error_type_name 恒 None——不得用它判 fatal）。"""
    return _resp(stream_error=True, error_type="fatal", error_msg=msg)


def _stub_time(monkeypatch, step=0.0):
    """把 niu_vision_server 命名空间里的 time 换成隔离 stub（不影响进程全局 time）：
    monotonic 每次调用前进 step（总预算测试模拟耗时），sleep 只记录参数（不真等）。
    返回记录的 sleep 列表。"""
    t = [0.0]
    sleeps = []

    def fake_monotonic():
        t[0] += step
        return t[0]

    stub = types.SimpleNamespace(monotonic=fake_monotonic, sleep=sleeps.append)
    monkeypatch.setattr(niu_vision_server, "time", stub)
    return sleeps


@pytest.fixture
def loguru_records():
    """收集模块 logger（loguru——caplog 捕不到）的 WARNING+ 记录。"""
    records = []
    sink_id = niu_vision_server.logger.add(lambda m: records.append(str(m)), level="WARNING")
    yield records
    niu_vision_server.logger.remove(sink_id)


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
        _install_fake_llm_config(monkeypatch, MAIN_CFG_WITH_VISION)
        # vision 段故意配了 model——若实现错误地落到 vision 段，model 断言必红
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_user_config(tmp_path, vision_model="vision-model"))
        png = _png_file(tmp_path)
        FakeLiteLLMSession.response = _resp("OK")

        niu_vision_server.analyze_image(str(png), "这张图里有什么")

        session = FakeLiteLLMSession.instances[0]
        assert session.cfg["model"] == "main-model"

    def test_main_without_vision_falls_back_to_vision_section(self, monkeypatch, tmp_path):
        """主模型无视觉 + 原始 JSON vision_llm.model 非空 → 链由该段构建（plan R-2 定案：
        自读原始 user-config.json，不走 get_llm_config）；空键继承主 llm 段。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_NO_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_user_config(tmp_path, vision_model="vision-model"))
        png = _png_file(tmp_path)
        FakeLiteLLMSession.response = _resp("OK")

        niu_vision_server.analyze_image(str(png), "这张图里有什么")

        session = FakeLiteLLMSession.instances[0]
        assert session.cfg["model"] == "vision-model"
        # 链节点来自原始 JSON + 主段继承（而非 get_llm_config 返回的快照）
        assert session.cfg["apikey"] == "main-key"

    def test_neither_main_nor_section_returns_error_with_guidance(self, monkeypatch, tmp_path):
        """主模型无视觉 + vision_llm.model 空 → 明确错误（含配置指引），不发起调用。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_NO_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_user_config(tmp_path, vision_model=""))
        png = _png_file(tmp_path)

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert "识图不可用" in result
        assert "vision_llm" in result  # 配置指引
        assert FakeLiteLLMSession.instances == []
        # 段判空走原始 user-config.json（不得用 get_llm_config 判空）

    def test_user_config_read_failure_degrades_to_error(self, monkeypatch, tmp_path):
        """user-config.json 读失败（主模型又无视觉）→ 错误串，不抛异常。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_NO_VISION)
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
        """3c：api_type == 配置 type（非恒 openai）——键映射照 llm_proxy.py:354-368 先例。
        链节点字段来自原始 user-config.json（plan R-2）：显式键直接写进 vision_llm 段。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_NO_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                            _write_user_config(tmp_path, vision={"model": "vision-model",
                                                                "type": "anthropic",
                                                                "apiKey": "vision-key"}))
        png = _png_file(tmp_path)
        FakeLiteLLMSession.response = _resp("OK")

        niu_vision_server.analyze_image(str(png), "这张图里有什么")

        cfg = FakeLiteLLMSession.instances[0].cfg
        assert cfg["api_type"] == "anthropic"  # 段的 type，而非默认 openai
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
        """5（plan §5 用例 18 改写）：工具内不自起 stop 等待包装（外层 agent_loop run_interruptibly
        统一提供）——这是行为测试覆盖不到的唯一架构边界断言（工具内自包时用例 13/14 仍会绿）。
        is_stop_requested 的源码字面禁令已删除（契约已变：新实现在调用间隙查全局停止状态，
        行为由用例 13/14 锁定；不加正向源码锁——脆弱实现钉，R4-B）。"""
        src = (Path(__file__).resolve().parent.parent / "mcp-servers" / "vision-server"
               / "src" / "niu_vision_server" / "__init__.py").read_text(encoding="utf-8")
        assert "run_interruptibly" not in src

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
        """stream_error + 429 文本 → retryable：带退避重试 3 次（time.sleep 打桩，勿真等）后单模型失败文案。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_WITH_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_user_config(tmp_path))
        png = _png_file(tmp_path)
        sleeps = _stub_time(monkeypatch)
        FakeLiteLLMSession.response = _resp(stream_error=True, error_msg="HTTP 429 rate limited")

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert "识图失败" in result and "429" in result
        assert "无备用模型可降级" in result  # 单模型链：重试耗尽后无备用
        assert len(FakeLiteLLMSession.instances) == 4  # 首调 + 3 次重试
        assert sleeps == [2, 5, 10]

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


# ============== plan §5 用例 1：链构建（_vision_chain） ==============

class TestChainBuilding:
    """数组顺序 / 单对象回退 / 空数组 / 非数组 / 空 model 节 / 残留组合告警 / 主模型入链首。"""

    def _chain(self, monkeypatch, tmp_path, main_cfg, vision):
        _install_fake_llm_config(monkeypatch, main_cfg)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_user_config(tmp_path, vision=vision))
        return niu_vision_server._vision_chain()

    def test_models_array_order_preserved(self, monkeypatch, tmp_path):
        """models 数组 → 链序与数组一致（显式断言）；各节独立继承主段空键。"""
        chain, skipped = self._chain(monkeypatch, tmp_path, MAIN_CFG_NO_VISION,
                                     {"models": [{"model": "glm-a", "apiKey": "ka"},
                                                 {"model": "qwen-b"}]})
        assert [c["model"] for c in chain] == ["glm-a", "qwen-b"]  # 显式数组顺序
        assert skipped == 0
        assert chain[0]["apikey"] == "ka"        # 自带键不被继承覆盖
        assert chain[1]["apikey"] == "main-key"  # 空键继承主 llm 段

    def test_single_object_fallback(self, monkeypatch, tmp_path):
        """无 models 键 → 回退单对象 vision_llm.model（链长 1，存量安装零迁移）。"""
        chain, skipped = self._chain(monkeypatch, tmp_path, MAIN_CFG_NO_VISION, {"model": "legacy"})
        assert [c["model"] for c in chain] == ["legacy"]
        assert skipped == 0

    def test_empty_array_without_legacy_yields_no_chain(self, monkeypatch, tmp_path):
        """models=[] 且无旧 model 键 → 空链（按未配置），无告警。"""
        chain, skipped = self._chain(monkeypatch, tmp_path, MAIN_CFG_NO_VISION, {"models": []})
        assert chain == [] and skipped == 0

    def test_non_array_models_warns_and_falls_back(self, monkeypatch, tmp_path, loguru_records):
        """models 非数组 → 告警 + 忽略，回退单对象（不抛异常）。"""
        chain, skipped = self._chain(monkeypatch, tmp_path, MAIN_CFG_NO_VISION,
                                     {"models": "glm-x", "model": "legacy"})
        assert [c["model"] for c in chain] == ["legacy"]
        assert any("非数组" in r for r in loguru_records)

    def test_empty_model_node_skipped_with_warning(self, monkeypatch, tmp_path, loguru_records):
        """含空 model 节 → 跳过 + 告警（不继承主 llm model）；有效节保留。"""
        chain, skipped = self._chain(monkeypatch, tmp_path, MAIN_CFG_NO_VISION,
                                     {"models": [{"model": ""}, {"model": "ok"}]})
        assert [c["model"] for c in chain] == ["ok"]
        assert skipped == 1
        assert any("model 为空" in r for r in loguru_records)

    def test_residual_combo_warns_and_falls_back(self, monkeypatch, tmp_path, loguru_records):
        """残留组合（models=[] + 旧 model 键仍在）→ 回退单对象 + 告警一次（不阻断）。"""
        chain, skipped = self._chain(monkeypatch, tmp_path, MAIN_CFG_NO_VISION,
                                     {"models": [], "model": "legacy"})
        assert [c["model"] for c in chain] == ["legacy"]
        assert any("models 为空数组但旧单对象 model 键仍在" in r for r in loguru_records)

    def test_main_with_vision_is_chain_head(self, monkeypatch, tmp_path):
        """主模型有视觉 → 链首 = 主模型（C-1），后接 models 数组。"""
        chain, skipped = self._chain(monkeypatch, tmp_path, MAIN_CFG_WITH_VISION,
                                     {"models": [{"model": "glm-a"}]})
        assert [c["model"] for c in chain] == ["main-model", "glm-a"]
        assert skipped == 0


# ============== plan §5 用例 2-10、16、17、20：降级行为 ==============

class TestFallbackBehavior:
    """链首成功无注记 / 重试后恢复 / fatal 直降 / 裸异常通道 / 重试耗尽 / 多跳独立配额 /
    主模型入链 / 单模型文案 / 全链汇总 / 跳过节注记 / 零回归 / 未知不重试。"""

    def test_first_success_no_note(self, monkeypatch, tmp_path):
        """用例 2：链首一次成功 → 返回原始答案，无任何注记（后续模型不被调用）。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_NO_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                            _write_user_config(tmp_path, vision=_models_section("m1", "m2")))
        png = _png_file(tmp_path)
        _stub_time(monkeypatch)
        FakeLiteLLMSession.script.append(_resp("答案A"))

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert result == "答案A"  # 零附加
        assert len(FakeLiteLLMSession.instances) == 1

    def test_retryable_then_success_recovered_note(self, monkeypatch, tmp_path):
        """用例 3：链首 retryable → 重试后成功：含「重试后恢复」，不含「已自动降级」（D-E 新行 / C-2）。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_WITH_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_user_config(tmp_path))
        png = _png_file(tmp_path)
        sleeps = _stub_time(monkeypatch)
        FakeLiteLLMSession.script.extend([
            _resp(stream_error=True, error_msg="HTTP 429 rate limited"),  # 文本兜底 → retryable
            _resp("答案A"),
        ])

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert result == "答案A\n\n（注：首模型曾报错（HTTP 429 rate limited），重试后恢复）"
        assert "已自动降级" not in result
        assert len(FakeLiteLLMSession.instances) == 2  # 调用计数 = 2
        assert sleeps == [2]

    def test_fatal_no_retry_direct_degrade(self, monkeypatch, tmp_path):
        """用例 4：链首 fatal（error_type='fatal'）→ 不重试，直接降级到第二个。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_NO_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                            _write_user_config(tmp_path, vision=_models_section("m1", "m2")))
        png = _png_file(tmp_path)
        sleeps = _stub_time(monkeypatch)
        FakeLiteLLMSession.script.extend([_fatal("Authentication failed: invalid key"), _resp("答案A")])

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert result == "答案A\n\n（注：首模型不可用（Authentication failed: invalid key），已自动降级到 m2）"
        assert len(FakeLiteLLMSession.instances) == 2  # 无重试（若重试计数 > 2）
        assert sleeps == []

    def test_bare_exception_channel_degrades(self, monkeypatch, tmp_path):
        """用例 5：裸异常通道——首模型建连阶段抛 AuthenticationError（适配层 re-raise 路径）→
        except 捕获 → 不重试 → 降级（锁 R1-A P1-2）。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_NO_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                            _write_user_config(tmp_path, vision=_models_section("m1", "m2")))
        png = _png_file(tmp_path)
        sleeps = _stub_time(monkeypatch)
        FakeLiteLLMSession.script.extend([AuthenticationError("invalid api key"), _resp("答案A")])

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")  # 不抛异常

        assert "已自动降级到 m2" in result
        assert "调用异常（AuthenticationError）" in result  # reason 来自通道 A
        assert len(FakeLiteLLMSession.instances) == 2
        assert sleeps == []

    def test_retry_exhausted_then_degrade(self, monkeypatch, tmp_path):
        """用例 6：链首 retryable 重试 3 次耗尽 → 降级到第二个并成功。
        首模型调用次数 = 首次 + 3 次重试 = 4（plan「call count=4」指首模型），加第二个一次成功共 5。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_NO_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                            _write_user_config(tmp_path, vision=_models_section("m1", "m2")))
        png = _png_file(tmp_path)
        sleeps = _stub_time(monkeypatch)
        FakeLiteLLMSession.script.extend([_r429()] * 4 + [_resp("答案A")])

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert "已自动降级到 m2" in result
        inst = FakeLiteLLMSession.instances
        assert len(inst) == 5
        assert [i.cfg["model"] for i in inst] == ["m1", "m1", "m1", "m1", "m2"]
        assert sleeps == [2, 5, 10]

    def test_multi_hop_degrade_independent_quota(self, monkeypatch, tmp_path):
        """用例 7：多跳降级——首模型耗尽自己的重试配额（首次+3 重试均 429）、第二个模型
        同样拿满 3 次退避后失败、第三个成功。注记只提首模型原因；第二个的重试配额
        独立（切换后重置）——若实现为链级全局累计配额，m2 至多再得 2 次重试、第 4 次
        调用直接降级，model 序列与 sleeps 两条断言必红。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_NO_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                            _write_user_config(tmp_path, vision=_models_section("m1", "m2", "m3")))
        png = _png_file(tmp_path)
        sleeps = _stub_time(monkeypatch)
        FakeLiteLLMSession.script.extend(
            [_r429()] * 4
            + [_resp(stream_error=True, error_type="retry_exhausted",
                     error_msg="m2 rate limit")] * 4
            + [_resp("答案A")])

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert result == ("答案A\n\n（注：首模型不可用（Error code: 429 - rate limit exceeded），"
                          "已自动降级到 m3）")
        assert "m2 rate limit" not in result  # 最终成功；注记只提首模型原因，不得带第二模型的失败原因
        inst = FakeLiteLLMSession.instances
        assert [i.cfg["model"] for i in inst] == ["m1"] * 4 + ["m2"] * 4 + ["m3"]
        assert sleeps == [2, 5, 10, 2, 5, 10]  # 两个模型各拿满 3 次退避：配额每模型独立

    def test_main_model_in_chain_degrades(self, monkeypatch, tmp_path):
        """用例 8：主模型入链（C-1）——主模型有视觉但失败 → 降级到 models[0]。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_WITH_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                            _write_user_config(tmp_path, vision=_models_section("glm-a")))
        png = _png_file(tmp_path)
        _stub_time(monkeypatch)
        FakeLiteLLMSession.script.extend([_fatal("boom-main"), _resp("答案A")])

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert FakeLiteLLMSession.instances[0].cfg["model"] == "main-model"  # 链首 = 主模型
        assert result == "答案A\n\n（注：首模型不可用（boom-main），已自动降级到 glm-a）"
        assert len(FakeLiteLLMSession.instances) == 2

    def test_single_model_unknown_failure_wording(self, monkeypatch, tmp_path):
        """用例 9：主模型有视觉且未配 models → unknown 失败走单模型文案「无备用模型可降级」。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_WITH_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_user_config(tmp_path))
        png = _png_file(tmp_path)
        _stub_time(monkeypatch)
        FakeLiteLLMSession.script.append(_resp(stream_error=True, error_msg="mystery failure"))

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert result == "识图失败：main-model 不可用：mystery failure（无备用模型可降级）"
        assert len(FakeLiteLLMSession.instances) == 1

    def test_full_chain_failure_summary(self, monkeypatch, tmp_path):
        """用例 10：全链失败 → 汇总文案含每个模型名与原因（N=3）。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_NO_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                            _write_user_config(tmp_path, vision=_models_section("m1", "m2", "m3")))
        png = _png_file(tmp_path)
        _stub_time(monkeypatch)
        FakeLiteLLMSession.script.extend([_fatal(f"boom-{i}") for i in (1, 2, 3)])

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert result == ("识图失败：首模型不可用（m1：boom-1），已自动降级尝试 3 个模型均失败："
                          "①m1：boom-1；②m2：boom-2；③m3：boom-3")
        assert len(FakeLiteLLMSession.instances) == 3

    def test_full_chain_failure_over_three_truncates(self, monkeypatch, tmp_path):
        """用例 10：>3 个模型 → 只列前 3 + 「等 N 个」收尾（第四个原因不列出）。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_NO_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                            _write_user_config(tmp_path, vision=_models_section("m1", "m2", "m3", "m4")))
        png = _png_file(tmp_path)
        _stub_time(monkeypatch)
        FakeLiteLLMSession.script.extend([_fatal(f"boom-{i}") for i in (1, 2, 3, 4)])

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert "等 4 个" in result
        assert "boom-4" not in result  # 第四个不列出
        assert len(FakeLiteLLMSession.instances) == 4

    def test_skipped_node_note_appended(self, monkeypatch, tmp_path):
        """用例 16：无效节被跳过 → 失败文案追加「（另有 K 个配置无效被跳过）」。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_NO_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                            _write_user_config(tmp_path, vision={"models": [{"model": ""}, {"model": "ok-m"}]}))
        png = _png_file(tmp_path)
        _stub_time(monkeypatch)
        FakeLiteLLMSession.script.append(_fatal("boom"))

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert result == "识图失败：ok-m 不可用：boom（无备用模型可降级）（另有 1 个配置无效被跳过）"

    def test_zero_regression_single_object_success(self, monkeypatch, tmp_path):
        """用例 17：零回归——单对象 vision_llm.model 形态（主模型无视觉）成功路径与工程前一致。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_NO_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_user_config(tmp_path, vision_model="legacy"))
        png = _png_file(tmp_path)
        _stub_time(monkeypatch)
        FakeLiteLLMSession.script.append(_resp("答案A"))

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert result == "答案A"  # 零附加（工程前行为：直接返回答案）
        assert FakeLiteLLMSession.instances[0].cfg["model"] == "legacy"
        assert len(FakeLiteLLMSession.instances) == 1

    def test_unknown_empty_content_no_retry(self, monkeypatch, tmp_path):
        """用例 20：未知类（空内容非 length）→ 不重试，直接降级。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_NO_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                            _write_user_config(tmp_path, vision=_models_section("m1", "m2")))
        png = _png_file(tmp_path)
        sleeps = _stub_time(monkeypatch)
        FakeLiteLLMSession.script.extend([_resp(content="", finish_reason="stop"), _resp("答案A")])

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert "已自动降级到 m2" in result
        assert len(FakeLiteLLMSession.instances) == 2  # 首模型只调一次（无重试）
        assert sleeps == []

    def test_unknown_unclassified_exception_no_retry(self, monkeypatch, tmp_path):
        """用例 20：未归类异常（不在两表、文本无提示）→ unknown，不重试，直接降级。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_NO_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                            _write_user_config(tmp_path, vision=_models_section("m1", "m2")))
        png = _png_file(tmp_path)
        sleeps = _stub_time(monkeypatch)
        FakeLiteLLMSession.script.extend([MysteryWeirdError("total nonsense"), _resp("答案A")])

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert "已自动降级到 m2" in result
        assert len(FakeLiteLLMSession.instances) == 2
        assert sleeps == []


# ============== plan §5 用例 12：重试节奏（退避 / retry-after 覆盖） ==============

class TestRetryTiming:
    def test_backoff_delays_2_5_10(self, monkeypatch, tmp_path):
        """用例 12：retryable 重试 3 次耗尽 → time.sleep 调用参数恰为 2/5/10（打桩，勿真等）。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_WITH_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_user_config(tmp_path))
        png = _png_file(tmp_path)
        sleeps = _stub_time(monkeypatch)
        FakeLiteLLMSession.script.extend(
            [_resp(stream_error=True, error_msg="HTTP 429 rate limited")] * 4)

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert sleeps == [2, 5, 10]
        assert len(FakeLiteLLMSession.instances) == 4
        assert "识图失败" in result  # 单模型：耗尽后单模型失败文案

    def test_retry_after_overrides_delay(self, monkeypatch, tmp_path):
        """用例 12：错误文本含「retry after 7」→ 该次等待覆盖为 7s（上限 15s），后续回退避序列。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_WITH_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_user_config(tmp_path))
        png = _png_file(tmp_path)
        sleeps = _stub_time(monkeypatch)
        FakeLiteLLMSession.script.extend([
            _resp(stream_error=True, error_msg="HTTP 429 rate limited, retry after 7 seconds"),
            _resp(stream_error=True, error_msg="HTTP 429 rate limited"),
            _resp(stream_error=True, error_msg="HTTP 429 rate limited"),
            _resp(stream_error=True, error_msg="HTTP 429 rate limited"),
        ])

        niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert sleeps == [7, 5, 10]


# ============== plan §5 用例 13/14：stop 三条路径 ==============

class TestStopPaths:
    def test_error_type_stopped_no_retry_no_degrade(self, monkeypatch, tmp_path):
        """用例 13：重试间隙 stop（error_type='stopped'）→ 不重试、不降级，返回「识图已停止」。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_NO_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                            _write_user_config(tmp_path, vision=_models_section("m1", "m2")))
        png = _png_file(tmp_path)
        _stub_time(monkeypatch)
        FakeLiteLLMSession.script.append(
            _resp(stream_error=True, error_type="stopped", error_msg="stop requested"))

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert result == "识图已停止"
        assert len(FakeLiteLLMSession.instances) == 1  # 不重试、不降级

    def _setup_stop_during_call(self, monkeypatch):
        """用例 14：调用期间置位 stop——在 fake chat() 的 gen() yield 之后（不能在调用前置位，
        否则循环顶首查即命中、调用计数 = 0）；patch 目标为函数级 import 解析点
        agent.generic.litellm_adapter.is_stop_requested。"""
        flag = {"v": False}
        monkeypatch.setattr("agent.generic.litellm_adapter.is_stop_requested", lambda: flag["v"])
        FakeLiteLLMSession.on_chunk = lambda: flag.__setitem__("v", True)

    def test_stop_during_call_returns_stopped(self, monkeypatch, tmp_path):
        """用例 14：主路径调用期间置位 stop → 「每次调用返回后」检查点命中 → 不降级。"""
        self._setup_stop_during_call(monkeypatch)
        _install_fake_llm_config(monkeypatch, MAIN_CFG_NO_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                            _write_user_config(tmp_path, vision=_models_section("m1", "m2")))
        png = _png_file(tmp_path)
        _stub_time(monkeypatch)
        FakeLiteLLMSession.script.extend([_resp("答案A"), _resp("答案B")])

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert result == "识图已停止"  # 即使答案正常，stop 优先
        assert len(FakeLiteLLMSession.instances) == 1  # 不降级

    def test_stop_during_call_single_model_variant(self, monkeypatch, tmp_path):
        """用例 14 变体：单模型链（存量默认形态）——锁「每次调用返回后」检查点
        （若只查循环顶/重试前，会误报「无备用模型可降级」而非「识图已停止」）。"""
        self._setup_stop_during_call(monkeypatch)
        _install_fake_llm_config(monkeypatch, MAIN_CFG_WITH_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_user_config(tmp_path))
        png = _png_file(tmp_path)
        _stub_time(monkeypatch)
        FakeLiteLLMSession.script.append(_resp("答案A"))

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert result == "识图已停止"
        assert "无备用模型可降级" not in result


# ============== plan §5 用例 15/21：总预算中断（time.monotonic 打桩） ==============

class TestChainBudget:
    def test_budget_interrupt_multi_model(self, monkeypatch, tmp_path):
        """用例 15：累计耗时超 600s → 停止降级，返回「预算中断」文案（M=2，含「尚有 K 个模型未尝试」），
        非全链失败模板。monotonic 每次调用前进 130s：前两个模型各失败一次（累计 <600s），
        第三个模型循环顶检查时累计 650s → 中断。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_NO_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                            _write_user_config(tmp_path, vision=_models_section("m1", "m2", "m3")))
        png = _png_file(tmp_path)
        _stub_time(monkeypatch, step=130)
        # step>0 使预检 deadline 恒负 → 显式打桩恒通过（v0.5 定案；预检路径由用例 9/10/11 覆盖）
        monkeypatch.setattr(niu_vision_server, "_probe_reachable", lambda api_base: (True, ""))
        FakeLiteLLMSession.script.extend([_fatal(f"boom-{i}") for i in (1, 2, 3)])

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert result == ("识图失败：m1 不可用（boom-1），已自动降级尝试 2 个模型均失败，"
                          "因累计耗时超 600s 停止继续降级（尚有 1 个模型未尝试）")
        assert "首模型不可用" not in result  # 与全链失败模板区分
        assert len(FakeLiteLLMSession.instances) == 2  # 第三个模型未被调用

    def test_budget_interrupt_m1_special_wording(self, monkeypatch, tmp_path):
        """用例 21：链首即预算耗尽（未发生降级）→ M=1 特例文案，不含「已自动降级」。
        monotonic 每次前进 250s：链首次调用失败后，第二个模型循环顶检查累计 750s → 中断。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_NO_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                            _write_user_config(tmp_path, vision=_models_section("m1", "m2")))
        png = _png_file(tmp_path)
        _stub_time(monkeypatch, step=250)
        # step>0 使预检 deadline 恒负 → 显式打桩恒通过（v0.5 定案；预检路径由用例 9/10/11 覆盖）
        monkeypatch.setattr(niu_vision_server, "_probe_reachable", lambda api_base: (True, ""))
        FakeLiteLLMSession.script.extend([_fatal("boom-1"), _resp("答案A")])

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert result == "识图失败：m1 不可用（boom-1），因累计耗时超 600s 停止继续降级（尚有 1 个模型未尝试）"
        assert "已自动降级" not in result
        assert len(FakeLiteLLMSession.instances) == 1

    def test_budget_hit_during_retry_wait_counts_pending_section(self, monkeypatch, tmp_path):
        """用例 15b：首模型重试等待期命中预算——该节已发起过调用但 failures 尚未写入
        （_budget_msg 的补计分支），文案必须含 m1 名与原因；若落到「理论不可达」兜底串
        会丢失模型名与原因（回归时现测全绿）。monotonic 每次前进 250s：首次调用失败 →
        第 2 次退避 sleep(2) 已记录 → 重试前检查累计 750s ≥ 600s → 中断。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_NO_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                            _write_user_config(tmp_path, vision=_models_section("m1", "m2")))
        png = _png_file(tmp_path)
        sleeps = _stub_time(monkeypatch, step=250)
        # step>0 使预检 deadline 恒负 → 显式打桩恒通过（v0.5 定案；预检路径由用例 9/10/11 覆盖）
        monkeypatch.setattr(niu_vision_server, "_probe_reachable", lambda api_base: (True, ""))
        FakeLiteLLMSession.script.extend([_r429(), _resp("答案A")])

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert result == ("识图失败：m1 不可用（Error code: 429 - rate limit exceeded），"
                          "因累计耗时超 600s 停止继续降级（尚有 1 个模型未尝试）")
        assert not result.startswith("识图失败：因累计耗时超")  # 非兜底串形态（兜底会丢模型名与原因）
        assert len(FakeLiteLLMSession.instances) == 1  # 仅首次调用发起，第 2 次重试未发出
        assert sleeps == [2]  # 已进入第 2 次退避等待期后命中预算检查


# ============== plan §5 用例 19：文案格式锁（不含 ![） ==============

class TestWordingFormat:
    def test_no_markdown_marker_in_any_wording(self, monkeypatch, tmp_path):
        """全部失败/注记文案不含 `![`（与既有 :318 锁同语义——纯文本，防模型误当图片引用）。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_NO_VISION)
        png = _png_file(tmp_path)
        _stub_time(monkeypatch)  # step=0：禁用预算、记录 sleep

        scenarios = [
            # (vision 段, script, expected 子串)——覆盖 D-E：降级成功 / 重试后恢复 / 单模型失败 /
            # 全链失败 / stopped；expected 子串使「走了哪条分支」本身成为断言（防任一场景
            # 提前短路也照样绿的空跑通过）
            (_models_section("m1", "m2"), [_fatal("boom-1"), _resp("答案A")], "已自动降级到 m2"),
            (_models_section("m1"), [_resp(stream_error=True, error_msg="HTTP 429 rate limited"),
                                     _resp("答案A")], "重试后恢复"),
            ({"model": "legacy"}, [_resp(stream_error=True, error_msg="mystery failure")],
             "无备用模型可降级"),
            (_models_section("m1", "m2"), [_fatal("boom-1"), _fatal("boom-2")],
             "已自动降级尝试 2 个模型均失败"),
            (_models_section("m1"), [_resp(stream_error=True, error_type="stopped",
                                           error_msg="stop requested")], "识图已停止"),
        ]
        for vision, script, expected in scenarios:
            FakeLiteLLMSession.instances.clear()
            FakeLiteLLMSession.script.extend(script)
            monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_user_config(tmp_path, vision=vision))
            result = niu_vision_server.analyze_image(str(png), "这张图里有什么")
            assert expected in result, f"未走预期分支（期望含 {expected!r}）: {result!r}"
            assert "![" not in result, f"文案含图片标记: {result!r}"

        # 预算中断文案（需 step>0 的时间 stub；step>0 使预检 deadline 恒负 → 显式打桩恒通过）
        _stub_time(monkeypatch, step=130)
        monkeypatch.setattr(niu_vision_server, "_probe_reachable", lambda api_base: (True, ""))
        FakeLiteLLMSession.instances.clear()
        FakeLiteLLMSession.script.extend([_fatal("boom-1"), _fatal("boom-2")])
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                            _write_user_config(tmp_path, vision=_models_section("m1", "m2", "m3")))
        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")
        assert "因累计耗时超 600s 停止继续降级" in result  # 走了预算中断分支
        assert "![" not in result


# ============== plan 2026-09-12-vision-retry-policy-fix §5 用例 1-7：F-1 重试收窄 ==============

class Timeout(Exception):
    """类名与 litellm 超时异常一致——D-D 通道 A 按 type(e).__name__ 查表（F-2：不重试）。"""


class APIConnectionError(Exception):
    """类名与 litellm 连接异常一致——F-2：连接失败不重试。"""


class RateLimitError(Exception):
    """类名与 litellm 429 限流异常一致——F-1：服务端说忙 → 可重试。"""


class ServiceUnavailableError(Exception):
    """类名与 litellm 503 暂不可用异常一致——F-1：加载中/暂不可用 → 可重试（R-5 翻转锁）。"""


class BudgetExceededError(Exception):
    """类名与 litellm 配额/欠费异常一致——fatal 表，不重试（回归锁）。"""


class TestRetryPolicyF1:
    """2026-09-12 plan §5 用例 1-7：只有服务端明确说忙/暂不可用才重试，其余一律直接降级（F-1/F-2）。"""

    def test_timeout_no_retry_direct_degrade(self, monkeypatch, tmp_path):
        """用例 1（用户本次场景）：Timeout 裸异常 → 不重试（调用计数=1）→ 直接降级。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_NO_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                            _write_user_config(tmp_path, vision=_models_section("m1", "m2")))
        png = _png_file(tmp_path)
        sleeps = _stub_time(monkeypatch)
        FakeLiteLLMSession.script.extend([Timeout("Request timed out"), _resp("答案A")])

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert "已自动降级到 m2" in result
        assert len(FakeLiteLLMSession.instances) == 2  # 首模型只调一次（无重试）
        assert sleeps == []

    def test_api_connection_error_no_retry(self, monkeypatch, tmp_path):
        """用例 2：APIConnectionError（连接失败/不可达）→ 不重试，直接降级。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_NO_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                            _write_user_config(tmp_path, vision=_models_section("m1", "m2")))
        png = _png_file(tmp_path)
        sleeps = _stub_time(monkeypatch)
        FakeLiteLLMSession.script.extend([APIConnectionError("connect failed"), _resp("答案A")])

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert "已自动降级到 m2" in result
        assert len(FakeLiteLLMSession.instances) == 2
        assert sleeps == []

    def test_rate_limit_error_retries_3_times(self, monkeypatch, tmp_path):
        """用例 3：RateLimitError（429 忙）→ 重试 3 次（计数=4），退避 [2,5,10]。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_WITH_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_user_config(tmp_path))
        png = _png_file(tmp_path)
        sleeps = _stub_time(monkeypatch)
        FakeLiteLLMSession.script.extend([RateLimitError("Error code: 429 - rate limit exceeded")] * 4)

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert len(FakeLiteLLMSession.instances) == 4
        assert sleeps == [2, 5, 10]
        assert "识图失败" in result  # 单模型：耗尽后单模型失败文案

    def test_service_unavailable_error_retries_3_times(self, monkeypatch, tmp_path):
        """用例 4：ServiceUnavailableError（503 加载中）→ 重试 3 次（计数=4）——R-5 翻转锁。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_WITH_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_user_config(tmp_path))
        png = _png_file(tmp_path)
        sleeps = _stub_time(monkeypatch)
        FakeLiteLLMSession.script.extend(
            [ServiceUnavailableError("503 service unavailable: loading model")] * 4)

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert len(FakeLiteLLMSession.instances) == 4
        assert sleeps == [2, 5, 10]
        assert "识图失败" in result

    def test_unclassified_with_retry_after_text_retries(self, monkeypatch, tmp_path):
        """用例 5：未归类异常但文本含 retry after 7 → 文本兜底 retryable，首次等待 = 7s。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_WITH_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_user_config(tmp_path))
        png = _png_file(tmp_path)
        sleeps = _stub_time(monkeypatch)
        # 首错带 retry after 7（覆盖首次等待）；后续错误只含忙信号无退避秒数 → 回退避序列
        FakeLiteLLMSession.script.extend(
            [MysteryWeirdError("server busy, retry after 7 seconds")]
            + [MysteryWeirdError("HTTP 429 rate limited")] * 3)

        niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert len(FakeLiteLLMSession.instances) == 4
        assert sleeps == [7, 5, 10]  # 首次等待被 retry after 覆盖为 7s

    def test_unclassified_without_busy_text_no_retry(self, monkeypatch, tmp_path):
        """用例 6：未归类异常且文本无忙信号 → 不重试，直接降级。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_NO_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                            _write_user_config(tmp_path, vision=_models_section("m1", "m2")))
        png = _png_file(tmp_path)
        sleeps = _stub_time(monkeypatch)
        FakeLiteLLMSession.script.extend([MysteryWeirdError("total nonsense"), _resp("答案A")])

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert "已自动降级到 m2" in result
        assert len(FakeLiteLLMSession.instances) == 2
        assert sleeps == []

    def test_auth_and_quota_errors_no_retry(self, monkeypatch, tmp_path):
        """用例 7：认证/配额（AuthenticationError / BudgetExceededError）→ 不重试（回归锁）。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_NO_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                            _write_user_config(tmp_path, vision=_models_section("m1", "m2")))
        png = _png_file(tmp_path)
        sleeps = _stub_time(monkeypatch)

        FakeLiteLLMSession.script.extend([AuthenticationError("401 invalid api key"), _resp("答案A")])
        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")
        assert "已自动降级到 m2" in result
        assert len(FakeLiteLLMSession.instances) == 2

        FakeLiteLLMSession.script.clear()
        FakeLiteLLMSession.instances.clear()
        FakeLiteLLMSession.script.extend([BudgetExceededError("402 billing: insufficient balance"), _resp("答案A")])
        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")
        assert "已自动降级到 m2" in result
        assert len(FakeLiteLLMSession.instances) == 2  # m1 仍只调一次（无重试）
        assert sleeps == []


# ============== plan 2026-09-12-vision-retry-policy-fix §5 用例 8-11：F-3 可达性预检 ==============

class TestReachabilityProbe:
    """预检失败零调用降级 / 成功路径 + close / 解析失败跳过 / 创建异常 fail-open / deadline 递减。"""

    def test_probe_unreachable_zero_calls_degrades(self, monkeypatch, tmp_path):
        """用例 8：预检 connect 失败（打桩 socket.socket，不打桩 _probe_reachable）→ 该模型零调用 →
        直接降级；文案含「不可达」。键名误写 apiBase 时本用例必红（预检恒跳过 → m1 会被调用）。"""

        probe_calls = {"n": 0}

        class _Unreachable(_FakeProbeSocket):
            def connect(self, sa):
                probe_calls["n"] += 1
                if probe_calls["n"] == 1:
                    raise OSError("connection refused")  # 仅首模型（m1）不可达；m2 预检通过

        monkeypatch.setattr(niu_vision_server.socket, "socket", _Unreachable)
        _install_fake_llm_config(monkeypatch, MAIN_CFG_NO_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                            _write_user_config(tmp_path, vision=_models_section("m1", "m2")))
        png = _png_file(tmp_path)
        sleeps = _stub_time(monkeypatch)
        FakeLiteLLMSession.script.append(_resp("答案A"))

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert "不可达" in result
        assert "已自动降级到 m2" in result
        inst = FakeLiteLLMSession.instances
        assert [i.cfg["model"] for i in inst] == ["m2"]  # m1 零调用
        assert sleeps == []

    def test_probe_success_normal_call_and_close(self, monkeypatch, tmp_path):
        """用例 9：预检成功 → 正常调用（计数=1），不改变成功路径；预检 socket 被显式 close()。"""
        _install_fake_llm_config(monkeypatch, MAIN_CFG_WITH_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_user_config(tmp_path))
        png = _png_file(tmp_path)
        FakeLiteLLMSession.script.append(_resp("答案A"))

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert result == "答案A"
        assert len(FakeLiteLLMSession.instances) == 1
        assert _FakeProbeSocket.instances, "预检未创建 socket（预检被跳过？）"
        assert all(s.closed for s in _FakeProbeSocket.instances)

    def test_probe_parse_failure_skips_precheck(self):
        """用例 10：apiBase 空串 / 无 scheme / 非法端口 / 未闭合 IPv6 → 跳过预检（fail-open 不阻断）。"""
        for bad in ("", "not-a-url", "http://h:70000/v1", "http://[::1"):
            assert niu_vision_server._probe_reachable(bad) == (True, ""), bad

    def test_probe_skipped_when_no_apibase_still_calls(self, monkeypatch, tmp_path):
        """用例 10（集成）：链节无 apiBase → 跳过预检（不创建 socket），仍发起真实调用。"""
        main_cfg = {**MAIN_CFG_NO_VISION, "apibase": ""}
        _install_fake_llm_config(monkeypatch, main_cfg)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                            _write_user_config(tmp_path, vision=_models_section("m1")))
        png = _png_file(tmp_path)
        FakeLiteLLMSession.script.append(_resp("答案A"))

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")

        assert result == "答案A"
        assert len(FakeLiteLLMSession.instances) == 1
        assert _FakeProbeSocket.instances == []  # 预检被跳过（未创建 socket）

    def test_probe_socket_creation_failure_fail_open(self, monkeypatch, tmp_path):
        """用例 10b：socket.socket 创建即抛 OSError（EMFILE 场景）→ fail-open (True, "")：
        ①不抛 UnboundLocalError ②不阻断 ③后续真实调用照常发生。"""

        def _boom(*args, **kwargs):
            raise OSError("Too many open files")

        monkeypatch.setattr(niu_vision_server.socket, "socket", _boom)

        assert niu_vision_server._probe_reachable("http://main/v1") == (True, "")  # ①②

        _install_fake_llm_config(monkeypatch, MAIN_CFG_NO_VISION)
        monkeypatch.setattr("niu_api.config.CONFIG_PATH",
                            _write_user_config(tmp_path, vision=_models_section("m1")))
        png = _png_file(tmp_path)
        FakeLiteLLMSession.script.append(_resp("答案A"))

        result = niu_vision_server.analyze_image(str(png), "这张图里有什么")  # ③

        assert result == "答案A"
        assert len(FakeLiteLLMSession.instances) == 1

    def test_probe_deadline_decrements_per_address(self, monkeypatch):
        """用例 11（时钟联动）：首个候选吃 4s 后失败 → 第二个拿剩余 ≤1s（真递减，非恒 5s）。"""
        clock = [0.0]
        stub = types.SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda s: None)
        monkeypatch.setattr(niu_vision_server, "time", stub)

        timeouts = []
        connects = {"n": 0}

        class _ProbeSock:
            def __init__(self, *a, **k):
                pass

            def settimeout(self, v):
                timeouts.append(v)

            def connect(self, sa):
                connects["n"] += 1
                if connects["n"] == 1:
                    clock[0] += 4.0  # 首个候选吃 4s 后失败
                    raise OSError("first candidate unreachable")

            def close(self):
                pass

        monkeypatch.setattr(niu_vision_server.socket, "socket", _ProbeSock)
        monkeypatch.setattr(
            niu_vision_server.socket, "getaddrinfo",
            lambda *a, **k: [(2, 1, 6, "", ("10.0.0.1", 80)), (2, 1, 6, "", ("10.0.0.2", 80))])

        ok, reason = niu_vision_server._probe_reachable("http://main/v1")

        assert (ok, reason) == (True, "")
        assert timeouts[0] == 5.0          # 首尝试拿满 5s
        assert timeouts[1] <= 1.0          # 第二次拿的是剩余时间（可区分「递减」与「恒 5s」）

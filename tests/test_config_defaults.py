"""首次启动标准缺省配置字段完整性测试（config-manager 兜底 = Python 侧缺省真相源）。

Why: 2026-07-27 首次启动 bug——缺省配置缺 thinking/reasoning_effort 等基础字段，
导致 probe 探测环境与运行时环境不一致（245s+ 重试预算耗尽 probe_failed），
且 probe_failed 仍写文件产生不完整配置。user-config.json 无模板文件设计（用户确认），
缺省由代码内联写出；本测试锁定 config-manager 兜底字段。

三处一致性的精确含义（第 1 轮审查澄清）：前端两处缺省（get-config 兜底 /
testAndSave 常量）与本处的**核心字段**一致（thinking / reasoning_effort /
temperature / context 四项 / logging）；targetThreshold、storage 内部结构为
Python 兜底特有的历史字段（向后兼容保留，JS 两处不含，已验证无害）。
"""
import json

import pytest


@pytest.fixture()
def fallback(tmp_path, monkeypatch) -> dict:
    """文件不存在时 load_user_config 的兜底返回。"""
    import niu_config_manager

    monkeypatch.setattr(niu_config_manager, "USER_CONFIG_PATH", tmp_path / "nonexistent.json")
    return niu_config_manager.load_user_config()


def test_fallback_llm_fields(fallback):
    llm = fallback["llm"]
    assert llm["apiKey"] == ""
    assert llm["apiBase"] == ""
    assert llm["model"] == ""
    assert llm["type"] == "openai"
    assert llm["reasoning_effort"] == ""
    # 主聊天模型开思考链（用户可见思考过程）
    assert llm["litellm_kwargs"]["thinking"] == {"type": "enabled"}
    # 主模型不需要温度字段（温度在提示词文档里，R14）
    assert "temperature" not in llm


def test_fallback_lightrag_llm_fields(fallback):
    lightrag = fallback["lightrag_llm"]
    assert lightrag["type"] == "openai"
    # 思维深度 High（用户确认的基础配置，R11）；历史错误值 "none"/"xhigh" 统一为 "high"
    assert lightrag["reasoning_effort"] == "high"
    # 知识图谱模型温度（与前端 testAndSave 现有缺省一致，R14）
    assert lightrag["temperature"] == 0.2
    # 关闭思考链返回（保证 JSON 输出干净）——探测环境=运行时环境的关键
    assert lightrag["litellm_kwargs"]["thinking"] == {"type": "disabled"}
    assert lightrag["litellm_kwargs"]["allowed_openai_params"] == []
    # probe 成功前缺省不含 response_format_mode（失败不写文件的语义保证，R4/R5）
    assert "response_format_mode" not in lightrag["litellm_kwargs"]


def test_fallback_context_fields(fallback):
    ctx = fallback["context"]
    assert ctx["contextWindowSize"] == 200000
    assert ctx["warningThreshold"] == 0.8
    assert ctx["compressTargetTokens"] == 60000
    # 缺省睡眠时间 5 分钟（R13：用户的 30 是个人优化，不进缺省）
    assert ctx["sleepTriggerMinutes"] == 5


def test_fallback_top_level_fields(fallback):
    assert fallback["firstRun"] is True
    assert "storage" in fallback
    # logging 字段结构补全，值用设计缺省（R13：用户的 true 是个人优化）
    assert fallback["logging"] == {"enabled": False, "level": "INFO"}

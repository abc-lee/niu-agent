"""验证 call_subagent 传 memory_context 时，子 Agent 每轮 LLM 调用前后更新 memory_context。

用真实 LLM 调一个简短任务（"直接回复 OK"），验证 memory_context.current_turn >= 1 且
last_llm_response 非空。
"""
import os

import pytest

from agent.subagent import call_subagent
from agent.subagent_memory import SubagentMemoryContext
from agent.subagent_supplement import SubagentSupplementQueue


@pytest.fixture
def llm_config():
    """读 config/user-config.json 的真实 LLM 配置。"""
    import json
    config_path = os.path.join(os.path.dirname(__file__), "..", "config", "user-config.json")
    with open(config_path) as f:
        cfg = json.load(f)
    llm = cfg.get("llm", {})
    return {
        "apikey": llm.get("apiKey", ""),
        "apibase": llm.get("apiBase", ""),
        "model": llm.get("model", ""),
        "type": llm.get("type", "openai"),
    }


def test_call_subagent_updates_memory_context(llm_config):
    """call_subagent 传 memory_context 时，子 Agent 跑完后 memory_context 有数据。"""
    if not llm_config["apikey"]:
        pytest.skip("LLM API key not configured")

    sq = SubagentSupplementQueue("test-mem-0001")
    mc = SubagentMemoryContext()

    result = call_subagent(
        agent_name="file-processor",
        task="直接回复 OK，不要调用任何工具",
        llm_config=llm_config,
        supplement_queue=sq,
        memory_context=mc,
    )

    assert result and len(result) > 0, "子 Agent 应有非空回复"
    # 不强断言 "OK" in result——LLM 可能调 read 等工具后回复，内容可能不含字面 "OK"
    snap = mc.snapshot()
    assert snap["current_turn"] >= 1
    assert snap["last_llm_response"] is not None
    assert len(snap["last_llm_response"]) > 0


def test_call_subagent_without_memory_context_unchanged(llm_config):
    """call_subagent 不传 memory_context 时，行为与阶段一一致（不报错）。"""
    if not llm_config["apikey"]:
        pytest.skip("LLM API key not configured")

    sq = SubagentSupplementQueue("test-nomem-0001")

    result = call_subagent(
        agent_name="file-processor",
        task="直接回复 OK，不要调用任何工具",
        llm_config=llm_config,
        supplement_queue=sq,
        # 不传 memory_context
    )

    assert result and len(result) > 0, "子 Agent 应有非空回复"

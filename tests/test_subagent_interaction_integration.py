"""阶段一集成测试：真实程序 + 真实 LLM。

按记忆 real-testing-only：禁止 mock 测试，必须真实起子 Agent 跑。
测试前必须清空数据库。

测试场景：
1. 主 Agent 同步调子 Agent，主写 @子名 补充，子 Agent 消费
2. 双击停止触发 /stop_all，子 Agent 终止
3. role=subagent_msg 消息不进主 Agent LLM 上下文

这些测试需真实启动 ./niu + 真实 LLM 调用，复杂度高，标记 skip。
手动测试在 Task 15 回归验证时做。
单元测试（test_subagent_supplement / test_subagent_registry / test_db_monitor 等）
已覆盖核心逻辑，集成测试补充端到端验证。
"""
import pytest


@pytest.mark.skip(reason="阶段一集成测试需手动执行：启动 ./niu，发消息触发子 Agent，观察日志验证")
def test_main_agent_supplement_to_subagent():
    """主 Agent 给同步调用的子 Agent 补充上下文。

    验证：
    1. 主 Agent 调 call_subagent 启动子 Agent
    2. 主 Agent 写 @子名 补充 到 db（通过回复解析）
    3. db 监测程序路由到子 Agent supplement queue
    4. 子 Agent 下一轮 LLM 调用前消费补充
    """
    # 手动测试步骤：
    # 1. 启动 ./niu
    # 2. 在 chat 窗口发消息让主 Agent 调子 Agent（如 file-processor）
    # 3. 在子 Agent 跑期间，主 Agent 回复含 @子名 补充内容
    # 4. 观察日志：db_monitor 路由日志 + 子 Agent 消费 supplement 日志
    # 5. 确认子 Agent 下一轮 LLM 请求含补充内容（次末位）
    pass


@pytest.mark.skip(reason="阶段一集成测试需手动执行")
def test_double_click_stop_all_subagents():
    """双击停止按钮触发 /stop_all。

    验证：
    1. 主 Agent 调多个子 Agent
    2. 调 POST /api/stop_all
    3. 所有子 Agent 收到 /stop 后终止
    """
    # 手动测试步骤：
    # 1. 启动 ./niu
    # 2. 发消息让主 Agent 调子 Agent
    # 3. 双击停止按钮
    # 4. 观察日志：request_stop_all_subagents + 子 Agent 收到 /stop + 终止
    # 5. 确认所有子 Agent 退出 + SubagentRegistry 清空
    pass


@pytest.mark.skip(reason="阶段一集成测试需手动执行")
def test_subagent_msg_not_in_llm_history():
    """role=subagent_msg 消息不进主 Agent LLM 上下文。

    验证：
    1. 写 @主Agent 消息到 db
    2. 主 Agent 下一轮调 LLM 时，LLM 请求不含 subagent_msg 内容（除非通过 supplement）
    """
    # 手动测试步骤：
    # 1. 启动 ./niu
    # 2. 触发子 Agent 问主 Agent（@主Agent 消息存 db）
    # 3. 主 Agent 下一轮调 LLM
    # 4. 检查 raw_http 日志：LLM 请求 messages 不含 role=subagent_msg 的历史消息
    # 5. 确认 supplement queue 含 @主Agent 消息（次末位插入）
    pass


@pytest.mark.skip(reason="阶段一集成测试需手动执行")
def test_at_message_parser_in_real_reply():
    """主 Agent 回复里的 @ 消息被后端解析存 subagent_msg role。

    验证：
    1. 主 Agent 回复含 @子名 内容
    2. persist_agent_reply 解析提取
    3. db 里 assistant 回复 strip 后（无 @ 消息）
    4. db 里 subagent_msg role 存了 @ 消息
    5. db_monitor 路由到子 Agent
    """
    # 手动测试步骤：
    # 1. 启动 ./niu
    # 2. 触发主 Agent 回复含 @子名 内容
    # 3. 查 db：assistant 回复无 @ 消息文本，subagent_msg role 有 @ 消息
    # 4. 观察日志：db_monitor 路由日志
    pass

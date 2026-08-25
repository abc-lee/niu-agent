"""子 Agent 守则清理后的措辞和位置验证。

验证三件事：
1. 补丁句"你不需要在输出里包含自己的标识符"已删除（Task A 的回归保护）
2. 新守则首句是 @niu-agent 提问语法（@niu-agent 整段传递——T2）
3. 新守则结尾是 @end 退出语法（任务完成退出 + 汇报内容写在 @end 前）
4. marker 与当前模板版本（v4）一致（强制走新模板）
5. 其他子 Agent（如 file-processor）仍被注入新守则（回归保护）
"""


def test_patch_sentence_removed():
    """补丁句"你不需要在输出里包含自己的标识符"已从守则删除"""
    from agent.subagent import _SUBAGENT_ASK_GUIDE_TEMPLATE

    assert "你不需要在输出里包含自己的标识符" not in _SUBAGENT_ASK_GUIDE_TEMPLATE
    assert "程序会自动在你的问题前加上唯一标识" not in _SUBAGENT_ASK_GUIDE_TEMPLATE


def test_guide_first_line_is_niu_agent_ask_rule():
    """新守则首句是 @niu-agent 提问语法——@niu-agent 整段传递

    首句强提醒：让子 Agent 第一眼看到提问方式（@niu-agent 整段传递）。
    利用 primacy effect（LLM 处理 system prompt 时首句 attention 权重最高）。
    「收到请回复」已删除（2026-08-15 用户拍板——需回复标志+30s 提醒已足够）。
    """
    from agent.subagent import _SUBAGENT_ASK_GUIDE_TEMPLATE

    # 去掉 marker 行、空行、markdown 标题行后，第一句实际内容是 @niu-agent 提问语法
    lines = [
        line for line in _SUBAGENT_ASK_GUIDE_TEMPLATE.splitlines()
        if line.strip()
        and not line.strip().startswith("<!--")
        and not line.strip().startswith("#")
    ]
    first_content_line = lines[0] if lines else ""
    # 首句是 @niu-agent 提问语法（主 Agent 通讯主通道）
    assert "@niu-agent" in first_content_line, f"首句应含'@niu-agent'，实际: {first_content_line}"
    # 「收到请回复」已删除（2026-08-15 用户拍板）——首句不应含 trailer 引导
    assert "收到请回复" not in first_content_line, f"首句不应含'收到请回复'，实际: {first_content_line}"
    assert "主 Agent" in first_content_line, f"首句应含'主 Agent'，实际: {first_content_line}"


def test_guide_last_line_is_end_exit_rule():
    """新守则结尾是 @end 退出语法（任务完成退出 + 汇报内容写在 @end 前）

    结尾总结：子 Agent 扫到结尾时再被提醒一次退出方式。
    利用 recency effect（结尾 attention 权重高）。
    """
    from agent.subagent import _SUBAGENT_ASK_GUIDE_TEMPLATE

    lines = [
        line for line in _SUBAGENT_ASK_GUIDE_TEMPLATE.splitlines()
        if line.strip()
        and not line.strip().startswith("<!--")
        and not line.strip().startswith("#")
    ]
    last_content_line = lines[-1] if lines else ""
    # 结尾是 @end 退出语法
    assert "@end" in last_content_line, f"结尾应含'@end'，实际: {last_content_line}"
    assert "任务完成" in last_content_line, f"结尾应含'任务完成'，实际: {last_content_line}"
    assert "退出" in last_content_line, f"结尾应含'退出'，实际: {last_content_line}"


def test_guide_marker_pinned_to_current_version():
    """marker 与当前模板版本（v4）一致，强制走新模板注入"""
    from agent.subagent import _SUBAGENT_ASK_GUIDE_MARKER, _SUBAGENT_ASK_GUIDE_TEMPLATE

    assert _SUBAGENT_ASK_GUIDE_MARKER == "<!-- NIU_SUBAGENT_GUIDE_v4 -->"
    assert "<!-- NIU_SUBAGENT_GUIDE_v4 -->" in _SUBAGENT_ASK_GUIDE_TEMPLATE
    # 旧 marker 不应出现在新模板里
    assert "<!-- NIU_SUBAGENT_GUIDE_v1 -->" not in _SUBAGENT_ASK_GUIDE_TEMPLATE


def test_other_subagents_still_injected_with_new_guide(tmp_path, monkeypatch):
    """其他子 Agent（如 my-agent）仍被注入新守则（回归保护）"""
    from agent import subagent

    user_dir = tmp_path / "user" / "agents"
    user_dir.mkdir(parents=True)
    (user_dir / "my-agent.md").write_text("---\ndescription: my agent\n---\nYou are my agent.")

    project_dir = tmp_path / "project" / "config" / "agents"
    project_dir.mkdir(parents=True)
    monkeypatch.setattr(subagent, "_PROJECT_AGENTS_DIR", str(project_dir))
    monkeypatch.setattr(subagent, "_USER_AGENTS_DIR", str(user_dir))

    static_system, _ = subagent.build_subagent_system_segments("my-agent")
    assert subagent._SUBAGENT_ASK_GUIDE_MARKER in static_system
    assert "@end" in static_system
    assert "@niu-agent" in static_system

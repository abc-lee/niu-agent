"""子 Agent 守则清理后的措辞和位置验证。

验证三件事：
1. 补丁句"你不需要在输出里包含自己的标识符"已删除（Task A 的回归保护）
2. 新守则首句是命令式强提醒"任务完成时必须用 @end"（Task B 的首句强提醒）
3. 新守则结尾是命令式总结"记住：完成用 @end"（Task B 的结尾总结）
4. marker 升级为 v2（强制走新模板）
5. context-manager 仍不被注入守则（回归保护）
6. 其他子 Agent（如 file-processor）仍被注入新守则（回归保护）
"""


def test_patch_sentence_removed():
    """补丁句"你不需要在输出里包含自己的标识符"已从守则删除"""
    from agent.subagent import _SUBAGENT_ASK_GUIDE_TEMPLATE

    assert "你不需要在输出里包含自己的标识符" not in _SUBAGENT_ASK_GUIDE_TEMPLATE
    assert "程序会自动在你的问题前加上唯一标识" not in _SUBAGENT_ASK_GUIDE_TEMPLATE


def test_guide_first_line_is_command_style_exit_reminder():
    """新守则首句是命令式强提醒，含"任务完成"和"@end"

    首句强提醒：让子 Agent 第一眼就建立"做完要 @end"的肌肉记忆。
    利用 primacy effect（LLM 处理 system prompt 时首句 attention 权重最高）。
    """
    from agent.subagent import _SUBAGENT_ASK_GUIDE_TEMPLATE

    # 去掉 marker 行、空行、markdown 标题行后，第一句实际内容应含"任务完成"和"@end"
    lines = [
        line for line in _SUBAGENT_ASK_GUIDE_TEMPLATE.splitlines()
        if line.strip()
        and not line.strip().startswith("<!--")
        and not line.strip().startswith("#")
    ]
    first_content_line = lines[0] if lines else ""
    # 首句应含"任务完成"和"@end"两个关键词（命令式强提醒）
    assert "任务完成" in first_content_line, f"首句应含'任务完成'，实际: {first_content_line}"
    assert "@end" in first_content_line, f"首句应含'@end'，实际: {first_content_line}"


def test_guide_last_line_is_command_style_summary():
    """新守则结尾是命令式总结"记住：完成用 @end"

    结尾总结：子 Agent 扫到结尾时再被提醒一次。
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
    # 结尾应含"记住"和"@end"（命令式总结）
    assert "记住" in last_content_line, f"结尾应含'记住'，实际: {last_content_line}"
    assert "@end" in last_content_line, f"结尾应含'@end'，实际: {last_content_line}"
    # 结尾应同时提到 @niu-agent（二选一）
    assert "@niu-agent" in last_content_line, f"结尾应含'@niu-agent'，实际: {last_content_line}"


def test_guide_marker_upgraded_to_v2():
    """marker 从 v1 升级到 v2，强制走新模板注入"""
    from agent.subagent import _SUBAGENT_ASK_GUIDE_MARKER, _SUBAGENT_ASK_GUIDE_TEMPLATE

    assert _SUBAGENT_ASK_GUIDE_MARKER == "<!-- NIU_SUBAGENT_GUIDE_v2 -->"
    assert "<!-- NIU_SUBAGENT_GUIDE_v2 -->" in _SUBAGENT_ASK_GUIDE_TEMPLATE
    # 旧 marker 不应出现在新模板里
    assert "<!-- NIU_SUBAGENT_GUIDE_v1 -->" not in _SUBAGENT_ASK_GUIDE_TEMPLATE


def test_context_manager_still_not_injected():
    """context-manager 仍不被注入守则（回归保护）"""
    from agent.subagent import _SUBAGENT_ASK_GUIDE_MARKER, build_subagent_system_segments

    static_system, _ = build_subagent_system_segments("context-manager")
    assert _SUBAGENT_ASK_GUIDE_MARKER not in static_system


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

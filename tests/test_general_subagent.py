"""通用子 Agent（阶段三）单元测试"""


def test_resolve_agent_md_path_project_priority(tmp_path, monkeypatch):
    """项目目录的 MD 优先于用户目录"""
    from agent import subagent

    project_dir = tmp_path / "project" / "config" / "agents"
    user_dir = tmp_path / "user" / "agents"
    project_dir.mkdir(parents=True)
    user_dir.mkdir(parents=True)

    # 两个目录都有同名文件
    (project_dir / "foo.md").write_text("---\ndescription: project\n---\nproject body")
    (user_dir / "foo.md").write_text("---\ndescription: user\n---\nuser body")

    # patch 模块级常量（不依赖 __file__，更稳健）
    monkeypatch.setattr(subagent, "_PROJECT_AGENTS_DIR", str(project_dir))
    monkeypatch.setattr(subagent, "_USER_AGENTS_DIR", str(user_dir))

    path = subagent._resolve_agent_md_path("foo")
    assert path == str(project_dir / "foo.md")


def test_resolve_agent_md_path_user_fallback(tmp_path, monkeypatch):
    """项目目录没有时回退到用户目录"""
    from agent import subagent

    project_dir = tmp_path / "project" / "config" / "agents"
    user_dir = tmp_path / "user" / "agents"
    project_dir.mkdir(parents=True)
    user_dir.mkdir(parents=True)

    (user_dir / "bar.md").write_text("---\ndescription: user\n---\nuser body")

    monkeypatch.setattr(subagent, "_PROJECT_AGENTS_DIR", str(project_dir))
    monkeypatch.setattr(subagent, "_USER_AGENTS_DIR", str(user_dir))

    path = subagent._resolve_agent_md_path("bar")
    assert path == str(user_dir / "bar.md")


def test_resolve_agent_md_path_not_found(tmp_path, monkeypatch):
    """都找不到返回 None"""
    from agent import subagent

    project_dir = tmp_path / "project" / "config" / "agents"
    user_dir = tmp_path / "user" / "agents"
    project_dir.mkdir(parents=True)
    user_dir.mkdir(parents=True)

    monkeypatch.setattr(subagent, "_PROJECT_AGENTS_DIR", str(project_dir))
    monkeypatch.setattr(subagent, "_USER_AGENTS_DIR", str(user_dir))

    path = subagent._resolve_agent_md_path("missing")
    assert path is None


def test_get_subagent_config_from_user_dir(tmp_path, monkeypatch):
    """从用户目录读取通用子 Agent 配置"""
    from agent import subagent

    user_dir = tmp_path / "user" / "agents"
    user_dir.mkdir(parents=True)
    (user_dir / "my-agent.md").write_text(
        "---\ndescription: my agent\nmcpServers: [photo-server]\n---\nbody"
    )

    # 项目目录指向空目录（让 _resolve_agent_md_path 回退到用户目录）
    project_dir = tmp_path / "project" / "config" / "agents"
    project_dir.mkdir(parents=True)
    monkeypatch.setattr(subagent, "_PROJECT_AGENTS_DIR", str(project_dir))
    monkeypatch.setattr(subagent, "_USER_AGENTS_DIR", str(user_dir))

    config = subagent.get_subagent_config("my-agent")
    assert config["description"] == "my agent"
    assert config["mcpServers"] == ["photo-server"]


def test_get_subagent_prompt_from_user_dir(tmp_path, monkeypatch):
    """从用户目录读取通用子 Agent 提示词"""
    from agent import subagent

    user_dir = tmp_path / "user" / "agents"
    user_dir.mkdir(parents=True)
    (user_dir / "my-agent.md").write_text(
        "---\ndescription: my agent\n---\nYou are my agent."
    )

    project_dir = tmp_path / "project" / "config" / "agents"
    project_dir.mkdir(parents=True)
    monkeypatch.setattr(subagent, "_PROJECT_AGENTS_DIR", str(project_dir))
    monkeypatch.setattr(subagent, "_USER_AGENTS_DIR", str(user_dir))

    prompt = subagent.get_subagent_prompt("my-agent")
    assert prompt == "You are my agent."


def test_get_subagent_config_missing_returns_empty(tmp_path, monkeypatch):
    """MD 文件不存在时返回空 dict（保持现有行为）"""
    from agent import subagent

    project_dir = tmp_path / "project" / "config" / "agents"
    user_dir = tmp_path / "user" / "agents"
    project_dir.mkdir(parents=True)
    user_dir.mkdir(parents=True)
    monkeypatch.setattr(subagent, "_PROJECT_AGENTS_DIR", str(project_dir))
    monkeypatch.setattr(subagent, "_USER_AGENTS_DIR", str(user_dir))

    config = subagent.get_subagent_config("nonexistent")
    assert config == {}


def test_get_tools_schema_includes_user_agents(tmp_path, monkeypatch):
    """get_tools_schema 扫描 ~/.niu/agents/ 把通用子 Agent 加入工具列表"""
    from agent import runner, subagent

    user_dir = tmp_path / "user" / "agents"
    user_dir.mkdir(parents=True)
    (user_dir / "photo-organizer.md").write_text(
        "---\ndescription: 整理照片\nmcpServers: [photo-server]\nallowAsync: true\n---\nbody"
    )
    (user_dir / "doc-summarizer.md").write_text(
        "---\ndescription: 总结文档\nmcpServers: [file-parser]\n---\nbody"
    )

    project_dir = tmp_path / "project"
    project_agents = project_dir / "config" / "agents"
    project_agents.mkdir(parents=True)
    (project_agents / "niu.md").write_text("---\nsub agents: []\n---\nniu prompt")

    monkeypatch.setattr(subagent, "_PROJECT_AGENTS_DIR", str(project_agents))
    monkeypatch.setattr(subagent, "_USER_AGENTS_DIR", str(user_dir))

    tools = runner.get_tools_schema()
    tool_names = [t["function"]["name"] for t in tools]
    assert "chat-with-photo-organizer" in tool_names
    assert "chat-with-doc-summarizer" in tool_names


def test_get_tools_schema_skips_bad_md(tmp_path, monkeypatch):
    """YAML 解析失败的 MD 被跳过，不生成对应工具（方式 B：不允许坏工具）"""
    from agent import runner, subagent

    user_dir = tmp_path / "user" / "agents"
    user_dir.mkdir(parents=True)
    (user_dir / "good.md").write_text(
        "---\ndescription: good\nmcpServers: []\n---\nbody"
    )
    (user_dir / "bad.md").write_text(
        "---\ndescription: : invalid yaml\n---\nbody"
    )

    project_dir = tmp_path / "project"
    project_agents = project_dir / "config" / "agents"
    project_agents.mkdir(parents=True)
    (project_agents / "niu.md").write_text("---\nsub agents: []\n---\nniu prompt")

    monkeypatch.setattr(subagent, "_PROJECT_AGENTS_DIR", str(project_agents))
    monkeypatch.setattr(subagent, "_USER_AGENTS_DIR", str(user_dir))

    tools = runner.get_tools_schema()
    tool_names = [t["function"]["name"] for t in tools]
    assert "chat-with-good" in tool_names
    assert "chat-with-bad" not in tool_names


def test_get_tools_schema_skips_non_kebab_name(tmp_path, monkeypatch):
    """文件名含空格/大写/非 kebab-case 的 MD 被跳过（避免工具名不合法）"""
    from agent import runner, subagent

    user_dir = tmp_path / "user" / "agents"
    user_dir.mkdir(parents=True)
    (user_dir / "good-agent.md").write_text("---\ndescription: good\n---\nbody")
    (user_dir / "bad agent.md").write_text("---\ndescription: bad space\n---\nbody")
    (user_dir / "BadCase.md").write_text("---\ndescription: bad case\n---\nbody")

    project_dir = tmp_path / "project"
    project_agents = project_dir / "config" / "agents"
    project_agents.mkdir(parents=True)
    (project_agents / "niu.md").write_text("---\nsub agents: []\n---\nniu prompt")

    monkeypatch.setattr(subagent, "_PROJECT_AGENTS_DIR", str(project_agents))
    monkeypatch.setattr(subagent, "_USER_AGENTS_DIR", str(user_dir))

    tools = runner.get_tools_schema()
    tool_names = [t["function"]["name"] for t in tools]
    assert "chat-with-good-agent" in tool_names
    assert "chat-with-bad agent" not in tool_names
    assert "chat-with-BadCase" not in tool_names


def test_get_tools_schema_skips_empty_frontmatter(tmp_path, monkeypatch):
    """空 frontmatter（无 description）的 MD 被跳过（视为无效子 Agent）"""
    from agent import runner, subagent

    user_dir = tmp_path / "user" / "agents"
    user_dir.mkdir(parents=True)
    (user_dir / "good.md").write_text("---\ndescription: good\n---\nbody")
    (user_dir / "empty.md").write_text("---\n---\nbody")

    project_dir = tmp_path / "project"
    project_agents = project_dir / "config" / "agents"
    project_agents.mkdir(parents=True)
    (project_agents / "niu.md").write_text("---\nsub agents: []\n---\nniu prompt")

    monkeypatch.setattr(subagent, "_PROJECT_AGENTS_DIR", str(project_agents))
    monkeypatch.setattr(subagent, "_USER_AGENTS_DIR", str(user_dir))

    tools = runner.get_tools_schema()
    tool_names = [t["function"]["name"] for t in tools]
    assert "chat-with-good" in tool_names
    assert "chat-with-empty" not in tool_names


def test_get_tools_schema_dedup(tmp_path, monkeypatch):
    """同名时专用子 Agent 优先，不重复生成工具"""
    from agent import runner, subagent

    project_dir = tmp_path / "project"
    project_agents = project_dir / "config" / "agents"
    project_agents.mkdir(parents=True)
    (project_agents / "shared.md").write_text(
        "---\ndescription: project shared\n---\nproject body"
    )
    (project_agents / "niu.md").write_text(
        "---\nsub agents: [shared]\n---\nniu prompt"
    )
    user_dir = tmp_path / "user" / "agents"
    user_dir.mkdir(parents=True)
    (user_dir / "shared.md").write_text(
        "---\ndescription: user shared\n---\nuser body"
    )

    monkeypatch.setattr(subagent, "_PROJECT_AGENTS_DIR", str(project_agents))
    monkeypatch.setattr(subagent, "_USER_AGENTS_DIR", str(user_dir))

    tools = runner.get_tools_schema()
    shared_tools = [t for t in tools if t["function"]["name"] == "chat-with-shared"]
    assert len(shared_tools) == 1
    assert shared_tools[0]["function"]["description"] == "project shared"

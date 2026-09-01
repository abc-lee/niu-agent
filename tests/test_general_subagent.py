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


def test_get_tools_schema_skips_non_dict_frontmatter(tmp_path, monkeypatch):
    """非标量 frontmatter（纯字符串/列表）不崩溃 get_tools_schema 且被优雅跳过（warn+skip）。"""
    from agent import runner, subagent

    user_dir = tmp_path / "user" / "agents"
    user_dir.mkdir(parents=True)
    (user_dir / "good.md").write_text("---\ndescription: good\n---\nbody")
    # 纯字符串 frontmatter：yaml.safe_load 返回 str（truthy）→ get_subagent_config 原样返回；
    # 缺 isinstance 守卫时 .get() 抛 AttributeError 逃逸出循环 → get_tools_schema 整体崩溃
    (user_dir / "strfm.md").write_text("---\njust a plain string frontmatter\n---\nbody")
    (user_dir / "listfm.md").write_text("---\n- a\n- b\n---\nbody")

    project_dir = tmp_path / "project"
    project_agents = project_dir / "config" / "agents"
    project_agents.mkdir(parents=True)
    (project_agents / "niu.md").write_text("---\nsub agents: []\n---\nniu prompt")

    monkeypatch.setattr(subagent, "_PROJECT_AGENTS_DIR", str(project_agents))
    monkeypatch.setattr(subagent, "_USER_AGENTS_DIR", str(user_dir))

    tools = runner.get_tools_schema()  # 不得抛 AttributeError
    tool_names = [t["function"]["name"] for t in tools]
    assert "chat-with-good" in tool_names
    assert "chat-with-strfm" not in tool_names
    assert "chat-with-listfm" not in tool_names


def test_get_tools_schema_skips_hidden_agent(tmp_path, monkeypatch):
    """visibility: hidden 的 MD 不注册 chat-with 工具（后台专用子 Agent）；普通 MD 不受影响"""
    from agent import runner, subagent

    user_dir = tmp_path / "user" / "agents"
    user_dir.mkdir(parents=True)
    (user_dir / "good.md").write_text(
        "---\ndescription: good\n---\nbody"
    )
    (user_dir / "hidden-bg.md").write_text(
        "---\ndescription: 后台 agent\nvisibility: hidden\n---\nbody"
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
    assert "chat-with-hidden-bg" not in tool_names


def test_get_tools_schema_hidden_agent_still_readable_by_name(tmp_path, monkeypatch):
    """hidden 只挡工具注册、不挡程序按名直调：get_subagent_config 仍能读到完整配置"""
    from agent import subagent

    user_dir = tmp_path / "user" / "agents"
    user_dir.mkdir(parents=True)
    (user_dir / "hidden-bg.md").write_text(
        "---\ndescription: 后台 agent\nvisibility: hidden\n---\nbody"
    )

    project_dir = tmp_path / "project" / "config" / "agents"
    project_dir.mkdir(parents=True)

    monkeypatch.setattr(subagent, "_PROJECT_AGENTS_DIR", str(project_dir))
    monkeypatch.setattr(subagent, "_USER_AGENTS_DIR", str(user_dir))

    config = subagent.get_subagent_config("hidden-bg")
    assert config["visibility"] == "hidden"
    assert config["description"] == "后台 agent"


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


def test_niu_runner_init_known_user_subagents(tmp_path, monkeypatch):
    """NiuRunner.__init__ 初始化 _known_user_subagents 集合"""
    from unittest import mock

    from agent import runner, subagent

    user_dir = tmp_path / "user" / "agents"
    user_dir.mkdir(parents=True)
    (user_dir / "foo.md").write_text("---\ndescription: foo\n---\nbody")
    (user_dir / "bar.md").write_text("---\ndescription: bar\n---\nbody")
    # 非法名文件不应计入（但此处只验证集合内容，跳过校验是 get_tools_schema 的事）
    (user_dir / "_skip.md").write_text("---\ndescription: skip\n---\nbody")

    project_agents = tmp_path / "project" / "config" / "agents"
    project_agents.mkdir(parents=True)
    (project_agents / "niu.md").write_text("---\n---\nniu prompt")

    monkeypatch.setattr(subagent, "_PROJECT_AGENTS_DIR", str(project_agents))
    monkeypatch.setattr(subagent, "_USER_AGENTS_DIR", str(user_dir))

    # mock LLM config 避免实际初始化 client
    llm_config = {"model": "test", "api_key": "test", "base_url": "http://localhost"}
    with mock.patch.object(runner.NiuRunner, "_build_static_system_prompt", return_value=""), \
         mock.patch.object(runner.NiuRunner, "_build_disk_description", return_value=""), \
         mock.patch.object(runner, "create_client", return_value=None), \
         mock.patch.object(runner, "get_skill_sync"), \
         mock.patch.object(runner, "get_registry"):
        r = runner.NiuRunner(llm_config)
    # 显式断言集合内容（不只是长度）
    assert r._known_user_subagents == {"foo.md", "bar.md"}


def test_niu_runner_init_known_user_subagents_no_dir(tmp_path, monkeypatch):
    """~/.niu/agents/ 不存在时初始化为空集合"""
    from unittest import mock

    from agent import runner, subagent

    project_agents = tmp_path / "project" / "config" / "agents"
    project_agents.mkdir(parents=True)
    (project_agents / "niu.md").write_text("---\n---\nniu prompt")

    # 用户目录指向不存在的路径
    monkeypatch.setattr(subagent, "_PROJECT_AGENTS_DIR", str(project_agents))
    monkeypatch.setattr(subagent, "_USER_AGENTS_DIR", str(tmp_path / "nonexistent"))

    llm_config = {"model": "test", "api_key": "test", "base_url": "http://localhost"}
    with mock.patch.object(runner.NiuRunner, "_build_static_system_prompt", return_value=""), \
         mock.patch.object(runner.NiuRunner, "_build_disk_description", return_value=""), \
         mock.patch.object(runner, "create_client", return_value=None), \
         mock.patch.object(runner, "get_skill_sync"), \
         mock.patch.object(runner, "get_registry"):
        r = runner.NiuRunner(llm_config)
    assert r._known_user_subagents == set()


def test_refresh_base_tools_schema_if_dirty_no_change(tmp_path, monkeypatch):
    """无新文件时不重算 base_tools_schema"""
    from unittest import mock

    from agent import runner, subagent

    user_dir = tmp_path / "user" / "agents"
    user_dir.mkdir(parents=True)
    (user_dir / "foo.md").write_text("---\ndescription: foo\n---\nbody")

    project_agents = tmp_path / "project" / "config" / "agents"
    project_agents.mkdir(parents=True)
    (project_agents / "niu.md").write_text("---\n---\nniu prompt")

    monkeypatch.setattr(subagent, "_PROJECT_AGENTS_DIR", str(project_agents))
    monkeypatch.setattr(subagent, "_USER_AGENTS_DIR", str(user_dir))

    llm_config = {"model": "test", "api_key": "test", "base_url": "http://localhost"}
    with mock.patch.object(runner.NiuRunner, "_build_static_system_prompt", return_value=""), \
         mock.patch.object(runner.NiuRunner, "_build_disk_description", return_value=""), \
         mock.patch.object(runner, "create_client", return_value=None), \
         mock.patch.object(runner, "get_skill_sync"), \
         mock.patch.object(runner, "get_registry"):
        r = runner.NiuRunner(llm_config)

    original_schema = r.base_tools_schema
    r._refresh_base_tools_schema_if_dirty()
    assert r.base_tools_schema is original_schema  # 同一对象，未重算


def test_refresh_base_tools_schema_if_dirty_new_file(tmp_path, monkeypatch):
    """有新 MD 文件时重算 base_tools_schema，且返回完整 base 集（含 check_subagent_progress）"""
    from unittest import mock

    from agent import runner, subagent

    user_dir = tmp_path / "user" / "agents"
    user_dir.mkdir(parents=True)
    (user_dir / "foo.md").write_text("---\ndescription: foo\n---\nbody")

    project_agents = tmp_path / "project" / "config" / "agents"
    project_agents.mkdir(parents=True)
    (project_agents / "niu.md").write_text("---\n---\nniu prompt")

    monkeypatch.setattr(subagent, "_PROJECT_AGENTS_DIR", str(project_agents))
    monkeypatch.setattr(subagent, "_USER_AGENTS_DIR", str(user_dir))

    llm_config = {"model": "test", "api_key": "test", "base_url": "http://localhost"}
    with mock.patch.object(runner.NiuRunner, "_build_static_system_prompt", return_value=""), \
         mock.patch.object(runner.NiuRunner, "_build_disk_description", return_value=""), \
         mock.patch.object(runner, "create_client", return_value=None), \
         mock.patch.object(runner, "get_skill_sync"), \
         mock.patch.object(runner, "get_registry"):
        r = runner.NiuRunner(llm_config)

    original_len = len(r.base_tools_schema)
    # 新建一个 MD 文件
    (user_dir / "bar.md").write_text("---\ndescription: bar\n---\nbody")

    r._refresh_base_tools_schema_if_dirty()
    tool_names = [t["function"]["name"] for t in r.base_tools_schema]
    assert "chat-with-bar" in tool_names  # 新子 Agent 已加入
    assert "chat-with-foo" in tool_names  # 原有子 Agent 仍在
    assert "check_subagent_progress" in tool_names  # 阶段二工具仍在（完整 base 集）
    assert len(r.base_tools_schema) == original_len + 1  # 仅 +1（新增 bar）


def test_refresh_base_tools_schema_if_dirty_no_dir(tmp_path, monkeypatch):
    """~/.niu/agents/ 不存在时跳过"""
    from unittest import mock

    from agent import runner, subagent

    project_agents = tmp_path / "project" / "config" / "agents"
    project_agents.mkdir(parents=True)
    (project_agents / "niu.md").write_text("---\n---\nniu prompt")

    # 用户目录指向不存在的路径
    monkeypatch.setattr(subagent, "_PROJECT_AGENTS_DIR", str(project_agents))
    monkeypatch.setattr(subagent, "_USER_AGENTS_DIR", str(tmp_path / "nonexistent"))

    llm_config = {"model": "test", "api_key": "test", "base_url": "http://localhost"}
    with mock.patch.object(runner.NiuRunner, "_build_static_system_prompt", return_value=""), \
         mock.patch.object(runner.NiuRunner, "_build_disk_description", return_value=""), \
         mock.patch.object(runner, "create_client", return_value=None), \
         mock.patch.object(runner, "get_skill_sync"), \
         mock.patch.object(runner, "get_registry"):
        r = runner.NiuRunner(llm_config)

    original_schema = r.base_tools_schema
    r._refresh_base_tools_schema_if_dirty()  # 不应抛异常
    assert r.base_tools_schema is original_schema  # 未重算


def test_resolve_agent_md_path_rejects_path_traversal(tmp_path, monkeypatch):
    """agent_name 含路径穿越字符（如 ../）时返回 None"""
    from agent import subagent

    project_dir = tmp_path / "project" / "config" / "agents"
    user_dir = tmp_path / "user" / "agents"
    project_dir.mkdir(parents=True)
    user_dir.mkdir(parents=True)

    monkeypatch.setattr(subagent, "_PROJECT_AGENTS_DIR", str(project_dir))
    monkeypatch.setattr(subagent, "_USER_AGENTS_DIR", str(user_dir))

    # 各种路径穿越尝试
    assert subagent._resolve_agent_md_path("../etc/passwd") is None
    assert subagent._resolve_agent_md_path("../../etc/passwd") is None
    assert subagent._resolve_agent_md_path("foo/../bar") is None
    # 空字符串也拒绝
    assert subagent._resolve_agent_md_path("") is None


def test_get_tools_schema_excludes_main_only_for_subagent():
    """get_tools_schema(include_main_only=False) 不含 check_subagent_progress"""
    from agent import runner
    tools = runner.get_tools_schema(include_main_only=False)
    tool_names = [t["function"]["name"] for t in tools]
    assert "check_subagent_progress" not in tool_names


def test_get_tools_schema_includes_main_only_by_default():
    """get_tools_schema() 默认含 check_subagent_progress"""
    from agent import runner
    tools = runner.get_tools_schema()
    tool_names = [t["function"]["name"] for t in tools]
    assert "check_subagent_progress" in tool_names


def test_build_subagent_system_segments_injects_guide_for_all_subagents(tmp_path, monkeypatch):
    """所有子 Agent（同步+异步）build_subagent_system_segments 都注入 @niu-agent/@end 守则"""
    from agent import subagent

    user_dir = tmp_path / "user" / "agents"
    user_dir.mkdir(parents=True)
    (user_dir / "my-agent.md").write_text("---\ndescription: my agent\n---\nYou are my agent.")

    project_dir = tmp_path / "project" / "config" / "agents"
    project_dir.mkdir(parents=True)
    monkeypatch.setattr(subagent, "_PROJECT_AGENTS_DIR", str(project_dir))
    monkeypatch.setattr(subagent, "_USER_AGENTS_DIR", str(user_dir))

    static_system, dynamic_system = subagent.build_subagent_system_segments("my-agent")
    assert subagent._SUBAGENT_ASK_GUIDE_MARKER in static_system
    assert "@niu-agent" in static_system
    assert "@end" in static_system


def test_build_subagent_system_segments_no_duplicate_injection(tmp_path, monkeypatch):
    """子 Agent 正文已含 marker 时不重复注入"""
    from agent import subagent

    user_dir = tmp_path / "user" / "agents"
    user_dir.mkdir(parents=True)
    (user_dir / "my-agent.md").write_text(
        "---\ndescription: my agent\n---\nYou are my agent.\n\n"
        + subagent._SUBAGENT_ASK_GUIDE_MARKER + "\n已有守则"
    )

    project_dir = tmp_path / "project" / "config" / "agents"
    project_dir.mkdir(parents=True)
    monkeypatch.setattr(subagent, "_PROJECT_AGENTS_DIR", str(project_dir))
    monkeypatch.setattr(subagent, "_USER_AGENTS_DIR", str(user_dir))

    static_system, _ = subagent.build_subagent_system_segments("my-agent")
    # 守则只出现一次（marker 计数 == 1）
    assert static_system.count(subagent._SUBAGENT_ASK_GUIDE_MARKER) == 1


# ============== R15a：dream-evolver 脑区预注入失败/空列表 warning 日志（P3 补测） ==============
# 注意：agent/subagent.py 用 loguru（不是标准 logging），pytest caplog 捕获不到，
# 必须用 loguru sink 捕获（同 test_subagent_tool_filter.py 模式）。


def test_build_subagent_system_segments_warns_when_get_brain_regions_fails(monkeypatch):
    """R15a：get_brain_regions 抛异常时产生 warning 日志（不静默）且 fallback 不注入脑区列表"""
    from loguru import logger

    from agent import subagent
    from agent.subagent import build_subagent_system_segments

    monkeypatch.setattr(subagent, "get_subagent_prompt", lambda name: "你是 dream-evolver。")
    monkeypatch.setattr(subagent, "_build_user_info_section", lambda: "")

    def _boom():
        raise RuntimeError("graph unavailable")

    # 函数体内 from niu_api.internal.lightrag_manager import get_brain_regions
    # → patch 源模块属性（函数级 import 每次取源模块）
    monkeypatch.setattr("niu_api.internal.lightrag_manager.get_brain_regions", _boom)

    warnings = []
    sink_id = logger.add(lambda m: warnings.append(str(m)), level="WARNING")
    try:
        _, dynamic_system = build_subagent_system_segments("dream-evolver")
    finally:
        logger.remove(sink_id)

    assert any("Failed to get brain regions" in w for w in warnings), (
        f"应有 warning 记录失败原因，实际: {warnings}"
    )
    # fallback 行为：_brain_region_section 保持空——不注入动态脑区列表
    assert "当前脑区列表" not in dynamic_system


def test_build_subagent_system_segments_warns_when_get_brain_regions_empty(monkeypatch):
    """R15a：get_brain_regions 返回空列表时产生 warning 日志（不静默）且 fallback 不注入脑区列表"""
    from loguru import logger

    from agent import subagent
    from agent.subagent import build_subagent_system_segments

    monkeypatch.setattr(subagent, "get_subagent_prompt", lambda name: "你是 dream-evolver。")
    monkeypatch.setattr(subagent, "_build_user_info_section", lambda: "")

    monkeypatch.setattr("niu_api.internal.lightrag_manager.get_brain_regions", lambda: [])

    warnings = []
    sink_id = logger.add(lambda m: warnings.append(str(m)), level="WARNING")
    try:
        _, dynamic_system = build_subagent_system_segments("dream-evolver")
    finally:
        logger.remove(sink_id)

    assert any("returned empty list" in w for w in warnings), (
        f"应有 warning 记录空列表，实际: {warnings}"
    )
    # fallback 行为：_brain_region_section 保持空——不注入动态脑区列表
    assert "当前脑区列表" not in dynamic_system


# ============== E4-14：提示词降级标注（call_subagent 三级降级） ==============
# 降级：build_subagent_system_segments 失败 → get_subagent_prompt → 裸 prompt。
# 标注 [子 Agent 提示词降级: <原因>]：非 JSON 结果 call_subagent 内拼接；
# JSON 结构化结果保持原样（游标/JSON 消费不受影响），降级事实经模块级标记旁路
# 在展示层（handler._call_subagent_gen display_result）补注。
import pytest  # noqa: E402（autouse fixture 用）


@pytest.fixture(autouse=True)
def _reset_prompt_degradation_marker():
    """E4-14：每个测试后清零降级标记（thread-local——防跨测试污染）。"""
    from agent import subagent

    yield
    subagent._set_subagent_prompt_degraded_reason(None)


def _call_subagent_hermetic(monkeypatch, run_result=("done", {"result": "CURRENT_TASK_DONE", "data": "ok"}, "")):
    """hermetic call_subagent 依赖集（范式照抄 test_subagent_current_time）。"""
    from unittest.mock import Mock

    import agent.runner as runner_mod
    from agent import subagent

    captured = {}

    def mock_run(client, system_prompt, user_input, handler, tools_schema, **kwargs):
        captured["user_input"] = user_input
        captured["tools_schema"] = tools_schema
        return run_result

    monkeypatch.setattr(subagent, "_run_agent_loop", mock_run)
    monkeypatch.setattr(subagent, "get_subagent_prompt", lambda name: "system")
    monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {})
    monkeypatch.setattr(subagent, "get_subagent_mcp_tools_schema", lambda name: [])
    monkeypatch.setattr(runner_mod, "create_client", lambda cfg: Mock())
    monkeypatch.setattr(runner_mod, "get_tools_schema", lambda include_main_only=False: [])
    return captured


def test_prompt_degradation_annotates_non_json_result(monkeypatch):
    """E4-14：系统提示词降级 + 非 JSON 结果 → 结果附加 [子 Agent 提示词降级: 原因]。"""
    from agent import subagent

    def _boom(name):
        raise RuntimeError("system segments boom")

    monkeypatch.setattr(subagent, "build_subagent_system_segments", _boom)
    _call_subagent_hermetic(monkeypatch)

    result = subagent.call_subagent(
        agent_name="test-agent",
        task="处理文件",
        llm_config={"apikey": "test", "apibase": "http://test", "model": "test"},
    )
    assert result == "done\n[子 Agent 提示词降级: 系统提示词构建失败：system segments boom]"
    # 降级标记置位（供展示层消费——同线程可读）
    assert subagent._get_subagent_prompt_degraded_reason() == "系统提示词构建失败：system segments boom"


def test_prompt_degradation_json_result_stays_clean(monkeypatch):
    """E4-14：降级 + JSON 结构化结果 → call_subagent 返回原样 JSON（不拼接标注——JSON 消费/游标不受影响）。"""
    from agent import subagent

    def _boom(name):
        raise RuntimeError("system segments boom")

    monkeypatch.setattr(subagent, "build_subagent_system_segments", _boom)
    _call_subagent_hermetic(
        monkeypatch,
        run_result=('{"ok": true}', {"result": "CURRENT_TASK_DONE", "data": "ok"}, ""),
    )

    result = subagent.call_subagent(
        agent_name="test-agent",
        task="处理文件",
        llm_config={"apikey": "test", "apibase": "http://test", "model": "test"},
    )
    assert result == '{"ok": true}'
    assert "子 Agent 提示词降级" not in result
    # 降级事实仍经降级标记旁路（展示层补注）
    assert subagent._get_subagent_prompt_degraded_reason() is not None


def test_prompt_degradation_marker_resets_on_next_call(monkeypatch):
    """E4-14：下一次调用起始清零标记——正常路径零变化（不残留上次降级标注）。"""
    from agent import subagent

    def _boom(name):
        raise RuntimeError("boom")

    monkeypatch.setattr(subagent, "build_subagent_system_segments", _boom)
    _call_subagent_hermetic(monkeypatch)
    subagent.call_subagent(
        agent_name="test-agent", task="t1",
        llm_config={"apikey": "k", "apibase": "http://x", "model": "m"},
    )
    assert subagent._get_subagent_prompt_degraded_reason() is not None  # 第一次调用降级置位

    # 第二次调用：正常构建 → 标记清零 + 结果无标注
    monkeypatch.setattr(subagent, "build_subagent_system_segments", lambda name: ("static", "dynamic"))
    _call_subagent_hermetic(monkeypatch)
    result = subagent.call_subagent(
        agent_name="test-agent", task="t2",
        llm_config={"apikey": "k", "apibase": "http://x", "model": "m"},
    )
    assert result == "done"
    assert subagent._get_subagent_prompt_degraded_reason() is None


def _drive_call_subagent_gen(agent_name, subagent_result):
    """hermetic 驱动 handler._call_subagent_gen 同步路径（范式照抄 test_incomplete_cursor.py）。"""
    from unittest import mock

    from agent.handler import NiuHandler

    handler = NiuHandler(mcp_client=None)
    fake_runner = mock.MagicMock()
    fake_runner.llm_config = {"model": "m", "apikey": "x", "apibase": "http://x"}
    with mock.patch("agent.runner.get_runner", return_value=fake_runner), \
         mock.patch("agent.subagent.call_subagent", return_value=subagent_result), \
         mock.patch("agent.subagent_registry.SubagentRegistry.get", return_value=None), \
         mock.patch("niu_api.internal.subagent_event_bus.pre_register"), \
         mock.patch("niu_api.internal.subagent_event_bus.has_subagent", return_value=False), \
         mock.patch("niu_api.chat._main_loop", None):
        gen = handler._call_subagent_gen(agent_name, {"task": "精加工实体"})
        try:
            while True:
                next(gen)
        except StopIteration as si:
            return si.value
    raise AssertionError("generator 未返回 StepOutcome")


def test_prompt_degradation_display_layer_annotates_json_result():
    """E4-14：降级标记置位 + JSON 结果 → 展示层 display_result 附标注（返回 LLM 的展示副本）。"""
    from agent import subagent

    subagent._set_subagent_prompt_degraded_reason("系统提示词构建失败：boom")
    outcome = _drive_call_subagent_gen("test-agent", '{"ok": true}')
    assert outcome.data["status"] == "success"
    assert outcome.data["result"] == '{"ok": true}\n[子 Agent 提示词降级: 系统提示词构建失败：boom]'


def test_prompt_degradation_display_layer_unchanged_without_marker():
    """E4-14：无降级标记 → 展示层 JSON 原样透传（正常路径零变化）。"""
    from agent import subagent

    assert subagent._get_subagent_prompt_degraded_reason() is None  # 前提：默认无标记
    outcome = _drive_call_subagent_gen("test-agent", '{"ok": true}')
    assert outcome.data["result"] == '{"ok": true}'


def test_prompt_degradation_json_result_display_layer_only(monkeypatch):
    """E4-14：降级返回的干净 JSON（无标注）→ 降级标注只进展示副本（JSON 结果本体不被腐蚀）。

    E4 T3 P3b 改版（T7：handler journal 游标钩子退役，journal-agent 走普通子 Agent
    路径）：驱动完整链路——真实 call_subagent（build_subagent_system_segments 降级）
    → handler._call_subagent_gen journal-agent 路径，直接断言 StepOutcome 返回的
    展示结果带标注、且原始 JSON 完整保留在结果文本头部。
    """
    from unittest import mock

    from agent import subagent

    def _boom(name):
        raise RuntimeError("system segments boom")

    monkeypatch.setattr(subagent, "build_subagent_system_segments", _boom)
    _call_subagent_hermetic(
        monkeypatch,
        run_result=('{"processed_up_to": 2}', {"result": "CURRENT_TASK_DONE", "data": "ok"}, ""),
    )

    from agent.handler import NiuHandler

    handler = NiuHandler(mcp_client=None)

    fake_runner = mock.MagicMock()
    fake_runner.llm_config = {"model": "m", "apikey": "x", "apibase": "http://x"}
    outcome = None
    with mock.patch("agent.runner.get_runner", return_value=fake_runner), \
         mock.patch("agent.subagent_registry.SubagentRegistry.get", return_value=None), \
         mock.patch("niu_api.internal.subagent_event_bus.pre_register"), \
         mock.patch("niu_api.internal.subagent_event_bus.has_subagent", return_value=False), \
         mock.patch("niu_api.chat._main_loop", None):
        gen = handler._call_subagent_gen("journal-agent", {"task": "精加工实体"})
        try:
            while True:
                next(gen)
        except StopIteration as si:
            outcome = si.value
    assert outcome is not None, "generator 未返回 StepOutcome"

    # 降级事实仍进展示层 display_result（返回 LLM 的展示副本，不解析）
    assert outcome.data["status"] == "success"
    assert outcome.data["result"] == '{"processed_up_to": 2}\n[子 Agent 提示词降级: 系统提示词构建失败：system segments boom]'


def test_prompt_degradation_annotates_compact_truncated(monkeypatch):
    """E4 T3 P3a：降级 + COMPACT_TRUNCATED 早期返回 → 标注拼接在截断内容末尾（前缀剥离消费不受影响）。"""
    from agent import subagent

    def _boom(name):
        raise RuntimeError("truncated boom")

    monkeypatch.setattr(subagent, "build_subagent_system_segments", _boom)
    _call_subagent_hermetic(
        monkeypatch,
        run_result=("partial done", {"result": "CURRENT_TASK_DONE", "data": "ok", "finish_reason": "length"}, ""),
    )

    result = subagent.call_subagent(
        agent_name="test-agent", task="t",
        llm_config={"apikey": "k", "apibase": "http://x", "model": "m"},
    )
    assert result.startswith("COMPACT_TRUNCATED:partial done")
    assert result.endswith("[子 Agent 提示词降级: 系统提示词构建失败：truncated boom]")
    # 前缀剥离后内容尾部仍含标注（消费端可见降级事实）
    assert "子 Agent 提示词降级" in result[len("COMPACT_TRUNCATED:"):]


def test_prompt_degradation_annotates_subagent_error(monkeypatch):
    """E4 T3 P3a：降级 + LLM_ERROR 早期返回 → SUBAGENT_ERROR 尾部附降级标注（错误文本含降级原因）。"""
    from agent import subagent

    def _boom(name):
        raise RuntimeError("error boom")

    monkeypatch.setattr(subagent, "build_subagent_system_segments", _boom)
    _call_subagent_hermetic(
        monkeypatch,
        run_result=("", {"result": "LLM_ERROR", "error_msg": "rate limit"}, ""),
    )

    result = subagent.call_subagent(
        agent_name="test-agent", task="t",
        llm_config={"apikey": "k", "apibase": "http://x", "model": "m"},
    )
    assert result == "SUBAGENT_ERROR:rate limit\n[子 Agent 提示词降级: 系统提示词构建失败：error boom]"

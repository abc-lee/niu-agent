"""通用子 Agent（阶段三）单元测试"""
import os
import tempfile
from unittest import mock
from pathlib import Path


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

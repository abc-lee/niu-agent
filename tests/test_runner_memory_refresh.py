"""验证 memory 派生段每轮从 memory.json 重读。

覆盖场景：
1. firstRun=true → false 后，"## 首次使用"段从 system prompt 消失
2. user.name 从占位符变为实值后，"## 用户信息"段出现在 system prompt
3. workspace.path 从占位符变为实值后，"## 工作目录"段出现在 system prompt
4. permanent 新增条目后，"### [用户长期记忆]"段立即反映
5. memory.json 不存在时，memory_section 为空字符串
"""

import json
import pytest
from pathlib import Path

from agent.runner import _load_memory_for_prompt


@pytest.fixture
def memory_file(tmp_path, monkeypatch):
    """Mock ~/.niu/memory.json 到独立临时目录（每个测试独立 home）"""
    fake_home = tmp_path
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    niu_dir = fake_home / ".niu"
    niu_dir.mkdir(parents=True, exist_ok=True)
    return niu_dir / "memory.json"


def test_first_run_true_includes_prompt(memory_file):
    """firstRun=true 时，memory_section 包含 '## 首次使用'"""
    memory_file.write_text(json.dumps({
        "identity": {"name": "妞妞"},
        "firstRun": True,
    }))
    section = _load_memory_for_prompt()
    assert "## 首次使用" in section
    assert "工作目录想放在哪里" in section


def test_first_run_false_excludes_prompt(memory_file):
    """firstRun=false 时，memory_section 不包含 '## 首次使用'"""
    memory_file.write_text(json.dumps({
        "identity": {"name": "妞妞"},
        "firstRun": False,
    }))
    section = _load_memory_for_prompt()
    assert "## 首次使用" not in section


def test_first_run_removed_after_write(memory_file):
    """模拟 Agent 写入 memory.json 把 firstRun 改为 false，下轮调用读到新状态"""
    # 初始：firstRun=true
    memory_file.write_text(json.dumps({
        "identity": {"name": "妞妞"},
        "firstRun": True,
    }))
    section1 = _load_memory_for_prompt()
    assert "## 首次使用" in section1

    # Agent 写入：firstRun=false + workspace.path
    memory_file.write_text(json.dumps({
        "identity": {"name": "妞妞"},
        "workspace": {"path": "/Users/li/work"},
        "firstRun": False,
    }))
    section2 = _load_memory_for_prompt()
    assert "## 首次使用" not in section2
    assert "/Users/li/work" in section2  # 工作目录段出现


def test_user_fields_placeholder_to_real(memory_file):
    """user 字段从占位符变为实值后，## 用户信息 段出现"""
    # 初始：占位符
    memory_file.write_text(json.dumps({
        "identity": {"name": "妞妞"},
        "user": {
            "name": "请询问用户真实姓名",
            "nickname": "请询问用户称呼",
            "occupation": "请询问用户职业",
            "organization": "请询问用户工作单位",
        },
    }))
    section1 = _load_memory_for_prompt()
    assert "## 用户信息" not in section1  # 占位符不出现

    # Agent 写入实值
    memory_file.write_text(json.dumps({
        "identity": {"name": "妞妞"},
        "user": {
            "name": "李雷",
            "nickname": "雷子",
            "occupation": "软件工程师",
            "organization": "ACME",
        },
    }))
    section2 = _load_memory_for_prompt()
    assert "## 用户信息" in section2
    assert "李雷" in section2
    assert "软件工程师" in section2


def test_permanent_updates_reflect_immediately(memory_file):
    """permanent 新增条目后，### [用户长期记忆] 段立即反映"""
    memory_file.write_text(json.dumps({
        "identity": {"name": "妞妞"},
        "permanent": [],
    }))
    section1 = _load_memory_for_prompt()
    # 空 permanent 应该没有 section 或显示 0 条
    assert "先做后说" not in section1

    memory_file.write_text(json.dumps({
        "identity": {"name": "妞妞"},
        "permanent": [
            {"type": "memory", "content": "座右铭：先做后说"}
        ],
    }))
    section2 = _load_memory_for_prompt()
    assert "先做后说" in section2


def test_memory_file_missing_returns_empty(memory_file):
    """memory.json 不存在时，返回空字符串"""
    # fixture 已创建 .niu 目录但不写 memory.json
    assert not memory_file.exists()
    section = _load_memory_for_prompt()
    assert section == ""


def test_workspace_placeholder_not_shown(memory_file):
    """workspace.path 是占位符时，## 工作目录 段不出现"""
    memory_file.write_text(json.dumps({
        "identity": {"name": "妞妞"},
        "workspace": {"path": "请询问用户指定工作目录"},
    }))
    section = _load_memory_for_prompt()
    assert "## 工作目录" not in section


def test_workspace_real_path_shown(memory_file):
    """workspace.path 是实值时，## 工作目录 段出现"""
    memory_file.write_text(json.dumps({
        "identity": {"name": "妞妞"},
        "workspace": {"path": "/Users/li/knowledge"},
    }))
    section = _load_memory_for_prompt()
    assert "## 工作目录" in section
    assert "/Users/li/knowledge" in section

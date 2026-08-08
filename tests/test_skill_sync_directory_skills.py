"""目录式 Skills（<dir>/SKILL.md）扫描支持测试。

全 mock：patch get_lightrag（函数级 import 目标=lightrag_manager 模块）+
LightRAGAdapter 替换 + _inject_skill_to_lightrag mock，不碰真实 LightRAG/LLM。
"""
import hashlib
from pathlib import Path
from unittest import mock

import pytest


@pytest.fixture
def fake_sync(tmp_path, monkeypatch):
    """构造轻量 SkillSync（同 test_skill_sync_no_write_when_unchanged 范式）。

    绕过 LightRAG 不可用的提前返回：patch get_lightrag 返回 MagicMock，
    scan_and_sync step 0（sync.py L296-304）不再提前 return (-1,-1,-1)，
    也不触发真实 LightRAG 初始化（model 加载/真实 ~/.niu 存储）。
    ghost 清理（sync.py L370-413）经 LightRAGAdapter.list_entities，
    需 mock 返回空列表保证确定性。
    注意：get_lightrag 是函数级 import（`from niu_api.internal.lightrag_manager
    import get_lightrag`），patch 必须打在 lightrag_manager 模块上。
    """
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    monkeypatch.setattr(
        "niu_api.internal.lightrag_manager.get_lightrag",
        mock.MagicMock(return_value=mock.MagicMock()),
    )
    import niu_api.internal.lightrag_adapter as la
    monkeypatch.setattr(la, "LightRAGAdapter", lambda: mock.MagicMock(
        list_entities=mock.MagicMock(return_value={"status": "ok", "data": []}),
        has_entity=mock.MagicMock(return_value=False),
    ))
    with mock.patch("pathlib.Path.home", return_value=tmp_path):
        from agent.injector.sync import SkillSync
        sync = SkillSync(skills_dir=str(skills_dir), use_watchdog=False)
    sync._inject_skill_to_lightrag = mock.Mock(return_value=True)  # 绕过真实注入
    return sync, skills_dir


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_scan_detects_flat_and_directory_skills(fake_sync):
    """平铺 a.md + 目录式 dir1/SKILL.md 都被识别，name 分别=stem/目录名。"""
    sync, skills_dir = fake_sync
    (skills_dir / "a.md").write_text("# A\n", encoding="utf-8")
    (skills_dir / "dir1").mkdir()
    (skills_dir / "dir1" / "SKILL.md").write_text("# D1\n", encoding="utf-8")

    added, updated, deleted = sync.scan_and_sync()

    assert added == 2
    assert set(sync._last_scan) == {"a", "dir1"}
    # 目录式注入用目录名 + SKILL.md 全文（_sync_skill 调 _inject_skill_to_lightrag(name, content)）
    calls = [c.args[0] for c in sync._inject_skill_to_lightrag.call_args_list]
    assert "dir1" in calls
    assert "a" in calls


def test_scan_ignores_deep_non_skill_md(fake_sync):
    """下一级目录内非 SKILL.md 的深层 .md（如 references/x.md）不扫描。"""
    sync, skills_dir = fake_sync
    (skills_dir / "dir1" / "references").mkdir(parents=True)
    (skills_dir / "dir1" / "references" / "x.md").write_text("x\n", encoding="utf-8")

    sync.scan_and_sync()

    assert sync._last_scan == {}
    sync._inject_skill_to_lightrag.assert_not_called()


def test_scan_ignores_sub_sub_skill_md(fake_sync):
    """两级以上目录（dir/sub/SKILL.md）不扫描（仅支持下一级）。"""
    sync, skills_dir = fake_sync
    (skills_dir / "dir1" / "sub").mkdir(parents=True)
    (skills_dir / "dir1" / "sub" / "SKILL.md").write_text("# S\n", encoding="utf-8")

    sync.scan_and_sync()

    assert sync._last_scan == {}
    sync._inject_skill_to_lightrag.assert_not_called()


def test_directory_skill_update_uses_directory_path(fake_sync):
    """目录式 skill 内容变化 → 更新分支用 <dir>/SKILL.md 路径重注入（而非 dir1.md）。"""
    sync, skills_dir = fake_sync
    (skills_dir / "dir1").mkdir()
    sk = skills_dir / "dir1" / "SKILL.md"
    sk.write_text("# v1\n", encoding="utf-8")
    sync.scan_and_sync()
    assert "dir1" in sync._last_scan

    # 手动放入 state（模拟已同步），改内容触发更新
    # 更新分支先 _delete_skill_from_lightrag（真实调用会建 LightRAGAdapter），需 mock
    # 注：_save_state 只写 self._state_file（构造期已捕获路径），不查 Path.home，无需补丁
    sync._last_scan["dir1"] = _sha256(sk)
    sync._save_state()
    sync._inject_skill_to_lightrag.reset_mock()
    sk.write_text("# v2 changed\n", encoding="utf-8")

    with mock.patch.object(sync, "_delete_skill_from_lightrag", return_value=True):
        added, updated, deleted = sync.scan_and_sync()

    assert updated == 1
    assert sync._inject_skill_to_lightrag.call_args.args[0] == "dir1"
    assert sync._last_scan["dir1"] == _sha256(sk)


def test_directory_skill_delete_removes_entity(fake_sync):
    """目录式 skill 文件删除 → 从图谱删除（name=目录名）。"""
    sync, skills_dir = fake_sync
    (skills_dir / "dir1").mkdir()
    sk = skills_dir / "dir1" / "SKILL.md"
    sk.write_text("# D1\n", encoding="utf-8")
    sync.scan_and_sync()
    assert "dir1" in sync._last_scan

    sk.unlink()
    with mock.patch.object(sync, "_delete_skill_from_lightrag", return_value=True) as deleter:
        added, updated, deleted = sync.scan_and_sync()

    assert deleted == 1
    deleter.assert_called_once_with("dir1")
    assert "dir1" not in sync._last_scan


def test_same_name_flat_wins_over_directory(fake_sync):
    """同名冲突（foo.md 与 foo/SKILL.md 并存）：平铺优先——hash 源=平铺，
    注入内容=平铺（_skill_file_for_name 平铺优先），目录式被跳过。"""
    sync, skills_dir = fake_sync
    (skills_dir / "foo.md").write_text("# flat\n", encoding="utf-8")
    (skills_dir / "foo").mkdir()
    (skills_dir / "foo" / "SKILL.md").write_text("# dir\n", encoding="utf-8")

    added, updated, deleted = sync.scan_and_sync()

    assert added == 1
    assert set(sync._last_scan) == {"foo"}
    assert sync._inject_skill_to_lightrag.call_args.args[1] == "# flat\n"
    # 目录式内容未注入（hash 源平铺；目录式被跳过）
    assert sync._last_scan["foo"] == _sha256(skills_dir / "foo.md")


def test_skill_name_from_path_unit(fake_sync):
    """name 解析：平铺=stem、目录式=父目录名、深层/越界=None。"""
    sync, skills_dir = fake_sync
    assert sync._skill_name_from_path(skills_dir / "foo.md", skills_dir) == "foo"
    assert sync._skill_name_from_path(skills_dir / "dir1" / "SKILL.md", skills_dir) == "dir1"
    assert sync._skill_name_from_path(skills_dir / "dir1" / "sub" / "SKILL.md", skills_dir) is None
    assert sync._skill_name_from_path(skills_dir / "dir1" / "refs.md", skills_dir) is None
    assert sync._skill_name_from_path(Path("/elsewhere/x.md"), skills_dir) is None


def test_skill_name_from_path_symlink_flat(fake_sync):
    """指向目录外的符号链接平铺 skill 仍被识别（resolve 会解析到目录外误丢）。"""
    sync, skills_dir = fake_sync
    outside = skills_dir.parent / "shared-foo.md"
    outside.write_text("# shared\n", encoding="utf-8")
    link = skills_dir / "foo-link.md"
    link.symlink_to(outside)

    assert sync._skill_name_from_path(link, skills_dir) == "foo-link"


def test_add_branch_none_guard_retries(fake_sync):
    """新增分支 _skill_file_for_name 返回 None → continue 重试，不写 hash 不注入。"""
    sync, skills_dir = fake_sync
    (skills_dir / "ghost.md").write_text("# G\n", encoding="utf-8")

    with mock.patch.object(sync, "_skill_file_for_name", return_value=None):
        added, updated, deleted = sync.scan_and_sync()

    assert added == 0
    assert "ghost" not in sync._last_scan
    sync._inject_skill_to_lightrag.assert_not_called()


def test_update_branch_none_guard_keeps_old_hash(fake_sync):
    """修改分支 _skill_file_for_name 返回 None → 保留旧 hash 供下次重试。"""
    sync, skills_dir = fake_sync
    (skills_dir / "dir1").mkdir()
    sk = skills_dir / "dir1" / "SKILL.md"
    sk.write_text("# v1\n", encoding="utf-8")
    sync.scan_and_sync()
    old_hash = sync._last_scan["dir1"]
    # 首次扫描已注入（added=1），清掉以便断言第二次扫描未注入
    sync._inject_skill_to_lightrag.reset_mock()

    sk.write_text("# v2 changed\n", encoding="utf-8")
    with mock.patch.object(sync, "_skill_file_for_name", return_value=None):
        with mock.patch.object(sync, "_delete_skill_from_lightrag", return_value=True):
            added, updated, deleted = sync.scan_and_sync()

    assert updated == 0
    assert sync._last_scan["dir1"] == old_hash  # 保留旧 hash（含已删实体的状态，下次扫描重试删除后注入）
    sync._inject_skill_to_lightrag.assert_not_called()


def test_skill_file_for_name_unit(fake_sync):
    """_skill_file_for_name 三态：平铺文件 / 目录式 / 双缺失 None。"""
    sync, skills_dir = fake_sync
    (skills_dir / "plain.md").write_text("# P\n", encoding="utf-8")
    (skills_dir / "dir1").mkdir()
    (skills_dir / "dir1" / "SKILL.md").write_text("# D\n", encoding="utf-8")

    assert sync._skill_file_for_name("plain") == skills_dir / "plain.md"
    assert sync._skill_file_for_name("dir1") == skills_dir / "dir1" / "SKILL.md"
    assert sync._skill_file_for_name("nonexistent") is None

"""SkillSync 无变化不写盘测试。

验证 scan_and_sync 在 skills + notes 都无变化时，不调用 _save_state，
避免每分钟无意义重写 ~/.niu/skill_sync_state.json。
"""
import json
from unittest import mock

import pytest


@pytest.fixture
def fake_skill_sync(tmp_path):
    """构造一个轻量 SkillSync 实例，绕过 LightRAG 真实初始化。

    skills 目录有 1 个 skill 文件，state 文件已记录其 hash。
    """
    # 准备 skills 目录
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill_file = skills_dir / "test-skill.md"
    skill_file.write_text("# Test Skill\n", encoding="utf-8")

    # 计算 hash
    import hashlib
    content_hash = hashlib.sha256(skill_file.read_bytes()).hexdigest()

    # 准备 state 文件（已记录 hash，模拟"已同步"状态）
    # 注意：SkillSync._state_file = Path.home() / ".niu" / "skill_sync_state.json"
    # Patch Path.home() 返回 tmp_path 后，_state_file = tmp_path / ".niu" / "skill_sync_state.json"
    # 所以 fixture 必须写到 tmp_path / ".niu" / "skill_sync_state.json"，先建 .niu 子目录
    niu_dir = tmp_path / ".niu"
    niu_dir.mkdir(parents=True, exist_ok=True)
    state_file = niu_dir / "skill_sync_state.json"
    state_file.write_text(
        json.dumps({"test-skill": content_hash, "_notes": {}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Patch Path.home() 让 SkillSync 找到 tmp_path 下的 state_file
    with mock.patch("pathlib.Path.home", return_value=tmp_path):
        from agent.injector.sync import SkillSync
        sync = SkillSync(skills_dir=str(skills_dir), use_watchdog=False)

    return sync, content_hash


def test_scan_and_sync_no_write_when_unchanged(fake_skill_sync):
    """skills 和 notes 都无变化时，scan_and_sync 不调用 _save_state"""
    sync, _ = fake_skill_sync

    # Patch get_lightrag 返回非 None，绕过 LightRAG 不可用的提前返回
    with mock.patch("niu_api.internal.lightrag_manager.get_lightrag", return_value=mock.MagicMock()), \
         mock.patch.object(sync, "_save_state") as mock_save, \
         mock.patch.object(sync, "_sync_skill", return_value=True) as mock_sync, \
         mock.patch.object(sync, "_delete_skill_from_lightrag", return_value=True), \
         mock.patch("niu_api.internal.lightrag_adapter.LightRAGAdapter") as MockAdapter:
        # list_entities 返回空 list（没有 KG ghost）
        MockAdapter.return_value.list_entities.return_value = {
            "status": "ok", "data": []
        }

        added, updated, deleted = sync.scan_and_sync()

    # 无变化：added=0, updated=0, deleted=0
    assert added == 0 and updated == 0 and deleted == 0
    # _sync_skill 不应被调用（skill 已知且 hash 未变）
    mock_sync.assert_not_called()
    # 关键断言：_save_state 不应被调用
    assert not mock_save.called, \
        "skills 和 notes 都无变化时不应调 _save_state，但被调用了"


def test_scan_and_sync_notes_changed_writes_once(fake_skill_sync, tmp_path, monkeypatch):
    """notes 有变化时，scan_and_sync 只调用一次 _save_state（不双重写盘）"""
    sync, _ = fake_skill_sync

    # _scan_notes 读 WORKSPACE_PATH/notes/notes.json（sync.py L615-618）
    # 必须设 WORKSPACE_PATH 环境变量 + 写到对应路径，否则 _scan_notes 返回 (0, 0)
    monkeypatch.setenv("WORKSPACE_PATH", str(tmp_path))
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    notes_file = notes_dir / "notes.json"
    notes_file.write_text(
        json.dumps([{"id": "note1", "content": "test content", "tags": []}], ensure_ascii=False),
        encoding="utf-8",
    )

    with mock.patch("niu_api.internal.lightrag_manager.get_lightrag", return_value=mock.MagicMock()), \
         mock.patch.object(sync, "_save_state") as mock_save, \
         mock.patch.object(sync, "_sync_skill", return_value=True), \
         mock.patch.object(sync, "_delete_skill_from_lightrag", return_value=True), \
         mock.patch.object(sync, "_inject_note_to_lightrag", return_value=set()) as mock_inject_note, \
         mock.patch("niu_api.internal.lightrag_adapter.LightRAGAdapter") as MockAdapter:
        MockAdapter.return_value.list_entities.return_value = {"status": "ok", "data": []}
        MockAdapter.return_value.delete_document.return_value = {"status": "ok"}

        added, updated, deleted = sync.scan_and_sync()

    # notes 新增了 1 条
    assert added >= 1, f"应至少有 1 条 notes 新增，实际 added={added}"
    # 关键断言：_save_state 只调用一次（L741 删除后不再双重写）
    assert mock_save.call_count == 1, \
        f"notes 变化时应只写一次盘，实际写了 {mock_save.call_count} 次"
    # _inject_note_to_lightrag 被调用
    mock_inject_note.assert_called_once()


def test_scan_and_sync_watchdog_concurrent_write(tmp_path):
    """watchdog 在 scan 期间往 _last_scan 塞新条目时，scan_and_sync 出口写盘

    覆盖审查 Agent 指出的 C1 风险：watchdog 并发新增条目未同步成功时
    added=0，但 _last_scan 已被 watchdog 修改，出口对比应捕获并写盘。

    关键：fixture state 必须为空，让 scan 走"新增"路径调 _sync_skill，
    side_effect 才能真正触发（如果 state 含 test-skill hash，scan 走
    unchanged 路径不调 _sync_skill，side_effect 永不触发）。
    """

    # 准备 skills 目录（state 为空 → scan 走新增路径调 _sync_skill）
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill_file = skills_dir / "test-skill.md"
    skill_file.write_text("# Test Skill\n", encoding="utf-8")

    # state 文件：空（只含 _notes 键）
    # SkillSync._state_file = Path.home() / ".niu" / "skill_sync_state.json"
    # 必须写到 tmp_path / ".niu" / "skill_sync_state.json"，先建 .niu 子目录
    niu_dir = tmp_path / ".niu"
    niu_dir.mkdir(parents=True, exist_ok=True)
    state_file = niu_dir / "skill_sync_state.json"
    state_file.write_text(json.dumps({"_notes": {}}, ensure_ascii=False, indent=2), encoding="utf-8")

    with mock.patch("pathlib.Path.home", return_value=tmp_path):
        from agent.injector.sync import SkillSync
        sync = SkillSync(skills_dir=str(skills_dir), use_watchdog=False)

    # 模拟 watchdog 在 scan 期间往 _last_scan 塞新条目
    # （实际场景：watchdog 触发 _execute 往 _last_scan[name] 塞 hash）
    def fake_sync_skill(name, skill_file):
        with sync._lock:
            sync._last_scan["watchdog-concurrent-skill"] = "fake_watchdog_hash"
        return True

    with mock.patch("niu_api.internal.lightrag_manager.get_lightrag", return_value=mock.MagicMock()), \
         mock.patch.object(sync, "_save_state") as mock_save, \
         mock.patch.object(sync, "_sync_skill", side_effect=fake_sync_skill), \
         mock.patch.object(sync, "_delete_skill_from_lightrag", return_value=True), \
         mock.patch("niu_api.internal.lightrag_adapter.LightRAGAdapter") as MockAdapter:
        MockAdapter.return_value.list_entities.return_value = {"status": "ok", "data": []}

        added, updated, deleted = sync.scan_and_sync()

    # 关键断言 1：scan 走新增路径调了 _sync_skill（fixture state 空导致 test-skill 是新增）
    # fake_sync_skill 往 _last_scan 塞了 watchdog-concurrent-skill
    assert "watchdog-concurrent-skill" in sync._last_scan, \
        "watchdog 并发塞的条目应保留在 _last_scan"
    # 关键断言 2：出口对比捕获 _last_scan 变化（多了 watchdog-concurrent-skill），写盘
    assert mock_save.call_count >= 1, \
        f"watchdog 并发修改 _last_scan 时应写盘，实际 _save_state 调用 {mock_save.call_count} 次"


def test_scan_and_sync_ghost_cleanup_failure_writes(fake_skill_sync):
    """KG ghost skill 删除失败时往 next_scan 塞空字符串，scan_and_sync 写盘

    覆盖审查 Agent 指出的 C2 风险：ghost 删除失败时 next_scan[entity_name]=''，
    added/updated/deleted 可能全 0，但 next_scan 跟入口快照不同，出口对比应写盘。
    否则下次扫描还会再扫到 ghost 无限重试。

    fixture 用 fake_skill_sync（state 含 test-skill hash），scan 走 unchanged
    路径不调 _sync_skill/_delete_skill_from_lightrag，只触发 L372 ghost 路径。
    """
    sync, _ = fake_skill_sync

    with mock.patch("niu_api.internal.lightrag_manager.get_lightrag", return_value=mock.MagicMock()), \
         mock.patch.object(sync, "_save_state") as mock_save, \
         mock.patch.object(sync, "_sync_skill", return_value=True), \
         mock.patch.object(sync, "_delete_skill_from_lightrag", return_value=False), \
         mock.patch("niu_api.internal.lightrag_adapter.LightRAGAdapter") as MockAdapter:
        # KG 里有 1 个 ghost skill（磁盘上不存在）
        MockAdapter.return_value.list_entities.return_value = {
            "status": "ok",
            "data": [{"entity_name": "ghost-skill"}]
        }

        added, updated, deleted = sync.scan_and_sync()

    # 关键断言 1：ghost 删除失败时写盘（出口对比捕获 next_scan 塞了空值）
    assert mock_save.call_count >= 1, \
        f"ghost cleanup 失败塞空值时应写盘，实际 _save_state 调用 {mock_save.call_count} 次"
    # 关键断言 2：_last_scan 含 ghost-skill 空值条目
    assert "ghost-skill" in sync._last_scan, \
        "ghost 删除失败应塞空值到 _last_scan，下次扫描不再当新发现"
    assert sync._last_scan["ghost-skill"] == "", \
        f"ghost-skill 应是空字符串，实际 {sync._last_scan['ghost-skill']!r}"


"""目录式 Skills（<dir>/SKILL.md）扫描支持测试。

全 mock：patch get_lightrag（函数级 import 目标=lightrag_manager 模块）+
LightRAGAdapter 替换 + _inject_skill_to_lightrag mock，不碰真实 LightRAG/LLM。
"""
import hashlib
import json
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


def test_watchdog_execute_directory_skill(fake_sync):
    """watchdog 事件：<dir>/SKILL.md 增改 → name=目录名注入 + hash 键=目录名。"""
    sync, skills_dir = fake_sync
    (skills_dir / "dir1").mkdir()
    sk = skills_dir / "dir1" / "SKILL.md"
    sk.write_text("# v1\n", encoding="utf-8")

    from agent.injector.sync import SkillFileHandler
    handler = SkillFileHandler(sync, debounce=0)
    with mock.patch.object(sync, "_sync_skill", wraps=sync._sync_skill) as sync_skill:
        handler._execute(str(sk), "sync")
        sync_skill.assert_called_once_with("dir1", sk)
    assert sync._last_scan["dir1"] == _sha256(sk)
    assert sync._state_file.read_text(encoding="utf-8").find('"dir1"') != -1


def test_watchdog_execute_deep_md_ignored(fake_sync):
    """watchdog 事件：深层非 SKILL.md .md → 忽略（不注入不写 state）。"""
    sync, skills_dir = fake_sync
    (skills_dir / "dir1" / "references").mkdir(parents=True)
    deep = skills_dir / "dir1" / "references" / "x.md"
    deep.write_text("x\n", encoding="utf-8")

    from agent.injector.sync import SkillFileHandler
    handler = SkillFileHandler(sync, debounce=0)
    with mock.patch.object(sync, "_sync_skill") as sync_skill:
        handler._execute(str(deep), "sync")
        sync_skill.assert_not_called()
    assert sync._last_scan == {}


def test_watchdog_schedule_recursive(fake_sync):
    """watchdog 启动时 observer.schedule 必须 recursive=True（子目录事件可达性核心）。"""
    sync, skills_dir = fake_sync
    (skills_dir / "dir1").mkdir()
    (skills_dir / "dir1" / "SKILL.md").write_text("# v1\n", encoding="utf-8")

    from agent.injector import sync as sync_mod
    fake_observer = mock.MagicMock()
    fake_observer_class = mock.MagicMock(return_value=fake_observer)
    sync.use_watchdog = True  # fixture 构造时 use_watchdog=False，_start_watchdog 会提前 return
    with mock.patch.object(sync_mod, "Observer", fake_observer_class):
        sync._start_watchdog()

    fake_observer.schedule.assert_called_once()
    call_kwargs = fake_observer.schedule.call_args.kwargs
    assert call_kwargs.get("recursive") is True
    assert fake_observer.start.called


def test_watchdog_execute_conflict_flat_wins(fake_sync):
    """同名冲突时 watchdog 事件：目录副本 sync/delete 均被忽略（权威源=平铺），
    _sync_skill/_delete_skill_from_lightrag 不触发。"""
    sync, skills_dir = fake_sync
    (skills_dir / "foo.md").write_text("# flat\n", encoding="utf-8")
    (skills_dir / "foo").mkdir()
    sub = skills_dir / "foo" / "SKILL.md"
    sub.write_text("# dir\n", encoding="utf-8")

    from agent.injector.sync import SkillFileHandler
    handler = SkillFileHandler(sync, debounce=0)
    with mock.patch.object(sync, "_sync_skill") as sync_skill:
        handler._execute(str(sub), "sync")  # 目录副本 sync → 忽略
        sync_skill.assert_not_called()
    with mock.patch.object(sync, "_delete_skill_from_lightrag") as deleter:
        handler._execute(str(sub), "delete")  # 目录副本 delete → 忽略
        deleter.assert_not_called()
    assert sync._last_scan == {}


def test_skill_file_for_name_md_suffix_dir_shadow(fake_sync):
    """目录名以 .md 结尾（如 x.md/ 目录 + x/SKILL.md）不遮蔽目录式反查；
    仅有 .md 目录无 SKILL.md 时返回 None。is_file 修复的回归钉。"""
    sync, skills_dir = fake_sync
    # 遮蔽场景：x.md 是目录 + x/SKILL.md 目录式 skill
    (skills_dir / "x.md").mkdir()
    (skills_dir / "x").mkdir()
    (skills_dir / "x" / "SKILL.md").write_text("# X\n", encoding="utf-8")

    assert sync._skill_file_for_name("x") == skills_dir / "x" / "SKILL.md"

    # 只有 .md 目录、无平铺文件无目录式 skill
    (skills_dir / "only-dir.md").mkdir()
    assert sync._skill_file_for_name("only-dir") is None


def test_skill_display_path_dir_vs_flat(tmp_path):
    """路径行：目录式输出 <name>/SKILL.md，平铺输出 <name>.md，同名冲突平铺优先，
    .md 后缀目录不遮蔽，都不存在平铺兜底。

    S8 D-2.1 / I-8：输出基于**真实解析到的目录**，不再写死 `~/.niu/skills/`
    前缀——对输出做 expanduser 后必须命中磁盘上真实存在的该 skill 文件。
    """
    import os

    from agent.runner import _skill_display_path
    skills_dir = tmp_path / "skills"
    (skills_dir / "h3-prompt-writing").mkdir(parents=True)
    (skills_dir / "h3-prompt-writing" / "SKILL.md").write_text("# H3\n", encoding="utf-8")
    (skills_dir / "plain.md").write_text("# P\n", encoding="utf-8")

    def _render(name: str) -> str:
        """渲染并核验：禁止形态（写死默认前缀）+ 目录段真实 + 文件可命中。"""
        out = _skill_display_path(name, skills_dir)
        assert "~/.niu/skills" not in out
        expanded = os.path.expanduser(out)
        assert str(skills_dir) in expanded
        return expanded

    assert _render("h3-prompt-writing") == str(skills_dir / "h3-prompt-writing" / "SKILL.md")
    assert (skills_dir / "h3-prompt-writing" / "SKILL.md").is_file()
    assert _render("plain") == str(skills_dir / "plain.md")

    # 都不存在（旧数据/幽灵实体）：平铺形式兜底，且仍指向传入的真实目录
    assert _render("ghost-entity") == str(skills_dir / "ghost-entity.md")

    # 同名冲突（conflict.md 文件与 conflict/SKILL.md 并存）：平铺优先
    (skills_dir / "conflict.md").write_text("# CF\n", encoding="utf-8")
    (skills_dir / "conflict").mkdir()
    (skills_dir / "conflict" / "SKILL.md").write_text("# CD\n", encoding="utf-8")
    assert _render("conflict") == str(skills_dir / "conflict.md")

    # .md 后缀目录（dir.md/ 无平铺文件）不遮蔽目录式：返回 dir/SKILL.md
    (skills_dir / "dir.md").mkdir()
    (skills_dir / "dir").mkdir()
    (skills_dir / "dir" / "SKILL.md").write_text("# D\n", encoding="utf-8")
    assert _render("dir") == str(skills_dir / "dir" / "SKILL.md")


def test_skill_display_path_multi_dir_first_hit_wins(tmp_path):
    """I-7/I-8：渲染也按目录集合顺序解析——同名 skill 第一个所配目录为唯一来源，
    仅存在于第二目录的 skill 也能渲染出真实路径（而不是写死的默认目录）。"""
    import os

    from agent.runner import _skill_display_path
    dir1 = tmp_path / "dir1" / "skills"
    dir2 = tmp_path / "dir2" / "skills"
    (dir1 / "shared").mkdir(parents=True)
    (dir1 / "shared" / "SKILL.md").write_text("# from dir1\n", encoding="utf-8")
    (dir2 / "only-in-dir2").mkdir(parents=True)
    (dir2 / "only-in-dir2" / "SKILL.md").write_text("# from dir2\n", encoding="utf-8")
    (dir2 / "shared").mkdir(parents=True)
    (dir2 / "shared" / "SKILL.md").write_text("# from dir2\n", encoding="utf-8")

    dirs = [dir1, dir2]

    # 仅存于第二目录 → 渲染真实落在 dir2（v1.0 会写死 ~/.niu/skills 而失效）
    out = _skill_display_path("only-in-dir2", skills_dirs=dirs)
    assert os.path.expanduser(out) == str(dir2 / "only-in-dir2" / "SKILL.md")

    # 同名冲突跨目录：第一个所配目录（dir1）为唯一来源
    out = _skill_display_path("shared", skills_dirs=dirs)
    assert os.path.expanduser(out) == str(dir1 / "shared" / "SKILL.md")


def test_skill_display_path_default_dir_keeps_tilde(tmp_path):
    """默认单目录装配下输出仍带 `~` 前缀（守护：逐字节等价，不因真实化改写）。

    目录来源用运行中 SkillSync 实例的 skills_dirs（runner 渲染的取值路径），
    不触探真实 user-config。
    """
    from unittest import mock as _mock

    from agent.runner import _skill_display_path
    home = tmp_path / "home"
    (home / ".niu" / "skills").mkdir(parents=True)
    (home / ".niu" / "skills" / "python.md").write_text("# P\n", encoding="utf-8")

    instance = _mock.MagicMock()
    instance.skills_dirs = [home / ".niu" / "skills"]
    with _mock.patch("pathlib.Path.home", return_value=home), \
         _mock.patch("agent.runner.get_skill_sync", return_value=instance):
        # 实例目录集合（= 默认单目录）→ 平铺形态，带 ~ 前缀
        assert _skill_display_path("python") == "~/.niu/skills/python.md"
        # 显式传同一目录 → 同上（逐字节等价）
        assert _skill_display_path("python", skills_dir=home / ".niu" / "skills") == "~/.niu/skills/python.md"
        # 幽灵实体兜底形态同样带 ~
        assert _skill_display_path("ghost-entity") == "~/.niu/skills/ghost-entity.md"


# ============== 多 skills 目录（S8 D-2 / I-7 / I-9） ==============


def _multi_dir(fake_sync, tmp_path):
    """把 fixture 的 SkillSync 改组为「dir1, dir2」双目录装配。

    直接改 skills_dirs/skills_dir 是为复用 fixture 已 patch 好的
    get_lightrag / LightRAGAdapter 环境；构造入参路径另由
    test_skills_dirs_constructor_param 覆盖。
    """
    sync, _ = fake_sync
    dir1 = tmp_path / "std" / "one"
    dir2 = tmp_path / "std" / "two"
    for d in (dir1, dir2):
        d.mkdir(parents=True)
    sync.skills_dirs = [dir1, dir2]
    sync.skills_dir = dir1
    return sync, dir1, dir2


def test_skills_dirs_constructor_param(tmp_path, monkeypatch):
    """skills_dirs 构造入参 → 保序去重集合；skills_dir 单目录入参仍受支持（R-5）。"""
    from agent.injector.sync import SkillSync
    # 不依赖开发机真实 user-config：指向一个不存在的路径 → 走缺省集合
    monkeypatch.setattr("niu_api.config.CONFIG_PATH", str(tmp_path / "no-such-config.json"))
    d1 = tmp_path / "a"
    d2 = tmp_path / "b"
    for d in (d1, d2):
        d.mkdir()
    with mock.patch("pathlib.Path.home", return_value=tmp_path):
        sync = SkillSync(skills_dirs=[str(d1), str(d2), str(d1)], use_watchdog=False)
        assert sync.skills_dirs == [d1, d2]  # 保序去重
        assert sync.skills_dir == d1  # 兼容视图 = 第一个目录
        # 既有调用方以字符串构造单目录（改签名会 TypeError，R-5）
        sync2 = SkillSync(skills_dir=str(d2), use_watchdog=False)
        assert sync2.skills_dirs == [d2]
        assert sync2.skills_dir == d2
        # 缺省集合：仅 ~/.niu/skills（不凭空改变装机行为）
        assert SkillSync(use_watchdog=False).skills_dirs == [tmp_path / ".niu" / "skills"]


def test_configured_skills_dirs_reads_user_config(tmp_path, monkeypatch):
    """skills.dirs 配置段：缺省仅 ~/.niu/skills；显式配置按顺序生效；
    单字符串/坏 JSON/类型错 → 回缺省（每次现读，热生效）。"""
    import json
    import os

    from agent.injector.sync import configured_skills_dirs
    cfg = tmp_path / "user-config.json"
    monkeypatch.setattr("niu_api.config.CONFIG_PATH", str(cfg))
    home = tmp_path / "home"
    with mock.patch("pathlib.Path.home", return_value=home):
        # 文件不存在 → 缺省
        assert configured_skills_dirs() == [str(home / ".niu" / "skills")]

    # 配置里的 ~ 按 os.path.expanduser 展开（与实现同口径，跨平台）
    cfg.write_text(json.dumps({"skills": {"dirs": ["~/claude/skills", "/abs/std/skills"]}}), encoding="utf-8")
    assert configured_skills_dirs() == [
        str(Path(os.path.expanduser("~")) / "claude" / "skills"),
        "/abs/std/skills",
    ]

    # 单字符串也接受
    cfg.write_text(json.dumps({"skills": {"dirs": "~/codex/skills"}}), encoding="utf-8")
    assert configured_skills_dirs() == [os.path.expanduser("~/codex/skills")]

    # 坏 JSON / 类型错 / 空段 → 缺省
    for bad in ("{not json", json.dumps({"skills": {"dirs": 42}}), json.dumps({"skills": {}})):
        cfg.write_text(bad, encoding="utf-8")
        with mock.patch("pathlib.Path.home", return_value=home):
            assert configured_skills_dirs() == [str(home / ".niu" / "skills")]


def test_multi_dir_scan_injects_skill_from_second_dir(fake_sync, tmp_path):
    """I-9：仅存在于第二所配目录的 skill 走通 扫描→反查→注入 全链
    （三处单目录锚点都改成按目录集合解析后，不再「扫到却永不注入」）。"""
    sync, dir1, dir2 = _multi_dir(fake_sync, tmp_path)
    (dir1 / "only-in-one.md").write_text("# one\n", encoding="utf-8")
    (dir2 / "only-in-two").mkdir()
    (dir2 / "only-in-two" / "SKILL.md").write_text("# two\n", encoding="utf-8")

    added, _, _ = sync.scan_and_sync()

    assert added == 2
    assert set(sync._last_scan) == {"only-in-one", "only-in-two"}
    # 反查：第二目录的目录式 skill 可被定位（_skill_file_for_name 跨目录）
    assert sync._skill_file_for_name("only-in-two") == dir2 / "only-in-two" / "SKILL.md"
    # 事件解析：目录外路径仍为 None，第二目录内路径解析出目录名
    assert sync._skill_name_for_event(dir2 / "only-in-two" / "SKILL.md") == "only-in-two"
    assert sync._skill_name_for_event(dir1 / "only-in-one.md") == "only-in-one"
    assert sync._skill_name_for_event(tmp_path / "outside" / "x.md") is None
    injected = {c.args[0]: c.args[1] for c in sync._inject_skill_to_lightrag.call_args_list}
    assert injected["only-in-two"] == "# two\n"


def test_multi_dir_same_name_first_dir_wins(fake_sync, tmp_path):
    """I-7：跨目录同名——第一个所配目录为唯一来源，hash 源与内容源同目录
    （杜绝「A 目录写 hash、B 目录内容被反查到」的撕裂）。"""
    sync, dir1, dir2 = _multi_dir(fake_sync, tmp_path)
    (dir1 / "dup.md").write_text("# from dir1\n", encoding="utf-8")
    (dir2 / "dup.md").write_text("# from dir2\n", encoding="utf-8")
    (dir2 / "dup").mkdir()
    (dir2 / "dup" / "SKILL.md").write_text("# dir2 目录式\n", encoding="utf-8")

    added, _, _ = sync.scan_and_sync()

    assert added == 1
    assert sync._skill_file_for_name("dup") == dir1 / "dup.md"
    contents = [c.args[1] for c in sync._inject_skill_to_lightrag.call_args_list]
    assert contents == ["# from dir1\n"]
    assert sync._last_scan["dup"] == _sha256(dir1 / "dup.md")


def test_multi_dir_watchdog_event_in_second_dir(fake_sync, tmp_path):
    """I-9：第二目录的文件事件也能解析出 name 并通过权威源自检后注入。"""
    sync, dir1, dir2 = _multi_dir(fake_sync, tmp_path)
    (dir2 / "evt").mkdir()
    sk = dir2 / "evt" / "SKILL.md"
    sk.write_text("# evt\n", encoding="utf-8")

    from agent.injector.sync import SkillFileHandler
    handler = SkillFileHandler(sync, debounce=0)
    with mock.patch.object(sync, "_sync_skill", wraps=sync._sync_skill) as sync_skill:
        handler._execute(str(sk), "sync")
        sync_skill.assert_called_once_with("evt", sk)
    assert sync._last_scan["evt"] == _sha256(sk)


def test_scan_gate_any_dir_exists(fake_sync, tmp_path):
    """两处存在性门（S8 §5.2）：任一所配目录存在即扫；全缺失才返回 (0,0,0)。"""
    sync, _ = fake_sync
    missing = tmp_path / "default-missing"
    present = tmp_path / "standard"
    present.mkdir()
    (present / "here.md").write_text("# here\n", encoding="utf-8")

    # 默认目录缺失 + 标准目录存在 → 仍扫描
    sync.skills_dirs = [missing, present]
    sync.skills_dir = missing
    added, _, _ = sync.scan_and_sync()
    assert added == 1

    # 两者都不存在 → 原行为：告警并返回 (0,0,0)，不触碰目录集合
    sync.skills_dirs = [tmp_path / "nope1", tmp_path / "nope2"]
    sync.skills_dir = tmp_path / "nope1"
    assert sync.scan_and_sync() == (0, 0, 0)


def test_watchdog_schedules_all_existing_dirs(fake_sync, tmp_path):
    """watchdog 按目录集合逐个 schedule（recursive=True）；全缺失则不启动。"""
    sync, _ = fake_sync
    d1 = tmp_path / "w1"
    d2 = tmp_path / "w2"
    d3 = tmp_path / "w3-missing"
    d1.mkdir()
    d2.mkdir()
    sync.skills_dirs = [d1, d2, d3]
    sync.skills_dir = d1
    sync.use_watchdog = True  # fixture 构造时 False，_start_watchdog 会提前 return

    from agent.injector import sync as sync_mod
    fake_observer = mock.MagicMock()
    with mock.patch.object(sync_mod, "Observer", mock.MagicMock(return_value=fake_observer)):
        sync._start_watchdog()

    scheduled = [c.args[1] for c in fake_observer.schedule.call_args_list]
    assert scheduled == [str(d1), str(d2)]  # 只调度存在的目录
    assert all(c.kwargs.get("recursive") is True for c in fake_observer.schedule.call_args_list)
    assert fake_observer.start.called

    # 全缺失 → 不启动 observer（原行为）
    sync._observer = None
    sync.skills_dirs = [d3]
    sync.skills_dir = d3
    fake_observer.reset_mock()
    with mock.patch.object(sync_mod, "Observer", mock.MagicMock(return_value=fake_observer)):
        sync._start_watchdog()
    fake_observer.schedule.assert_not_called()
    assert sync._observer is None


def test_inject_skill_passes_empty_chunks(tmp_path, monkeypatch):
    """I-1/I-2：skill 注入不再拼正文 chunk（chunks=[]）；实体 description
    的构造逻辑逐字节不变（与 frontmatter+触发词/标签公式一致）。"""
    content = (
        "---\n"
        "description: 用于测试的描述\n"
        "triggers: [alpha, beta]\n"
        "---\n\n"
        "# My Skill\n\n"
        "正文不应进入 chunks。\n"
    )
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    monkeypatch.setattr(
        "niu_api.internal.lightrag_manager.get_lightrag",
        mock.MagicMock(return_value=mock.MagicMock()),
    )
    import niu_api.internal.lightrag_adapter as la
    monkeypatch.setattr(
        la, "LightRAGAdapter",
        lambda: mock.MagicMock(has_entity=mock.MagicMock(return_value=False)),
    )
    with mock.patch("pathlib.Path.home", return_value=tmp_path):
        from agent.injector.sync import SkillSync
        sync = SkillSync(skills_dir=str(skills_dir), use_watchdog=False)

    ingester = mock.MagicMock()
    ingester.inject_custom_kg.return_value = {"status": "ok"}
    with mock.patch.object(la, "LightRAGIngester", return_value=ingester):
        assert sync._inject_skill_to_lightrag("my-skill", content) is True

    kwargs = ingester.inject_custom_kg.call_args.kwargs
    # I-1：不传正文（空列表由 adapter 生成 "{name}: {desc}" 虚拟块，I-3）
    assert kwargs["chunks"] == []

    # I-2：description 构造逐字节等价（复算公式比对，不依赖 set 顺序）
    from agent.injector.sync import (
        extract_description,
        extract_tags,
        extract_triggers,
        parse_yaml_frontmatter,
    )
    fm = parse_yaml_frontmatter(content)
    triggers = extract_triggers(content, fm)
    tags = extract_tags(content, fm, triggers)
    parts = []
    if triggers:
        parts.append(f"触发词: {', '.join(triggers)}")
    if tags:
        parts.append(f"标签: {', '.join(tags)}")
    expected_desc = extract_description(content, fm)
    if parts:
        expected_desc += " | " + "; ".join(parts)

    entity = kwargs["entities"][0]
    assert entity["entity_name"] == "my-skill"
    assert entity["entity_type"] == "Skill"
    assert entity["description"] == expected_desc
    assert entity["source_id"] == "skill://my-skill"
    assert entity["file_path"] == "skill_sync"

    rel = kwargs["relationships"][0]
    assert rel["src_id"] == "知识体系脑区"
    assert rel["tgt_id"] == "my-skill"
    assert rel["keywords"] == "包含"
    assert rel["description"] == "my-skill 属于知识体系"
    assert rel["source_id"] == "skill://my-skill"
    assert kwargs["source_id"] == "skill://my-skill"

    # 禁止形态：任何注入载荷里都不得出现正文片段（不论 chunk 还是实体/关系）
    payload = repr(kwargs)
    assert "正文不应进入 chunks" not in payload
    assert "Skill: my-skill" not in payload
    assert "# My Skill" not in payload


# ============== 状态写盘顺序（磁盘唯一真相，回归） ==============


def test_scan_persists_new_skills_to_state_file(fake_sync, tmp_path):
    """回归：scan_and_sync 必须把**本轮 next_scan**落盘。

    旧顺序先 `_save_state()` 再 `self._last_scan = next_scan`，写的是尚未提交
    的旧内存，磁盘永远缺本轮新增/更新/删除（现场：ego-browser 已在 KG，
    ~/.niu/skill_sync_state.json 却无条目），进程重启后又当新增重注。
    """
    sync, skills_dir = fake_sync
    flat = skills_dir / "ego-browser.md"
    flat.write_text("# ego browser\n", encoding="utf-8")
    (skills_dir / "dir-skill").mkdir()
    dir_sk = skills_dir / "dir-skill" / "SKILL.md"
    dir_sk.write_text("# dir skill\n", encoding="utf-8")

    added, updated, deleted = sync.scan_and_sync()

    assert (added, updated, deleted) == (2, 0, 0)
    # 内存已推进
    assert sync._last_scan["ego-browser"] == _sha256(flat)
    assert sync._last_scan["dir-skill"] == _sha256(dir_sk)
    # 关键断言：磁盘 state.json 同步含两个新条目（含 hash）+ notes 子状态
    data = json.loads(sync._state_file.read_text(encoding="utf-8"))
    assert data["ego-browser"] == _sha256(flat)
    assert data["dir-skill"] == _sha256(dir_sk)
    assert data["_notes"] == {}
    # 重启语义：新实例从同一磁盘 state.json 加载后不会把这两个 skill 当新增重注
    with mock.patch("pathlib.Path.home", return_value=tmp_path):
        reloaded = type(sync)(skills_dir=str(skills_dir), use_watchdog=False)
    assert reloaded._last_scan["ego-browser"] == _sha256(flat)
    assert reloaded._last_scan["dir-skill"] == _sha256(dir_sk)


def test_scan_save_failure_does_not_advance_memory(fake_sync, monkeypatch):
    """写盘失败（_save_state 返回 False）→ 内存 _last_scan 不推进，下轮重试。"""
    sync, skills_dir = fake_sync
    sk = skills_dir / "will-retry.md"
    sk.write_text("# retry\n", encoding="utf-8")

    saved_args = []

    def failing_save(scan_state=None):
        saved_args.append(scan_state)
        return False

    monkeypatch.setattr(sync, "_save_state", failing_save)

    added, _, _ = sync.scan_and_sync()

    assert added == 1  # 注入成功但状态没能落盘
    assert "will-retry" not in sync._last_scan
    assert not sync._state_file.exists()
    # 传给 _save_state 的是 next_scan（含新条目），不是旧内存
    assert len(saved_args) == 1 and saved_args[0] is not None
    assert saved_args[0].get("will-retry") == _sha256(sk)


def test_save_state_writes_given_dict_and_reports_oserror(fake_sync, tmp_path):
    """`_save_state(scan_state)` 写传入 dict（不动内存）、带 `_notes`、返回 True；
    OSError → 返回 False；无参调用仍写当前内存（watchdog 路径语义不变）。"""
    sync, _ = fake_sync
    sync._last_notes_scan = {"note-1": "h1"}

    assert sync._save_state({"pending": "pend-hash"}) is True
    data = json.loads(sync._state_file.read_text(encoding="utf-8"))
    assert data == {"pending": "pend-hash", "_notes": {"note-1": "h1"}}
    assert sync._last_scan == {}  # 未提交内存，由调用方决定

    with sync._lock:
        sync._last_scan = {"mem-only": "mem-hash"}
    assert sync._save_state() is True  # 无参 → 写当前内存
    data = json.loads(sync._state_file.read_text(encoding="utf-8"))
    assert data == {"mem-only": "mem-hash", "_notes": {"note-1": "h1"}}

    # 写盘失败：state_file 的父路径是普通文件 → mkdir 抛 OSError → False
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir", encoding="utf-8")
    sync._state_file = blocker / "skill_sync_state.json"
    assert sync._save_state({"x": "y"}) is False

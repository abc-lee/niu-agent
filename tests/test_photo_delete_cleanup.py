"""T2：cleanup_deleted_photos 接线两项新清理（A 类 chunk / 媒体向量；B 类关系虚拟块已随 F-3 T0 回退）。

隔离面（仿 test_photo_media_ingest.py 的 photo_env 模式）：
- 照片库：``NIU_DB_PATH`` 指向临时目录，并复位模块级连接缓存
  ``niu_photo_server._conn`` / ``_db_path_failed``（get_connection 只在
  ``_conn is None`` 时重连，不复位会指向真实 ``~/.niu/work/photos.db``）。
- 工作区：``WORKSPACE_PATH`` 指向临时目录。
- 媒体库：``NIU_MEDIA_STORE`` 环境变量指向不存在的临时路径（``media_store_path()``
  每次现读、无缓存 ⇒ 天然重定向），并属性补丁 ``media_store_path`` 指向同一路径
  作双保险——绝不触碰 ``~/.niu`` 真实存储。
- 打桩对象 = adapter 类（类级 patch ``niu_api.internal.lightrag_adapter.LightRAGAdapter``，
  函数内惰性 import 在调用时现读模块属性 ⇒ 补丁生效）；KG 实体/边删除走
  ``agent.tool_registry.get_registry``（与 test_lightrag_unified.py 同缝）。
- 组 ④ 用例**不打桩** ``media_vectors.remove``，真跑 D-4 口径：键不存在 ⇒ 不落盘、
  不创建文件、不计错。

用例：
① 两项新清理全部被调用，且参数正确：delete_chunks_by_source 收**归一化路径**
   （正斜杠+小写）、media_vectors.remove 收照片 stem；成功数累进返回体两个
   新计数字段；kg_errors 为空。
② 任一失败 ⇒ 进 kg_errors、status 不翻转、计数不累加（remove 的 False 不计错，
   True 照常计数）。
③ 早退分支（无照片可删）两个新计数字段同为 0，绝不触碰 KG registry。
④ 真跑 remove：库文件不存在且键不存在 ⇒ 返回 False、**不产生新文件**、不计错。
⑤ 真跑 remove：键存在 ⇒ 删除落盘（原子重写），其余条目保留，计数 +1。
⑥ 逐照片异常隔离：第一张待删照片的 remove 抛硬异常 ⇒ 第二张的两项新清理仍执行，
   异常条目进 kg_errors，status 不翻转。
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 让 niu_photo_server 模块可导入（其源码在 mcp-servers/photo-server/src 下）
_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "mcp-servers", "photo-server", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import niu_api.internal.media_vectors as mv  # noqa: E402
import niu_photo_server  # noqa: E402


def _make_registry(tool_map):
    """registry.get(name) → tool_map.get(name) 的 mock registry（未列出的工具返回 None）。"""
    mock_registry = MagicMock()
    mock_registry.get.side_effect = lambda name: tool_map.get(name)
    return mock_registry


def _reset_photo_module_state():
    """模块级缓存必须测试间复位：连接缓存不复位会指向真实库，
    会话缓存不复位会让用例串状态。"""
    niu_photo_server._conn = None
    niu_photo_server._db_path_failed = False
    niu_photo_server._ingest_sessions.clear()


@pytest.fixture()
def cleanup_env(tmp_path, monkeypatch):
    """全套隔离面：临时照片库/工作区 + 媒体库双保险重定向（环境变量 + 属性补丁）。"""
    _reset_photo_module_state()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_file = tmp_path / "photos.db"
    store_file = tmp_path / "media" / "media_vectors.json"
    store_file.parent.mkdir()

    monkeypatch.setenv("NIU_DB_PATH", str(db_file))
    monkeypatch.setenv("WORKSPACE_PATH", str(workspace))
    # 媒体库隔离：环境变量为主（media_store_path 每次现读），属性补丁双保险
    monkeypatch.setenv("NIU_MEDIA_STORE", str(store_file))
    monkeypatch.setattr(mv, "media_store_path", lambda: store_file)

    yield {
        "tmp_path": tmp_path,
        "workspace": workspace,
        "db_file": db_file,
        "store_file": store_file,
    }
    _reset_photo_module_state()


def _seed(cleanup_env):
    """李磊出现在两张照片（一张文件已删、一张还在）；未命名人物_9 只在已删照片里。
    返回 (已删照片路径, 保留照片路径)。"""
    kept = cleanup_env["tmp_path"] / "IMG_20240102.jpg"
    kept.write_bytes(b"\xff\xd8\xff\xdb" + b"\x00" * 32)
    conn = niu_photo_server.get_connection()
    conn.execute(
        "INSERT INTO persons (id, name, auto_label, photo_count) VALUES ('p_lilei', '李磊', 'auto_lilei', 2)"
    )
    conn.execute(
        "INSERT INTO persons (id, name, auto_label, photo_count) VALUES ('p_unnamed', NULL, '未命名人物_9', 1)"
    )
    deleted = cleanup_env["tmp_path"] / "IMG_20240101.jpg"  # 不落盘 ⇒ 文件不存在
    conn.execute(
        "INSERT INTO photos (id, file_path, abstract, kg_synced) VALUES ('ph_deleted', ?, '旧照片', 1)",
        (str(deleted),),
    )
    conn.execute(
        "INSERT INTO photos (id, file_path, abstract, kg_synced) VALUES ('ph_keep', ?, '还在的照片', 1)",
        (str(kept),),
    )
    conn.execute("INSERT INTO faces (id, photo_id, person_id) VALUES ('f1', 'ph_deleted', 'p_lilei')")
    conn.execute("INSERT INTO faces (id, photo_id, person_id) VALUES ('f2', 'ph_deleted', 'p_unnamed')")
    conn.execute("INSERT INTO faces (id, photo_id, person_id) VALUES ('f3', 'ph_keep', 'p_lilei')")
    conn.commit()
    return deleted, kept


def _expected_stem(file_path: Path) -> str:
    """绑定键：落盘路径归一化（正斜杠+小写）后的 stem，与 format_photo_ingest_data 同一把键。"""
    return Path(str(file_path).replace("\\", "/").lower()).stem


# ---------------------------------------------------------------------------
# ① 三件事全部被调用，参数正确（全 mock 面）
# ---------------------------------------------------------------------------


@patch("niu_api.internal.media_vectors.remove", return_value=True)
@patch("niu_api.internal.lightrag_adapter.LightRAGAdapter")
@patch("agent.tool_registry.get_registry")
def test_cleanup_invokes_two_new_cleanups_with_correct_args(
    mock_get_registry, mock_adapter_cls, mock_remove, cleanup_env
):
    """删一张照片 ⇒ 两项新清理全部被调用，且参数各自正确；成功数累进返回体新字段。"""
    deleted, _kept = _seed(cleanup_env)
    mock_get_registry.return_value = _make_registry({
        "lightrag-server/lightrag_delete_entity": MagicMock(return_value={"status": "ok"}),
        "lightrag-server/lightrag_delete_relation": MagicMock(return_value={"status": "ok"}),
    })
    adapter = mock_adapter_cls.return_value
    adapter.delete_chunks_by_source.return_value = {
        "status": "ok", "deleted_chunks": 3, "deleted_vectors": 3, "matched_ids": ["a1", "a2", "a3"],
    }

    result = niu_photo_server.cleanup_deleted_photos()

    stem = _expected_stem(deleted)
    # ① 两项新清理全部被调用（各一次：只有一张已删照片）
    assert adapter.delete_chunks_by_source.call_count == 1
    assert mock_remove.call_count == 1
    # A 类：收到的是归一化路径（正斜杠+小写），不是 DB 里的原始路径
    assert adapter.delete_chunks_by_source.call_args.args[0] == str(deleted).replace("\\", "/").lower()
    # 媒体向量：收到照片 stem
    assert mock_remove.call_args.args[0] == stem
    # 返回体：两个新计数字段按成功数累加，无失败、status 不翻转
    assert result["status"] == "success"
    assert result["deleted_photos"] == 1
    assert result["kg_errors"] == []
    assert result["kg_chunks_deleted"] == 3
    assert "kg_vectors_deleted" not in result
    assert result["media_vectors_removed"] == 1


# ---------------------------------------------------------------------------
# ② 任一失败 ⇒ kg_errors 记录、status 不翻转、计数不累加
# ---------------------------------------------------------------------------


@patch("niu_api.internal.media_vectors.remove", return_value=True)
@patch("niu_api.internal.lightrag_adapter.LightRAGAdapter")
@patch("agent.tool_registry.get_registry")
def test_new_cleanup_failure_goes_to_kg_errors_without_flipping_status(
    mock_get_registry, mock_adapter_cls, mock_remove, cleanup_env
):
    """A 类清理失败 ⇒ 进 kg_errors；status 仍 success、计数为 0；
    remove 返回 True 照常计数（False 才是本无、不计错，见用例 ④）。"""
    _deleted, _kept = _seed(cleanup_env)
    mock_get_registry.return_value = _make_registry({
        "lightrag-server/lightrag_delete_entity": MagicMock(return_value={"status": "ok"}),
        "lightrag-server/lightrag_delete_relation": MagicMock(return_value={"status": "ok"}),
    })
    adapter = mock_adapter_cls.return_value
    adapter.delete_chunks_by_source.return_value = {"status": "error", "message": "text_chunks 未初始化"}

    result = niu_photo_server.cleanup_deleted_photos()

    assert result["status"] == "success", "新清理失败不翻转 DB 清理结果"
    assert result["deleted_photos"] == 1
    assert result["kg_chunks_deleted"] == 0, "失败不计数"
    assert "kg_vectors_deleted" not in result
    assert result["media_vectors_removed"] == 1, "remove 成功仍照常计数"
    assert any("text_chunks 未初始化" in e for e in result["kg_errors"])


# ---------------------------------------------------------------------------
# ③ 早退分支：两个新计数字段同为 0
# ---------------------------------------------------------------------------


def test_early_return_has_zero_new_fields(cleanup_env):
    """无缺失照片 ⇒ 早退，绝不触碰 KG registry/adapter/媒体库，两个新计数字段为 0。"""
    kept = cleanup_env["tmp_path"] / "IMG_20240102.jpg"
    kept.write_bytes(b"\xff\xd8\xff\xdb" + b"\x00" * 32)
    conn = niu_photo_server.get_connection()
    conn.execute(
        "INSERT INTO photos (id, file_path, abstract, kg_synced) VALUES ('ph_keep', ?, '还在的照片', 1)",
        (str(kept),),
    )
    conn.commit()

    with patch("agent.tool_registry.get_registry", side_effect=AssertionError("无删除照片时不得触碰 KG")):
        result = niu_photo_server.cleanup_deleted_photos()

    assert result["deleted_photos"] == 0
    assert result["kg_errors"] == []
    assert result["kg_chunks_deleted"] == 0
    assert "kg_vectors_deleted" not in result
    assert result["media_vectors_removed"] == 0
    assert not cleanup_env["store_file"].exists(), "早退不得触碰媒体库"


# ---------------------------------------------------------------------------
# ④⑤ 真跑 media_vectors.remove（不打桩）：D-4 口径实测
# ---------------------------------------------------------------------------


@patch("niu_api.internal.lightrag_adapter.LightRAGAdapter")
@patch("agent.tool_registry.get_registry")
def test_real_remove_absent_key_creates_no_file(mock_get_registry, mock_adapter_cls, cleanup_env):
    """库文件不存在且键不存在 ⇒ remove 返回 False、不落盘、**不产生新文件**、不计错。"""
    _deleted, _kept = _seed(cleanup_env)
    assert not cleanup_env["store_file"].exists()
    mock_get_registry.return_value = _make_registry({
        "lightrag-server/lightrag_delete_entity": MagicMock(return_value={"status": "ok"}),
        "lightrag-server/lightrag_delete_relation": MagicMock(return_value={"status": "ok"}),
    })
    adapter = mock_adapter_cls.return_value
    adapter.delete_chunks_by_source.return_value = {
        "status": "ok", "deleted_chunks": 1, "deleted_vectors": 1, "matched_ids": ["a1"],
    }

    result = niu_photo_server.cleanup_deleted_photos()

    assert result["status"] == "success"
    assert result["kg_errors"] == [], "remove 返回 False = 本就没有，不得计入失败"
    assert result["media_vectors_removed"] == 0
    assert not cleanup_env["store_file"].exists(), "键不存在不得创建新库文件"
    assert mv.count() == 0


@patch("niu_api.internal.lightrag_adapter.LightRAGAdapter")
@patch("agent.tool_registry.get_registry")
def test_real_remove_existing_entry_deleted_others_kept(mock_get_registry, mock_adapter_cls, cleanup_env):
    """键存在 ⇒ 真 remove 删掉该条目并原子落盘，其余条目保留，计数 +1。"""
    deleted, _kept = _seed(cleanup_env)
    stem = _expected_stem(deleted)
    mv.add(stem, "image", [0.1] * 768)
    mv.add("other_photo", "image", [0.2] * 768)
    assert cleanup_env["store_file"].exists()

    mock_get_registry.return_value = _make_registry({
        "lightrag-server/lightrag_delete_entity": MagicMock(return_value={"status": "ok"}),
        "lightrag-server/lightrag_delete_relation": MagicMock(return_value={"status": "ok"}),
    })
    adapter = mock_adapter_cls.return_value
    adapter.delete_chunks_by_source.return_value = {
        "status": "ok", "deleted_chunks": 1, "deleted_vectors": 1, "matched_ids": ["a1"],
    }

    result = niu_photo_server.cleanup_deleted_photos()

    assert result["status"] == "success"
    assert result["kg_errors"] == []
    assert result["media_vectors_removed"] == 1
    assert mv.has(stem) is False, "该照片的媒体向量已被移除"
    assert mv.has("other_photo") is True, "无关条目不得被误删"
    assert mv.count() == 1
    assert cleanup_env["store_file"].exists(), "删后库文件仍存在（原子重写，非删除文件）"

# ---------------------------------------------------------------------------
# ⑥ 逐照片异常隔离：一张照片 remove 硬异常不得中断其余照片的五步清理
# ---------------------------------------------------------------------------


@patch("niu_api.internal.media_vectors.remove")
@patch("niu_api.internal.lightrag_adapter.LightRAGAdapter")
@patch("agent.tool_registry.get_registry")
def test_per_photo_remove_exception_does_not_abort_remaining_photos(
    mock_get_registry, mock_adapter_cls, mock_remove, cleanup_env
):
    """两张待删照片（文件均不存在）；remove 在第一张照片抛 RuntimeError ⇒ 第二张照片
    的两项新清理仍执行（A 类 adapter 与 remove 各被调用 2 次），该照片异常条目进 kg_errors，
    status 仍 success，DB 结果不翻转。"""
    # 两张待删照片按序插入 ⇒ 逐照片循环按插入序处理（SQLite rowid 序）
    conn = niu_photo_server.get_connection()
    for pid, fname in (("ph_del_a", "IMG_A.jpg"), ("ph_del_b", "IMG_B.jpg")):
        conn.execute(
            "INSERT INTO photos (id, file_path, abstract, kg_synced) VALUES (?, ?, '待删', 1)",
            (pid, str(cleanup_env["tmp_path"] / fname)),
        )
    conn.commit()
    mock_get_registry.return_value = _make_registry({})  # 无实体/边工具 ⇒ ①②软失败，不干扰四步
    adapter = mock_adapter_cls.return_value
    adapter.delete_chunks_by_source.return_value = {
        "status": "ok", "deleted_chunks": 1, "deleted_vectors": 1, "matched_ids": ["a1"],
    }
    # remove：第一次调用（第一张照片）抛硬异常，第二次（第二张照片）返回 True
    mock_remove.side_effect = [RuntimeError("boom"), True]

    result = niu_photo_server.cleanup_deleted_photos()

    assert result["status"] == "success", "逐照片硬异常不翻转 DB 清理结果"
    assert result["deleted_photos"] == 2
    # 第二张照片的两项新清理仍然执行：A 类 adapter 与 remove 各被调用 2 次（未被中断）
    assert adapter.delete_chunks_by_source.call_count == 2
    assert mock_remove.call_count == 2
    # 第一张照片的硬异常进 kg_errors（实体名 = 归一化 stem，小写）
    assert any("img_a" in e and "boom" in e for e in result["kg_errors"])
    assert result["media_vectors_removed"] == 1, "第二张照片 remove 返回 True 照常计数"

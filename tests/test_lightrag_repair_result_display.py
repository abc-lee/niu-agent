"""run_repair_on_user_request repaired 判定逻辑的单元测试。

背景：原代码 repaired=True 硬编码，repair_all 永不抛异常所以永远成功。
本测试验证：
1. 所有 vdb status=ok 时 repaired=True
2. 任一 vdb status=error 时 repaired=False
3. entity_sync/relationship_sync status=error 时 repaired=False
"""
from unittest import mock


def test_run_repair_all_ok_returns_repaired_true():
    """所有 vdb 和 sync 都是 ok 时，repaired=True"""
    from niu_api.internal import lightrag_manager

    # mock repair_all 返回全 ok（repair_all 在 lightrag_repair 模块，run_repair_on_user_request
    # 函数内 from ... import repair_all，所以 patch 源模块 lightrag_repair.repair_all）
    all_ok_result = {
        "vdb_entities.json": {"status": "ok", "rebuilt_count": 5, "source": "vdb_data_field"},
        "vdb_relationships.json": {"status": "ok", "rebuilt_count": 3},
        "vdb_chunks.json": {"status": "ok", "rebuilt_count": 10},
        "entity_sync": {"status": "ok", "renamed": 0, "removed": 0, "rebuilt": 0},
        "relationship_sync": {"status": "ok", "renamed": 0, "removed": 0},
    }
    with mock.patch("niu_api.internal.lightrag_repair.repair_all", return_value=all_ok_result):
        with mock.patch("niu_api.internal.lightrag_integrity.check_all", return_value={"ok": True}):
            with mock.patch.object(lightrag_manager, "get_lightrag", return_value=None):
                result = lightrag_manager.run_repair_on_user_request()

    assert result["repaired"] is True
    assert result["check_ok"] is True


def test_run_repair_one_vdb_error_returns_repaired_false():
    """任一 vdb status=error 时，repaired=False"""
    from niu_api.internal import lightrag_manager

    one_error_result = {
        "vdb_entities.json": {"status": "error", "message": "无可用数据源重建"},
        "vdb_relationships.json": {"status": "ok", "rebuilt_count": 3},
        "vdb_chunks.json": {"status": "ok", "rebuilt_count": 10},
        "entity_sync": {"status": "ok"},
        "relationship_sync": {"status": "ok"},
    }
    with mock.patch("niu_api.internal.lightrag_repair.repair_all", return_value=one_error_result):
        with mock.patch("niu_api.internal.lightrag_integrity.check_all", return_value={"ok": False, "total_errors": 1}):
            with mock.patch.object(lightrag_manager, "get_lightrag", return_value=None):
                result = lightrag_manager.run_repair_on_user_request()

    assert result["repaired"] is False
    assert "vdb_entities.json" in result["repair_result"]
    assert result["repair_result"]["vdb_entities.json"]["status"] == "error"


def test_run_repair_sync_error_returns_repaired_false():
    """entity_sync 或 relationship_sync status=error 时，repaired=False"""
    from niu_api.internal import lightrag_manager

    sync_error_result = {
        "vdb_entities.json": {"status": "ok"},
        "vdb_relationships.json": {"status": "ok"},
        "vdb_chunks.json": {"status": "ok"},
        "entity_sync": {"status": "error", "message": "GraphML 读取失败"},
        "relationship_sync": {"status": "ok"},
    }
    with mock.patch("niu_api.internal.lightrag_repair.repair_all", return_value=sync_error_result):
        with mock.patch("niu_api.internal.lightrag_integrity.check_all", return_value={"ok": False}):
            with mock.patch.object(lightrag_manager, "get_lightrag", return_value=None):
                result = lightrag_manager.run_repair_on_user_request()

    assert result["repaired"] is False


def test_kg_api_repair_endpoint_is_async():
    """/api/kg/lightrag/repair 端点必须是 async def（asyncio.to_thread 避免阻塞 event loop）"""
    import inspect
    from niu_api.kg_api import repair_lightrag_storage

    assert inspect.iscoroutinefunction(repair_lightrag_storage), (
        "repair_lightrag_storage 必须是 async def（避免 run_repair_on_user_request 阻塞 event loop）"
    )

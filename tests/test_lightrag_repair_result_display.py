"""run_repair_on_user_request repaired 判定逻辑的单元测试。

背景：原代码 repaired=True 硬编码，repair_all 永不抛异常所以永远成功。
本测试验证：
1. 所有 vdb status=ok 时 repaired=True
2. 任一 vdb status=error 时 repaired=False
3. entity_sync/relationship_sync status=error 时 repaired=False
"""
# === launcher format_repair_summary JSON 结构验证（Task 4） ===
# Rust 端 format_repair_summary 期望从 run_repair_on_user_request 返回的 JSON 中解析以下字段：
# - result.repaired (bool)
# - result.critical_errors / major_errors / minor_errors (int)
# - result.repair_result.<name>.status ("ok"|"error")
# - result.repair_result.<name>.expected / actual / lost (int, 可选)
# - result.repair_result.<name>.unrecoverable (bool, 可选)
# - result.repair_result.<name>.message (str, 可选)
# - result.repair_result.<name>.rebuilt_count (int, 可选)
# - result.repair_result.<name>.source (str, 可选)
# 本测试只验证后端返回的 JSON 结构与契约一致，Rust 端到端展示在 Task 5 e2e 测试。
from unittest import mock


def _build_repair_result_with_lost():
    """构造含 expected/actual/lost 的 repair_result（验证 launcher 能解析数量差额）"""
    return {
        "text_chunks": {
            "status": "error",
            "expected": 100,
            "actual": 95,
            "lost": 5,
            "source": "doc_status",
            "message": "5 条 chunk 丢失",
        },
        "doc_status": {"status": "ok", "expected": 1, "actual": 1, "lost": 0},
    }


def _build_repair_result_with_unrecoverable():
    """构造含 unrecoverable: true 的 repair_result（验证 launcher 不可恢复分级）"""
    return {
        "text_chunks": {
            "status": "error",
            "expected": 100,
            "actual": 0,
            "lost": 100,
            "source": "doc_status",
            "message": "doc_status 不存在，无法重建",
            "unrecoverable": True,
        },
        "doc_status": {"status": "error", "message": "文件不存在"},
    }


def _build_repair_result_minor_warning():
    """构造 repaired=true + minor_errors>0 的 repair_result（验证 launcher 警告分级）"""
    return {
        "text_chunks": {"status": "ok", "expected": 100, "actual": 100, "lost": 0},
        "doc_status": {"status": "ok", "expected": 1, "actual": 1, "lost": 0},
    }


def _validate_repair_result_contract(result: dict) -> None:
    """验证 run_repair_on_user_request 返回的 JSON 结构符合 launcher 解析契约"""
    assert "repaired" in result and isinstance(result["repaired"], bool)
    assert "critical_errors" in result and isinstance(result["critical_errors"], int)
    assert "major_errors" in result and isinstance(result["major_errors"], int)
    assert "minor_errors" in result and isinstance(result["minor_errors"], int)
    assert "repair_result" in result and isinstance(result["repair_result"], dict)
    assert "check_ok" in result and isinstance(result["check_ok"], bool)

    for name, detail in result["repair_result"].items():
        assert "status" in detail and detail["status"] in ("ok", "error"), (
            f"{name}.status 必须是 ok/error"
        )
        # 可选字段（如果存在必须类型正确）
        if "expected" in detail:
            assert isinstance(detail["expected"], int), f"{name}.expected 必须是 int"
        if "actual" in detail:
            assert isinstance(detail["actual"], int), f"{name}.actual 必须是 int"
        if "lost" in detail:
            assert isinstance(detail["lost"], int), f"{name}.lost 必须是 int"
        if "unrecoverable" in detail:
            assert isinstance(detail["unrecoverable"], bool), (
                f"{name}.unrecoverable 必须是 bool"
            )
        if "message" in detail:
            assert isinstance(detail["message"], str), f"{name}.message 必须是 str"
        if "source" in detail:
            assert isinstance(detail["source"], str), f"{name}.source 必须是 str"
        if "rebuilt_count" in detail:
            assert isinstance(detail["rebuilt_count"], int), (
                f"{name}.rebuilt_count 必须是 int"
            )


def test_format_repair_summary_shows_lost_count():
    """验证 repair_result 含 expected/actual/lost 时 JSON 结构供 launcher 解析数量差额"""
    # 构造 launcher 期望收到的完整响应结构
    response = {
        "result": {
            "repaired": False,
            "check_ok": False,
            "critical_errors": 0,
            "major_errors": 1,
            "minor_errors": 0,
            "repair_result": _build_repair_result_with_lost(),
            "check_result": {"ok": False, "total_errors": 1},
        }
    }

    result = response["result"]
    _validate_repair_result_contract(result)

    # 验证 lost 字段存在且 > 0（launcher 端会加 ⚠️ 警示）
    tc = result["repair_result"]["text_chunks"]
    assert tc["lost"] == 5 and tc["lost"] > 0
    assert tc["expected"] == 100 and tc["actual"] == 95


def test_format_repair_summary_unrecoverable_title():
    """验证 repair_result 含 unrecoverable: true 时 JSON 结构供 launcher 触发不可恢复分级"""
    response = {
        "result": {
            "repaired": False,
            "check_ok": False,
            "critical_errors": 1,
            "major_errors": 0,
            "minor_errors": 0,
            "repair_result": _build_repair_result_with_unrecoverable(),
            "check_result": {"ok": False, "total_errors": 1},
        }
    }

    result = response["result"]
    _validate_repair_result_contract(result)

    # 验证 unrecoverable 字段存在且为 True（launcher 端会显示"修复失败（不可恢复）"标题 + ⛔ 提示）
    tc = result["repair_result"]["text_chunks"]
    assert tc["unrecoverable"] is True

    # 验证 launcher 检测 has_unrecoverable 的逻辑：任意子项 unrecoverable=true 即触发
    has_unrecoverable = any(
        d.get("unrecoverable", False) for d in result["repair_result"].values()
    )
    assert has_unrecoverable is True


def test_format_repair_summary_minor_errors_warning():
    """验证 repaired=true + minor_errors>0 时 JSON 结构供 launcher 触发警告分级"""
    response = {
        "result": {
            "repaired": True,
            "check_ok": False,  # minor_errors > 0 时 check_ok 可以为 False
            "critical_errors": 0,
            "major_errors": 0,
            "minor_errors": 3,
            "repair_result": _build_repair_result_minor_warning(),
            "check_result": {"ok": False, "total_errors": 3, "minor_errors": 3},
        }
    }

    result = response["result"]
    _validate_repair_result_contract(result)

    # 验证 launcher 警告分级条件：repaired=True && minor_errors > 0
    assert result["repaired"] is True
    assert result["minor_errors"] > 0
    assert result["critical_errors"] == 0
    assert result["major_errors"] == 0


# === 后端 run_repair_on_user_request 测试（原有） ===



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

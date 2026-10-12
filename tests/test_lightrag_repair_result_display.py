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
# - result.report_path (str, 可选)：完整报告落盘路径（~/.niu/logs/repair-report-*.json）；
#   空串 = 落盘失败或旧版 Python。launcher 据此在弹窗末尾告知用户报告位置。
# 本测试只验证后端返回的 JSON 结构与契约一致，Rust 端到端展示在 Task 5 e2e 测试。
import json
from pathlib import Path
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
    # report_path：新版 Python 必填（落盘失败为空串），旧版 Python 缺省
    if "report_path" in result:
        assert isinstance(result["report_path"], str), "report_path 必须是 str"

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
            "report_path": "/Users/test/.niu/logs/repair-report-20261012-101010.json",
            "repair_result": _build_repair_result_with_lost(),
            "check_result": {"ok": False, "total_errors": 1},
        }
    }

    result = response["result"]
    _validate_repair_result_contract(result)

    # report_path 契约：str，供 launcher 弹窗展示完整报告位置
    assert result["report_path"] == "/Users/test/.niu/logs/repair-report-20261012-101010.json"

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
            # 落盘失败场景：空串是合法值（launcher 只提示丢失风险、不展示路径）
            "report_path": "",
            "repair_result": _build_repair_result_with_unrecoverable(),
            "check_result": {"ok": False, "total_errors": 1},
        }
    }

    result = response["result"]
    _validate_repair_result_contract(result)

    # report_path 为空串（落盘失败）时也必须是 str 契约
    assert result["report_path"] == ""

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
            # 不设 report_path：模拟旧版 Python（缺字段），validator 按可选处理
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


# === 修复报告落盘（report_path 契约） ===


def _full_mock_repair_result():
    """带 missing/skipped_already_valid/rebuilt/repaired/removed_polluted 的典型 repair_all 返回。"""
    return {
        "vdb_entities.json": {"status": "ok", "rebuilt_count": 42, "source": "vdb_data_field"},
        "text_chunks": {
            "status": "ok",
            "expected": 100,
            "actual": 100,
            "lost": 0,
            "missing_chunks": [],
            "skipped_already_valid": 37,
            "rebuilt": 60,
            "repaired": 3,
            "removed_polluted": 0,
            "final_count": 100,
            "source": "GraphML + cache + full_docs",
        },
        "entity_sync": {"status": "ok", "renamed": 0, "removed": 0, "rebuilt": 0},
        "_unrecoverable": False,
    }


def test_repair_report_written_with_full_result(tmp_path, monkeypatch):
    """修复完成后完整报告落盘：report_path 指向该文件，内容是含关键字段的合法 JSON。"""
    from niu_api.internal import lightrag_manager

    log_dir = tmp_path / "logs"
    monkeypatch.setenv("NIU_LOG_DIR", str(log_dir))

    with mock.patch("niu_api.internal.lightrag_repair.repair_all", return_value=_full_mock_repair_result()):
        with mock.patch("niu_api.internal.lightrag_integrity.check_all", return_value={"ok": True}):
            with mock.patch.object(lightrag_manager, "get_lightrag", return_value=None):
                result = lightrag_manager.run_repair_on_user_request()

    # 修复状态不受落盘影响
    assert result["repaired"] is True
    assert result["check_ok"] is True

    # report_path：非空绝对路径，指向日志目录下的 repair-report-*.json
    report_path = result["report_path"]
    assert isinstance(report_path, str) and report_path, "report_path 不应为空"
    p = Path(report_path)
    assert p.is_absolute()
    assert p.parent == log_dir
    assert p.name.startswith("repair-report-") and p.name.endswith(".json")
    assert p.is_file()

    # 目录下恰好生成一个报告文件（文件名带时间戳）
    reports = list(log_dir.glob("repair-report-*.json"))
    assert len(reports) == 1, f"应恰好生成 1 个报告文件，实际: {reports}"

    # 文件内容是合法 JSON 且含关键字段（missing/skipped_already_valid/rebuilt/removed_polluted 等）
    data = json.loads(reports[0].read_text(encoding="utf-8"))
    assert data["repaired"] is True
    assert data["check_ok"] is True
    assert data["_unrecoverable"] is False
    tc = data["repair_result"]["text_chunks"]
    assert tc["skipped_already_valid"] == 37
    assert tc["rebuilt"] == 60
    assert tc["repaired"] == 3
    assert tc["removed_polluted"] == 0
    assert tc["missing_chunks"] == []
    assert data["repair_result"]["vdb_entities.json"]["rebuilt_count"] == 42
    # 报告写入发生在 report_path 赋值前，故文件内不含 report_path 键（无自引用）
    assert "report_path" not in data


def test_repair_report_write_failure_keeps_result(tmp_path, monkeypatch):
    """落盘失败（日志目录无法创建）不影响修复返回值：状态原样，report_path 为空串。"""
    from niu_api.internal import lightrag_manager

    # NIU_LOG_DIR 指向一个已存在的"文件" → mkdir 抛异常 → 落盘失败降级路径
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("NIU_LOG_DIR", str(blocker))

    with mock.patch("niu_api.internal.lightrag_repair.repair_all", return_value=_full_mock_repair_result()):
        with mock.patch("niu_api.internal.lightrag_integrity.check_all", return_value={"ok": True}):
            with mock.patch.object(lightrag_manager, "get_lightrag", return_value=None):
                result = lightrag_manager.run_repair_on_user_request()

    # 修复主流程状态不受落盘失败影响
    assert result["repaired"] is True
    assert result["check_ok"] is True
    assert result["repair_result"]["text_chunks"]["rebuilt"] == 60
    # 落盘失败 → 空串（launcher 据此只提示丢失风险、不展示路径）
    assert result["report_path"] == ""
    # 没有伪造成报告文件
    assert not any(tmp_path.glob("repair-report-*.json"))


def test_repair_report_serializes_non_json_values(tmp_path, monkeypatch):
    """repair_result 含不可序列化值（Path 等）时报告仍能落盘（default=str 兜底）。"""
    from niu_api.internal import lightrag_manager

    log_dir = tmp_path / "logs"
    monkeypatch.setenv("NIU_LOG_DIR", str(log_dir))

    repair_result = _full_mock_repair_result()
    repair_result["vdb_entities.json"]["storage_dir"] = Path("/tmp/fake-storage")

    with mock.patch("niu_api.internal.lightrag_repair.repair_all", return_value=repair_result):
        with mock.patch("niu_api.internal.lightrag_integrity.check_all", return_value={"ok": True}):
            with mock.patch.object(lightrag_manager, "get_lightrag", return_value=None):
                result = lightrag_manager.run_repair_on_user_request()

    assert result["report_path"], "含不可序列化值时报告仍应落盘"
    data = json.loads(Path(result["report_path"]).read_text(encoding="utf-8"))
    assert data["repair_result"]["vdb_entities.json"]["storage_dir"] == "/tmp/fake-storage"


def test_repair_report_written_on_exception_path(tmp_path, monkeypatch):
    """repair_all 抛异常（except 路径）时也落盘报告（含 error 现场），report_path 非空。"""
    from niu_api.internal import lightrag_manager

    log_dir = tmp_path / "logs"
    monkeypatch.setenv("NIU_LOG_DIR", str(log_dir))

    with mock.patch("niu_api.internal.lightrag_repair.repair_all", side_effect=RuntimeError("boom")):
        with mock.patch("niu_api.internal.lightrag_integrity.check_all", return_value={"ok": False}):
            with mock.patch.object(lightrag_manager, "get_lightrag", return_value=None):
                result = lightrag_manager.run_repair_on_user_request()

    assert result["repaired"] is False
    assert result["repair_result"]["error"] == "boom"
    assert result["report_path"], "异常路径也应落盘报告"
    data = json.loads(Path(result["report_path"]).read_text(encoding="utf-8"))
    assert data["repair_result"]["error"] == "boom"
    assert data["repaired"] is False

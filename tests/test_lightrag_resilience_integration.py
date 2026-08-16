"""LightRAG 韧性集成测试——v6 用户决策驱动启动流程"""

import pytest

pytestmark = pytest.mark.integration


def test_phase1_only_checks_no_backup_or_cleanup(monkeypatch):
    """v6 Phase 1：只调 check_all，不调 cleanup/backup/repair（备份是用户的事）"""
    from niu_api.internal import lightrag_manager

    check_calls = []
    monkeypatch.setattr("niu_api.internal.lightrag_integrity.check_all",
                        lambda: check_calls.append("check") or {"ok": True, "total_errors": 0})

    # 验证 lightrag_backup 模块不存在（已删除）
    try:
        import niu_api.internal.lightrag_backup  # noqa: F401
        raise AssertionError("lightrag_backup 模块应已删除")
    except ImportError:
        pass  # 预期：模块已删除

    result = lightrag_manager.run_resilience_phase1()

    assert check_calls == ["check"]
    assert result["check_ok"] is True
    assert result["need_repair"] is False


def test_phase1_corrupt_sets_need_repair(monkeypatch):
    """Phase 1 检测到损坏（非 vdb_matrix_mismatch）→ need_repair=True，不自动修复。

    v3 例外：vdb_matrix_mismatch（matrix/data 行数不一致）由启动自检自动修复——
    其他损坏（真相源 corrupt / vdb_missing）仍不自动修（走 rfd 弹窗）。

    E3 契约反转：need_repair 公式改写为基于 critical/major 错误计数（A3 P1-3）——
    mock 补 critical_errors: 2（旧 mock 无该字段，新公式 .get 默认 0 会翻红）。
    """
    from niu_api.internal import lightrag_manager

    monkeypatch.setattr("niu_api.internal.lightrag_integrity.check_all",
                        lambda: {"ok": False, "total_errors": 2, "critical_errors": 2,
                                 "vdb": {"vdb_entities.json": {"ok": False}}})

    result = lightrag_manager.run_resilience_phase1()

    assert result["check_ok"] is False
    assert result["need_repair"] is True


def test_phase2_does_not_auto_repair(monkeypatch):
    """v6 Phase 2：不自动修复，只记录 need_repair 状态等用户决策"""
    from niu_api.internal import lightrag_manager

    repair_calls = []
    monkeypatch.setattr("niu_api.internal.lightrag_repair.repair_all",
                        lambda: repair_calls.append("repair") or {})

    # v6 删除了 run_resilience_phase2（不自动修复）
    assert not hasattr(lightrag_manager, "run_resilience_phase2"), \
        "v6 应删除 run_resilience_phase2（不自动修复）"
    assert repair_calls == []


def test_run_repair_on_user_request_repairs_and_resets(monkeypatch):
    """v9: run_repair_on_user_request 用户点'尝试修复'后调 repair_all + reset_init_state + 重跑 check_all，
    不调 get_lightrag（v8 起铁律 3：让下次用户请求自然触发初始化——旧 v6 断言已对齐）"""
    from niu_api.internal import lightrag_manager

    repair_calls = []
    reset_calls = []
    check_calls = []
    get_lightrag_calls = []
    monkeypatch.setattr("niu_api.internal.lightrag_repair.repair_all",
                        lambda: repair_calls.append("repair") or {"vdb_entities.json": {"status": "ok"}})
    monkeypatch.setattr("niu_api.internal.lightrag_manager.reset_init_state",
                        lambda: reset_calls.append("reset"))
    monkeypatch.setattr("niu_api.internal.lightrag_integrity.check_all",
                        lambda: check_calls.append("check") or {"ok": True, "total_errors": 0})
    # mock get_lightrag 避免真实初始化
    monkeypatch.setattr("niu_api.internal.lightrag_manager.get_lightrag",
                        lambda: get_lightrag_calls.append("get_lightrag") or None)

    result = lightrag_manager.run_repair_on_user_request()

    assert repair_calls == ["repair"]
    assert reset_calls == ["reset"]
    assert check_calls == ["check"]
    assert get_lightrag_calls == []  # v9：不调 get_lightrag/apipeline（铁律 3）——让下次用户请求自然触发
    assert result["repaired"] is True
    assert result["check_ok"] is True


def test_get_lightrag_status_includes_integrity(monkeypatch):
    from niu_api.internal import lightrag_manager

    # _init_failed_at: Optional[float] = None，设 None 让 init_failed=False
    monkeypatch.setattr(lightrag_manager, "_init_failed_at", None)
    # mock 对齐真实 check_all 形状（v4：total_errors = critical + major + minor，
    # 顶层必带 critical_errors 字段——旧 mock 缺该字段导致 total_errors 恒 0 的 pre-existing 失败）
    monkeypatch.setattr(lightrag_manager, "_integrity_result", {
        "ok": False, "total_errors": 2, "critical_errors": 2,
        "vdb": {"vdb_entities.json": {"ok": False}},
    })

    status = lightrag_manager.get_lightrag_status()
    assert status["init_failed"] is False
    assert "integrity" in status
    assert status["integrity"]["ok"] is False
    assert status["integrity"]["total_errors"] == 2
    # E3 契约 #7：存储路径（成功形态）——check_failed=False + error=None
    assert status["integrity"]["check_failed"] is False
    assert status["integrity"]["error"] is None


def test_phase1_check_all_exception_is_detection_failure_not_corruption(monkeypatch):
    """E3 契约 #7：check_all 异常 = 检测失败 ≠ 数据损坏——
    ok=True（无损坏语义，不触发 launcher !ok 修复弹窗闩锁）+ check_failed=True + error=str(e) + need_repair=False。
    """
    from niu_api.internal import lightrag_manager

    def raise_check_all():
        raise RuntimeError("check_all boom")

    monkeypatch.setattr("niu_api.internal.lightrag_integrity.check_all", raise_check_all)

    result = lightrag_manager.run_resilience_phase1()

    assert result["check_ok"] is True  # ok 保持 True——"无损坏"语义
    assert result["need_repair"] is False  # 检测失败 ≠ 损坏——不触发修复门控
    cr = result["check_result"]
    assert cr["ok"] is True
    assert cr["check_failed"] is True  # 检测未完成显式标识
    assert cr["error"] == "check_all boom"
    assert cr["critical_errors"] == 0
    assert cr["major_errors"] == 0


def test_get_lightrag_status_exposes_check_failed(monkeypatch):
    """E3 契约 #7 补充：integrity 四子路径统一输出 check_failed/error——
    失败 True + error=str(e)，成功 False + error=None。
    """
    from niu_api.internal import lightrag_manager

    monkeypatch.setattr(lightrag_manager, "_init_failed_at", None)

    # 路径①: Phase-1 存储路径（check_all 异常结果）——带 check_failed=True + error
    monkeypatch.setattr(lightrag_manager, "_integrity_result", {
        "ok": True, "critical_errors": 0, "major_errors": 0, "minor_errors": 0,
        "error": "check_all boom", "check_failed": True,
    })
    status = lightrag_manager.get_lightrag_status()
    assert status["integrity"]["check_failed"] is True
    assert status["integrity"]["error"] == "check_all boom"

    # 路径②: fresh check_all 成功——check_failed=False + error=None
    monkeypatch.setattr(lightrag_manager, "_integrity_result", None)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity.check_all",
                        lambda: {"ok": True, "total_errors": 0})
    status = lightrag_manager.get_lightrag_status()
    assert status["integrity"]["check_failed"] is False
    assert status["integrity"]["error"] is None

    # 路径③: fresh check_all 异常——check_failed=True + error=str(e)（与 Phase-1 路径对齐）
    def raise_check_all():
        raise RuntimeError("fresh check boom")

    monkeypatch.setattr("niu_api.internal.lightrag_integrity.check_all", raise_check_all)
    status = lightrag_manager.get_lightrag_status()
    assert status["integrity"]["check_failed"] is True
    assert status["integrity"]["error"] == "fresh check boom"

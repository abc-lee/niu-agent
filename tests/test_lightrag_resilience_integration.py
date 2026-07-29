"""LightRAG 韧性集成测试——v6 用户决策驱动启动流程"""



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
    """Phase 1 检测到损坏时设 need_repair=True，但不立即修复（v6 不自动修复）"""
    from niu_api.internal import lightrag_manager

    monkeypatch.setattr("niu_api.internal.lightrag_integrity.check_all",
                        lambda: {"ok": False, "total_errors": 2, "vdb": {"vdb_entities.json": {"ok": False}}})

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
    """v6: run_repair_on_user_request 用户点'尝试修复'后调 repair_all + reset_init_state + 重跑 check_all + get_lightrag"""
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
    assert get_lightrag_calls == ["get_lightrag"]  # v6 改进 5：主动触发重试
    assert result["repaired"] is True
    assert result["check_ok"] is True


def test_get_lightrag_status_includes_integrity(monkeypatch):
    from niu_api.internal import lightrag_manager

    # _init_failed_at: Optional[float] = None，设 None 让 init_failed=False
    monkeypatch.setattr(lightrag_manager, "_init_failed_at", None)
    monkeypatch.setattr(lightrag_manager, "_integrity_result", {
        "ok": False, "total_errors": 2,
        "vdb": {"vdb_entities.json": {"ok": False}},
    })

    status = lightrag_manager.get_lightrag_status()
    assert status["init_failed"] is False
    assert "integrity" in status
    assert status["integrity"]["ok"] is False
    assert status["integrity"]["total_errors"] == 2

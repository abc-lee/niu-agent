"""
Tests for niu_api/internal/lightrag_manager.py 三级启动门控 + _repairing 保护

覆盖：
- A 级 critical 拒绝启动
- B 级 major 拒绝启动
- C 级 minor 降级启动
- 空数据场景（全 0 → 正常初始化）
- _repairing=True → get_lightrag 静默返回 None
- run_repair_on_user_request try/finally 清 _repairing
- pipeline busy 超时 300s → 拒绝 repair
- unrecoverable → repaired=False

用 monkeypatch + 直接操作模块全局变量，不碰用户真实数据。
"""

import time
from unittest.mock import patch

import pytest

import niu_api.internal.lightrag_manager as mgr


@pytest.fixture(autouse=True)
def _reset_module_state():
    """每个测试前重置 lightrag_manager 模块全局状态。"""
    saved = {
        "rag": mgr._rag_instance,
        "failed_at": mgr._init_failed_at,
        "integrity": mgr._integrity_result,
        "repairing": mgr._repairing,
    }
    mgr._rag_instance = None
    mgr._init_failed_at = None
    mgr._integrity_result = None
    mgr._repairing = False
    # 同时重置 _lightrag_ready Event，避免上一个测试 set 后污染
    mgr._lightrag_ready.clear()
    yield
    mgr._rag_instance = saved["rag"]
    mgr._init_failed_at = saved["failed_at"]
    mgr._integrity_result = saved["integrity"]
    mgr._repairing = saved["repairing"]


# ============== 三级门控测试 ==============


class TestThreeTierGating:
    """覆盖 A/B/C 三级启动门控。"""

    def test_grade_A_critical_rejects_init(self):
        """A 级：critical > 0 → 拒绝初始化，返回 None。"""
        mgr._integrity_result = {
            "ok": False,
            "critical_errors": 1,
            "major_errors": 0,
            "minor_errors": 0,
        }
        with patch("niu_api.internal.lightrag_manager._create_lightrag_instance") as mock_create:
            result = mgr.get_lightrag()
            assert result is None
            assert not mock_create.called, "critical 应拒绝初始化，不应调 _create_lightrag_instance"
            assert mgr._init_failed_at is not None, "应设置 _init_failed_at 启动冷却"

    def test_grade_B_major_rejects_init(self):
        """B 级：major > 0 → 拒绝初始化，返回 None。"""
        mgr._integrity_result = {
            "ok": False,
            "critical_errors": 0,
            "major_errors": 2,
            "minor_errors": 0,
        }
        with patch("niu_api.internal.lightrag_manager._create_lightrag_instance") as mock_create:
            result = mgr.get_lightrag()
            assert result is None
            assert not mock_create.called, "major 应拒绝初始化"
            assert mgr._init_failed_at is not None

    def test_grade_C_minor_degraded_init(self):
        """C 级：仅 minor > 0 → 降级启动，调 _create_lightrag_instance。"""
        mgr._integrity_result = {
            "ok": True,
            "critical_errors": 0,
            "major_errors": 0,
            "minor_errors": 3,
        }
        fake_rag = object()
        with patch("niu_api.internal.lightrag_manager._create_lightrag_instance", return_value=fake_rag) as mock_create:
            result = mgr.get_lightrag()
            assert result is fake_rag
            assert mock_create.called, "minor 应降级启动，仍调 _create_lightrag_instance"

    def test_no_errors_normal_init(self):
        """空场景：critical/major/minor 全 0 → 正常初始化。"""
        mgr._integrity_result = {
            "ok": True,
            "critical_errors": 0,
            "major_errors": 0,
            "minor_errors": 0,
        }
        fake_rag = object()
        with patch("niu_api.internal.lightrag_manager._create_lightrag_instance", return_value=fake_rag) as mock_create:
            result = mgr.get_lightrag()
            assert result is fake_rag
            assert mock_create.called

    def test_no_integrity_result_allows_init(self):
        """_integrity_result is None（未跑过 Phase 1）→ 允许初始化。"""
        mgr._integrity_result = None
        fake_rag = object()
        with patch("niu_api.internal.lightrag_manager._create_lightrag_instance", return_value=fake_rag) as mock_create:
            result = mgr.get_lightrag()
            assert result is fake_rag
            assert mock_create.called

    def test_already_initialized_short_circuits(self):
        """_rag_instance 已存在 → 直接返回，不跑门控。"""
        fake_rag = object()
        mgr._rag_instance = fake_rag
        # 即便 critical=99，也应直接返回已存在的实例
        mgr._integrity_result = {
            "ok": False,
            "critical_errors": 99,
            "major_errors": 99,
            "minor_errors": 99,
        }
        with patch("niu_api.internal.lightrag_manager._create_lightrag_instance") as mock_create:
            result = mgr.get_lightrag()
            assert result is fake_rag
            assert not mock_create.called


# ============== _repairing 保护测试 ==============


class TestRepairingGuard:
    """覆盖 repair 期间 _repairing 保护。"""

    def test_repairing_silently_returns_none(self):
        """_repairing=True → get_lightrag 静默返回 None，不调 _create。"""
        mgr._repairing = True
        # 即便没有 critical 错误，也应返回 None
        mgr._integrity_result = {
            "ok": True,
            "critical_errors": 0,
            "major_errors": 0,
            "minor_errors": 0,
        }
        with patch("niu_api.internal.lightrag_manager._create_lightrag_instance") as mock_create:
            result = mgr.get_lightrag()
            assert result is None
            assert not mock_create.called, "_repairing 期间不应尝试初始化"
            # 关键：不应设置 _init_failed_at（避免 SkillSync 误报 critical）
            assert mgr._init_failed_at is None, "_repairing 期间不应设 _init_failed_at"

    def test_repairing_takes_priority_over_critical(self):
        """_repairing=True + critical=99 → 仍静默返回 None，不报 critical 日志。"""
        mgr._repairing = True
        mgr._integrity_result = {
            "ok": False,
            "critical_errors": 99,
            "major_errors": 0,
            "minor_errors": 0,
        }
        with patch("niu_api.internal.lightrag_manager._create_lightrag_instance") as mock_create:
            result = mgr.get_lightrag()
            assert result is None
            assert not mock_create.called
            # 关键：不设 _init_failed_at（如果设了，SkillSync 轮询会看到 critical）
            assert mgr._init_failed_at is None


# ============== run_repair_on_user_request 测试 ==============


class TestRunRepairOnUserRequest:
    """覆盖 run_repair_on_user_request 的 _repairing try/finally + pipeline busy + unrecoverable。"""

    def test_repairing_cleared_on_success(self):
        """repair 成功 → _repairing 在 finally 清回 False。"""
        # 预设 pipeline 不 busy
        with patch("niu_api.kg_api._read_pipeline_busy", return_value=False), \
             patch("niu_api.internal.lightrag_repair.repair_all", return_value={}), \
             patch("niu_api.internal.lightrag_integrity.check_all", return_value={
                 "ok": True, "critical_errors": 0, "major_errors": 0, "minor_errors": 0,
             }), \
             patch("niu_api.internal.lightrag_manager.get_lightrag", return_value=None):
            result = mgr.run_repair_on_user_request()
            assert result["repaired"] is True
            assert mgr._repairing is False, "finally 应清 _repairing"

    def test_repairing_cleared_on_exception(self):
        """repair 抛异常 → _repairing 仍应在 finally 清回 False。"""
        with patch("niu_api.kg_api._read_pipeline_busy", return_value=False), \
             patch("niu_api.internal.lightrag_repair.repair_all", side_effect=RuntimeError("boom")):
            result = mgr.run_repair_on_user_request()
            assert result["repaired"] is False
            assert mgr._repairing is False, "异常路径 finally 也应清 _repairing"

    def test_pipeline_busy_timeout_rejects_repair(self):
        """pipeline busy 超过 300s → 拒绝 repair，返回 message。"""
        # _read_pipeline_busy 永远返回 True（busy）
        # time.sleep mock 避免真实等待
        sleep_calls = []

        def fake_sleep(seconds):
            sleep_calls.append(seconds)
            # 模拟时间流逝，让 while 循环能退出（300/5 = 60 次迭代）
            # 但我们不希望真跑 60 次，用 counter 截断
            if len(sleep_calls) >= 3:
                # 强制让 time.monotonic 越过 deadline
                _fake_time.value = 999999.0

        _fake_time = type("FakeTime", (), {"value": 0.0})()
        original_monotonic = time.monotonic

        def fake_monotonic():
            return _fake_time.value

        with patch("niu_api.kg_api._read_pipeline_busy", return_value=True), \
             patch("niu_api.internal.lightrag_manager.time.sleep", side_effect=fake_sleep), \
             patch("niu_api.internal.lightrag_manager.time.monotonic", side_effect=fake_monotonic), \
             patch("niu_api.internal.lightrag_repair.repair_all") as mock_repair:
            result = mgr.run_repair_on_user_request()
            assert result["repaired"] is False
            assert "pipeline busy" in result.get("message", "")
            assert not mock_repair.called, "busy 超时不应调 repair_all"
            assert mgr._repairing is False

    def test_pipeline_busy_then_idle_proceeds_repair(self):
        """pipeline 先 busy 后 idle → 等待后正常 repair。"""
        call_count = [0]

        def busy_pattern():
            call_count[0] += 1
            # 前 2 次 busy，第 3 次起 idle
            return call_count[0] < 3

        with patch("niu_api.kg_api._read_pipeline_busy", side_effect=busy_pattern), \
             patch("niu_api.internal.lightrag_manager.time.sleep"), \
             patch("niu_api.internal.lightrag_repair.repair_all", return_value={}), \
             patch("niu_api.internal.lightrag_integrity.check_all", return_value={
                 "ok": True, "critical_errors": 0, "major_errors": 0, "minor_errors": 0,
             }), \
             patch("niu_api.internal.lightrag_manager.get_lightrag", return_value=None):
            result = mgr.run_repair_on_user_request()
            assert result["repaired"] is True
            assert call_count[0] >= 3, "应多次轮询 pipeline busy"

    def test_unrecoverable_marks_repaired_false(self):
        """repair_result 含 unrecoverable=True → repaired=False。"""
        repair_result = {
            "repair_text_chunks": {
                "status": "error",
                "unrecoverable": True,
                "message": "full_docs 损坏",
            },
            "_unrecoverable": True,
        }
        with patch("niu_api.kg_api._read_pipeline_busy", return_value=False), \
             patch("niu_api.internal.lightrag_repair.repair_all", return_value=repair_result), \
             patch("niu_api.internal.lightrag_integrity.check_all", return_value={
                 "ok": False, "critical_errors": 1, "major_errors": 0, "minor_errors": 0,
             }), \
             patch("niu_api.internal.lightrag_manager.get_lightrag", return_value=None):
            result = mgr.run_repair_on_user_request()
            assert result["repaired"] is False
            assert result["critical_errors"] == 1

    def test_repair_status_error_marks_repaired_false(self):
        """repair_result 任一 status=error（非 unrecoverable）→ repaired=False。"""
        repair_result = {
            "repair_vdb_chunks": {
                "status": "error",
                "message": "embedding 失败率 >10%",
            },
        }
        with patch("niu_api.kg_api._read_pipeline_busy", return_value=False), \
             patch("niu_api.internal.lightrag_repair.repair_all", return_value=repair_result), \
             patch("niu_api.internal.lightrag_integrity.check_all", return_value={
                 "ok": True, "critical_errors": 0, "major_errors": 0, "minor_errors": 0,
             }), \
             patch("niu_api.internal.lightrag_manager.get_lightrag", return_value=None):
            result = mgr.run_repair_on_user_request()
            assert result["repaired"] is False

    def test_recheck_major_marks_repaired_false(self):
        """重检 check_all major>0 → repaired=False（即使 repair_result 全 ok）。"""
        repair_result = {
            "repair_vdb_entities": {"status": "ok"},
        }
        with patch("niu_api.kg_api._read_pipeline_busy", return_value=False), \
             patch("niu_api.internal.lightrag_repair.repair_all", return_value=repair_result), \
             patch("niu_api.internal.lightrag_integrity.check_all", return_value={
                 "ok": False, "critical_errors": 0, "major_errors": 2, "minor_errors": 0,
             }), \
             patch("niu_api.internal.lightrag_manager.get_lightrag", return_value=None):
            result = mgr.run_repair_on_user_request()
            assert result["repaired"] is False
            assert result["major_errors"] == 2

    def test_get_lightrag_called_after_repair(self):
        """repair 完成后应主动调 get_lightrag 触发重试初始化。"""
        with patch("niu_api.kg_api._read_pipeline_busy", return_value=False), \
             patch("niu_api.internal.lightrag_repair.repair_all", return_value={}), \
             patch("niu_api.internal.lightrag_integrity.check_all", return_value={
                 "ok": True, "critical_errors": 0, "major_errors": 0, "minor_errors": 0,
             }), \
             patch("niu_api.internal.lightrag_manager.get_lightrag", return_value=None) as mock_get:
            mgr.run_repair_on_user_request()
            assert mock_get.called, "修复后应调 get_lightrag 触发重试"

    def test_rag_instance_set_none_during_repair(self):
        """repair 期间应置 _rag_instance = None（避免并发写文件竞争）。"""
        fake_rag = object()
        mgr._rag_instance = fake_rag

        # 用一个能捕获 _rag_instance 当前值的 repair_all
        captured = {}

        def capture_repair():
            captured["rag_at_repair"] = mgr._rag_instance
            return {}

        with patch("niu_api.kg_api._read_pipeline_busy", return_value=False), \
             patch("niu_api.internal.lightrag_repair.repair_all", side_effect=capture_repair), \
             patch("niu_api.internal.lightrag_integrity.check_all", return_value={
                 "ok": True, "critical_errors": 0, "major_errors": 0, "minor_errors": 0,
             }), \
             patch("niu_api.internal.lightrag_manager.get_lightrag", return_value=None):
            mgr.run_repair_on_user_request()
            assert captured["rag_at_repair"] is None, "repair 期间 _rag_instance 应为 None"

    def test_return_includes_severity_counts(self):
        """返回值含 critical/major/minor_errors 三个字段。"""
        with patch("niu_api.kg_api._read_pipeline_busy", return_value=False), \
             patch("niu_api.internal.lightrag_repair.repair_all", return_value={}), \
             patch("niu_api.internal.lightrag_integrity.check_all", return_value={
                 "ok": True, "critical_errors": 0, "major_errors": 0, "minor_errors": 5,
             }), \
             patch("niu_api.internal.lightrag_manager.get_lightrag", return_value=None):
            result = mgr.run_repair_on_user_request()
            assert result["critical_errors"] == 0
            assert result["major_errors"] == 0
            assert result["minor_errors"] == 5
            assert result["check_ok"] is True
            assert result["repaired"] is True

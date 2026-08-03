"""Tests for dream-evolver proactive trigger threshold calculation.

Tests the dynamic threshold algorithm `_calc_dream_trigger_threshold_dynamic`,
which reads a persistent asymmetric EMA (exponential moving average) of
per-turn token cost and derives the trigger threshold from the context
window's 30% incremental budget. Also tests EMA persistence helpers and
the asymmetric EMA update logic.
"""


class TestEMAReadWrite:
    """测试 EMA 读写持久化方法。"""

    def test_read_ema_no_file(self, tmp_path):
        """文件不存在时返回 (0.0, 0)。"""
        from agent.runner import NiuRunner
        ema, count = NiuRunner._read_ema(tmp_path / "nonexistent.json")
        assert ema == 0.0
        assert count == 0

    def test_write_then_read_ema(self, tmp_path):
        """写入后读取应一致。"""
        from agent.runner import NiuRunner
        path = tmp_path / "avg.json"
        NiuRunner._write_ema(path, ema=3500.0, sample_count=10)
        ema, count = NiuRunner._read_ema(path)
        assert ema == 3500.0
        assert count == 10

    def test_read_ema_corrupt_file(self, tmp_path):
        """文件损坏时返回 (0.0, 0)。"""
        from agent.runner import NiuRunner
        path = tmp_path / "avg.json"
        path.write_text("corrupt json")
        ema, count = NiuRunner._read_ema(path)
        assert ema == 0.0
        assert count == 0

    def test_read_ema_missing_fields(self, tmp_path):
        """文件存在但 ema/sample_count 字段缺失时返回默认值。"""
        from agent.runner import NiuRunner
        path = tmp_path / "avg.json"
        path.write_text('{"other": "data"}')
        ema, count = NiuRunner._read_ema(path)
        assert ema == 0.0
        assert count == 0

    def test_write_ema_creates_parent_dir(self, tmp_path):
        """_write_ema 应创建不存在的父目录。"""
        from agent.runner import NiuRunner
        path = tmp_path / "subdir" / "avg.json"
        NiuRunner._write_ema(path, ema=3500.0, sample_count=10)
        assert path.exists()
        ema, count = NiuRunner._read_ema(path)
        assert ema == 3500.0

    def test_read_ema_negative_value(self, tmp_path):
        """CQ-12: ema 为负值时应归零。"""
        import json
        from agent.runner import NiuRunner
        path = tmp_path / "avg.json"
        path.write_text(json.dumps({"ema": -100.0, "sample_count": 5}))
        ema, count = NiuRunner._read_ema(path)
        assert ema == 0.0
        assert count == 5

    def test_read_ema_nan(self, tmp_path):
        """CQ-12: ema 为 NaN 时应归零。"""
        import json
        import math
        from agent.runner import NiuRunner
        path = tmp_path / "avg.json"
        path.write_text(json.dumps({"ema": float("nan"), "sample_count": 5}))
        ema, count = NiuRunner._read_ema(path)
        assert ema == 0.0  # NaN != NaN → 归零
        assert count == 5

    def test_read_ema_negative_sample_count(self, tmp_path):
        """CQ-12: sample_count 为负值时应归零。"""
        import json
        from agent.runner import NiuRunner
        path = tmp_path / "avg.json"
        path.write_text(json.dumps({"ema": 3000.0, "sample_count": -5}))
        ema, count = NiuRunner._read_ema(path)
        assert ema == 3000.0
        assert count == 0


class TestCalcDreamTriggerThresholdEMA:
    """测试改造后的动态阈值函数（读持久化 EMA）。"""

    def test_cold_start_sample_below_5(self, tmp_path):
        """样本数 < 5 时返回保底 10。"""
        from agent.runner import _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "avg.json"
        threshold = _calc_dream_trigger_threshold_dynamic(200000, ema_path)
        assert threshold == 10

    def test_ema_3700_200k(self, tmp_path):
        """EMA=3700, 200K 窗口 → threshold=16。"""
        from agent.runner import NiuRunner, _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "avg.json"
        NiuRunner._write_ema(ema_path, ema=3700.0, sample_count=10)
        threshold = _calc_dream_trigger_threshold_dynamic(200000, ema_path)
        assert threshold == 16

    def test_ema_6000_200k_floor(self, tmp_path):
        """EMA=6000, 200K 窗口 → threshold=10（下限兜底）。"""
        from agent.runner import NiuRunner, _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "avg.json"
        NiuRunner._write_ema(ema_path, ema=6000.0, sample_count=20)
        threshold = _calc_dream_trigger_threshold_dynamic(200000, ema_path)
        assert threshold == 10

    def test_ema_1500_200k_threshold_40(self, tmp_path):
        """EMA=1500, 200K 窗口 → threshold=40。"""
        from agent.runner import NiuRunner, _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "avg.json"
        NiuRunner._write_ema(ema_path, ema=1500.0, sample_count=15)
        threshold = _calc_dream_trigger_threshold_dynamic(200000, ema_path)
        assert threshold == 40

    def test_zero_context_window(self, tmp_path):
        """context_window=0 → 返回 10。"""
        from agent.runner import _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "avg.json"
        threshold = _calc_dream_trigger_threshold_dynamic(0, ema_path)
        assert threshold == 10

    def test_negative_context_window(self, tmp_path):
        """context_window=-1 → 返回 10。"""
        from agent.runner import _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "avg.json"
        threshold = _calc_dream_trigger_threshold_dynamic(-1, ema_path)
        assert threshold == 10

    def test_sample_count_5_boundary(self, tmp_path):
        """sample_count=5（边界值）→ 使用 EMA 公式。"""
        from agent.runner import NiuRunner, _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "avg.json"
        NiuRunner._write_ema(ema_path, ema=3000.0, sample_count=5)
        threshold = _calc_dream_trigger_threshold_dynamic(200000, ema_path)
        assert threshold == 20  # int(60000 / 3000) = 20

    def test_sample_count_4_cold_start(self, tmp_path):
        """sample_count=4（边界值）→ 冷启动返回 10。"""
        from agent.runner import NiuRunner, _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "avg.json"
        NiuRunner._write_ema(ema_path, ema=3000.0, sample_count=4)
        threshold = _calc_dream_trigger_threshold_dynamic(200000, ema_path)
        assert threshold == 10

    def test_ema_zero_with_samples(self, tmp_path):
        """ema=0 且 sample_count>=5 → max(1000, 0)=1000 → threshold=50。"""
        from agent.runner import NiuRunner, _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "avg.json"
        NiuRunner._write_ema(ema_path, ema=0.0, sample_count=10)
        threshold = _calc_dream_trigger_threshold_dynamic(200000, ema_path)
        assert threshold == 50  # int(60000 / 1000) = 60, min(50, 60) = 50

    def test_large_context_window(self, tmp_path):
        """CQ-14: 极大 context_window=2000000, EMA=1000 → threshold=50（上限兜底）。"""
        from agent.runner import NiuRunner, _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "avg.json"
        NiuRunner._write_ema(ema_path, ema=1000.0, sample_count=10)
        threshold = _calc_dream_trigger_threshold_dynamic(2000000, ema_path)
        # int(2000000*0.30 / 1000) = 600, min(50, 600) = 50
        assert threshold == 50

    def test_large_ema(self, tmp_path):
        """CQ-14: 极大 EMA=100000 → threshold=10（下限兜底）。"""
        from agent.runner import NiuRunner, _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "avg.json"
        NiuRunner._write_ema(ema_path, ema=100000.0, sample_count=10)
        threshold = _calc_dream_trigger_threshold_dynamic(200000, ema_path)
        # int(60000 / 100000) = 0, max(10, min(50, 0)) = 10
        assert threshold == 10


class TestEMAUpdateLogic:
    """测试非对称 EMA 更新逻辑。"""

    def test_cold_start_overwrite(self):
        """冷启动期（sample_count < 5）：直接用 current_avg 覆盖。"""
        from agent.runner import _compute_ema_update
        new_ema, new_count = _compute_ema_update(ema_old=0.0, sample_count=0, current_avg=3000.0)
        assert new_ema == 3000.0
        assert new_count == 1

    def test_cold_start_overwrite_at_4(self):
        """sample_count=4 仍走冷启动。"""
        from agent.runner import _compute_ema_update
        new_ema, new_count = _compute_ema_update(ema_old=2500.0, sample_count=4, current_avg=4000.0)
        assert new_ema == 4000.0  # 覆盖，不是 EMA 公式
        assert new_count == 5

    def test_ema_old_zero_overwrite(self):
        """ema_old=0 时直接初始化（即使 sample_count >= 5）。"""
        from agent.runner import _compute_ema_update
        new_ema, new_count = _compute_ema_update(ema_old=0.0, sample_count=10, current_avg=3000.0)
        assert new_ema == 3000.0
        assert new_count == 11

    def test_rising_branch(self):
        """上升分支：current_avg > ema_old → α_up=0.2。"""
        from agent.runner import _compute_ema_update
        new_ema, new_count = _compute_ema_update(ema_old=3000.0, sample_count=10, current_avg=5000.0)
        assert new_ema == 0.2 * 5000.0 + 0.8 * 3000.0  # 3400.0
        assert new_count == 11

    def test_falling_branch(self):
        """下降分支：current_avg <= ema_old → α_down=0.5。"""
        from agent.runner import _compute_ema_update
        new_ema, new_count = _compute_ema_update(ema_old=5000.0, sample_count=10, current_avg=3000.0)
        assert new_ema == 0.5 * 3000.0 + 0.5 * 5000.0  # 4000.0
        assert new_count == 11

    def test_equal_branch(self):
        """current_avg == ema_old → 下降分支（α_down=0.5），结果不变。"""
        from agent.runner import _compute_ema_update
        new_ema, new_count = _compute_ema_update(ema_old=3000.0, sample_count=10, current_avg=3000.0)
        assert new_ema == 3000.0  # 0.5*3000 + 0.5*3000 = 3000
        assert new_count == 11

    def test_current_avg_zero(self):
        """CQ-13: current_avg=0, ema_old=3000, sample_count=10 → 下降分支 → ema_old/2。"""
        from agent.runner import _compute_ema_update
        new_ema, new_count = _compute_ema_update(ema_old=3000.0, sample_count=10, current_avg=0.0)
        # 0 <= 3000 → 下降分支: 0.5*0 + 0.5*3000 = 1500
        assert new_ema == 1500.0
        assert new_count == 11

"""Tests for dream-evolver proactive trigger threshold calculation.

Tests the threshold EMA tension model:
- `_calc_dream_trigger_threshold_dynamic`: reads persistent threshold directly
- `_compute_threshold_update`: asymmetric tension model (slow rise, fast fall)
- `_read_ema` / `_write_ema`: persistence of (threshold, sample_count, cumulative_tokens)
"""


class TestEMAReadWrite:
    """测试 threshold EMA 读写持久化方法（三元组返回值）。"""

    def test_read_ema_no_file(self, tmp_path):
        """文件不存在时返回 (10.0, 0, 0)。"""
        from agent.runner import NiuRunner
        threshold, count, cumulative = NiuRunner._read_ema(tmp_path / "nonexistent.json")
        assert threshold == 10.0
        assert count == 0
        assert cumulative == 0

    def test_write_then_read_ema(self, tmp_path):
        """写入后读取应一致（threshold, sample_count, cumulative_tokens）。"""
        from agent.runner import NiuRunner
        path = tmp_path / "threshold.json"
        NiuRunner._write_ema(path, threshold=25.0, sample_count=10, cumulative_tokens=5000)
        threshold, count, cumulative = NiuRunner._read_ema(path)
        assert threshold == 25.0
        assert count == 10
        assert cumulative == 5000

    def test_read_ema_corrupt_file(self, tmp_path):
        """文件损坏时返回 (10.0, 0, 0)。"""
        from agent.runner import NiuRunner
        path = tmp_path / "threshold.json"
        path.write_text("corrupt json")
        threshold, count, cumulative = NiuRunner._read_ema(path)
        assert threshold == 10.0
        assert count == 0
        assert cumulative == 0

    def test_read_ema_missing_fields(self, tmp_path):
        """文件存在但 threshold/sample_count 字段缺失时返回默认值。"""
        from agent.runner import NiuRunner
        path = tmp_path / "threshold.json"
        path.write_text('{"other": "data"}')
        threshold, count, cumulative = NiuRunner._read_ema(path)
        assert threshold == 10.0
        assert count == 0
        assert cumulative == 0

    def test_write_ema_creates_parent_dir(self, tmp_path):
        """_write_ema 应创建不存在的父目录。"""
        from agent.runner import NiuRunner
        path = tmp_path / "subdir" / "threshold.json"
        NiuRunner._write_ema(path, threshold=25.0, sample_count=10, cumulative_tokens=5000)
        assert path.exists()
        threshold, count, cumulative = NiuRunner._read_ema(path)
        assert threshold == 25.0
        assert count == 10
        assert cumulative == 5000

    def test_read_ema_negative_threshold(self, tmp_path):
        """threshold 为负值时应归 10.0。"""
        import json
        from agent.runner import NiuRunner
        path = tmp_path / "threshold.json"
        path.write_text(json.dumps({"threshold": -100.0, "sample_count": 5, "cumulative_tokens": 1000}))
        threshold, count, cumulative = NiuRunner._read_ema(path)
        assert threshold == 10.0
        assert count == 5
        assert cumulative == 1000

    def test_read_ema_nan_threshold(self, tmp_path):
        """threshold 为 NaN 时应归 10.0。"""
        import json
        from agent.runner import NiuRunner
        path = tmp_path / "threshold.json"
        path.write_text(json.dumps({"threshold": float("nan"), "sample_count": 5, "cumulative_tokens": 1000}))
        threshold, count, cumulative = NiuRunner._read_ema(path)
        assert threshold == 10.0  # NaN != NaN → 归 _THRESHOLD_MIN
        assert count == 5
        assert cumulative == 1000

    def test_read_ema_negative_sample_count(self, tmp_path):
        """sample_count 为负值时应归 0。"""
        import json
        from agent.runner import NiuRunner
        path = tmp_path / "threshold.json"
        path.write_text(json.dumps({"threshold": 30.0, "sample_count": -5, "cumulative_tokens": 1000}))
        threshold, count, cumulative = NiuRunner._read_ema(path)
        assert threshold == 30.0
        assert count == 0
        assert cumulative == 1000

    def test_read_ema_negative_cumulative_tokens(self, tmp_path):
        """cumulative_tokens 为负值时应归 0。"""
        import json
        from agent.runner import NiuRunner
        path = tmp_path / "threshold.json"
        path.write_text(json.dumps({"threshold": 30.0, "sample_count": 10, "cumulative_tokens": -500}))
        threshold, count, cumulative = NiuRunner._read_ema(path)
        assert threshold == 30.0
        assert count == 10
        assert cumulative == 0


class TestCalcDreamTriggerThresholdEMA:
    """测试改造后的动态阈值函数（直接读 threshold，不做除法）。"""

    def test_cold_start_sample_below_5(self, tmp_path):
        """样本数 < 5 时返回保底 10。"""
        from agent.runner import _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "threshold.json"
        threshold = _calc_dream_trigger_threshold_dynamic(200000, ema_path)
        assert threshold == 10

    def test_sample_count_5(self, tmp_path):
        """sample_count=5（边界值）→ 返回 int(threshold)。"""
        from agent.runner import NiuRunner, _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "threshold.json"
        NiuRunner._write_ema(ema_path, threshold=25.0, sample_count=5, cumulative_tokens=1000)
        threshold = _calc_dream_trigger_threshold_dynamic(200000, ema_path)
        assert threshold == 25

    def test_threshold_25(self, tmp_path):
        """threshold=25.0 → 返回 25。"""
        from agent.runner import NiuRunner, _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "threshold.json"
        NiuRunner._write_ema(ema_path, threshold=25.0, sample_count=10, cumulative_tokens=2000)
        threshold = _calc_dream_trigger_threshold_dynamic(200000, ema_path)
        assert threshold == 25

    def test_threshold_10_floor(self, tmp_path):
        """threshold=10.0 → 返回 10。"""
        from agent.runner import NiuRunner, _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "threshold.json"
        NiuRunner._write_ema(ema_path, threshold=10.0, sample_count=10, cumulative_tokens=2000)
        threshold = _calc_dream_trigger_threshold_dynamic(200000, ema_path)
        assert threshold == 10

    def test_threshold_50_cap(self, tmp_path):
        """threshold=50.0 → 返回 50。"""
        from agent.runner import NiuRunner, _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "threshold.json"
        NiuRunner._write_ema(ema_path, threshold=50.0, sample_count=10, cumulative_tokens=2000)
        threshold = _calc_dream_trigger_threshold_dynamic(200000, ema_path)
        assert threshold == 50

    def test_zero_context_window(self, tmp_path):
        """context_window=0 → 返回 10（不依赖 context_window）。"""
        from agent.runner import NiuRunner, _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "threshold.json"
        NiuRunner._write_ema(ema_path, threshold=30.0, sample_count=10, cumulative_tokens=2000)
        threshold = _calc_dream_trigger_threshold_dynamic(0, ema_path)
        assert threshold == 30  # 新模型不依赖 context_window

    def test_threshold_clamped(self, tmp_path):
        """threshold=100.0（超出上限）→ 返回 50。"""
        from agent.runner import NiuRunner, _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "threshold.json"
        NiuRunner._write_ema(ema_path, threshold=100.0, sample_count=10, cumulative_tokens=2000)
        threshold = _calc_dream_trigger_threshold_dynamic(200000, ema_path)
        assert threshold == 50


class TestThresholdUpdateLogic:
    """测试对数渐近张力模型更新逻辑。"""

    def test_cold_start_no_change(self):
        """冷启动期（sample_count < 5）：threshold 不变。"""
        from agent.runner import _compute_threshold_update
        new_threshold, new_count = _compute_threshold_update(
            threshold_old=10.0, sample_count=3, current_turn_tokens=500,
            cumulative_tokens=1500
        )
        assert new_threshold == 10.0
        assert new_count == 4

    def test_cold_start_at_4(self):
        """sample_count=4 仍走冷启动（< 5），threshold 不变。"""
        from agent.runner import _compute_threshold_update
        new_threshold, new_count = _compute_threshold_update(
            threshold_old=10.0, sample_count=4, current_turn_tokens=500,
            cumulative_tokens=2000
        )
        assert new_threshold == 10.0
        assert new_count == 5

    def test_light_rising(self):
        """轻量（本轮 token <= 累积平均）→ threshold 上升。

        threshold=10, turn_tokens=100, cumulative_tokens=2200, sample_count=10
        → new_sample_count=11, cumulative_avg=2200/11=200
        → 100 <= 200 → 上升: 10 + (50-10)*0.1 = 14.0
        """
        from agent.runner import _compute_threshold_update
        new_threshold, new_count = _compute_threshold_update(
            threshold_old=10.0, sample_count=10, current_turn_tokens=100,
            cumulative_tokens=2200
        )
        assert new_threshold == 14.0
        assert new_count == 11

    def test_heavy_falling(self):
        """重量（本轮 token > 累积平均）→ threshold 下降。

        threshold=30, turn_tokens=1000, cumulative_tokens=2200, sample_count=10
        → new_sample_count=11, cumulative_avg=2200/11=200
        → 1000 > 200 → 下降: 30 - (30-10)*0.4 = 22.0
        """
        from agent.runner import _compute_threshold_update
        new_threshold, new_count = _compute_threshold_update(
            threshold_old=30.0, sample_count=10, current_turn_tokens=1000,
            cumulative_tokens=2200
        )
        assert new_threshold == 22.0
        assert new_count == 11

    def test_equal_light(self):
        """turn_tokens == cumulative_avg → 上升（<= 判轻）。

        cumulative_tokens=2200, sample_count=10, new_count=11
        cumulative_avg = 2200/11 = 200
        turn_tokens = 200 → 200 <= 200 → 上升
        """
        from agent.runner import _compute_threshold_update
        new_threshold, new_count = _compute_threshold_update(
            threshold_old=10.0, sample_count=10, current_turn_tokens=200,
            cumulative_tokens=2200
        )
        # 上升: 10 + (50-10)*0.1 = 14.0
        assert new_threshold == 14.0
        assert new_count == 11

    def test_rising_decelerating(self):
        """上升减速：threshold 越接近 50，每步增量越小。

        threshold=10 → +4.0 → 14.0
        threshold=40 → +1.0 → 41.0
        threshold=45 → +0.5 → 45.5
        """
        from agent.runner import _compute_threshold_update

        # threshold=10: 10 + (50-10)*0.1 = 14.0
        t1, _ = _compute_threshold_update(10.0, 10, 100, 2200)
        assert t1 == 14.0

        # threshold=40: 40 + (50-40)*0.1 = 41.0
        t2, _ = _compute_threshold_update(40.0, 10, 100, 2200)
        assert t2 == 41.0

        # threshold=45: 45 + (50-45)*0.1 = 45.5
        t3, _ = _compute_threshold_update(45.0, 10, 100, 2200)
        assert t3 == 45.5

    def test_falling_fast(self):
        """下降快速：threshold=45 → 45-(45-10)*0.4 = 31.0（一步降 14）。

        cumulative_tokens=2200, sample_count=10, new_count=11
        cumulative_avg = 200, turn_tokens=1000 > 200 → 下降
        """
        from agent.runner import _compute_threshold_update
        new_threshold, new_count = _compute_threshold_update(
            threshold_old=45.0, sample_count=10, current_turn_tokens=1000,
            cumulative_tokens=2200
        )
        assert new_threshold == 31.0
        assert new_count == 11

    def test_clamp_at_max(self):
        """clamp 上限：threshold=49.5, 轻量 → 49.5+(50-49.5)*0.1 = 49.55，不超 50。

        cumulative_tokens=2200, sample_count=10, new_count=11
        cumulative_avg = 200, turn_tokens=100 <= 200 → 上升
        """
        from agent.runner import _compute_threshold_update
        new_threshold, new_count = _compute_threshold_update(
            threshold_old=49.5, sample_count=10, current_turn_tokens=100,
            cumulative_tokens=2200
        )
        # 49.5 + (50-49.5)*0.1 = 49.55, 在 [10, 50] 内
        assert new_threshold == 49.55
        assert new_count == 11

    def test_clamp_at_min(self):
        """clamp 下限：threshold=11, 重量 → 11-(11-10)*0.4 = 10.6，不低于 10。

        cumulative_tokens=2200, sample_count=10, new_count=11
        cumulative_avg = 200, turn_tokens=1000 > 200 → 下降
        """
        from agent.runner import _compute_threshold_update
        new_threshold, new_count = _compute_threshold_update(
            threshold_old=11.0, sample_count=10, current_turn_tokens=1000,
            cumulative_tokens=2200
        )
        # 11 - (11-10)*0.4 = 10.6, 在 [10, 50] 内
        assert new_threshold == 10.6
        assert new_count == 11

    def test_cumulative_tokens_tracking(self):
        """累积 token 正确累加：多轮调用后 cumulative_tokens 增长正确。

        验证 sample_count 递增 + threshold 按张力模型变化。
        """
        from agent.runner import _compute_threshold_update

        # 初始: threshold=10, sample_count=5, cumulative=1000
        # 本轮 token=100, new_cumulative=1100, new_count=6, avg=1100/6≈183.3
        # 100 <= 183.3 → 上升: 10 + (50-10)*0.1 = 14.0
        t1, c1 = _compute_threshold_update(10.0, 5, 100, 1100)
        assert t1 == 14.0
        assert c1 == 6

        # 第二轮: threshold=14, sample_count=6, cumulative=1100
        # 本轮 token=200, new_cumulative=1300, new_count=7, avg=1300/7≈185.7
        # 200 > 185.7 → 下降: 14 - (14-10)*0.4 = 12.4
        t2, c2 = _compute_threshold_update(14.0, 6, 200, 1300)
        assert t2 == 12.4
        assert c2 == 7

        # 第三轮: threshold=12.4, sample_count=7, cumulative=1300
        # 本轮 token=50, new_cumulative=1350, new_count=8, avg=1350/8=168.75
        # 50 <= 168.75 → 上升: 12.4 + (50-12.4)*0.1 = 16.16
        t3, c3 = _compute_threshold_update(12.4, 7, 50, 1350)
        assert t3 == 16.16
        assert c3 == 8

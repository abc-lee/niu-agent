"""Tests for dream-evolver proactive trigger threshold calculation.

Tests the threshold EMA tension model:
- `_calc_dream_trigger_threshold_dynamic`: reads persistent threshold directly
- `_compute_threshold_update`: asymmetric tension model (slow rise, fast fall)
- `_read_ema` / `_write_ema`: persistence of (threshold, sample_count, ref_ema)
"""

import pytest


class TestEMAReadWrite:
    """测试 threshold EMA 读写持久化方法（三元组返回值）。"""

    def test_read_ema_no_file(self, tmp_path):
        """文件不存在时返回 (10.0, 0, 0)。"""
        from agent.runner import NiuRunner
        threshold, count, ref_ema = NiuRunner._read_ema(tmp_path / "nonexistent.json")
        assert threshold == 10.0
        assert count == 0
        assert ref_ema == 0

    def test_write_then_read_ema(self, tmp_path):
        """写入后读取应一致（threshold, sample_count, ref_ema）。"""
        from agent.runner import NiuRunner
        path = tmp_path / "threshold.json"
        NiuRunner._write_ema(path, threshold=25.0, sample_count=10, ref_ema=5000)
        threshold, count, ref_ema = NiuRunner._read_ema(path)
        assert threshold == 25.0
        assert count == 10
        assert ref_ema == 5000

    def test_read_ema_corrupt_file(self, tmp_path):
        """文件损坏时返回 (10.0, 0, 0)。"""
        from agent.runner import NiuRunner
        path = tmp_path / "threshold.json"
        path.write_text("corrupt json")
        threshold, count, ref_ema = NiuRunner._read_ema(path)
        assert threshold == 10.0
        assert count == 0
        assert ref_ema == 0

    def test_read_ema_missing_fields(self, tmp_path):
        """文件存在但 threshold/sample_count 字段缺失时返回默认值。

        无 ref_ema 字段走旧文件迁移：ct=0/sc=0 → ref 回退 0.0。
        """
        from agent.runner import NiuRunner
        path = tmp_path / "threshold.json"
        path.write_text('{"other": "data"}')
        threshold, count, ref_ema = NiuRunner._read_ema(path)
        assert threshold == 10.0
        assert count == 0
        assert ref_ema == 0

    def test_write_ema_creates_parent_dir(self, tmp_path):
        """_write_ema 应创建不存在的父目录。"""
        from agent.runner import NiuRunner
        path = tmp_path / "subdir" / "threshold.json"
        NiuRunner._write_ema(path, threshold=25.0, sample_count=10, ref_ema=5000)
        assert path.exists()
        threshold, count, ref_ema = NiuRunner._read_ema(path)
        assert threshold == 25.0
        assert count == 10
        assert ref_ema == 5000

    def test_read_ema_negative_threshold(self, tmp_path):
        """threshold 为负值时应归 10.0（ref 走旧文件迁移：ct/sc=200）。"""
        import json
        from agent.runner import NiuRunner
        path = tmp_path / "threshold.json"
        path.write_text(json.dumps({"threshold": -100.0, "sample_count": 5, "cumulative_tokens": 1000}))
        threshold, count, ref_ema = NiuRunner._read_ema(path)
        assert threshold == 10.0
        assert count == 5
        assert ref_ema == 200.0

    def test_read_ema_nan_threshold(self, tmp_path):
        """threshold 为 NaN 时应归 10.0（ref 走旧文件迁移：ct/sc=200）。"""
        import json
        from agent.runner import NiuRunner
        path = tmp_path / "threshold.json"
        path.write_text(json.dumps({"threshold": float("nan"), "sample_count": 5, "cumulative_tokens": 1000}))
        threshold, count, ref_ema = NiuRunner._read_ema(path)
        assert threshold == 10.0  # NaN != NaN → 归 _THRESHOLD_MIN
        assert count == 5
        assert ref_ema == 200.0

    def test_read_ema_negative_sample_count(self, tmp_path):
        """sample_count 为负值时应归 0（迁移回退 ref=0）。"""
        import json
        from agent.runner import NiuRunner
        path = tmp_path / "threshold.json"
        path.write_text(json.dumps({"threshold": 30.0, "sample_count": -5, "cumulative_tokens": 1000}))
        threshold, count, ref_ema = NiuRunner._read_ema(path)
        assert threshold == 30.0
        assert count == 0
        assert ref_ema == 0.0

    def test_read_ema_negative_cumulative_tokens(self, tmp_path):
        """旧格式 cumulative_tokens 为负 → 迁移 ref 为负 → 归 0。"""
        import json
        from agent.runner import NiuRunner
        path = tmp_path / "threshold.json"
        path.write_text(json.dumps({"threshold": 30.0, "sample_count": 10, "cumulative_tokens": -500}))
        threshold, count, ref_ema = NiuRunner._read_ema(path)
        assert threshold == 30.0
        assert count == 10
        assert ref_ema == 0


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
        NiuRunner._write_ema(ema_path, threshold=25.0, sample_count=5, ref_ema=1000)
        threshold = _calc_dream_trigger_threshold_dynamic(200000, ema_path)
        assert threshold == 25

    def test_threshold_25(self, tmp_path):
        """threshold=25.0 → 返回 25。"""
        from agent.runner import NiuRunner, _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "threshold.json"
        NiuRunner._write_ema(ema_path, threshold=25.0, sample_count=10, ref_ema=2000)
        threshold = _calc_dream_trigger_threshold_dynamic(200000, ema_path)
        assert threshold == 25

    def test_threshold_10_floor(self, tmp_path):
        """threshold=10.0 → 返回 10。"""
        from agent.runner import NiuRunner, _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "threshold.json"
        NiuRunner._write_ema(ema_path, threshold=10.0, sample_count=10, ref_ema=2000)
        threshold = _calc_dream_trigger_threshold_dynamic(200000, ema_path)
        assert threshold == 10

    def test_threshold_50_cap(self, tmp_path):
        """threshold=50.0 → 返回 50。"""
        from agent.runner import NiuRunner, _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "threshold.json"
        NiuRunner._write_ema(ema_path, threshold=50.0, sample_count=10, ref_ema=2000)
        threshold = _calc_dream_trigger_threshold_dynamic(200000, ema_path)
        assert threshold == 50

    def test_zero_context_window(self, tmp_path):
        """context_window=0 → 返回 10（不依赖 context_window）。"""
        from agent.runner import NiuRunner, _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "threshold.json"
        NiuRunner._write_ema(ema_path, threshold=30.0, sample_count=10, ref_ema=2000)
        threshold = _calc_dream_trigger_threshold_dynamic(0, ema_path)
        assert threshold == 30  # 新模型不依赖 context_window

    def test_threshold_clamped(self, tmp_path):
        """threshold=100.0（超出上限）→ 返回 50。"""
        from agent.runner import NiuRunner, _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "threshold.json"
        NiuRunner._write_ema(ema_path, threshold=100.0, sample_count=10, ref_ema=2000)
        threshold = _calc_dream_trigger_threshold_dynamic(200000, ema_path)
        assert threshold == 50


class TestThresholdUpdateLogic:
    """测试对数渐近张力模型 + EMA 参考线更新逻辑。"""

    def test_cold_start_no_change(self):
        """冷启动期（sample_count < 5）：threshold 不变，参考线仍更新。"""
        from agent.runner import _compute_threshold_update
        new_threshold, new_count, new_ref = _compute_threshold_update(
            threshold_old=10.0, sample_count=3, current_turn_tokens=500,
            ref_old=200
        )
        assert new_threshold == 10.0
        assert new_count == 4
        assert new_ref == 260.0  # 0.2*500 + 0.8*200 = 260

    def test_cold_start_at_4(self):
        """sample_count=4 仍走冷启动（< 5），threshold 不变。"""
        from agent.runner import _compute_threshold_update
        new_threshold, new_count, new_ref = _compute_threshold_update(
            threshold_old=10.0, sample_count=4, current_turn_tokens=500,
            ref_old=200
        )
        assert new_threshold == 10.0
        assert new_count == 5
        assert new_ref == 260.0

    def test_light_rising(self):
        """轻量（本轮 token <= EMA 参考线）→ threshold 上升。

        ref_old=200, turn_tokens=100 → new_ref=0.2*100+0.8*200=180
        → 100 <= 180 → 上升: 10 + (50-10)*0.1 = 14.0
        """
        from agent.runner import _compute_threshold_update
        new_threshold, new_count, new_ref = _compute_threshold_update(
            threshold_old=10.0, sample_count=10, current_turn_tokens=100,
            ref_old=200
        )
        assert new_threshold == 14.0
        assert new_count == 11
        assert new_ref == 180.0

    def test_heavy_falling(self):
        """重量（本轮 token > EMA 参考线）→ threshold 下降。

        ref_old=200, turn_tokens=1000 → new_ref=0.2*1000+0.8*200=360
        → 1000 > 360 → 下降: 30 - (30-10)*0.4 = 22.0
        """
        from agent.runner import _compute_threshold_update
        new_threshold, new_count, new_ref = _compute_threshold_update(
            threshold_old=30.0, sample_count=10, current_turn_tokens=1000,
            ref_old=200
        )
        assert new_threshold == 22.0
        assert new_count == 11
        assert new_ref == 360.0

    def test_equal_light(self):
        """turn_tokens == EMA 参考线 → 上升（<= 判轻）。

        ref_old=200, turn_tokens=200 → new_ref=0.2*200+0.8*200=200
        → 200 <= 200 → 上升: 10 + (50-10)*0.1 = 14.0
        """
        from agent.runner import _compute_threshold_update
        new_threshold, new_count, new_ref = _compute_threshold_update(
            threshold_old=10.0, sample_count=10, current_turn_tokens=200,
            ref_old=200
        )
        assert new_threshold == 14.0
        assert new_count == 11
        assert new_ref == 200.0

    def test_rising_decelerating(self):
        """上升减速：threshold 越接近 50，每步增量越小。

        threshold=10 → +4.0 → 14.0
        threshold=40 → +1.0 → 41.0
        threshold=45 → +0.5 → 45.5
        """
        from agent.runner import _compute_threshold_update

        # threshold=10: 10 + (50-10)*0.1 = 14.0
        t1, _, _ = _compute_threshold_update(10.0, 10, 100, 200)
        assert t1 == 14.0

        # threshold=40: 40 + (50-40)*0.1 = 41.0
        t2, _, _ = _compute_threshold_update(40.0, 10, 100, 200)
        assert t2 == 41.0

        # threshold=45: 45 + (50-45)*0.1 = 45.5
        t3, _, _ = _compute_threshold_update(45.0, 10, 100, 200)
        assert t3 == 45.5

    def test_falling_fast(self):
        """下降快速：threshold=45 → 45-(45-10)*0.4 = 31.0（一步降 14）。

        ref_old=200, turn_tokens=1000 → new_ref=360，1000 > 360 → 下降
        """
        from agent.runner import _compute_threshold_update
        new_threshold, new_count, new_ref = _compute_threshold_update(
            threshold_old=45.0, sample_count=10, current_turn_tokens=1000,
            ref_old=200
        )
        assert new_threshold == 31.0
        assert new_count == 11
        assert new_ref == 360.0

    def test_clamp_at_max(self):
        """clamp 上限：threshold=100 轻量 → 95 → clamp 50。"""
        from agent.runner import _compute_threshold_update
        new_threshold, new_count, new_ref = _compute_threshold_update(
            threshold_old=100.0, sample_count=10, current_turn_tokens=100,
            ref_old=200
        )
        # ref=180，100<=180 轻量 → 100+(50-100)*0.1=95 → clamp 50.0
        assert new_threshold == 50.0
        assert new_count == 11
        assert new_ref == 180.0

    def test_clamp_at_min(self):
        """clamp 下限：threshold=8 重量 → 8.8 → clamp 10。"""
        from agent.runner import _compute_threshold_update
        new_threshold, new_count, new_ref = _compute_threshold_update(
            threshold_old=8.0, sample_count=10, current_turn_tokens=1000,
            ref_old=200
        )
        # ref=360，1000>360 重量 → 8-(8-10)*0.4=8.8 → clamp 10.0
        assert new_threshold == 10.0
        assert new_count == 11
        assert new_ref == 360.0

    def test_ref_ema_tracking(self):
        """多轮调用后参考线 EMA 正确递进（不再累积）。

        轮1: (10.0, 5, 100, 200) → ref=180, 100<=180 上升 → (14.0, 6, 180.0)
        轮2: (14.0, 6, 300, 180) → ref=204, 300>204 下降 → (12.4, 7, 204.0)
        轮3: (12.4, 7, 50, 204)  → ref=173.2, 50<=173.2 上升 → (16.16, 8, 173.2)
        """
        from agent.runner import _compute_threshold_update

        # 轮1
        t1, c1, r1 = _compute_threshold_update(10.0, 5, 100, 200)
        assert t1 == 14.0
        assert c1 == 6
        assert r1 == 180.0

        # 轮2
        t2, c2, r2 = _compute_threshold_update(14.0, 6, 300, 180)
        assert t2 == 12.4
        assert c2 == 7
        assert r2 == 204.0

        # 轮3（ref 有浮点误差：0.2*50+0.8*204=173.20000000000002）
        t3, c3, r3 = _compute_threshold_update(12.4, 7, 50, 204)
        assert t3 == 16.16
        assert c3 == 8
        assert r3 == pytest.approx(173.2)


def test_ref_ema_follows_recent():
    """连续重量轮 → 参考线快速抬升（360→688→1150.4），threshold 连续下降。

    重量轮 token 全部远超新参考线 → 每轮都判重量 → threshold 单调下降。
    """
    from agent.runner import _compute_threshold_update

    # 轮1: token=1000, ref_old=200 → ref=360, 1000>360 下降
    t1, c1, r1 = _compute_threshold_update(30.0, 10, 1000, 200)
    assert t1 == 22.0
    assert c1 == 11
    assert r1 == 360.0

    # 轮2: token=2000, ref_old=360 → ref=688, 2000>688 下降
    t2, c2, r2 = _compute_threshold_update(t1, c1, 2000, r1)
    assert t2 == 17.2
    assert c2 == 12
    assert r2 == 688.0

    # 轮3: token=3000, ref_old=688 → ref=1150.4, 3000>1150.4 下降
    t3, c3, r3 = _compute_threshold_update(t2, c2, 3000, r2)
    assert t3 == 14.32
    assert c3 == 13
    assert r3 == 1150.4


def test_ref_ema_light_follows_down():
    """连续轻量轮 → 参考线回落（180→154→127.2）。

    轻量轮 token 全部低于新参考线 → 每轮都判轻量 → threshold 上升。
    """
    from agent.runner import _compute_threshold_update

    # 轮1: token=100, ref_old=200 → ref=180, 100<=180 上升
    t1, c1, r1 = _compute_threshold_update(10.0, 10, 100, 200)
    assert t1 == 14.0
    assert c1 == 11
    assert r1 == 180.0

    # 轮2: token=50, ref_old=180 → ref=154, 50<=154 上升
    t2, c2, r2 = _compute_threshold_update(t1, c1, 50, r1)
    assert t2 == 17.6
    assert c2 == 12
    assert r2 == 154.0

    # 轮3: token=20, ref_old=154 → ref=127.2, 20<=127.2 上升
    # 17.6 + (50-17.6)*0.1 = 20.84（浮点误差）
    t3, c3, r3 = _compute_threshold_update(t2, c2, 20, r2)
    assert t3 == pytest.approx(20.84)
    assert c3 == 13
    assert r3 == 127.2


def test_old_file_migration(tmp_path):
    """旧格式文件（cumulative_tokens + sample_count，无 ref_ema）→ 迁移热启动 ref=ct/sc。"""
    import json
    from agent.runner import NiuRunner
    path = tmp_path / "threshold.json"
    path.write_text(json.dumps({"threshold": 30.0, "sample_count": 10, "cumulative_tokens": 1000}))
    threshold, count, ref_ema = NiuRunner._read_ema(path)
    assert threshold == 30.0
    assert count == 10
    assert ref_ema == 100.0  # 1000 / 10 = 100


class TestExtractPrevCompleteTurn:
    """测试 _extract_prev_complete_turn_msgs：延迟结算取上一完整轮。

    上一轮 = 倒数第二条 user（含）到倒数第一条 user（不含）之间的所有消息；
    最新 user 之后属于"本轮"（进行中），不参与计算。
    """

    @staticmethod
    def _make(roles):
        """按 role 序列构造消息；user 消息带递增 id，其余 role/content/tool_calls 占位。"""
        import types
        msgs = []
        user_no = 0
        for r in roles:
            if r == "user":
                user_no += 1
                msgs.append(types.SimpleNamespace(role="user", content="hi", tool_calls=[], id=f"u{user_no}"))
            else:
                msgs.append(types.SimpleNamespace(role=r, content="x", tool_calls=[], id=""))
        return msgs

    @staticmethod
    def _roles(msgs):
        return [m.role for m in msgs]

    def test_empty(self):
        """空消息列表 → 无完整上一轮 → []。"""
        from agent.runner import _extract_prev_complete_turn_msgs
        assert _extract_prev_complete_turn_msgs([]) == []

    def test_single_user(self):
        """仅 1 条 user 消息 → 不足两轮 → []。"""
        from agent.runner import _extract_prev_complete_turn_msgs
        assert _extract_prev_complete_turn_msgs(self._make(["user"])) == []

    def test_two_users_basic(self):
        """两轮基本场景：上一轮 = user0 + assistant0 + tool0（最新 user 后不参与）。"""
        from agent.runner import _extract_prev_complete_turn_msgs
        extracted = _extract_prev_complete_turn_msgs(self._make(["user", "assistant", "tool", "user", "assistant"]))
        assert self._roles(extracted) == ["user", "assistant", "tool"]

    def test_three_users(self):
        """三轮：上一轮 = user1（含）到 user2（不含），含全部 assistant/tool 输出。"""
        from agent.runner import _extract_prev_complete_turn_msgs
        extracted = _extract_prev_complete_turn_msgs(
            self._make(["user", "a", "tool", "user", "a", "tool", "a", "user", "a"])
        )
        assert self._roles(extracted) == ["user", "a", "tool", "a"]

    def test_consecutive_users(self):
        """连续两条 user：上一轮仅含前一条 user 本身。"""
        from agent.runner import _extract_prev_complete_turn_msgs
        extracted = _extract_prev_complete_turn_msgs(self._make(["user", "user", "a"]))
        assert self._roles(extracted) == ["user"]

    def test_prev_compressed_absent(self):
        """上一轮被压缩（游标切割后仅剩 1 条 user）→ 返回 []。"""
        from agent.runner import _extract_prev_complete_turn_msgs
        assert _extract_prev_complete_turn_msgs(self._make(["user", "a"])) == []

    def test_tool_heavy_prev_turn(self):
        """上一轮含多条 tool 输出：全部 5 条纳入计算。"""
        from agent.runner import _extract_prev_complete_turn_msgs
        extracted = _extract_prev_complete_turn_msgs(self._make(["user", "a", "tool", "tool", "a", "user"]))
        assert self._roles(extracted) == ["user", "a", "tool", "tool", "a"]


class TestEmaMarkerStep:
    """测试 _ema_marker_step：EMA 去重 marker 状态机（skip/init/settle）。"""

    @pytest.mark.parametrize("last_user_id,prev_marker,expected", [
        ("", "", ("skip", "")),           # 无 user 消息 → 跳过
        ("u1", "", ("init", "u1")),       # 启动后首次 → 只设 marker 不结算
        ("u1", "u1", ("skip", "u1")),     # 同轮重复回调（id 未变）→ 跳过
        ("u2", "u1", ("settle", "u2")),   # 新 user 消息到来 → 结算上一完整轮
        ("u2", "u2", ("skip", "u2")),     # 压缩后 same last user → 不重复结算
        ("u1", "u2", ("settle", "u1")),   # 压缩 removed last user → 新 id 结算
    ])
    def test_marker_step(self, last_user_id, prev_marker, expected):
        from agent.runner import _ema_marker_step
        assert _ema_marker_step(last_user_id, prev_marker) == expected


class TestPrevTurnComplete:
    """测试 _prev_turn_is_complete：上一轮是否完整可结算（尾部 role 判定）。"""

    @staticmethod
    def _make(roles):
        """按 role 序列构造消息。"""
        import types
        return [types.SimpleNamespace(role=r) for r in roles]

    @pytest.mark.parametrize("roles,expected", [
        (["user", "assistant", "tool", "assistant"], True),  # 尾部 assistant：轮完整 → 可结算
        (["user", "user"], True),                            # 连续 user：纯 user 消息轮 → 可结算
        (["user", "assistant", "tool"], False),              # 尾部 tool：工具循环进行中 → 不可结算
        ([], False),                                         # 空列表 → 不可结算
    ])
    def test_prev_turn_is_complete(self, roles, expected):
        from agent.runner import _prev_turn_is_complete
        assert _prev_turn_is_complete(self._make(roles)) is expected

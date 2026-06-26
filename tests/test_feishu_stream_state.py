"""测试 StreamState 原子快照 + generation 守卫。"""

import threading
from niu_api.channel.feishu_channel import StreamState


class TestStreamStateDataclass:
    """StreamState 数据类基本功能。"""

    def test_default_values(self):
        s = StreamState()
        assert s.generation == 0
        assert s.waiting is False
        assert s.card_id is None
        assert s.pending_images == []
        assert s.sent_media_paths == set()

    def test_generation_increments(self):
        s1 = StreamState(generation=1)
        s2 = StreamState(generation=2)
        assert s2.generation > s1.generation

    def test_mutable_fields_are_independent(self):
        """两个 StreamState 实例的可变字段互不影响。"""
        s1 = StreamState(generation=1, pending_images=[{"a": 1}])
        s2 = StreamState(generation=1, pending_images=[{"b": 2}])
        s1.pending_images.append({"c": 3})
        assert len(s2.pending_images) == 1  # s2 不受影响


class TestStreamStateGeneration:
    """generation 守卫逻辑。"""

    def test_generation_mismatch_means_stale(self):
        entry_gen = 5
        current = StreamState(generation=6)
        assert current.generation != entry_gen  # 过期

    def test_generation_match_means_current(self):
        entry_gen = 5
        current = StreamState(generation=5)
        assert current.generation == entry_gen  # 仍有效


class TestStreamStateAtomicSwap:
    """原子交换的线程安全性。"""

    def test_concurrent_swap_no_partial_state(self):
        """并发 _new_generation 不会产生部分状态。"""

        class FakeAdapter:
            def __init__(self):
                self._stream = StreamState(generation=0)
                self._stream_lock = threading.Lock()

            def _new_generation(self, **overrides):
                with self._stream_lock:
                    new_gen = self._stream.generation + 1
                    new_state = StreamState(generation=new_gen, **overrides)
                    self._stream = new_state
                    return StreamState(
                        generation=new_gen,
                        waiting=new_state.waiting,
                        target=new_state.target,
                    )

        adapter = FakeAdapter()
        results = []
        errors = []

        def worker(target_val):
            try:
                state = adapter._new_generation(waiting=True, target=target_val)
                results.append(state)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(f"target_{i}",)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors: {errors}"
        # 最终 generation 应为 20（每个线程递增一次）
        assert adapter._stream.generation == 20
        # 每个 result 的 generation 应唯一
        gens = {r.generation for r in results}
        assert len(gens) == 20

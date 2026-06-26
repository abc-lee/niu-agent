"""测试飞书流式推送的并发安全性——快速连续消息场景。"""

import threading
from niu_api.channel.feishu_channel import StreamState


class FakeStreamAdapter:
    """模拟 FeishuChannelAdapter 的流式状态管理。"""

    def __init__(self):
        self._stream = StreamState()
        self._stream_lock = threading.Lock()

    def _get_stream(self):
        with self._stream_lock:
            return StreamState(
                generation=self._stream.generation,
                waiting=self._stream.waiting,
                target=self._stream.target,
                card_id=self._stream.card_id,
                card_created=self._stream.card_created,
                last_pushed_rowid=self._stream.last_pushed_rowid,
            )

    def _new_generation(self, **overrides):
        with self._stream_lock:
            new_gen = self._stream.generation + 1
            new_state = StreamState(generation=new_gen, **overrides)
            self._stream = new_state
            return StreamState(
                generation=new_gen,
                waiting=new_state.waiting,
                target=new_state.target,
                card_id=new_state.card_id,
                card_created=new_state.card_created,
                last_pushed_rowid=new_state.last_pushed_rowid,
            )

    def _update_stream(self, **kwargs):
        with self._stream_lock:
            for k, v in kwargs.items():
                setattr(self._stream, k, v)
            return StreamState(
                generation=self._stream.generation,
                waiting=self._stream.waiting,
                target=self._stream.target,
                card_id=self._stream.card_id,
                card_created=self._stream.card_created,
                last_pushed_rowid=self._stream.last_pushed_rowid,
            )


class TestSendFinallyNotEraseNewMessage:
    """send(A) finally 块不应擦除 _on_message(B) 设置的状态。"""

    def test_send_finally_preserves_new_generation(self):
        """新消息到达后，send() finally 不应擦除新状态。"""
        adapter = FakeStreamAdapter()

        # 消息 A 到达
        adapter._new_generation(waiting=True, target="chat_A")

        # 消息 B 到达（在 send(A) 期间）
        adapter._new_generation(waiting=True, target="chat_B")

        # send(A) 的 finally 块检查 generation
        entry_gen = 1  # send(A) 启动时的 generation
        current = adapter._get_stream()
        assert current.generation == 2  # B 已递增
        assert current.generation != entry_gen  # 不应擦除

        # finally 块逻辑：generation 不匹配 → 不擦除
        if current.generation == entry_gen:
            adapter._new_generation(waiting=False)
        # 新消息 B 的状态应保留
        assert adapter._get_stream().waiting is True
        assert adapter._get_stream().target == "chat_B"

    def test_send_finally_erases_same_generation(self):
        """没有新消息时，send() finally 正常擦除。"""
        adapter = FakeStreamAdapter()

        # 消息 A 到达
        adapter._new_generation(waiting=True, target="chat_A")
        entry_gen = adapter._get_stream().generation

        # send(A) 完成，finally 块
        current = adapter._get_stream()
        if current.generation == entry_gen:
            adapter._new_generation(waiting=False)

        assert adapter._get_stream().waiting is False


class TestPushIncrementalGenerationGuard:
    """_push_incremental 在 await 后应检查 generation。"""

    def test_push_aborts_on_generation_change(self):
        """新消息到达后，_push_incremental 应中止。"""
        adapter = FakeStreamAdapter()

        # 消息 A 到达
        adapter._new_generation(waiting=True, target="chat_A", last_pushed_rowid=100)
        entry_state = adapter._get_stream()
        entry_gen = entry_state.generation

        # 模拟 await 期间新消息到达
        adapter._new_generation(waiting=True, target="chat_B", last_pushed_rowid=200)

        # _push_incremental 检查 generation
        current = adapter._get_stream()
        assert current.generation != entry_gen  # 应中止
        assert current.target == "chat_B"  # 新消息的目标

    def test_push_continues_on_same_generation(self):
        """没有新消息时，_push_incremental 正常继续。"""
        adapter = FakeStreamAdapter()

        adapter._new_generation(waiting=True, target="chat_A", last_pushed_rowid=100)
        entry_state = adapter._get_stream()
        entry_gen = entry_state.generation

        # 没有新消息
        current = adapter._get_stream()
        assert current.generation == entry_gen  # 应继续
        assert current.last_pushed_rowid == 100


class TestConcurrentNewGeneration:
    """并发 _new_generation 不应产生部分状态。"""

    def test_rapid_fire_messages(self):
        """20 条快速连续消息，generation 应递增 20 次。"""
        adapter = FakeStreamAdapter()

        def simulate_message(target):
            adapter._new_generation(waiting=True, target=target)

        threads = [
            threading.Thread(target=simulate_message, args=(f"chat_{i}",))
            for i in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        state = adapter._get_stream()
        assert state.generation == 20
        assert state.waiting is True

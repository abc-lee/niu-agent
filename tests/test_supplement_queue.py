"""Tests for the supplement queue mechanism (见缝插针)."""
import threading


def test_supplement_queue_initially_empty():
    """Supplement queue starts empty."""
    from agent.runner import drain_supplements
    assert drain_supplements() == []


def test_enqueue_supplement_adds_message():
    """enqueue_supplement adds a message to the queue."""
    from agent.runner import enqueue_supplement, drain_supplements
    enqueue_supplement("用户补充的信息")
    result = drain_supplements()
    assert len(result) == 1
    assert result[0] == "用户补充的信息"


def test_drain_supplements_empties_queue():
    """drain_supplements removes all messages from the queue."""
    from agent.runner import enqueue_supplement, drain_supplements
    enqueue_supplement("消息1")
    enqueue_supplement("消息2")
    assert len(drain_supplements()) == 2
    assert drain_supplements() == []


def test_supplement_queue_thread_safe():
    """enqueue_supplement and drain_supplements are thread-safe."""
    from agent.runner import enqueue_supplement, drain_supplements
    errors = []

    def enqueue_many():
        try:
            for i in range(100):
                enqueue_supplement(f"msg-{i}")
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=enqueue_many)
    t2 = threading.Thread(target=enqueue_many)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    all_msgs = []
    while True:
        batch = drain_supplements()
        if not batch:
            break
        all_msgs.extend(batch)

    assert len(all_msgs) == 200
    assert len(errors) == 0


def test_drain_supplements_no_race_condition():
    """drain_supplements uses get_nowait() without empty() check — no race."""
    from agent.runner import enqueue_supplement, drain_supplements
    for i in range(50):
        enqueue_supplement(f"race-{i}")
    result = drain_supplements()
    assert len(result) == 50
    assert drain_supplements() == []


def test_drain_supplement_empty_returns_none():
    """No pending messages returns None."""
    from agent.runner import drain_supplement
    assert drain_supplement() is None


def test_drain_supplement_single_returns_raw():
    """Single pending message returned as-is."""
    from agent.runner import enqueue_supplement, drain_supplement
    enqueue_supplement("只有一条补充")
    assert drain_supplement() == "只有一条补充"


def test_drain_supplement_multiple_joins_with_prefix():
    """Multiple pending messages joined with [补充] prefix."""
    from agent.runner import enqueue_supplement, drain_supplement
    enqueue_supplement("第一个补充")
    enqueue_supplement("第二个补充")
    result = drain_supplement()
    assert "[补充] 第一个补充" in result
    assert "[补充] 第二个补充" in result

from niu_api.internal.ha_watcher.watcher import (
    _HAWatcher,
    _is_restored_state_transition,
    _normalize_triggers,
)


def _make_event(from_state, to_state, platform="state", variables=True):
    trigger = {"platform": platform}
    if from_state is not None or "from" in (from_state or {}):
        trigger["from_state"] = from_state
    trigger["to_state"] = to_state
    if variables:
        return {"event": {"variables": {"trigger": trigger}}}
    return {"event": {}}


def test_unavailable_to_outside_no_push():
    w = _HAWatcher()
    calls = []
    w._submit_push = lambda d: calls.append(d)
    ev = {"event": {"variables": {"trigger": {"platform": "state", "from_state": {"state": "unavailable"}, "to_state": {"state": "outside_unlock"}}}}}
    w._handle_trigger_event(ev, [])
    assert calls == []


def test_unknown_no_push():
    w = _HAWatcher()
    calls = []
    w._submit_push = lambda d: calls.append(d)
    ev = {"event": {"variables": {"trigger": {"platform": "state", "from_state": {"state": "unknown"}, "to_state": {"state": "outside_unlock"}}}}}
    w._handle_trigger_event(ev, [])
    assert calls == []


def test_none_from_no_push():
    w = _HAWatcher()
    calls = []
    w._submit_push = lambda d: calls.append(d)
    ev = {"event": {"variables": {"trigger": {"platform": "state", "from_state": None, "to_state": {"state": "outside_unlock"}}}}}
    w._handle_trigger_event(ev, [])
    assert calls == []


def test_inside_to_outside_push():
    w = _HAWatcher()
    calls = []
    w._submit_push = lambda d: calls.append(d)
    ev = {"event": {"variables": {"trigger": {"platform": "state", "from_state": {"state": "inside_unlock"}, "to_state": {"state": "outside_unlock"}}}}}
    w._handle_trigger_event(ev, [])
    assert len(calls) == 1


def test_same_value_no_push():
    w = _HAWatcher()
    calls = []
    w._submit_push = lambda d: calls.append(d)
    ev = {"event": {"variables": {"trigger": {"platform": "state", "from_state": {"state": "outside_unlock"}, "to_state": {"state": "outside_unlock"}}}}}
    w._handle_trigger_event(ev, [])
    assert calls == []


def test_non_state_platform_push():
    w = _HAWatcher()
    calls = []
    w._submit_push = lambda d: calls.append(d)
    ev = {"event": {"variables": {"trigger": {"platform": "numeric_state"}}}}
    w._handle_trigger_event(ev, [])
    assert len(calls) == 1


def test_missing_variables_push():
    w = _HAWatcher()
    calls = []
    w._submit_push = lambda d: calls.append(d)
    ev = {"event": {}}
    w._handle_trigger_event(ev, [])
    assert len(calls) == 1


def test_guard_boundaries():
    assert _is_restored_state_transition({"platform": "state", "from_state": None, "to_state": {"state": "x"}}) is True
    assert _is_restored_state_transition({"platform": "state", "from_state": {"state": "UNAVAILABLE"}, "to_state": {"state": "x"}}) is True
    assert _is_restored_state_transition({"platform": "state", "from_state": {"state": ""}, "to_state": {"state": "x"}}) is True
    assert _is_restored_state_transition({"platform": "state", "from_state": {"state": "none"}, "to_state": {"state": "x"}}) is True
    assert _is_restored_state_transition({"platform": "state", "from_state": {"state": "a"}, "to_state": {"state": "a"}}) is True
    assert _is_restored_state_transition({"platform": "state", "from_state": {"state": "a"}, "to_state": {"state": "b"}}) is False
    assert _is_restored_state_transition({"platform": "state", "from_state": "x", "to_state": {"state": "b"}}) is False
    assert _is_restored_state_transition({"platform": "state", "from_state": {"state": "a"}, "to_state": "b"}) is False
    assert _is_restored_state_transition({"platform": "numeric_state"}) is False
    assert _is_restored_state_transition({}) is False
    assert _is_restored_state_transition(None) is False

def test_msg_none_no_push_no_raise():
    w = _HAWatcher()
    calls = []
    w._submit_push = lambda d: calls.append(d)
    w._handle_trigger_event(None, [])
    assert calls == []


def test_msg_str_no_push_no_raise():
    w = _HAWatcher()
    calls = []
    w._submit_push = lambda d: calls.append(d)
    w._handle_trigger_event("x", [])
    assert calls == []


def test_event_non_dict_no_push_no_raise():
    w = _HAWatcher()
    calls = []
    w._submit_push = lambda d: calls.append(d)
    w._handle_trigger_event({"event": "x"}, [])
    assert calls == []


def test_variables_non_dict_push():
    w = _HAWatcher()
    calls = []
    w._submit_push = lambda d: calls.append(d)
    w._handle_trigger_event({"event": {"variables": "x"}}, [])
    assert len(calls) == 1


def test_from_state_missing_state_key_push():
    w = _HAWatcher()
    calls = []
    w._submit_push = lambda d: calls.append(d)
    ev = {"event": {"variables": {"trigger": {"platform": "state", "from_state": {}, "to_state": {"state": "outside_unlock"}}}}}
    w._handle_trigger_event(ev, [])
    assert len(calls) == 1


def test_to_state_missing_state_key_push():
    w = _HAWatcher()
    calls = []
    w._submit_push = lambda d: calls.append(d)
    ev = {"event": {"variables": {"trigger": {"platform": "state", "from_state": {"state": "inside"}, "to_state": {}}}}}
    w._handle_trigger_event(ev, [])
    assert len(calls) == 1


def test_triggers_none_no_raise_push():
    w = _HAWatcher()
    calls = []
    w._submit_push = lambda d: calls.append(d)
    ev = {"event": {}}
    w._handle_trigger_event(ev, None)
    assert len(calls) == 1


def test_triggers_non_dict_elements_no_raise_push():
    w = _HAWatcher()
    calls = []
    w._submit_push = lambda d: calls.append(d)
    ev = {"event": {}}
    w._handle_trigger_event(ev, ["x"])
    assert len(calls) == 1



def test_normalize_triggers_none():
    assert _normalize_triggers(None) == []


def test_normalize_triggers_str():
    assert _normalize_triggers("x") == []


def test_normalize_triggers_mixed():
    assert _normalize_triggers([{"entity_id": "a"}, "x", {"id": "no-eid"}, {"entity_id": "  "}, {"entity_id": "b"}]) == [{"entity_id": "a"}, {"entity_id": "b"}]


def test_normalize_triggers_same_object():
    d = {"entity_id": "a"}
    out = _normalize_triggers([d])
    assert out == [d]
    assert out[0] is d


def test_bypass_explicit_from_state_push():
    w = _HAWatcher()
    calls = []
    w._submit_push = lambda d: calls.append(d)
    w._current_subscriptions = {"t1": 2}
    triggers = [{"id": "t1", "entity_id": "sensor.x", "from_state": "unavailable", "description": "d"}]
    msg = {"id": 2, "event": {"variables": {"trigger": {"platform": "state", "from_state": {"state": "unavailable"}, "to_state": {"state": "outside_unlock"}}}}}
    w._handle_trigger_event(msg, triggers)
    assert len(calls) == 1


def test_bypass_no_from_state_no_push():
    w = _HAWatcher()
    calls = []
    w._submit_push = lambda d: calls.append(d)
    w._current_subscriptions = {"t1": 2}
    triggers = [{"id": "t1", "entity_id": "sensor.x", "description": "d"}]
    msg = {"id": 2, "event": {"variables": {"trigger": {"platform": "state", "from_state": {"state": "unavailable"}, "to_state": {"state": "outside_unlock"}}}}}
    w._handle_trigger_event(msg, triggers)
    assert calls == []


def test_ignored_log_dedup(capsys):
    w = _HAWatcher()
    w._submit_push = lambda d: None
    ev = {"id": 2, "event": {"variables": {"trigger": {"platform": "state", "from_state": {"state": "unavailable"}, "to_state": {"state": "outside_unlock"}}}}}
    w._current_subscriptions = {"t1": 2}
    triggers = [{"id": "t1", "entity_id": "sensor.x", "description": "d"}]
    w._handle_trigger_event(ev, triggers)
    w._handle_trigger_event(ev, triggers)
    out = capsys.readouterr().out
    assert out.count("忽略非真实状态转换") == 1

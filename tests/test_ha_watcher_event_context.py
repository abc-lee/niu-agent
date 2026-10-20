from niu_api.internal.ha_watcher.watcher import _format_trigger_context, _HAWatcher


def _capture():
    w = _HAWatcher()
    calls = []
    w._submit_push = lambda message: calls.append(message)
    return w, calls


def _event(trigger, extra=None, msg_id=3):
    variables = {"trigger": trigger}
    if extra:
        variables.update(extra)
    return {"id": msg_id, "event": {"variables": variables}}


def test_state_event_includes_entity_transition_and_time():
    w, calls = _capture()
    w._current_subscriptions = {"door": 3}
    triggers = [{"id": "door", "entity_id": "sensor.lock_action", "description": "门锁：有人从门外开锁"}]
    ev = _event({
        "platform": "state",
        "entity_id": "sensor.lock_action",
        "from_state": {"state": "inside_unlock", "attributes": {"friendly_name": "Z" * 800, "marker": "ATTR-LEAK-CHECK"}},
        "to_state": {"state": "outside_unlock"},
    })
    w._handle_trigger_event(ev, triggers)
    assert len(calls) == 1
    text = calls[0]
    assert text.splitlines()[0] == "门锁：有人从门外开锁"
    assert "实体: sensor.lock_action" in text
    assert "触发类型: state" in text
    assert "状态变化: inside_unlock -> outside_unlock" in text
    assert "时间: " in text
    assert "Z" * 800 not in text
    assert "ATTR-LEAK-CHECK" not in text


def test_extra_variables_are_appended_without_trigger_key():
    w, calls = _capture()
    w._current_subscriptions = {"door": 3}
    triggers = [{"id": "door", "entity_id": "sensor.lock_action", "description": "门锁"}]
    ev = _event(
        {"platform": "state", "entity_id": "sensor.lock_action",
         "from_state": {"state": "inside_unlock"}, "to_state": {"state": "outside_unlock"}},
        extra={"lock_method": "指纹", "lock_key_id": 2147549186},
    )
    w._handle_trigger_event(ev, triggers)
    text = calls[0]
    assert "附加: lock_method=指纹, lock_key_id=2147549186" in text
    assert "trigger=" not in text


def test_missing_from_to_does_not_raise_and_keeps_description():
    # 用 numeric_state：state 平台缺 from_state 会被现有守卫判为 restored 而丢弃，不能拿它验 AC-3。
    w, calls = _capture()
    w._current_subscriptions = {"door": 3}
    triggers = [{"id": "door", "entity_id": "sensor.lock_action", "description": "门锁"}]
    ev = _event({"platform": "numeric_state", "entity_id": "sensor.lock_action"})
    w._handle_trigger_event(ev, triggers)
    assert len(calls) == 1
    assert calls[0].splitlines()[0] == "门锁"
    assert "实体: sensor.lock_action" in calls[0]
    assert "状态变化:" not in calls[0]


def test_format_context_collapses_newlines():
    text = _format_trigger_context(
        {"platform": "state", "from_state": {"state": "off"}, "to_state": {"state": "on\n附加: fake=1"}},
        {},
    )
    assert "on 附加: fake=1" in text
    assert "\n附加: fake=1" not in text


def test_long_state_is_truncated():
    text = _format_trigger_context(
        {"platform": "state", "entity_id": "e" * 500, "to_state": {"state": "x" * 5000}},
        {},
    )
    assert "x" * 200 in text
    assert "x" * 201 not in text
    assert "e" * 200 in text
    assert "e" * 201 not in text


def test_template_platform_without_from_to_pushes():
    w, calls = _capture()
    w._current_subscriptions = {"tpl": 3}
    triggers = [{"id": "tpl", "entity_id": "sensor.x", "description": "模板触发"}]
    ev = _event({"platform": "template"})
    w._handle_trigger_event(ev, triggers)
    assert len(calls) == 1
    assert "触发类型: template" in calls[0]
    assert "状态变化:" not in calls[0]


def test_format_context_limits_extras_and_skips_nested():
    extras = {"long": "x" * 500}
    extras.update({f"k{i}": i for i in range(12)})
    extras["nested"] = {"a": 1}
    text = _format_trigger_context({"platform": "numeric_state", "entity_id": "sensor.x"}, extras)
    line = next(line for line in text.splitlines() if line.startswith("附加: "))
    assert line.count("=") == 10
    assert "long=" + "x" * 200 in line
    assert "x" * 201 not in line
    assert "nested" not in line


def test_format_context_non_dict_variables_no_raise():
    text = _format_trigger_context({"platform": "state", "entity_id": "sensor.x"}, ["bad"])
    assert "实体: sensor.x" in text
    assert "附加:" not in text


def test_format_context_empty_for_empty_trigger():
    assert _format_trigger_context({}, {}) == ""


def test_build_trigger_config_puts_variables_inside_trigger():
    w = _HAWatcher()
    trigger = w._build_trigger_config({
        "id": "door",
        "entity_id": "sensor.lock_action",
        "condition": "state_change",
        "to_state": "outside_unlock",
        "variables": {"lock_method": "{{ states('sensor.lock_method') }}"},
    })
    assert trigger == {
        "platform": "state",
        "entity_id": "sensor.lock_action",
        "to": "outside_unlock",
        "variables": {"lock_method": "{{ states('sensor.lock_method') }}"},
    }


def test_build_trigger_config_omits_variables_when_absent():
    w = _HAWatcher()
    trigger = w._build_trigger_config({
        "id": "door", "entity_id": "sensor.lock_action", "condition": "state_change",
    })
    assert "variables" not in trigger


def test_build_trigger_config_drops_reserved_trigger_variable():
    w = _HAWatcher()
    trigger = w._build_trigger_config({
        "entity_id": "sensor.lock_action",
        "condition": "state_change",
        "variables": {"trigger": "bad", "site": "front"},
    })
    assert trigger["variables"] == {"site": "front"}


def test_build_trigger_config_drops_non_dict_variables():
    w = _HAWatcher()
    trigger = w._build_trigger_config({
        "entity_id": "sensor.lock_action",
        "condition": "state_change",
        "variables": ["bad"],
    })
    assert "variables" not in trigger


def test_build_trigger_config_drops_nested_variable_values():
    w = _HAWatcher()
    trigger = w._build_trigger_config({
        "entity_id": "sensor.lock_action",
        "condition": "state_change",
        "variables": {"meta": {"a": 1}, "site": "front"},
    })
    assert trigger["variables"] == {"site": "front"}


def test_build_trigger_config_above_carries_variables():
    w = _HAWatcher()
    trigger = w._build_trigger_config({
        "entity_id": "sensor.temp",
        "condition": "above",
        "threshold": 30,
        "variables": {"site": "front"},
    })
    assert trigger["platform"] == "numeric_state"
    assert trigger["above"] == 30
    assert trigger["variables"] == {"site": "front"}

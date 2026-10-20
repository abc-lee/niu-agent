import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp-servers", "ha-server", "src"))
import niu_ha_server as ha  # noqa: E402

import niu_api.internal.ha_watcher as ha_watcher  # noqa: E402


def _install_fake_config(monkeypatch, saved):
    base = {"ha_url": "http://ha.local", "ha_token": "token", "triggers": []}
    saved.update(base)

    def fake_update(update_fn):
        result = update_fn(base)
        saved.clear()
        saved.update(base)
        return result

    monkeypatch.setattr(ha, "_read_config", lambda: base)
    monkeypatch.setattr(ha, "_atomic_update", fake_update)
    monkeypatch.setattr(ha_watcher, "check_and_start", lambda: None)


def test_ha_subscribe_persists_variables(monkeypatch):
    saved = {}
    _install_fake_config(monkeypatch, saved)
    result = ha.ha_subscribe(
        entity_id="sensor.lock_action",
        condition="state_change",
        description="门锁",
        variables={"site": "front", "lock_method": "指纹"},
    )
    assert result["success"] is True
    entry = saved["triggers"][0]
    assert entry["variables"] == {"site": "front", "lock_method": "指纹"}


def test_ha_subscribe_without_variables_keeps_entry_clean(monkeypatch):
    saved = {}
    _install_fake_config(monkeypatch, saved)
    ha.ha_subscribe(entity_id="sensor.lock_action", condition="state_change", description="门锁")
    assert "variables" not in saved["triggers"][0]


def test_ha_subscribe_rejects_non_dict_variables(monkeypatch):
    saved = {}
    _install_fake_config(monkeypatch, saved)
    result = ha.ha_subscribe(
        entity_id="sensor.lock_action",
        condition="state_change",
        variables=["bad"],
    )
    assert result["success"] is False
    assert "variables" in result["error"]


def test_ha_subscribe_rejects_nested_variables(monkeypatch):
    saved = {}
    _install_fake_config(monkeypatch, saved)
    result = ha.ha_subscribe(
        entity_id="sensor.lock_action",
        condition="state_change",
        variables={"meta": {"a": 1}},
    )
    assert result["success"] is False
    assert "标量" in result["error"]


def test_ha_subscribe_rejects_trigger_key(monkeypatch):
    saved = {}
    _install_fake_config(monkeypatch, saved)
    result = ha.ha_subscribe(
        entity_id="sensor.lock_action",
        condition="state_change",
        variables={"trigger": "bad"},
    )
    assert result["success"] is False
    assert "trigger" in result["error"]


def test_ha_subscribe_list_returns_variables(monkeypatch):
    saved = {}
    _install_fake_config(monkeypatch, saved)
    ha.ha_subscribe(
        entity_id="sensor.lock_action",
        condition="state_change",
        description="门锁",
        variables={"site": "front"},
    )
    listed = ha.ha_subscribe(operation="list")
    assert listed["triggers"][0]["variables"] == {"site": "front"}


def test_tool_schema_exposes_variables():
    prop = ha.TOOL_SCHEMAS["ha_subscribe"]["input_schema"]["properties"]["variables"]
    assert prop["type"] == "object"


def test_disk_schema_exposes_variables():
    from pathlib import Path

    import yaml

    path = Path(__file__).parents[1] / "config" / "disk" / "ha-server.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    tool = next(t for t in doc["tools"] if t["name"] == "ha_subscribe")
    prop = next(p for p in tool["parameters"] if p["name"] == "variables")
    assert prop["type"] == "object"
    assert prop["cli_format"] == "json"

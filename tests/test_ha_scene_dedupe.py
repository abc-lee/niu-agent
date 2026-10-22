"""spec §9.1 的 15 行覆盖清单：场景/脚本补录去重 + 真实 entity_id（Task B 修复的回归网）。

矩阵行 ↔ 用例（每行 ≥1 个用例，参数化变体计入同一行）：
  1  TestStatusSceneDedupe          ha_status 场景去重
  2  TestStatusScriptDedupe         ha_status 脚本去重
  3  TestStatusFilter               ha_status 过滤（domain / area / entity_id）
  4  TestStatusRegistryUnavailable  ha_status 注册表不可用
  5  TestSceneListDedupe            ha_scene list 去重
  6  TestSceneListSupplement        ha_scene list 补缺
  7  TestSceneListUnresolvable      ha_scene list 不可解析（strict → [] / 无匹配 / None）
  8  TestScriptListDedupe           ha_script list 去重（不触发 registry）
  9  TestSceneCreateSnapshotTruth   ha_scene create / snapshot 真值
  10 TestSceneFallbackNoGhostId     ha_scene get / update / delete 回退
  11 Test404CleanupGuard            ha_scene 404 清理守卫（get / delete）
  12 TestFetchEntityRegistryStrict  _fetch_entity_registry_strict 成功 / 失败 / 超时
  13 TestVerifyEntityExistsStates   _verify_entity_exists 三态
  14 TestRegistryFailureNoRaise     registry 取数失败不抛异常
  15 TestChineseNames               中文名脚本 / 场景

隔离口径（I-10 / PS-10）：整体替换 ha._requests（fake 对象，绝不 patch requests.get 属性），
patch 读路径 _read_config / _get_ha_client / _read_services_cache + 写路径 _write_config，
并把 CONFIG_PATH / SERVICES_CACHE_PATH 指向 tmp_path；_ws_batch_call 与两个 registry 取数
函数一律替换 ⇒ 用例既不连真实 HA，也不读写真实 ~/.niu/ha-config.json。
"""
import asyncio
import json
import os
import sys
import time

import pytest
import websockets

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp-servers", "ha-server", "src"))
import niu_ha_server as ha  # noqa: E402

URL = "http://ha.test"
HEADERS = {"Authorization": "Bearer tok"}

# 场景：config_key 是 uuid，HA 侧真实实体 id 是 alias slug —— 两者不同源（§1.2 R1）
SCENE_KEY = "1b8418f1c9b14a9a9b6f0f9a3d0d0aa1"
SCENE_ID = "scene.mcp_dup_scene"
SCENE_KEY_2 = "aaaabbbbccccddddeeeeffff00001111"
SCENE_ID_2 = "scene.another_scene"
SCRIPT_KEY = "mcp_dup_script"
SCRIPT_ID = "script.mcp_dup_script"
# 中文名实测值（§7）：scene.ce_shi_chang_jing 的 unique_id == config_key
ZH_SCENE_NAME = "测试场景"
ZH_SCENE_ID = "scene.ce_shi_chang_jing"
ZH_SCENE_KEY = "32fd6843a1b24c8e9a0b7f5d0c1e2f30"
ZH_SCRIPT_NAME = "测试脚本"
ZH_SCRIPT_KEY = "29edf933d4e546f7a8b9c0d1e2f3a4b5"
ZH_SCRIPT_ID = f"script.{ZH_SCRIPT_KEY}"


def _state(entity_id, name=None, state="unknown", **attrs):
    payload = dict(attrs)
    if name is not None:
        payload["friendly_name"] = name
    return {"entity_id": entity_id, "state": state, "attributes": payload}


def _reg(entity_id, unique_id):
    """entity_registry/list 的一条记录（只保留本修复关心的两个键）。"""
    return {"entity_id": entity_id, "unique_id": unique_id}


class _Resp:
    def __init__(self, status_code=200, data=None, text=""):
        self.status_code = status_code
        self._data = {} if data is None else data
        self.text = text

    def json(self):
        return self._data


class _Requests:
    """ha._requests 的替身：routes = [(method, url 子串, 响应 | handler)]，首条命中生效。

    未匹配到的调用登记进 `unmatched` 后抛错——避免用例因为漏 route 而静默走到真实网络栈
    （ha 内部的 except Exception 会把抛出的错吞掉，所以 `env` fixture 收尾还要兜底断言）。
    """

    def __init__(self):
        self.routes = []
        self.calls = []
        self.unmatched = []
        self.created_key = ""

    def add(self, method, needle, behavior, first=False):
        entry = (method, needle, behavior)
        (self.routes.insert(0, entry) if first else self.routes.append(entry))

    def _dispatch(self, method, url, kwargs):
        self.calls.append((method, url))
        for m, needle, behavior in self.routes:
            if m == method and needle in url:
                resp = behavior(method, url, kwargs) if callable(behavior) else behavior
                if isinstance(resp, Exception):
                    raise resp
                return resp
        self.unmatched.append(f"{method} {url}")
        raise AssertionError(f"未预期的 HTTP 调用: {method} {url}")

    def get(self, url, headers=None, timeout=None):
        return self._dispatch("GET", url, {"headers": headers, "timeout": timeout})

    def post(self, url, headers=None, json=None, timeout=None):  # noqa: A002
        return self._dispatch("POST", url, {"headers": headers, "json": json, "timeout": timeout})

    def delete(self, url, headers=None, timeout=None):
        return self._dispatch("DELETE", url, {"headers": headers, "timeout": timeout})


class _Env:
    """一个用例的全部隔离面：读写路径 + HTTP + WS + registry 取数。"""

    def __init__(self, monkeypatch, tmp_path):
        self.mp = monkeypatch
        self.cfg = {
            "ha_url": URL,
            "ha_token": "tok",
            "triggers": [],
            "scene_name_map": {},
            "script_name_map": {},
        }
        self.requests = _Requests()
        self.writes = []
        self.unregistered = []
        self.ws_calls = 0
        self.strict_calls = []
        self.legacy_calls = []
        self.states_calls = []
        self._ws_result = ([], [], [])
        self._strict_result = []
        self._legacy_registry = []

        # 写路径与文件路径双保险：即使某条路径漏 patch，也只落到 tmp
        monkeypatch.setattr(ha, "CONFIG_PATH", str(tmp_path / "ha-config.json"))
        monkeypatch.setattr(ha, "SERVICES_CACHE_PATH", str(tmp_path / "ha-services.json"))
        # 读路径（PS-10）
        monkeypatch.setattr(ha, "_read_config", lambda: self.cfg)
        monkeypatch.setattr(ha, "_get_ha_client", lambda config: (URL, dict(HEADERS), None))
        monkeypatch.setattr(ha, "_read_services_cache", lambda: {})
        # 写路径（PS-10）：_atomic_update 就地改 cfg，所以 _lookup_slug 能读到注册/清理结果
        monkeypatch.setattr(ha, "_write_config", lambda config: self.writes.append(config))
        monkeypatch.setattr(ha, "_requests", self.requests)
        monkeypatch.setattr(ha, "_unregister_name",
                            lambda domain, name: self.unregistered.append((domain, name)))
        # WS / registry 取数：绝不允许真实连接
        monkeypatch.setattr(ha, "_ws_batch_call", self._ws_batch_call)
        monkeypatch.setattr(ha, "_fetch_entity_registry_strict", self._strict)
        monkeypatch.setattr(ha, "_fetch_entity_registry", self._legacy)

    def _ws_batch_call(self, ha_url, ha_token, commands, timeout=20):
        self.ws_calls += 1
        return tuple(self._ws_result)

    def _strict(self, ha_url, headers, timeout=2.0):
        self.strict_calls.append(timeout)
        return self._strict_result() if callable(self._strict_result) else self._strict_result

    def _legacy(self, ha_url, headers):
        self.legacy_calls.append(ha_url)
        return self._legacy_registry

    # --- 装配辅助 ---
    def set_states_route(self, states):
        self.requests.add("GET", "/api/states", _Resp(200, states))

    def set_ws(self, devices, areas, entities_raw):
        self._ws_result = (devices, areas, entities_raw)

    def set_strict(self, value):
        self._strict_result = value

    def set_legacy_registry(self, value):
        self._legacy_registry = value

    def patch_states(self, result):
        """替换 _fetch_domain_states（固定返回值 + 记录调用域）。"""
        def _fetch(ha_url, headers, domain):
            self.states_calls.append(domain)
            return result
        self.mp.setattr(ha, "_fetch_domain_states", _fetch)

    def http_calls(self, method, needle):
        return [u for (m, u) in self.requests.calls if m == method and needle in u]


@pytest.fixture
def env(monkeypatch, tmp_path):
    fake_env = _Env(monkeypatch, tmp_path)
    yield fake_env
    # ha 内部普遍 except Exception ⇒ 漏配 route 抛的 AssertionError 可能被吞掉，用例仍"绿"。
    # 收尾兜底：任何未匹配的 HTTP 调用都必须让用例失败。
    assert fake_env.requests.unmatched == [], f"漏配 HTTP route: {fake_env.requests.unmatched}"


def rows_named(rows, name):
    return [r for r in rows if r.get("name") == name]


def rows_by_prefix(rows, prefix):
    return [r for r in rows if str(r.get("entity_id") or "").startswith(prefix)]


# ---------------------------------------------------------------- 行 1

class TestStatusSceneDedupe:
    """行 1：ha_status 场景补录去重（entity_id 主判据 + 归一化 name 次判据）。"""

    def _run(self, env, name_map, registry):
        env.set_states_route([_state(SCENE_ID, "MCP Dup Scene", state="scening"),
                              _state("light.k", "Light K")])
        env.set_ws([], [], registry)
        env.cfg["scene_name_map"] = name_map
        return ha.ha_status()

    def test_same_entity_id_is_not_appended_again(self, env):
        """registry 解析出的 entity_id 已在 states 中 ⇒ 不再补录（映射名不同也一样）。"""
        out = self._run(env, {"Renamed In HA": SCENE_KEY}, [_reg(SCENE_ID, SCENE_KEY)])
        assert out["connected"] is True
        assert len(rows_named(out["scenes"], "Renamed In HA")) == 0
        assert len(out["scenes"]) == 1
        assert out["scenes"][0]["entity_id"] == SCENE_ID

    def test_same_normalized_name_is_not_appended_again(self, env):
        """大小写/空白差异经 casefold().strip() 归一 ⇒ name 次判据同样拦下补录。"""
        out = self._run(env, {"  MCP DUP Scene ": SCENE_KEY_2}, [_reg(SCENE_ID_2, SCENE_KEY_2)])
        assert len(out["scenes"]) == 1
        assert out["scenes"][0]["entity_id"] == SCENE_ID

    def test_genuine_supplement_uses_registry_entity_id(self, env):
        """确实缺项 ⇒ 补 1 条，entity_id 取 registry 真值，不用 config_key 拼接。"""
        out = self._run(env, {"Missing Scene": SCENE_KEY_2}, [_reg(SCENE_ID_2, SCENE_KEY_2)])
        assert len(rows_named(out["scenes"], "Missing Scene")) == 1
        row = rows_named(out["scenes"], "Missing Scene")[0]
        assert row["entity_id"] == SCENE_ID_2
        assert row["entity_id"] != f"scene.{SCENE_KEY_2}"
        assert row["state"] == "unavailable"
        assert "note" not in row

    def test_supplement_without_registry_match_is_null_id(self, env):
        """registry 可用但无匹配 ⇒ entity_id null + state unavailable + note。"""
        out = self._run(env, {"Missing Scene": SCENE_KEY_2}, [])
        assert len(rows_named(out["scenes"], "Missing Scene")) == 1
        row = rows_named(out["scenes"], "Missing Scene")[0]
        assert row["entity_id"] is None
        assert row["state"] == "unavailable"
        assert "注册表中无该 config_key 对应的实体" in row["note"]

    def test_states_entries_are_kept_in_front_untouched(self, env):
        """补录只追加，不删除/替换/重排 states 条目（INV-3）。"""
        out = self._run(env, {"Missing Scene": SCENE_KEY_2}, [_reg(SCENE_ID_2, SCENE_KEY_2)])
        assert [s["name"] for s in out["scenes"]] == ["MCP Dup Scene", "Missing Scene"]


# ---------------------------------------------------------------- 行 2

class TestStatusScriptDedupe:
    """行 2：ha_status 脚本补录去重（R3：判重集合必须由未过滤 states 播种）。"""

    def _run(self, env, states, name_map):
        env.set_states_route(states)
        env.set_ws([], [], [])
        env.cfg["script_name_map"] = name_map
        return ha.ha_status()

    def test_no_ghost_idle_duplicate(self, env):
        """脚本已在 states 中 ⇒ 只有一条，且 state 是真实值，不是凭空 idle。"""
        out = self._run(env, [_state(SCRIPT_ID, "MCP Dup Script", state="off")],
                        {"MCP Dup Script": SCRIPT_KEY})
        assert len(rows_named(out["devices"], "MCP Dup Script")) == 1
        row = rows_named(out["devices"], "MCP Dup Script")[0]
        assert row["entity_id"] == SCRIPT_ID
        assert row["state"] == "off"

    def test_supplement_builds_real_script_id_and_keeps_type(self, env):
        """脚本未加载 ⇒ 补 1 条，entity_id 直构 script.<config_key>，保留 type 键。"""
        out = self._run(env, [_state("light.k", "Light K")], {"Ghost Script": "ghost_key"})
        assert len(rows_named(out["devices"], "Ghost Script")) == 1
        row = rows_named(out["devices"], "Ghost Script")[0]
        assert row["entity_id"] == "script.ghost_key"
        assert row["type"] == "脚本"
        assert "note" not in row

    def test_name_criterion_blocks_duplicate_with_other_key(self, env):
        """同名不同 config_key（states 已有同名）⇒ name 次判据拦下补录（PS-7）。"""
        out = self._run(env, [_state(SCRIPT_ID, "MCP Dup Script", state="off")],
                        {"MCP Dup Script": "another_key"})
        assert len(rows_by_prefix(out["devices"], "script.")) == 1


# ---------------------------------------------------------------- 行 3

class TestStatusFilter:
    """行 3：ha_status 过滤语义——domain 只留同域补录；area / entity_id 不追加补录。"""

    def _run(self, env, entity_registry=None, **kwargs):
        env.set_states_route([_state("light.k", "Light K"),
                              _state(SCENE_ID, "MCP Dup Scene"),
                              _state(SCRIPT_ID, "MCP Dup Script")])
        env.set_ws([{"id": "d1", "name": "Light K", "area_id": "living"}],
                   [{"area_id": "living", "name": "客厅"}],
                   [_reg(SCENE_ID, SCENE_KEY)] if entity_registry is None else entity_registry)
        env.cfg["scene_name_map"] = {"Missing Scene": SCENE_KEY_2}
        env.cfg["script_name_map"] = {"Ghost Script": "ghost_key"}
        return ha.ha_status(**kwargs)

    def test_domain_filter_drops_other_domain_supplements(self, env):
        out = self._run(env, domain="light")
        assert out["connected"] is True
        assert out["scenes"] == []
        assert rows_by_prefix(out["devices"], "script.") == []
        assert [d["entity_id"] for d in out["devices"]] == ["light.k"]

    def test_same_domain_supplement_survives_domain_filter(self, env):
        out = self._run(env, domain="script")
        assert [d["name"] for d in out["devices"]] == ["MCP Dup Script", "Ghost Script"]
        assert out["scenes"] == []

    def test_area_filter_appends_no_supplement(self, env):
        """area 非空 ⇒ 不追加任何补录行；states 命中行仍按原过滤语义保留。"""
        out = self._run(env, area="Dup")
        assert [s["entity_id"] for s in out["scenes"]] == [SCENE_ID]
        assert rows_named(out["scenes"], "Missing Scene") == []
        assert [d["entity_id"] for d in rows_by_prefix(out["devices"], "script.")] == [SCRIPT_ID]
        assert rows_named(out["devices"], "Ghost Script") == []

    def test_area_filter_drops_states_rows_with_mismatched_area(self, env):
        """area 走 area_name 分支：只有设备区域名匹配的行留下，name/area 都不匹配的被丢弃。

        area="Dup" 那条用例命中的是 `area in name` 分支（实体名里含 Dup），从未验证过
        area_name 匹配 / 丢弃分支，这里用「客厅」补齐。
        """
        out = self._run(env,
                        entity_registry=[{**_reg("light.k", "light_k_uid"), "device_id": "d1"},
                                         _reg(SCENE_ID, SCENE_KEY)],
                        area="客厅")
        assert out["connected"] is True
        assert out["devices"][0]["area"] == "客厅"
        assert [d["entity_id"] for d in out["devices"]] == ["light.k"]
        # scene/script 的 states 行 name 不含「客厅」、也没有设备区域 ⇒ 丢弃，且不追加补录
        assert out["scenes"] == []
        assert rows_by_prefix(out["devices"], "script.") == []

    def test_single_entity_mode_appends_no_supplement(self, env):
        out = self._run(env, entity_id="light.k")
        assert out["scenes"] == []
        assert rows_by_prefix(out["devices"], "script.") == []
        assert [d["entity_id"] for d in out["devices"]] == ["light.k"]

    def test_dedupe_sets_are_built_from_unfiltered_states(self, env):
        """判重集合来自未过滤 states：domain=script 时已在 states 的脚本仍不重复补录。"""
        env.set_states_route([_state(SCRIPT_ID, "MCP Dup Script", state="off")])
        env.set_ws([], [], [])
        env.cfg["script_name_map"] = {"MCP Dup Script": SCRIPT_KEY}
        out = ha.ha_status(domain="script")
        assert len(rows_named(out["devices"], "MCP Dup Script")) == 1


# ---------------------------------------------------------------- 行 4

class TestStatusRegistryUnavailable:
    """行 4：注册表取数失败（_ws_batch_call 把失败命令压成 None）⇒ 补录行降级。"""

    def test_supplement_degrades_to_null_id_with_note(self, env):
        env.set_states_route([_state("light.k", "Light K")])
        env.set_ws([], [], None)
        env.cfg["scene_name_map"] = {"Missing Scene": SCENE_KEY_2}
        env.cfg["script_name_map"] = {"Ghost Script": "ghost_key"}
        out = ha.ha_status()
        assert len(rows_named(out["scenes"], "Missing Scene")) == 1
        row = rows_named(out["scenes"], "Missing Scene")[0]
        assert row["entity_id"] is None
        assert row["state"] == "unavailable"
        assert row["note"] == "注册表不可用，无法解析实体 id"

    def test_script_supplement_is_unaffected(self, env):
        """脚本域豁免解析：注册表失败也照样给出真实 script.<config_key>，不带 note。"""
        env.set_states_route([_state("light.k", "Light K")])
        env.set_ws([], [], None)
        env.cfg["script_name_map"] = {"Ghost Script": "ghost_key"}
        out = ha.ha_status()
        row = rows_named(out["devices"], "Ghost Script")[0]
        assert row["entity_id"] == "script.ghost_key"
        assert "note" not in row

    def test_empty_registry_is_not_reported_as_unavailable(self, env):
        """空表（[]）与取数失败（None）必须可区分。"""
        env.set_states_route([_state("light.k", "Light K")])
        env.set_ws([], [], [])
        env.cfg["scene_name_map"] = {"Missing Scene": SCENE_KEY_2}
        row = rows_named(ha.ha_status()["scenes"], "Missing Scene")[0]
        assert "注册表不可用" not in row["note"]
        assert "无该 config_key 对应的实体" in row["note"]


# ---------------------------------------------------------------- 行 5

class TestSceneListDedupe:
    """行 5：ha_scene list 去重（同名仅 1 条，entity_id 为真值）。"""

    def test_duplicate_mapping_is_not_appended(self, env):
        env.set_states_route([_state(SCENE_ID, "MCP Dup Scene", state="scening")])
        env.cfg["scene_name_map"] = {"MCP Dup Scene": SCENE_KEY}
        env.set_strict([_reg(SCENE_ID, SCENE_KEY)])
        out = ha.ha_scene("list")
        assert len(rows_named(out["scenes"], "MCP Dup Scene")) == 1
        assert out["scenes"][0]["entity_id"] == SCENE_ID

    def test_list_fetches_registry_only_once(self, env):
        """INV-6 / I-11：一次 list 最多一次 registry 取数，不逐实体回落。"""
        env.set_states_route([_state(SCENE_ID, "MCP Dup Scene"),
                              _state("scene.other", "Other Scene")])
        env.cfg["scene_name_map"] = {"MCP Dup Scene": SCENE_KEY}
        env.set_strict([_reg(SCENE_ID, SCENE_KEY)])
        ha.ha_scene("list")
        assert len(env.strict_calls) == 1
        assert env.legacy_calls == []

    def test_unreachable_states_keeps_error_shape(self, env):
        """INV-4：states 不可达仍返回 _ha_unavailable_error()，不引入第三种形态。"""
        env.patch_states(None)
        assert ha.ha_scene("list") == ha._ha_unavailable_error()


# ---------------------------------------------------------------- 行 6

class TestSceneListSupplement:
    """行 6：ha_scene list 补缺——registry 有 unique_id 匹配 ⇒ 补 1 条真值 entity_id。"""

    @pytest.mark.parametrize("detail", [False, True])
    def test_missing_scene_is_supplemented_with_real_id(self, env, detail):
        env.set_states_route([])
        env.requests.add("GET", "api/config/scene/config",
                         _Resp(200, {"name": "Missing Scene", "entities": {"light.k": {"state": "on"}}}))
        env.cfg["scene_name_map"] = {"Missing Scene": SCENE_KEY_2}
        env.set_strict([_reg(SCENE_ID_2, SCENE_KEY_2)])
        out = ha.ha_scene("list", detail=detail)
        assert len(out["scenes"]) == 1
        row = out["scenes"][0]
        assert row["entity_id"] == SCENE_ID_2
        assert row["entity_id"] != f"scene.{SCENE_KEY_2}"
        assert row["state"] == "unavailable"
        assert "note" not in row
        if detail:
            assert row["config"]["name"] == "Missing Scene"
            assert len(env.strict_calls) == 1


# ---------------------------------------------------------------- 行 7

class TestSceneListUnresolvable:
    """行 7：ha_scene list 不可解析三种来源（[] / 无匹配 / None）。"""

    @pytest.mark.parametrize("registry,expected_note", [
        ([], "注册表中无该 config_key 对应的实体"),
        ([_reg("scene.someone_elses", "unrelated_key")], "注册表中无该 config_key 对应的实体"),
        (None, "注册表不可用，无法解析实体 id"),
    ])
    def test_supplement_degrades_without_fabricating_id(self, env, registry, expected_note):
        env.set_states_route([])
        env.cfg["scene_name_map"] = {"Missing Scene": SCENE_KEY_2}
        env.set_strict(registry)
        out = ha.ha_scene("list")
        assert len(out["scenes"]) == 1
        row = out["scenes"][0]
        assert row["entity_id"] is None
        assert row["state"] == "unavailable"
        assert expected_note in row["note"]

    def test_states_rows_still_return_when_registry_fails(self, env):
        """PS-9：注册表取数失败只降级补录行，states 条目照常返回。"""
        env.set_states_route([_state(SCENE_ID, "MCP Dup Scene")])
        env.cfg["scene_name_map"] = {"Missing Scene": SCENE_KEY_2}
        env.set_strict(None)
        out = ha.ha_scene("list")
        assert [s["entity_id"] for s in out["scenes"]] == [SCENE_ID, None]
        assert len(env.strict_calls) == 1


# ---------------------------------------------------------------- 行 8

class TestScriptListDedupe:
    """行 8：ha_script list 去重 + 脚本域不取 registry。"""

    def test_no_duplicate_and_no_registry_call(self, env):
        env.patch_states([_state(SCRIPT_ID, "MCP Dup Script", state="off")])
        env.cfg["script_name_map"] = {"MCP Dup Script": SCRIPT_KEY}
        out = ha.ha_script("list")
        assert len(rows_named(out["scripts"], "MCP Dup Script")) == 1
        assert out["scripts"][0]["entity_id"] == SCRIPT_ID
        assert env.strict_calls == []
        assert env.legacy_calls == []

    def test_supplement_uses_direct_script_id(self, env):
        env.patch_states([])
        env.cfg["script_name_map"] = {"Ghost Script": "ghost_key"}
        out = ha.ha_script("list")
        assert out["scripts"] == [{"name": "Ghost Script", "entity_id": "script.ghost_key",
                                   "state": "idle"}]
        assert env.strict_calls == []
        assert env.legacy_calls == []

    def test_duplicate_by_name_only(self, env):
        env.patch_states([_state(SCRIPT_ID, "MCP Dup Script", state="off")])
        env.cfg["script_name_map"] = {"MCP Dup Script": "another_key"}
        out = ha.ha_script("list")
        assert len(out["scripts"]) == 1


# ---------------------------------------------------------------- 行 9

class TestSceneCreateSnapshotTruth:
    """行 9：create / snapshot 的 entity_id 只可能是 registry 解析到的真值。"""

    def _create_routes(self, env):
        def _capture(_method, url, _kwargs):
            env.requests.created_key = url.rsplit("/", 1)[-1]
            return _Resp(200, {})

        env.requests.add("POST", "api/config/scene/config", _capture)
        env.requests.add("POST", "api/services/scene/reload", _Resp(200, []))
        env.requests.add("GET", "api/config/scene/config",
                         lambda _m, _u, _k: _Resp(200, {
                             "id": env.requests.created_key,
                             "name": "MCP Dup Scene",
                             "entities": {"light.k": {"state": "on"}},
                         }))

    def test_create_returns_real_entity_id(self, env):
        self._create_routes(env)
        env.set_strict(lambda: [_reg(SCENE_ID, env.requests.created_key)])
        out = ha.ha_scene("create", name="MCP Dup Scene",
                          config={"entities": {"light.k": {"state": "on"}}})
        assert out["success"] is True
        assert out["entity_id"] == SCENE_ID
        assert out["entity_id"] != f"scene.{env.requests.created_key}"
        assert "warning" not in out
        assert env.cfg["scene_name_map"]["MCP Dup Scene"] == env.requests.created_key
        assert env.writes, "映射写入路径必须被走到（且只落到 tmp）"

    def test_create_warns_when_registry_has_no_match(self, env):
        """success 只取决于配置写入；解析不到 ⇒ entity_id null + warning（I-7）。"""
        self._create_routes(env)
        env.set_strict([])
        out = ha.ha_scene("create", name="MCP Dup Scene",
                          config={"entities": {"light.k": {"state": "on"}}})
        assert out["success"] is True
        assert out["entity_id"] is None
        assert "未在注册表中解析到实体" in out["warning"]

    def test_create_warns_when_registry_unreachable(self, env):
        """全部轮次取数失败 ⇒ 哨兵映射为 null + 「HA 不可达」warning。"""
        self._create_routes(env)
        env.set_strict(None)
        out = ha.ha_scene("create", name="MCP Dup Scene",
                          config={"entities": {"light.k": {"state": "on"}}})
        assert out["success"] is True
        assert out["entity_id"] is None
        assert "HA 不可达" in out["warning"]

    def test_snapshot_returns_real_entity_id(self, env):
        env.requests.add("GET", "/api/states/light.k",
                         _Resp(200, {"entity_id": "light.k", "state": "on",
                                     "attributes": {"friendly_name": "Light K"}}))
        self._create_routes(env)
        env.set_strict(lambda: [_reg(SCENE_ID, env.requests.created_key)])
        out = ha.ha_scene("snapshot", name="MCP Dup Scene", entity_ids=["light.k"])
        assert out["success"] is True
        assert out["entity_id"] == SCENE_ID
        assert out["entity_id"] != f"scene.{env.requests.created_key}"
        assert "warning" not in out


# ---------------------------------------------------------------- 行 10

class TestSceneFallbackNoGhostId:
    """行 10：get / update / delete 回退分支不再拼 scene.<uuid>。"""

    NAME = "Missing Scene"

    def _fallback_env(self, env, registry):
        env.patch_states([])
        env.cfg["scene_name_map"] = {self.NAME: SCENE_KEY_2}
        env.set_strict(registry)
        env.requests.add("GET", "api/config/scene/config",
                         _Resp(200, {"name": self.NAME, "entities": {"light.k": {"state": "on"}}}))
        env.requests.add("POST", "api/config/scene/config", _Resp(200, {}))
        env.requests.add("POST", "api/services/scene/reload", _Resp(200, []))

    def test_get_resolves_entity_id_from_registry(self, env):
        self._fallback_env(env, [_reg(SCENE_ID_2, SCENE_KEY_2)])
        out = ha.ha_scene("get", name=self.NAME)
        assert out["entity_id"] == SCENE_ID_2
        assert "note" not in out
        assert f"scene.{SCENE_KEY_2}" not in json.dumps(out, ensure_ascii=False)
        assert len(env.strict_calls) == 1

    def test_get_nulls_entity_id_when_no_match(self, env):
        self._fallback_env(env, [])
        out = ha.ha_scene("get", name=self.NAME)
        assert out["entity_id"] is None
        assert "注册表中无该 config_key 对应的实体" in out["note"]
        assert "config" in out

    def test_update_resolves_entity_id_from_registry(self, env):
        self._fallback_env(env, [_reg(SCENE_ID_2, SCENE_KEY_2)])
        out = ha.ha_scene("update", name=self.NAME, config={"entities": {"light.k": {"state": "off"}}})
        assert out["success"] is True
        assert out["entity_id"] == SCENE_ID_2
        assert "note" not in out

    def test_delete_preview_resolves_entity_id_from_registry(self, env):
        self._fallback_env(env, [_reg(SCENE_ID_2, SCENE_KEY_2)])
        out = ha.ha_scene("delete", name=self.NAME)
        assert out["preview"] is True
        assert out["entity_id"] == SCENE_ID_2
        assert "note" not in out

    def test_delete_preview_nulls_entity_id_with_note(self, env):
        self._fallback_env(env, None)
        out = ha.ha_scene("delete", name=self.NAME)
        assert out["preview"] is True
        assert out["entity_id"] is None
        assert out["note"] == "注册表不可用，无法解析实体 id"

    def test_states_down_returns_unavailable_without_registry_fetch(self, env):
        """states 不可达且映射未命中 ⇒ 直接 _ha_unavailable_error()，不发起 registry 取数。"""
        env.patch_states(None)
        out = ha.ha_scene("get", name=self.NAME)
        assert out == ha._ha_unavailable_error()
        assert env.strict_calls == []
        assert env.legacy_calls == []

    def test_states_down_still_operates_when_map_hits(self, env):
        """映射命中时 states 不可达仍可凭 config_key 取配置，entity_id null + states note。"""
        env.patch_states(None)
        env.cfg["scene_name_map"] = {self.NAME: SCENE_KEY_2}
        env.requests.add("GET", "api/config/scene/config",
                         _Resp(200, {"name": self.NAME, "entities": {"light.k": {"state": "on"}}}))
        out = ha.ha_scene("get", name=self.NAME)
        assert out["entity_id"] is None
        assert "HA states 不可达" in out["note"]
        assert env.strict_calls == []


# ---------------------------------------------------------------- 行 11

class Test404CleanupGuard:
    """行 11：404 分支的清理必须先核对现读映射 == config_key（I-12 / INV-7）。"""

    NAME = "MCP Dup Scene"
    RESOLVED_KEY = "aaa-key"
    MAPPED_KEY_MATCH = "aaa-key"
    MAPPED_KEY_OTHER = "bbb-key"

    def _states_hit_env(self, env, mapped_key):
        env.patch_states([_state(SCENE_ID, self.NAME)])
        env.set_strict([_reg(SCENE_ID, self.RESOLVED_KEY)])
        env.cfg["scene_name_map"] = {self.NAME: mapped_key}

    @pytest.mark.parametrize("mapped_key,expect_note,cleaned", [
        (MAPPED_KEY_MATCH, "配置已不存在，已清理本地映射", True),
        (MAPPED_KEY_OTHER, "HA 中已无该配置，本地映射保留", False),
    ])
    def test_delete_404_cleans_only_matching_map(self, env, mapped_key, expect_note, cleaned):
        self._states_hit_env(env, mapped_key)
        env.requests.add("DELETE", "api/config/scene/config", _Resp(404, {}, "not found"))
        out = ha.ha_scene("delete", name=self.NAME, confirm=True)
        assert out["success"] is True
        assert out["deleted"] == self.NAME
        assert out["note"] == expect_note
        assert env.unregistered == ([("scene", self.NAME)] if cleaned else [])

    def test_delete_success_path_clears_unconditionally(self, env):
        """2xx 成功路径沿用既有无条件清理（R1），不受映射校验影响。"""
        self._states_hit_env(env, self.MAPPED_KEY_OTHER)
        env.requests.add("DELETE", "api/config/scene/config", _Resp(200, {}))
        env.requests.add("POST", "api/services/scene/reload", _Resp(200, []))
        out = ha.ha_scene("delete", name=self.NAME, confirm=True)
        assert out == {"success": True, "deleted": self.NAME}
        assert env.unregistered == [("scene", self.NAME)]

    @pytest.mark.parametrize("states,registry,expect_entity_id", [
        ([_state(SCENE_ID, NAME)], [_reg(SCENE_ID, RESOLVED_KEY)], SCENE_ID),
        ([], [], None),
    ])
    def test_get_404_cleanup_branch_shape(self, env, states, registry, expect_entity_id):
        """清理分支：不带 success 键，entity_id 回显真实 id 或 null，带 note。

        两种来源：states 命中（registry 解析出 config_key）／states 未命中走映射回退。
        """
        env.patch_states(states)
        env.set_strict(registry)
        env.cfg["scene_name_map"] = {self.NAME: self.MAPPED_KEY_MATCH}
        env.requests.add("GET", "api/config/scene/config", _Resp(404, {}, "not found"))
        out = ha.ha_scene("get", name=self.NAME)
        assert "success" not in out
        assert "error" not in out
        assert out["name"] == self.NAME
        assert out["entity_id"] == expect_entity_id
        assert out["note"] == "配置已不存在，已清理本地映射"
        assert env.unregistered == [("scene", self.NAME)]

    def test_get_404_mismatch_keeps_error_shape(self, env):
        """映射指向别的 config_key ⇒ 保留原 error 形态，不清理映射。"""
        self._states_hit_env(env, self.MAPPED_KEY_OTHER)
        env.requests.add("GET", "api/config/scene/config", _Resp(404, {}, "not found"))
        out = ha.ha_scene("get", name=self.NAME)
        assert out == {"error": "HA API 返回 404: not found"}
        assert env.unregistered == []

    def test_list_detail_404_cleans_only_supplement_rows(self, env):
        """list detail 的 404 清理只作用于补录行；states 命中行不清理。"""
        env.set_states_route([_state(SCENE_ID, self.NAME)])
        env.set_strict([_reg(SCENE_ID, self.MAPPED_KEY_MATCH)])
        env.cfg["scene_name_map"] = {self.NAME: self.MAPPED_KEY_MATCH,
                                     "Ghost Scene": "ghost-key"}
        env.requests.add("GET", "api/config/scene/config", _Resp(404, {}, "not found"))
        out = ha.ha_scene("list", detail=True)
        assert len(out["scenes"]) == 2
        assert env.unregistered == [("scene", "Ghost Scene")]


# ---------------------------------------------------------------- 行 12

class _FakeWS:
    def __init__(self, frames, hang=False):
        self._frames = list(frames)
        self._hang = hang
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def recv(self):
        if self._hang:
            await asyncio.Event().wait()
        if not self._frames:
            raise AssertionError("fake WS 帧已耗尽")
        return json.dumps(self._frames.pop(0))


class TestFetchEntityRegistryStrict:
    """行 12：strict 取数的成功 / 失败 / 超时三态（自建 WS，整段自带 deadline）。"""

    AUTH_HANDSHAKE = [{"type": "auth_required"}, {"type": "auth_ok"}]

    @pytest.fixture
    def ws(self, monkeypatch):
        box = {"connect": None}

        def _install(frames, hang=False):
            def _connect(ws_url, **kwargs):
                box["ws_url"] = ws_url
                box["kwargs"] = kwargs
                return _FakeWS(frames, hang=hang)

            monkeypatch.setattr(websockets, "connect", _connect)
            return box

        return _install

    def test_returns_list_and_skips_frames_without_id(self, ws):
        box = ws(self.AUTH_HANDSHAKE + [
            {"type": "event", "event": {"event_type": "themes_updated"}},
            {"id": 1, "type": "result", "success": True, "result": [_reg(SCENE_ID, SCENE_KEY)]},
        ])
        out = ha._fetch_entity_registry_strict(URL, HEADERS)
        assert out == [_reg(SCENE_ID, SCENE_KEY)]
        assert box["ws_url"] == "ws://ha.test/api/websocket"

    def test_empty_result_is_a_list_not_none(self, ws):
        ws(self.AUTH_HANDSHAKE + [{"id": 1, "success": True, "result": []}])
        assert ha._fetch_entity_registry_strict(URL, HEADERS) == []

    def test_unsuccessful_result_is_none(self, ws):
        ws(self.AUTH_HANDSHAKE + [{"id": 1, "success": False}])
        assert ha._fetch_entity_registry_strict(URL, HEADERS) is None

    def test_auth_rejected_is_none(self, ws):
        ws([{"type": "auth_required"}, {"type": "auth_not_required"}])
        assert ha._fetch_entity_registry_strict(URL, HEADERS) is None

    def test_hanging_connection_returns_none_within_deadline(self, ws):
        ws(self.AUTH_HANDSHAKE, hang=True)
        started = time.time()
        assert ha._fetch_entity_registry_strict(URL, HEADERS, timeout=0.3) is None
        assert time.time() - started < 1.5


# ---------------------------------------------------------------- 行 13

class TestVerifyEntityExistsStates:
    """行 13：_verify_entity_exists 的三态语义与改动前一致（D-7）。"""

    def test_real_entity_id(self, env):
        env.patch_states([_state("automation.mcp_rule")])
        env.set_legacy_registry([_reg("automation.mcp_rule", "cfg-1")])
        assert ha._verify_entity_exists(URL, HEADERS, "automation", "cfg-1", timeout=0) == "automation.mcp_rule"

    def test_genuinely_missing_is_none(self, env):
        env.patch_states([_state("automation.mcp_rule")])
        env.set_legacy_registry([_reg("automation.other", "cfg-2")])
        assert ha._verify_entity_exists(URL, HEADERS, "automation", "cfg-1", timeout=0) is None

    def test_unavailable_is_sentinel_not_none(self, env):
        env.patch_states(None)
        assert ha._verify_entity_exists(URL, HEADERS, "automation", "cfg-1", timeout=0) is ha._VERIFY_UNAVAILABLE


# ---------------------------------------------------------------- 行 14

class TestRegistryFailureNoRaise:
    """行 14：states 命中分支的 registry 取数失败 ⇒ 正常结构，不抛异常（I-6 / PS-13）。"""

    NAME = "MCP Dup Scene"

    def _hit_env(self, env):
        env.patch_states([_state(SCENE_ID, self.NAME)])
        env.set_strict(None)
        env.cfg["scene_name_map"] = {self.NAME: SCENE_KEY}
        env.requests.add("GET", "api/config/scene/config",
                         _Resp(200, {"name": self.NAME, "entities": {"light.k": {"state": "on"}}}))
        env.requests.add("POST", "api/config/scene/config", _Resp(200, {}))
        env.requests.add("POST", "api/services/scene/reload", _Resp(200, []))

    def test_get(self, env):
        self._hit_env(env)
        out = ha.ha_scene("get", name=self.NAME)
        assert "error" not in out
        assert out["entity_id"] == SCENE_ID
        assert len(env.strict_calls) == 1

    def test_update(self, env):
        self._hit_env(env)
        out = ha.ha_scene("update", name=self.NAME, config={"entities": {"light.k": {"state": "off"}}})
        assert out["success"] is True
        assert out["entity_id"] == SCENE_ID

    def test_delete_preview(self, env):
        self._hit_env(env)
        out = ha.ha_scene("delete", name=self.NAME)
        assert out["preview"] is True
        assert out["entity_id"] == SCENE_ID

    def test_activate_falls_back_to_apply(self, env):
        env.patch_states([_state(SCENE_ID, self.NAME)])
        env.set_strict(None)
        env.cfg["scene_name_map"] = {self.NAME: SCENE_KEY}
        env.requests.add("GET", "api/config/scene/config",
                         _Resp(200, {"name": self.NAME, "entities": {"light.k": {"state": "on"}}}))
        env.requests.add("POST", "api/services/scene/apply", _Resp(200, []))
        out = ha.ha_scene("activate", name=self.NAME)
        assert out["success"] is True
        assert out["method"] == "apply"
        assert "entity_id" not in out or out.get("entity_id") is None


# ---------------------------------------------------------------- 行 15

class TestChineseNames:
    """行 15：中文名场景/脚本的去重与 entity_id 规则与 ASCII 名一致（§1.1 中文实测）。"""

    def test_status_scene_dedupe(self, env):
        env.set_states_route([_state(ZH_SCENE_ID, ZH_SCENE_NAME, state="scening")])
        env.set_ws([], [], [_reg(ZH_SCENE_ID, ZH_SCENE_KEY)])
        env.cfg["scene_name_map"] = {ZH_SCENE_NAME: ZH_SCENE_KEY}
        out = ha.ha_status()
        assert len(rows_named(out["scenes"], ZH_SCENE_NAME)) == 1
        assert out["scenes"][0]["entity_id"] == ZH_SCENE_ID

    def test_status_script_dedupe(self, env):
        env.set_states_route([_state(ZH_SCRIPT_ID, ZH_SCRIPT_NAME, state="off")])
        env.set_ws([], [], [])
        env.cfg["script_name_map"] = {ZH_SCRIPT_NAME: ZH_SCRIPT_KEY}
        out = ha.ha_status()
        assert len(rows_named(out["devices"], ZH_SCRIPT_NAME)) == 1
        assert out["devices"][-1]["entity_id"] == ZH_SCRIPT_ID
        assert out["devices"][-1]["state"] == "off"

    def test_scene_list_dedupe(self, env):
        env.set_states_route([_state(ZH_SCENE_ID, ZH_SCENE_NAME)])
        env.cfg["scene_name_map"] = {ZH_SCENE_NAME: ZH_SCENE_KEY}
        env.set_strict([_reg(ZH_SCENE_ID, ZH_SCENE_KEY)])
        out = ha.ha_scene("list")
        assert len(out["scenes"]) == 1
        assert out["scenes"][0]["entity_id"] == ZH_SCENE_ID

    def test_script_list_dedupe(self, env):
        env.patch_states([_state(ZH_SCRIPT_ID, ZH_SCRIPT_NAME, state="off")])
        env.cfg["script_name_map"] = {ZH_SCRIPT_NAME: ZH_SCRIPT_KEY}
        out = ha.ha_script("list")
        assert len(out["scripts"]) == 1
        assert out["scripts"][0]["entity_id"] == ZH_SCRIPT_ID

    def test_create_never_returns_pinyin_slug_guess(self, env):
        """中文场景名：create 的 entity_id 来自 registry，不是音译 slug 猜测。"""
        def _capture(_method, url, _kwargs):
            env.requests.created_key = url.rsplit("/", 1)[-1]
            return _Resp(200, {})

        env.requests.add("POST", "api/config/scene/config", _capture)
        env.requests.add("POST", "api/services/scene/reload", _Resp(200, []))
        env.requests.add("GET", "api/config/scene/config",
                         lambda _m, _u, _k: _Resp(200, {"id": env.requests.created_key,
                                                        "name": ZH_SCENE_NAME,
                                                        "entities": {"light.k": {"state": "on"}}}))
        env.set_strict(lambda: [_reg(ZH_SCENE_ID, env.requests.created_key)])
        out = ha.ha_scene("create", name=ZH_SCENE_NAME, config={"entities": {"light.k": {"state": "on"}}})
        assert out["success"] is True
        assert out["entity_id"] == ZH_SCENE_ID
        assert out["entity_id"] != f"scene.{env.requests.created_key}"

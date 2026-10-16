"""§9 同族缺陷收口（2026-10-16）三态单测：_fetch_domain_states / _verify_entity_exists / MCP 处理器。

家族口径：失败必须与「空列表 / 无实体」可区分——
- _fetch_domain_states：成功 ⇒ list（可为空）；HTTP 非 200 / 异常 ⇒ None（不可用）
- _verify_entity_exists：entity_id（存在）/ None（真不存在）/ _VERIFY_UNAVAILABLE（不可用，不得当作「不存在」）
- MCP 处理器：states=None ⇒ 如实报「不可达」；states=[] ⇒ 保持原「未找到」语义
全部 mock，不依赖真实 HA。
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp-servers", "ha-server", "src"))
import niu_ha_server as ha  # noqa: E402


class _FakeRequests:
    """mock requests 模块：behavior = ("status", code, data) 或 ("exc", 异常)。"""

    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        if self.behavior[0] == "status":
            code, data = self.behavior[1], self.behavior[2]
            resp = types.SimpleNamespace(status_code=code, text=f"HTTP {code}")
            resp.json = lambda: data
            return resp
        raise self.behavior[1]


class TestFetchDomainStates:
    def test_http_500_returns_none(self, monkeypatch):
        monkeypatch.setattr(ha, "_requests", _FakeRequests(("status", 500, [])))
        assert ha._fetch_domain_states("http://ha", {}, "automation") is None

    def test_network_exception_returns_none(self, monkeypatch):
        monkeypatch.setattr(ha, "_requests", _FakeRequests(("exc", ConnectionError("boom"))))
        assert ha._fetch_domain_states("http://ha", {}, "automation") is None

    def test_200_empty_list_returns_empty_list(self, monkeypatch):
        """真 200 空列表 ⇒ []（该 domain 真的没有实体），不是 None。"""
        monkeypatch.setattr(ha, "_requests", _FakeRequests(("status", 200, [])))
        assert ha._fetch_domain_states("http://ha", {}, "automation") == []

    def test_200_filters_by_domain(self, monkeypatch):
        data = [
            {"entity_id": "automation.a", "state": "off"},
            {"entity_id": "scene.b", "state": "off"},
            {"entity_id": "automation.b", "state": "off"},
        ]
        monkeypatch.setattr(ha, "_requests", _FakeRequests(("status", 200, data)))
        result = ha._fetch_domain_states("http://ha", {}, "automation")
        assert [s["entity_id"] for s in result] == ["automation.a", "automation.b"]


class TestVerifyEntityExists:
    def test_unavailable_not_concluded_as_missing(self, monkeypatch):
        """HA 全程不可达（states 恒 None）⇒ 返回 _VERIFY_UNAVAILABLE 哨兵，而非 None（不存在）。"""
        monkeypatch.setattr(ha, "_fetch_domain_states", lambda *a, **kw: None)
        result = ha._verify_entity_exists("http://ha", {}, "automation", "cfg-1", timeout=0)
        assert result is ha._VERIFY_UNAVAILABLE
        assert result is not None  # 不得被当作「不存在」

    def test_found_entity_returns_entity_id(self, monkeypatch):
        monkeypatch.setattr(ha, "_fetch_domain_states",
                            lambda *a, **kw: [{"entity_id": "automation.new_x", "state": "off"}])
        monkeypatch.setattr(ha, "_fetch_entity_registry",
                            lambda *a, **kw: [{"unique_id": "cfg-1", "entity_id": "automation.new_x"}])
        assert ha._verify_entity_exists("http://ha", {}, "automation", "cfg-1", timeout=0) == "automation.new_x"

    def test_reachable_but_missing_returns_none(self, monkeypatch):
        """HA 可达（states 获取成功）但 entity 确实没有 ⇒ 真「不存在」⇒ None。"""
        monkeypatch.setattr(ha, "_fetch_domain_states", lambda *a, **kw: [])
        monkeypatch.setattr(ha, "_fetch_entity_registry", lambda *a, **kw: [])
        assert ha._verify_entity_exists("http://ha", {}, "automation", "cfg-1", timeout=0) is None


class TestMcpHandlersUnreachable:
    """处理器层：states=None ⇒ 如实报「不可达」；states=[] ⇒ 保持原「未找到」语义。"""

    @staticmethod
    def _patch_client(monkeypatch, behavior):
        monkeypatch.setattr(ha, "_read_config",
                            lambda: {"ha_url": "http://ha", "ha_token": "t"})
        monkeypatch.setattr(ha, "_requests", _FakeRequests(behavior))

    def test_automation_get_unreachable_reports_unavailable(self, monkeypatch):
        self._patch_client(monkeypatch, ("status", 500, []))
        result = ha.ha_automation(action="get", name="x")
        assert "不可达" in result["error"]
        assert "未找到" not in result["error"]

    def test_automation_get_empty_states_keeps_not_found(self, monkeypatch):
        self._patch_client(monkeypatch, ("status", 200, []))
        result = ha.ha_automation(action="get", name="x")
        assert "未找到" in result["error"]

    def test_scene_get_unreachable_reports_unavailable(self, monkeypatch):
        """场景有 name_map 回退路径：states=None 且回退也失败 ⇒ 仍报不可达，不报「未找到」。"""
        self._patch_client(monkeypatch, ("exc", ConnectionError("down")))
        monkeypatch.setattr(ha, "_lookup_slug", lambda *a: None)
        result = ha.ha_scene(action="get", name="x")
        assert "不可达" in result["error"]
        assert "未找到" not in result["error"]

    def test_script_get_unreachable_reports_unavailable(self, monkeypatch):
        """脚本三级查找全失败且 HA 不可达 ⇒ 报不可达，不报「未找到」。"""
        self._patch_client(monkeypatch, ("exc", ConnectionError("down")))
        monkeypatch.setattr(ha, "_lookup_slug", lambda *a: None)
        result = ha.ha_script(action="get", name="x")
        assert "不可达" in result["error"]
        assert "未找到" not in result["error"]

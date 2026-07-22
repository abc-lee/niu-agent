"""测试 http_log_router 在 logging.enabled=false 时不被 include。

缺省情况下 /http-log/ 端点不应存在（避免暴露 LLM 请求日志）。
只有 logging.enabled=true 才挂载路由。

注意：未挂载 http_log_router 时，GET /http-log/ 无匹配路由，返回 404 Not Found。
断言用 != 200 表达"HTTP log viewer 不暴露"，能正确处理 404 情况。
"""
import json
from fastapi.testclient import TestClient


def _build_app_with_logging(tmp_path, monkeypatch, logging_enabled: bool):
    """构建一个临时 niu_api app，注入指定的 logging 配置"""
    from niu_api import config as cfg_mod

    cfg_file = tmp_path / "user-config.json"
    cfg_file.write_text(json.dumps({
        "llm": {"apikey": "x"},
        "logging": {"enabled": logging_enabled, "level": "INFO"},
    }), encoding="utf-8")
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", str(cfg_file))
    if hasattr(cfg_mod, "_config"):
        cfg_mod._config = None
    cfg_mod.get_config()  # 预热单例（防 get_logging_config 兜底吞异常导致 false pass）

    import importlib
    import niu_api.__main__ as main_mod
    importlib.reload(main_mod)
    return main_mod.app


def test_http_log_router_not_included_when_logging_disabled(tmp_path, monkeypatch):
    """logging.enabled=false 时，/http-log/ 端点不暴露（返回 404 非 200）"""
    app = _build_app_with_logging(tmp_path, monkeypatch, logging_enabled=False)
    client = TestClient(app)
    resp = client.get("/http-log/")
    assert resp.status_code != 200, f"logging.enabled=false 时 /http-log/ 不应暴露，但返回 {resp.status_code}"


def test_http_log_router_included_when_logging_enabled(tmp_path, monkeypatch):
    """logging.enabled=true 时，/http-log/ 端点返回 200"""
    app = _build_app_with_logging(tmp_path, monkeypatch, logging_enabled=True)
    client = TestClient(app)
    resp = client.get("/http-log/")
    assert resp.status_code == 200, f"logging.enabled=true 时 /http-log/ 应返回 200，但返回 {resp.status_code}"

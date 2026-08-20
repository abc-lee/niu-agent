"""T1 睡眠状态通道测试：set_spirit_state/is_sleeping 单测 + /api/spirit-state 端点 + tidy mode=sleep 置位。

安全约束：不触碰真实 messages.db / 真实 LLM / 图谱写入——
tidy 端点测试用 mock _tidy_context_impl 拦截真实实现。
"""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from niu_api.compat import is_sleeping, router, set_spirit_state


@pytest.fixture(autouse=True)
def _reset_spirit_state():
    """每个用例前复位精灵状态（模块级全局，避免用例间串扰）。"""
    set_spirit_state("idle")
    yield
    set_spirit_state("idle")


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# set_spirit_state / is_sleeping 单测
# ---------------------------------------------------------------------------

def test_default_not_sleeping():
    assert is_sleeping() is False


def test_set_sleep():
    set_spirit_state("sleep")
    assert is_sleeping() is True


def test_set_idle_wakes():
    set_spirit_state("sleep")
    set_spirit_state("idle")
    assert is_sleeping() is False


def test_case_insensitive():
    set_spirit_state("SLEEP")
    assert is_sleeping() is True
    set_spirit_state("Sleep")
    assert is_sleeping() is True


def test_falsy_state_defaults_idle():
    set_spirit_state("sleep")
    set_spirit_state("")
    assert is_sleeping() is False
    set_spirit_state("sleep")
    set_spirit_state("  ")
    assert is_sleeping() is False


def test_unknown_state_not_sleeping():
    set_spirit_state("napping")
    assert is_sleeping() is False


# ---------------------------------------------------------------------------
# /api/spirit-state 端点
# ---------------------------------------------------------------------------

def test_endpoint_sets_sleep(client):
    resp = client.post("/api/spirit-state", json={"state": "sleep"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["state"] == "sleep"
    assert is_sleeping() is True


def test_endpoint_wakes(client):
    client.post("/api/spirit-state", json={"state": "sleep"})
    resp = client.post("/api/spirit-state", json={"state": "idle"})
    assert resp.status_code == 200
    assert is_sleeping() is False


# ---------------------------------------------------------------------------
# tidy 端点 mode='sleep' 置位（挂点 2 冗余）
# ---------------------------------------------------------------------------

def test_tidy_sleep_sets_spirit_state(client):
    with patch("niu_api.compat._tidy_context_impl", new=AsyncMock(return_value={"status": "success"})):
        resp = client.post("/api/context/tidy", json={"mode": "sleep"})
    assert resp.status_code == 200
    assert is_sleeping() is True


def test_tidy_force_does_not_set_sleep(client):
    with patch("niu_api.compat._tidy_context_impl", new=AsyncMock(return_value={"status": "success"})):
        resp = client.post("/api/context/tidy", json={"mode": "force"})
    assert resp.status_code == 200
    assert is_sleeping() is False

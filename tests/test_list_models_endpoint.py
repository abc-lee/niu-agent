"""list-models 端点单测：双形态解析 / 窗口字段优先级 / 三态返回 / URL 组装。

mock Request（async json()）+ patch urllib.request.urlopen（HTTP 层全 mock，禁真实网络）。
端点是 asyncio.to_thread(urllib 同步请求) 的薄壳，不依赖活 API、不起服务。
"""
import io
import json as _json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import urllib.error


def _make_request(body):
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    return req


def _ok_response(payload):
    """200 响应对象（urlopen 上下文管理器形态：with urlopen(...) as resp）。"""
    resp = MagicMock()
    resp.status = 200
    resp.read.return_value = _json.dumps(payload).encode("utf-8")
    resp.__enter__.return_value = resp
    return resp


def _http_error(code, body=b""):
    """urllib HTTPError（4xx/5xx）——helper 捕获后返回 (code, body)。"""
    return urllib.error.HTTPError(
        url="http://x/models", code=code, msg=str(code), hdrs=None, fp=io.BytesIO(body)
    )


def _sent_request(mock_urlopen):
    """取出传给 urlopen 的 Request 对象（URL/请求头断言用）。"""
    args, _kwargs = mock_urlopen.call_args
    return args[0]


@pytest.mark.asyncio
async def test_openai_shape_parse_and_url():
    """OpenAI 形 {"data":[{"id":...}]} → ok + id 列表（顺序保留）；URL={apiBase.rstrip('/')}/models。"""
    from niu_api.compat import list_models

    body = {"apiKey": "sk", "apiBase": "https://api.openai.com/v1/", "type": "openai"}
    payload = {"data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}]}
    with patch("urllib.request.urlopen", return_value=_ok_response(payload)) as mock_urlopen:
        result = await list_models(_make_request(body))

    assert result["status"] == "ok"
    assert [m["id"] for m in result["models"]] == ["gpt-4o", "gpt-4o-mini"]
    assert result["count"] == 2
    # 无窗口字段 → 不挂 context_window 键（D5 零猜测）
    assert all("context_window" not in m for m in result["models"])
    req = _sent_request(mock_urlopen)
    assert req.full_url == "https://api.openai.com/v1/models"
    headers = dict(req.header_items())
    assert headers.get("Authorization") == "Bearer sk"


@pytest.mark.asyncio
async def test_anthropic_shape_parse():
    """Anthropic 形 {"data":[{"id":...,"display_name":...}]} → 同构 id 提取（display_name 不入返回）。"""
    from niu_api.compat import list_models

    body = {"apiKey": "sk-ant", "apiBase": "https://api.anthropic.com/v1", "type": "anthropic"}
    payload = {"data": [{"id": "claude-x", "display_name": "Claude X"}, {"id": "claude-y"}]}
    with patch("urllib.request.urlopen", return_value=_ok_response(payload)):
        result = await list_models(_make_request(body))

    assert result["status"] == "ok"
    assert [m["id"] for m in result["models"]] == ["claude-x", "claude-y"]
    assert result["count"] == 2
    assert all("display_name" not in m for m in result["models"])


@pytest.mark.asyncio
async def test_window_field_priority():
    """窗口字段优先级：context_length > max_input_tokens > context_window > top_provider.context_length，
    首个非空整数胜出（含 OpenRouter 嵌套形单独命中与无字段不挂键）。"""
    from niu_api.compat import list_models

    body = {"apiKey": "sk", "apiBase": "https://openrouter.ai/api/v1"}
    payload = {
        "data": [
            # 四键全在 → context_length 胜出
            {"id": "a", "context_length": 100, "max_input_tokens": 200, "context_window": 300,
             "top_provider": {"context_length": 400}},
            # 无 context_length → max_input_tokens
            {"id": "b", "max_input_tokens": 200, "context_window": 300},
            # 仅 context_window
            {"id": "c", "context_window": 300},
            # OpenRouter 嵌套形单独命中
            {"id": "d", "top_provider": {"context_length": 400}},
            # 无窗口字段 → 不挂键
            {"id": "e"},
        ]
    }
    with patch("urllib.request.urlopen", return_value=_ok_response(payload)):
        result = await list_models(_make_request(body))

    assert result["status"] == "ok"
    by_id = {m["id"]: m for m in result["models"]}
    assert by_id["a"]["context_window"] == 100
    assert by_id["b"]["context_window"] == 200
    assert by_id["c"]["context_window"] == 300
    assert by_id["d"]["context_window"] == 400
    assert "context_window" not in by_id["e"]

@pytest.mark.asyncio
async def test_404_returns_unsupported():
    """HTTP 404（如豆包 Plan 网关不暴露 /models）→ unsupported，前端降级手输不显示错误。"""
    from niu_api.compat import list_models

    body = {"apiKey": "sk", "apiBase": "https://ark.example.com/api/plan/v3"}
    with patch("urllib.request.urlopen", side_effect=_http_error(404)):
        result = await list_models(_make_request(body))

    assert result == {"status": "unsupported", "reason": "网关不支持模型列表接口"}


@pytest.mark.asyncio
async def test_405_returns_unsupported():
    """HTTP 405（方法不允许）→ 同 404 归 unsupported。"""
    from niu_api.compat import list_models

    body = {"apiKey": "sk", "apiBase": "https://ark.example.com/api/plan/v3"}
    with patch("urllib.request.urlopen", side_effect=_http_error(405)):
        result = await list_models(_make_request(body))

    assert result["status"] == "unsupported"


@pytest.mark.asyncio
async def test_401_returns_error():
    """HTTP 401/403 → error + Key 无效文案（前端提示 reason，可重试）。"""
    from niu_api.compat import list_models

    body = {"apiKey": "bad", "apiBase": "https://api.example.com/v1"}
    with patch("urllib.request.urlopen", side_effect=_http_error(401)):
        result = await list_models(_make_request(body))

    assert result["status"] == "error"
    assert result["reason"] == "API Key 无效或无权访问模型列表"


@pytest.mark.asyncio
async def test_timeout_returns_error():
    """urllib 超时（socket.timeout/TimeoutError）→ error，不抛 500。"""
    from niu_api.compat import list_models

    body = {"apiKey": "sk", "apiBase": "https://api.example.com/v1"}
    with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
        result = await list_models(_make_request(body))

    assert result["status"] == "error"
    assert "获取模型列表失败" in result["reason"]


@pytest.mark.asyncio
async def test_5xx_returns_error():
    """HTTP 5xx → error（带状态码，前端提示可重试）。"""
    from niu_api.compat import list_models

    body = {"apiKey": "sk", "apiBase": "https://api.example.com/v1"}
    with patch("urllib.request.urlopen", side_effect=_http_error(502)):
        result = await list_models(_make_request(body))

    assert result["status"] == "error"
    assert "502" in result["reason"]


@pytest.mark.asyncio
async def test_200_non_json_returns_error():
    """200 但 body 非 JSON（网关错误页等）→ 解析失败 error。"""
    from niu_api.compat import list_models

    body = {"apiKey": "sk", "apiBase": "https://api.example.com/v1"}
    resp = MagicMock()
    resp.status = 200
    resp.read.return_value = b"<html>gateway error page</html>"
    resp.__enter__.return_value = resp
    with patch("urllib.request.urlopen", return_value=resp):
        result = await list_models(_make_request(body))

    assert result["status"] == "error"
    assert "解析失败" in result["reason"]


@pytest.mark.asyncio
async def test_200_missing_data_array_returns_error():
    """200 但 JSON 缺 data 数组（非预期形状）→ error。"""
    from niu_api.compat import list_models

    body = {"apiKey": "sk", "apiBase": "https://api.example.com/v1"}
    with patch("urllib.request.urlopen", return_value=_ok_response({"models": []})):
        result = await list_models(_make_request(body))

    assert result["status"] == "error"
    assert "格式非预期" in result["reason"]

@pytest.mark.asyncio
async def test_missing_api_base():
    """apiBase 空 → error「API 地址未配置」，不发 HTTP 请求。"""
    from niu_api.compat import list_models

    with patch("urllib.request.urlopen") as mock_urlopen:
        result = await list_models(_make_request({"apiKey": "sk"}))

    assert result == {"status": "error", "reason": "API 地址未配置"}
    mock_urlopen.assert_not_called()


@pytest.mark.asyncio
async def test_non_string_api_base():
    """apiBase 非字符串（如 123）→ 按缺字段处理，返回 error「API 地址未配置」，不抛 500。"""
    from niu_api.compat import list_models

    with patch("urllib.request.urlopen") as mock_urlopen:
        result = await list_models(_make_request({"apiKey": "sk", "apiBase": 123}))

    assert result == {"status": "error", "reason": "API 地址未配置"}
    mock_urlopen.assert_not_called()


@pytest.mark.asyncio
async def test_missing_key_non_local():
    """apiKey 空且非本地网关 → error「API Key 未配置」，不发 HTTP 请求。"""
    from niu_api.compat import list_models

    with patch("urllib.request.urlopen") as mock_urlopen:
        result = await list_models(_make_request({"apiBase": "https://api.example.com/v1"}))

    assert result == {"status": "error", "reason": "API Key 未配置"}
    mock_urlopen.assert_not_called()


@pytest.mark.asyncio
async def test_local_api_base_exempts_key():
    """本地模型（127.0.0.1）免 apiKey——请求照发，且不挂 Authorization 头。"""
    from niu_api.compat import list_models

    body = {"apiBase": "http://127.0.0.1:8080/v1"}
    payload = {"data": [{"id": "qwen3-8b"}]}
    with patch("urllib.request.urlopen", return_value=_ok_response(payload)) as mock_urlopen:
        result = await list_models(_make_request(body))

    assert result["status"] == "ok"
    assert result["count"] == 1
    req = _sent_request(mock_urlopen)
    assert "Authorization" not in dict(req.header_items())


@pytest.mark.asyncio
async def test_anthropic_url_appends_v1_and_headers():
    """anthropic：apiBase 不以 /v1 结尾 → 补 /v1 再拼 /models；请求头 x-api-key +
    anthropic-version: 2023-06-01（非 Authorization）。"""
    from niu_api.compat import list_models

    body = {"apiKey": "sk-ant", "apiBase": "https://api.anthropic.com", "type": "anthropic"}
    with patch("urllib.request.urlopen", return_value=_ok_response({"data": []})) as mock_urlopen:
        result = await list_models(_make_request(body))

    assert result["status"] == "ok"
    req = _sent_request(mock_urlopen)
    assert req.full_url == "https://api.anthropic.com/v1/models"
    headers = dict(req.header_items())
    assert headers.get("X-api-key") == "sk-ant"
    assert headers.get("Anthropic-version") == "2023-06-01"
    assert "Authorization" not in headers


@pytest.mark.asyncio
async def test_anthropic_url_v1_trailing_slash_no_double():
    """/v1/ 结尾输入 → 先 rstrip('/') 再判 /v1 后缀，不拼出 /v1//v1/models（R3-P2-4）。"""
    from niu_api.compat import list_models

    body = {"apiKey": "sk-ant", "apiBase": "https://api.anthropic.com/v1/", "type": "anthropic"}
    with patch("urllib.request.urlopen", return_value=_ok_response({"data": []})) as mock_urlopen:
        await list_models(_make_request(body))

    req = _sent_request(mock_urlopen)
    assert req.full_url == "https://api.anthropic.com/v1/models"


@pytest.mark.asyncio
async def test_anthropic_url_already_v1_no_append():
    """apiBase 已以 /v1 结尾 → 不再补 /v1。"""
    from niu_api.compat import list_models

    body = {"apiKey": "sk-ant", "apiBase": "https://api.anthropic.com/v1", "type": "anthropic"}
    with patch("urllib.request.urlopen", return_value=_ok_response({"data": []})) as mock_urlopen:
        await list_models(_make_request(body))

    req = _sent_request(mock_urlopen)
    assert req.full_url == "https://api.anthropic.com/v1/models"


@pytest.mark.asyncio
async def test_empty_list_passthrough():
    """200 data:[] 空列表 → 后端原样透传 ok+count:0（降级判定在前端，不在后端）。"""
    from niu_api.compat import list_models

    body = {"apiKey": "sk", "apiBase": "https://api.example.com/v1"}
    with patch("urllib.request.urlopen", return_value=_ok_response({"data": []})):
        result = await list_models(_make_request(body))

    assert result == {"status": "ok", "models": [], "count": 0}


@pytest.mark.asyncio
async def test_user_agent_header_set():
    """请求头包含 User-Agent: Niu/0.3.1（Cloudflare 拦截 Python 默认 UA）。"""
    from niu_api.compat import list_models

    body = {"apiKey": "sk", "apiBase": "https://api.example.com/v1"}
    with patch("urllib.request.urlopen", return_value=_ok_response({"data": [{"id": "m1"}]})) as mock_urlopen:
        await list_models(_make_request(body))
        req = _sent_request(mock_urlopen)
        assert req.get_header("User-agent") == "Niu/0.3.1"


@pytest.mark.asyncio
async def test_user_agent_anthropic_path():
    """anthropic 路径同样带 User-Agent 头。"""
    from niu_api.compat import list_models

    body = {"apiKey": "sk", "apiBase": "https://api.anthropic.com", "type": "anthropic"}
    with patch("urllib.request.urlopen", return_value=_ok_response({"data": [{"id": "claude-4"}]})) as mock_urlopen:
        await list_models(_make_request(body))
        req = _sent_request(mock_urlopen)
        assert req.get_header("User-agent") == "Niu/0.3.1"

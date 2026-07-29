"""
Tests for niu_api/llm_proxy.py

OpenAI-compatible LLM proxy utilities: format conversion helpers,
direct LLM call functions, and remaining HTTP endpoints
(/llm/v1/models, /llm/v1/health, /llm/v1/status).
"""

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Import the router directly to avoid the full app lifespan
from niu_api.llm_proxy import router as llm_proxy_router


@pytest.fixture
def app():
    """Create a minimal FastAPI app with just the LLM proxy router."""
    app = FastAPI()
    app.include_router(llm_proxy_router)
    return app


@pytest.fixture
def client(app):
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def mock_llm_config():
    """Mock LLM config with test values."""
    return {
        "type": "openai",
        "apikey": "test-key-123",
        "apibase": "https://api.example.com/v1",
        "model": "test-model",
    }


# ============== Health Endpoint Tests ==============


class TestHealthEndpoint:
    """Test /llm/v1/health endpoint."""

    def test_health_returns_ok_with_config(self, client, mock_llm_config):
        with patch("niu_api.llm_proxy.get_llm_config", return_value=mock_llm_config):
            response = client.get("/llm/v1/health")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "ok"
            assert data["model"] == "test-model"

    def test_health_returns_error_without_apikey(self, client):
        with patch("niu_api.llm_proxy.get_llm_config", return_value={
            "type": "openai", "apikey": "", "apibase": "", "model": ""
        }):
            response = client.get("/llm/v1/health")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "error"


# ============== Models Endpoint Tests ==============


class TestModelsEndpoint:
    """Test /llm/v1/models endpoint."""

    def test_models_returns_list(self, client, mock_llm_config):
        with patch("niu_api.llm_proxy.get_llm_config", return_value=mock_llm_config):
            response = client.get("/llm/v1/models")
            assert response.status_code == 200
            data = response.json()
            assert data["object"] == "list"
            assert len(data["data"]) == 1
            assert data["data"][0]["id"] == "test-model"

    def test_models_object_type(self, client, mock_llm_config):
        with patch("niu_api.llm_proxy.get_llm_config", return_value=mock_llm_config):
            response = client.get("/llm/v1/models")
            data = response.json()
            assert data["data"][0]["object"] == "model"


# ============== Status Endpoint Tests ==============


class TestStatusEndpoint:
    """Test /llm/v1/status endpoint."""

    def test_status_returns_model_info(self, client):
        with patch("niu_api.internal.embedding.get_current_model_info", return_value={
            "name": "minilm-l12", "dim": 384, "desc": "test", "loaded": True
        }):
            with patch("niu_api.internal.reranker.get_current_reranker_info", return_value={
                "name": "none", "desc": "no reranker", "loaded": False
            }):
                response = client.get("/llm/v1/status")
                assert response.status_code == 200
                data = response.json()
                assert "embedding" in data
                assert "reranker" in data
                assert data["embedding"]["name"] == "minilm-l12"
                assert data["reranker"]["name"] == "none"


# ============== Format Conversion Tests ==============


class TestFormatConversion:
    """Test OpenAI ↔ LiteLLM format conversion utilities."""

    def test_openai_to_litellm_messages(self):
        from niu_api.llm_proxy import OpenAIMessage, openai_to_litellm_messages
        messages = [
            OpenAIMessage(role="system", content="You are helpful"),
            OpenAIMessage(role="user", content="Hello"),
        ]
        result = openai_to_litellm_messages(messages)
        assert len(result) == 2
        assert result[0]["role"] == "system"
        assert result[1]["role"] == "user"

    def test_openai_to_litellm_messages_with_name(self):
        from niu_api.llm_proxy import OpenAIMessage, openai_to_litellm_messages
        messages = [
            OpenAIMessage(role="tool", content="result", name="tool_name"),
        ]
        result = openai_to_litellm_messages(messages)
        assert result[0]["name"] == "tool_name"

    def test_openai_to_litellm_tools_none(self):
        from niu_api.llm_proxy import openai_to_litellm_tools
        assert openai_to_litellm_tools(None) is None

    def test_openai_to_litellm_tools_empty(self):
        from niu_api.llm_proxy import openai_to_litellm_tools
        assert openai_to_litellm_tools([]) is None

    def test_litellm_to_openai_response(self):
        from niu_api.llm_proxy import litellm_to_openai_response
        litellm_resp = {
            "choices": [{
                "message": {"role": "assistant", "content": "Hello!"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        result = litellm_to_openai_response(litellm_resp, "test-model")
        assert result.object == "chat.completion"
        assert result.model == "test-model"
        assert result.choices[0].message.content == "Hello!"
        assert result.usage.total_tokens == 15

    def test_litellm_to_openai_response_with_tool_calls(self):
        from niu_api.llm_proxy import litellm_to_openai_response
        litellm_resp = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_123",
                        "type": "function",
                        "function": {"name": "test_tool", "arguments": '{"key": "value"}'},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        result = litellm_to_openai_response(litellm_resp, "test-model")
        assert result.choices[0].message.tool_calls is not None
        assert len(result.choices[0].message.tool_calls) == 1
        assert result.choices[0].message.tool_calls[0].function.name == "test_tool"


# ============== Endpoint Removal Tests ==============


class TestEndpointRemoval:
    """Verify that removed endpoints no longer exist."""

    def test_chat_completions_endpoint_removed(self):
        """The /chat/completions POST endpoint should no longer exist."""
        from niu_api.llm_proxy import router
        routes = [r.path for r in router.routes]
        assert "/chat/completions" not in routes

    def test_embeddings_endpoint_removed(self):
        """The /embeddings POST endpoint should no longer exist."""
        from niu_api.llm_proxy import router
        routes = [r.path for r in router.routes]
        assert "/embeddings" not in routes

    def test_remaining_endpoints_still_exist(self):
        """Health, models, and status endpoints should still exist."""
        from niu_api.llm_proxy import router
        routes = [r.path for r in router.routes]
        assert "/health" in routes or "/llm/v1/health" in routes
        assert "/models" in routes or "/llm/v1/models" in routes
        assert "/status" in routes or "/llm/v1/status" in routes

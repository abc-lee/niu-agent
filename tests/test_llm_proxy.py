"""
Tests for niu_api/llm_proxy.py

OpenAI-compatible LLM proxy endpoints: /llm/v1/chat/completions,
/llm/v1/embeddings, /llm/v1/models, /llm/v1/health, /llm/v1/status.
"""

import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
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


# ============== Chat Completions Tests ==============


class TestChatCompletions:
    """Test /llm/v1/chat/completions endpoint."""

    def test_rejects_without_api_key(self, client):
        with patch("niu_api.llm_proxy.get_llm_config", return_value={
            "type": "openai", "apikey": "", "apibase": "", "model": ""
        }):
            response = client.post("/llm/v1/chat/completions", json={
                "model": "test",
                "messages": [{"role": "user", "content": "hello"}],
            })
            assert response.status_code == 500

    def test_accepts_valid_request(self, client, mock_llm_config):
        mock_response = {
            "choices": [{
                "message": {"role": "assistant", "content": "Hello!"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

        async def mock_call(**kwargs):
            return mock_response

        with patch("niu_api.llm_proxy.get_llm_config", return_value=mock_llm_config):
            with patch("niu_api.llm_proxy.call_llm_via_litellm", side_effect=mock_call):
                response = client.post("/llm/v1/chat/completions", json={
                    "model": "test",
                    "messages": [{"role": "user", "content": "hello"}],
                })
                assert response.status_code == 200
                data = response.json()
                assert data["object"] == "chat.completion"
                assert len(data["choices"]) == 1
                assert data["choices"][0]["message"]["content"] == "Hello!"

    def test_passes_response_format(self, client, mock_llm_config):
        """Verify response_format is forwarded to call_llm_via_litellm."""
        mock_response = {
            "choices": [{
                "message": {"role": "assistant", "content": "{}"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

        captured_kwargs = {}

        async def mock_call(**kwargs):
            captured_kwargs.update(kwargs)
            return mock_response

        with patch("niu_api.llm_proxy.get_llm_config", return_value=mock_llm_config):
            with patch("niu_api.llm_proxy.call_llm_via_litellm", side_effect=mock_call):
                response = client.post("/llm/v1/chat/completions", json={
                    "model": "test",
                    "messages": [{"role": "user", "content": "hello"}],
                    "response_format": {"type": "json_object"},
                })
                assert response.status_code == 200
                assert captured_kwargs.get("response_format") == {"type": "json_object"}

    def test_passes_tools(self, client, mock_llm_config):
        """Verify tools are forwarded."""
        mock_response = {
            "choices": [{
                "message": {"role": "assistant", "content": None, "tool_calls": None},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

        captured_kwargs = {}

        async def mock_call(**kwargs):
            captured_kwargs.update(kwargs)
            return mock_response

        with patch("niu_api.llm_proxy.get_llm_config", return_value=mock_llm_config):
            with patch("niu_api.llm_proxy.call_llm_via_litellm", side_effect=mock_call):
                response = client.post("/llm/v1/chat/completions", json={
                    "model": "test",
                    "messages": [{"role": "user", "content": "hello"}],
                    "tools": [{"type": "function", "function": {"name": "test_tool", "parameters": {}}}],
                })
                assert response.status_code == 200
                assert captured_kwargs.get("tools") is not None


# ============== Embeddings Endpoint Tests ==============


class TestEmbeddingsEndpoint:
    """Test /llm/v1/embeddings endpoint."""

    def test_single_text_embedding(self, client):
        mock_embeddings = [[0.1, 0.2, 0.3]]
        with patch("niu_api.internal.embedding.batch_encode", return_value=mock_embeddings):
            response = client.post("/llm/v1/embeddings", json={
                "model": "test-embed",
                "input": "hello world",
            })
            assert response.status_code == 200
            data = response.json()
            assert data["object"] == "list"
            assert len(data["data"]) == 1
            assert data["data"][0]["object"] == "embedding"
            assert data["data"][0]["embedding"] == [0.1, 0.2, 0.3]

    def test_batch_text_embeddings(self, client):
        mock_embeddings = [[0.1, 0.2], [0.3, 0.4]]
        with patch("niu_api.internal.embedding.batch_encode", return_value=mock_embeddings):
            response = client.post("/llm/v1/embeddings", json={
                "model": "test-embed",
                "input": ["hello", "world"],
            })
            assert response.status_code == 200
            data = response.json()
            assert len(data["data"]) == 2
            assert data["data"][0]["index"] == 0
            assert data["data"][1]["index"] == 1

    def test_dimension_reduction(self, client):
        mock_embeddings = [[0.1, 0.2, 0.3, 0.4, 0.5]]
        with patch("niu_api.internal.embedding.batch_encode", return_value=mock_embeddings):
            response = client.post("/llm/v1/embeddings", json={
                "model": "test-embed",
                "input": "hello",
                "dimensions": 3,
            })
            assert response.status_code == 200
            data = response.json()
            # Should truncate to 3 dimensions
            assert len(data["data"][0]["embedding"]) == 3

    def test_embedding_response_format(self, client):
        mock_embeddings = [[0.1, 0.2]]
        with patch("niu_api.internal.embedding.batch_encode", return_value=mock_embeddings):
            response = client.post("/llm/v1/embeddings", json={
                "model": "test-embed",
                "input": "hello",
            })
            data = response.json()
            assert "id" in data
            assert "created" in data
            assert "model" in data
            assert "usage" in data
            assert "prompt_tokens" in data["usage"]


# ============== Format Conversion Tests ==============


class TestFormatConversion:
    """Test OpenAI ↔ LiteLLM format conversion utilities."""

    def test_openai_to_litellm_messages(self):
        from niu_api.llm_proxy import openai_to_litellm_messages, OpenAIMessage
        messages = [
            OpenAIMessage(role="system", content="You are helpful"),
            OpenAIMessage(role="user", content="Hello"),
        ]
        result = openai_to_litellm_messages(messages)
        assert len(result) == 2
        assert result[0]["role"] == "system"
        assert result[1]["role"] == "user"

    def test_openai_to_litellm_messages_with_name(self):
        from niu_api.llm_proxy import openai_to_litellm_messages, OpenAIMessage
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
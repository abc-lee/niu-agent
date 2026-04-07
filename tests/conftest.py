"""pytest fixtures"""
import pytest
import sys
import asyncio
import tempfile
import os

# Add project root to path
sys.path.insert(0, "E:/tools/ai-bot")


@pytest.fixture
def event_loop():
    """Create event loop for async tests"""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def temp_db():
    """Create temporary database for testing"""
    db_path = tempfile.mktemp(suffix=".db")
    yield db_path
    if os.path.exists(db_path):
        os.unlink(db_path)


@pytest.fixture
def mock_config():
    """Mock configuration for testing"""
    return {
        "apikey": "test-api-key",
        "apibase": "https://api.anthropic.com",
        "model": "claude-3-5-sonnet-20241022",
        "context_win": 200000,
        "max_retries": 3
    }

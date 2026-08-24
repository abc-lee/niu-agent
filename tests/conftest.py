"""pytest fixtures"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Add project root to path（跨平台，不硬编码绝对路径）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


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

# ============================================================================
# E2E / Integration 测试保护
# ============================================================================
# 默认不收集 e2e/integration 测试（会操作真实用户数据 ~/.niu/）
# 只有显式加 --run-e2e 参数才收集

def pytest_addoption(parser):
    """添加 --run-e2e 命令行参数"""
    parser.addoption(
        "--run-e2e",
        action="store_true",
        default=False,
        help="收集并运行 e2e/integration 测试（会操作真实用户数据 ~/.niu/）"
    )


def pytest_collection_modifyitems(config, items):
    """默认跳过标记为 e2e 的测试"""
    if config.getoption("--run-e2e"):
        return  # 显式要求跑 e2e，不跳过

    skip_e2e = pytest.mark.skip(reason="需要 --run-e2e 参数才运行（会操作真实用户数据）")
    for item in items:
        if "e2e" in item.keywords or "integration" in item.keywords:
            item.add_marker(skip_e2e)


@pytest.fixture(autouse=True)
def _isolate_md_mirror(tmp_path, monkeypatch):
    """MD 镜像路径全局指向临时文件——防止任何测试污染用户真实 ~/.niu/md/F1。"""
    import agent.md_mirror as mdm
    import agent.session as sess
    fake = str(tmp_path / "isolated_f1.md")
    monkeypatch.setattr(mdm, "F1_PATH", fake, raising=False)
    if hasattr(sess, "F1_PATH"):
        monkeypatch.setattr(sess, "F1_PATH", fake)
    yield

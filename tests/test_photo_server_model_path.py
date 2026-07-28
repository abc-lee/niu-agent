"""验证 get_face_model 不再把 bundle 内路径传给 InsightFace 的 root 参数。

bundle 签名后不可写，root 指向 bundle 会导致首次下载失败。
应让 InsightFace 用默认 ~/.insightface/。
"""
import os
import sys
import inspect
from unittest.mock import patch, MagicMock

# 让 niu_photo_server 模块可导入（其源码在 mcp-servers/photo-server/src 下）
_SRC = os.path.join(os.path.dirname(__file__), "..", "mcp-servers", "photo-server", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def test_face_analysis_not_passed_bundle_root():
    """FaceAnalysis 构造时不应传 root=bundle_path，应让 InsightFace 用默认 ~/.insightface/"""
    source = inspect.getsource(__import__("niu_photo_server", fromlist=["get_face_model"]).get_face_model)
    # 不应出现 root=str(models_dir) 这种把 bundle 路径传给 root 的写法
    assert "root=str(models_dir)" not in source, (
        "get_face_model 仍在把 bundle 内路径传给 FaceAnalysis root 参数，"
        "这会让 InsightFace 试图下载到签名后不可写的 bundle 内路径。"
        "应移除 root 参数，让 InsightFace 用默认 ~/.insightface/。"
    )


def test_face_analysis_construction_uses_default_root():
    """FaceAnalysis 构造调用应不含 root 参数（或 root=None）"""
    source = inspect.getsource(__import__("niu_photo_server", fromlist=["get_face_model"]).get_face_model)
    # 找到 FaceAnalysis(...) 构造调用，确认没有 root=
    # 用 mock 实际拦截一次调用更可靠
    # 注意：get_face_model 内是 `from insightface.app import FaceAnalysis` 函数内 import，
    # 必须 patch 源模块 insightface.app.FaceAnalysis，不能 patch niu_photo_server.FaceAnalysis
    # （后者会因模块命名空间无该属性而抛 AttributeError）
    with patch("insightface.app.FaceAnalysis") as mock_fa:
        mock_fa.return_value = MagicMock()
        # 让 _detect_available_providers 返回 CPU only，避免 GPU 检测副作用
        with patch("niu_photo_server._detect_available_providers", return_value=["CPUExecutionProvider"]):
            try:
                __import__("niu_photo_server", fromlist=["get_face_model"]).get_face_model()
            except Exception:
                pass  # 模型加载会失败，但我们要看的是构造调用
        if mock_fa.called:
            _, kwargs = mock_fa.call_args
            assert "root" not in kwargs or kwargs["root"] is None, (
                f"FaceAnalysis 被传了 root={kwargs.get('root')}，"
                "应不传 root 让 InsightFace 用默认 ~/.insightface/"
            )

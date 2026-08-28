"""验证 get_face_model 从 bundle 内 models 目录加载人脸模型、且不做程序内下载。

设计（f609f571，README「可选：启用照片处理」）：
- buffalo_l 非商业许可，DMG 默认不含；用户手动装到 models_dir/models/buffalo_l/
- 禁止程序内下载：本地没有直接返回 None（不构造 FaceAnalysis，避免下载卡死）
- FaceAnalysis 的 root 必须指向 models_dir，InsightFace 才能找到 bundle 内模型
  （旧设计曾让 InsightFace 用默认 ~/.insightface/ 并允许下载，571e64ba → f609f571
  已整体替换为「bundle 内手动安装 + 不下载」，本文件随之更新）
"""
import os
import sys
from unittest.mock import MagicMock, patch

# 让 niu_photo_server 模块可导入（其源码在 mcp-servers/photo-server/src 下）
_SRC = os.path.join(os.path.dirname(__file__), "..", "mcp-servers", "photo-server", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def _reset_model_cache():
    """get_face_model 有模块级缓存，测试间必须复位。"""
    import niu_photo_server

    niu_photo_server._face_model = None


def test_face_analysis_uses_models_dir_root(tmp_path, monkeypatch):
    """模型本地存在时，FaceAnalysis 的 root 必须指向 models_dir（bundle 内模型目录）。"""
    import niu_photo_server

    _reset_model_cache()
    models_dir = tmp_path / "models"
    (models_dir / "models" / "buffalo_l").mkdir(parents=True)
    (models_dir / "models" / "buffalo_l" / "det_10g.onnx").write_bytes(b"fake onnx")

    monkeypatch.setenv("NIU_MODELS_PATH", str(models_dir))
    with patch("insightface.app.FaceAnalysis") as mock_fa:
        mock_model = MagicMock()
        mock_fa.return_value = mock_model
        # 让 _detect_available_providers 返回 CPU only，避免 GPU 检测副作用
        with patch("niu_photo_server._detect_available_providers", return_value=["CPUExecutionProvider"]):
            result = niu_photo_server.get_face_model()

    _reset_model_cache()
    assert mock_fa.called, "模型存在时 FaceAnalysis 未被构造"
    _, kwargs = mock_fa.call_args
    assert kwargs.get("root") == str(models_dir), (
        f"FaceAnalysis root 应指向 models_dir（{models_dir}）以找到 bundle 内模型，"
        f"实际: {kwargs.get('root')}"
    )
    assert result is mock_model


def test_missing_model_returns_none_without_faceanalysis(tmp_path, monkeypatch):
    """本地没有模型时直接返回 None，且不构造 FaceAnalysis（禁止程序内下载）。"""
    import niu_photo_server

    _reset_model_cache()
    models_dir = tmp_path / "models"
    models_dir.mkdir()

    monkeypatch.setenv("NIU_MODELS_PATH", str(models_dir))
    with patch("insightface.app.FaceAnalysis") as mock_fa:
        result = niu_photo_server.get_face_model()

    _reset_model_cache()
    assert result is None, "模型缺失时应返回 None"
    assert not mock_fa.called, "模型缺失时不得构造 FaceAnalysis（禁止程序内下载）"

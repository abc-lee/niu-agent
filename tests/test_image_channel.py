"""识图通道共享 helper 测试（plan 2026-09-11-vision-channel-refactor：图片直通通道退役后保留项）。

覆盖：
① mask_image_data_uris 共享打码 helper——str/dict/list 递归、字节数标注、非图 URI 不动。
② main_has_vision 判定 helper（analyze_image 选模守卫）——llm_config capabilities 判定：
   input 含 image + model 匹配；无/不匹配/坏形状 → False（fail-closed）。
③ 读图 helper _image_to_data_uri——MIME 按载荷魔数（非图片 → None）/ >4MB PIL 降采样
   （jpeg 产物受预算约束 / 降采样失败 → None 由调用方降级）。
④ 出站打码三写函数——http_logger._write_log_entry / litellm_adapter._write_raw_log /
   _write_interaction_log：落盘文件无长 base64，[image data, N bytes] 在场。

全 mock：禁真实 LLM；配置路径 monkeypatch 到 tmp_path；tmp sqlite 禁碰 ~/.niu。
"""
import base64
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.image_channel import (  # noqa: E402
    MAX_IMAGE_BYTES,
    _image_to_data_uri,
    main_has_vision,
    mask_image_data_uris,
)


# ---------------------------------------------------------------------------
# 基建：PNG 工厂
# ---------------------------------------------------------------------------

def _make_png(path, size=(8, 8), color=(255, 0, 0)):
    """生成纯色 PNG；返回文件字节。"""
    from PIL import Image
    img = Image.new("RGB", size, color)
    img.save(str(path), format="PNG")
    return Path(path).read_bytes()


# ---------------------------------------------------------------------------
# ① mask_image_data_uris 共享打码 helper
# ---------------------------------------------------------------------------

class TestMaskImageDataUris:
    def test_str_masked_with_byte_count(self):
        b64 = base64.b64encode(b"01234567").decode()
        s = f"前缀 data:image/png;base64,{b64} 后缀"
        out = mask_image_data_uris(s)
        # 计数口径 = base64 文本长度（helper 只见字符串，不解码）
        assert out == f"前缀 [image data, {len(b64)} bytes] 后缀"
        assert b64 not in out

    def test_nested_dict_list_recursive(self):
        b64 = base64.b64encode(b"abcdefgh").decode()
        obj = {
            "messages": [
                {"role": "user", "content": f"x data:image/jpeg;base64,{b64} y"},
                {"role": "assistant", "content": [{"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}"}}]},
            ],
            "n": 42,
        }
        out = mask_image_data_uris(obj)
        dumped = json.dumps(out, ensure_ascii=False)
        assert b64 not in dumped
        assert f"[image data, {len(b64)} bytes]" in dumped
        # 非字符串叶子原样
        assert out["n"] == 42

    def test_non_image_content_untouched(self):
        s = "普通文本没有 URI"
        assert mask_image_data_uris(s) is s
        assert mask_image_data_uris(123) == 123
        assert mask_image_data_uris(None) is None


# ---------------------------------------------------------------------------
# ② main_has_vision 判定 helper（analyze_image 选模守卫）
# ---------------------------------------------------------------------------

class TestJudgementHelpers:
    MAIN_CFG = {"apibase": "http://x/v1", "model": "m"}

    @staticmethod
    def _cfg_with_caps(model="m", caps_model="m", input=None):
        cfg = dict(TestJudgementHelpers.MAIN_CFG)
        cfg["model"] = model
        if input is not None:
            cfg["capabilities"] = {"model": caps_model, "input": input, "probed_at": "2026-01-01T00:00:00"}
        return cfg

    def test_main_has_vision_true_when_image_input_and_model_match(self):
        """capabilities.input 含 image 且 capabilities.model == llm model → True。"""
        assert main_has_vision(self._cfg_with_caps(input=["text", "image"])) is True

    def test_main_has_vision_false_when_text_only_input(self):
        """input=["text"]（探测未命中视觉）→ False。"""
        assert main_has_vision(self._cfg_with_caps(input=["text"])) is False

    def test_main_has_vision_false_when_model_mismatch(self):
        """换模型后旧 capabilities.model 不匹配 → False（fail-closed，不采信旧能力）。"""
        assert main_has_vision(self._cfg_with_caps(model="new-model", caps_model="m")) is False

    def test_main_has_vision_false_when_no_capabilities(self):
        # 未探测过（无 capabilities 键）/ 无配置 → False（fail-closed）
        assert main_has_vision(self.MAIN_CFG) is False
        assert main_has_vision({"model": "m"}) is False
        assert main_has_vision(None) is False

    def test_main_has_vision_false_on_bad_shape(self):
        """capabilities 非 dict / input 缺省 → False（不抛）。"""
        assert main_has_vision({**self.MAIN_CFG, "capabilities": "text,image"}) is False
        assert main_has_vision({**self.MAIN_CFG, "capabilities": {"model": "m"}}) is False


# ---------------------------------------------------------------------------
# ③ 读图 helper：MIME 魔数 + >4MB 降采样（analyze_image 复用 _image_to_data_uri）
# ---------------------------------------------------------------------------

class TestImageToDataUri:
    def test_small_png_media_type_and_bytes(self, tmp_path):
        """小图直读：media type 按载荷魔数（png），base64 与文件字节一致。"""
        import agent.image_channel as ic
        p = tmp_path / "s.png"
        _make_png(p)
        uri = ic._image_to_data_uri(str(p))
        assert uri is not None
        assert uri.startswith("data:image/png;base64,")
        assert base64.b64decode(uri.split(",", 1)[1]) == Path(p).read_bytes()

    def test_non_image_payload_returns_none(self, tmp_path):
        """非图片载荷（假扩展名）→ None——MIME 按魔数判定，不按扩展名猜。"""
        import agent.image_channel as ic
        p = tmp_path / "fake.png"
        p.write_bytes(b"this is not an image at all")
        assert ic._image_to_data_uri(str(p)) is None

    def test_heic_by_payload_not_extension(self, tmp_path):
        """HEIC 魔数分支：扩展名 .heic + ftypheic 载荷 → image/heic（_detect_image_mime 魔数判定）。"""
        import agent.image_channel as ic
        p = tmp_path / "s.heic"
        p.write_bytes(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 32)
        uri = ic._image_to_data_uri(str(p))
        assert uri is not None and uri.startswith("data:image/heic;base64,")

    def test_extension_mismatch_follows_payload(self, tmp_path):
        """扩展名与内容不符（.jpg 装 PNG）→ MIME 跟载荷走 image/png，不按扩展名。"""
        import agent.image_channel as ic
        p = tmp_path / "s.jpg"
        _make_png(p)
        uri = ic._image_to_data_uri(str(p))
        assert uri is not None and uri.startswith("data:image/png;base64,")

    @pytest.mark.skipif(
        importlib.util.find_spec("PIL") is None,
        reason="Pillow 不可用（降采样走失败回落路径）")
    def test_oversize_file_downsamples_to_budget(self, tmp_path):
        # 3000x2000 随机像素 PNG：deflate 压缩率极低，文件必 >4MB（不依赖 numpy）
        import os
        from PIL import Image
        p = tmp_path / "big.png"
        img = Image.frombytes("RGB", (3000, 2000), os.urandom(3000 * 2000 * 3))
        img.save(str(p), format="PNG")
        assert p.stat().st_size > MAX_IMAGE_BYTES
        uri = _image_to_data_uri(str(p))
        # 降采样产物为 jpeg，且总长受预算约束（base64 膨胀 4/3 + 头部余量）
        assert uri is not None
        assert uri.startswith("data:image/jpeg;base64,")
        assert len(uri) <= MAX_IMAGE_BYTES * 4 // 3 + 1024

    def test_oversize_downsample_failure_returns_none(self, tmp_path, monkeypatch):
        import agent.image_channel as ic
        p = tmp_path / "big.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * (MAX_IMAGE_BYTES + 1))
        monkeypatch.setattr(ic, "_downsample_to_data_uri", lambda path: None)
        assert ic._image_to_data_uri(str(p)) is None


# ---------------------------------------------------------------------------
# ④ 出站打码三写函数
# ---------------------------------------------------------------------------

_PAYLOAD = base64.b64encode(b"A" * 2048).decode()
_URI = f"data:image/png;base64,{_PAYLOAD}"


def _enabled_logging(monkeypatch):
    """三写函数共用的 get_logging_config().enabled 开关（模块内局部 import → patch 源模块）。"""
    import niu_api.config as niu_cfg
    monkeypatch.setattr(
        niu_cfg, "get_logging_config", lambda: SimpleNamespace(enabled=True))


class TestOutboundMasking:
    def test_http_logger_write_log_entry_masks(self, tmp_path, monkeypatch):
        import agent.generic.http_logger as hl
        _enabled_logging(monkeypatch)
        monkeypatch.setattr(hl, "_get_log_dir", lambda: tmp_path)
        entry = {"request": {"body": {"messages": [
            {"role": "user", "content": f"看图 {_URI}"}]}}}
        hl._write_log_entry(1, entry)
        files = list(tmp_path.glob("*.json"))
        assert len(files) == 1
        text = files[0].read_text(encoding="utf-8")
        assert _PAYLOAD not in text
        assert f"[image data, {len(_PAYLOAD)} bytes]" in text

    def test_litellm_write_raw_log_masks(self, tmp_path, monkeypatch):
        import agent.generic.litellm_adapter as la
        _enabled_logging(monkeypatch)
        monkeypatch.setattr(la, "_get_app_log_dir", lambda: tmp_path)
        data = {"request": {"body": {"messages": [
            {"role": "user", "content": f"看图 {_URI}"}]}}}
        la._write_raw_log("request", data, seq=1)
        # 落盘路径 = <app_log_dir>/raw_http/<YYYYMMDD>/<seq:06d>_request.json
        files = list(tmp_path.glob("raw_http/*/*.json"))
        assert len(files) == 1
        text = files[0].read_text(encoding="utf-8")
        assert _PAYLOAD not in text
        assert f"[image data, {len(_PAYLOAD)} bytes]" in text

    def test_litellm_write_interaction_log_masks(self, tmp_path, monkeypatch):
        import agent.generic.litellm_adapter as la
        _enabled_logging(monkeypatch)
        monkeypatch.setattr(la, "_get_app_log_dir", lambda: tmp_path)
        # 小载荷（32B）：交互日志用户输入 400 字截断不生效——打码是唯一防线
        small_b64 = base64.b64encode(b"B" * 32).decode()
        entry = {
            "type": "request",
            "timestamp": "2026-09-10T10:00:00",
            "model": "m",
            "messages": [{"role": "user", "content": f"看图 data:image/jpeg;base64,{small_b64} 描述"}],
            "tools": [],
        }
        la._write_interaction_log(entry)
        files = list(tmp_path.glob("llm_interaction_*.log"))
        assert len(files) == 1
        text = files[0].read_text(encoding="utf-8")
        assert small_b64 not in text
        assert f"[image data, {len(small_b64)} bytes]" in text

# 飞书图片格式 fallback 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 飞书 adapter 在发送图片时，如果文件格式不被 IM 平台支持（如 SVG），自动降级为文件传输方式发送，确保用户始终能看到内容。

**Architecture:** 在 `_filter_media()` 中，图片走 `upload_image()` 之前先检查文件扩展名。如果扩展名不在 IM 平台支持的图片格式白名单中（png/jpg/jpeg/gif/webp/bmp），将其路由到文件上传路径（`upload_file()`），而非图片上传路径。

**Tech Stack:** Python 3.11, 飞书 IM Adapter (lark-oapi)

---

## 已验证的关键事实

1. **`_filter_media()`** 是图片/文件路由核心（`im-adapters/feishu/src/niu_feishu_adapter/adapter.py:440-486`）：`![...](...)` → `upload_image()`，`[...](...)` → `upload_file()`
2. **`upload_image()`** 无任何格式校验（`feishu_api.py:115-149`），SVG 原样 POST 到飞书图片 API → 飞书拒绝 → 返回 None → 图片静默丢弃
3. **`upload_file()`** 用 `file_type=stream`，不校验格式，任何文件都能上传成功（`feishu_api.py:152-177`）
4. **飞书图片 API 支持格式**：png、jpeg、gif、webp、bmp（飞书官方文档，SVG 不在其中）
5. **`compress_image()`** 只在文件 >10MB 时转 JPEG（`feishu_api.py:271-290`），不做格式校验
6. **`_on_send` fallback**（`adapter.py:326-336`）：上传失败的图片重试 `upload_image`——同样的格式问题会再次失败
7. **Pillow 已安装**（`Pillow==12.2.0`），可用于格式检测但不需要用于转换

## 设计决策

- **白名单方式**：定义 IM 平台支持的图片格式集合，不在白名单中的走文件路径
- **检查点在 `_filter_media`**：在调用 `upload_image` 之前检查，而非在 `upload_image` 内部——保持单一职责
- **不删除原 Markdown**：不支持格式的图片转为文件后，原位置替换为 `↑ 文件名` 文本提示
- **`_on_send` fallback 不改**：fallback 重试只针对已经走图片路径但上传失败的，不影响已转为文件的
- **已知限制：无卡片 else 分支不覆盖**：`_on_send` 的 else 分支（adapter.py:337-354，无流式卡片时发送纯 Markdown）直接删除所有图片标记，不经过 `_filter_media`。SVG 在该路径中会被静默删除。这是预存缺口，本次不扩大范围修复。该路径在非流式 Agent 直接 SEND 回复时触发，实际使用中绝大多数回复都走流式卡片路径。

## File Structure

| 文件 | 职责 | 操作 |
|------|------|------|
| `im-adapters/feishu/src/niu_feishu_adapter/feishu_api.py` | 新增 `is_supported_image()` 格式检查函数 | 修改 |
| `im-adapters/feishu/src/niu_feishu_adapter/adapter.py` | `_filter_media()` 中加格式判断，不支持的走文件路径 | 修改 |
| `tests/test_feishu_image_format.py` | 格式检查 + fallback 逻辑测试 | 创建 |

---

### Task 1: is_supported_image 函数实现

**Files:**
- Modify: `im-adapters/feishu/src/niu_feishu_adapter/feishu_api.py`（在 `compress_image` 函数之后插入）
- Test: `tests/test_feishu_image_format.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_feishu_image_format.py`：

```python
"""飞书图片格式检查测试"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "im-adapters", "feishu", "src"))
from niu_feishu_adapter.feishu_api import is_supported_image


class TestIsSupportedImage:
    def test_png_supported(self):
        assert is_supported_image(Path("photo.png")) is True

    def test_jpg_supported(self):
        assert is_supported_image(Path("photo.jpg")) is True

    def test_jpeg_supported(self):
        assert is_supported_image(Path("photo.jpeg")) is True

    def test_gif_supported(self):
        assert is_supported_image(Path("animation.gif")) is True

    def test_webp_supported(self):
        assert is_supported_image(Path("photo.webp")) is True

    def test_bmp_supported(self):
        assert is_supported_image(Path("photo.bmp")) is True

    def test_svg_not_supported(self):
        assert is_supported_image(Path("map.svg")) is False

    def test_tiff_not_supported(self):
        assert is_supported_image(Path("scan.tiff")) is False

    def test_uppercase_extension(self):
        assert is_supported_image(Path("PHOTO.PNG")) is True

    def test_no_extension(self):
        assert is_supported_image(Path("README")) is False

    def test_real_svg_file(self):
        """用真实 SVG 文件测试"""
        with tempfile.NamedTemporaryFile(suffix=".svg", mode="w", delete=False) as f:
            f.write('<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>')
            path = Path(f.name)
        try:
            assert is_supported_image(path) is False
        finally:
            os.unlink(path)

    def test_real_png_file(self):
        """用真实 PNG 文件测试"""
        from PIL import Image
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = Path(f.name)
        try:
            Image.new("RGB", (10, 10)).save(str(path), "PNG")
            assert is_supported_image(path) is True
        finally:
            os.unlink(path)
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd /Users/lilei/tools/ai-bot && python -m pytest tests/test_feishu_image_format.py::TestIsSupportedImage::test_png_supported -v`
Expected: FAIL with `ImportError: cannot import name 'is_supported_image'`

- [ ] **Step 3: 实现 is_supported_image 函数**

在 `im-adapters/feishu/src/niu_feishu_adapter/feishu_api.py` 的 `compress_image` 函数之后（约 L291），插入：

```python
# 飞书图片 API 支持的格式（飞书官方文档）
# https://open.feishu.cn/document/server-docs/im-v1/image/create
_SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def is_supported_image(path: Path) -> bool:
    """检查文件是否为飞书图片 API 支持的格式。

    飞书图片 API 支持 png/jpeg/gif/webp/bmp，不支持 SVG/TIFF 等。
    用于在 _filter_media 中决定走图片上传还是文件上传。
    只做扩展名判断，不做存在性检查（_filter_media 调用前已确保文件存在）。

    Args:
        path: 文件路径

    Returns:
        True 如果扩展名在支持列表中
    """
    return path.suffix.lower() in _SUPPORTED_IMAGE_EXTS
```

- [ ] **Step 4: 运行测试验证通过**

Run: `cd /Users/lilei/tools/ai-bot && python -m pytest tests/test_feishu_image_format.py -v`
Expected: 12 PASSED

- [ ] **Step 5: ruff 检查**

Run: `cd /Users/lilei/tools/ai-bot && ruff check im-adapters/feishu/src/niu_feishu_adapter/feishu_api.py`
Expected: OK

- [ ] **Step 6: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add im-adapters/feishu/src/niu_feishu_adapter/feishu_api.py tests/test_feishu_image_format.py
git commit -m "feat(feishu): 新增 is_supported_image 格式检查函数"
```

---

### Task 2: _filter_media 中加格式 fallback

**Files:**
- Modify: `im-adapters/feishu/src/niu_feishu_adapter/adapter.py:440-486`（`_filter_media` 方法）

- [ ] **Step 1: 写失败测试**

在 `tests/test_feishu_image_format.py` 末尾追加：

```python
from unittest.mock import patch, MagicMock
from niu_feishu_adapter.adapter import FeishuAdapter


class TestFilterMediaFormatFallback:
    """测试 _filter_media 中不支持格式的图片自动转为文件"""

    def _make_adapter(self):
        """创建测试用 adapter（不连接飞书）"""
        adapter = FeishuAdapter.__new__(FeishuAdapter)
        adapter._app_id = "test_id"
        adapter._app_secret = "test_secret"
        return adapter

    def test_svg_falls_back_to_file(self):
        """SVG 图片应走文件上传路径"""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".svg", mode="w", delete=False) as f:
            f.write('<svg xmlns="http://www.w3.org/2000/svg"/>')
            svg_path = f.name

        adapter = self._make_adapter()
        try:
            with patch("niu_feishu_adapter.feishu_api.upload_file", return_value="file_key_123") as mock_file, \
                 patch("niu_feishu_adapter.feishu_api.upload_image", return_value=None) as mock_img:
                content = f"![地图]({svg_path})"
                filtered, images, files = adapter._filter_media(content)
                # 应走文件路径
                assert len(files) == 1
                assert files[0]["file_key"] == "file_key_123"
                # 不应走图片路径
                assert len(images) == 0
                # mock 验证
                mock_file.assert_called_once()
                mock_img.assert_not_called()
        finally:
            os.unlink(svg_path)

    def test_png_goes_image_path(self):
        """PNG 图片应走图片上传路径"""
        import tempfile
        from PIL import Image
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            png_path = f.name
        Image.new("RGB", (10, 10)).save(png_path, "PNG")

        adapter = self._make_adapter()
        try:
            with patch("niu_feishu_adapter.feishu_api.upload_image", return_value="img_key_456") as mock_img, \
                 patch("niu_feishu_adapter.feishu_api.upload_file", return_value=None) as mock_file:
                content = f"![照片]({png_path})"
                filtered, images, files = adapter._filter_media(content)
                # 应走图片路径
                assert len(images) == 1
                assert images[0]["img_key"] == "img_key_456"
                # 不应走文件路径
                assert len(files) == 0
                mock_img.assert_called_once()
                mock_file.assert_not_called()
        finally:
            os.unlink(png_path)

    def test_svg_replaced_with_file_marker(self):
        """SVG 转文件后，Markdown 中替换为 ↑ 文件名"""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".svg", mode="w", delete=False) as f:
            f.write('<svg/>')
            svg_path = f.name

        adapter = self._make_adapter()
        try:
            with patch("niu_feishu_adapter.feishu_api.upload_file", return_value="file_key_789"):
                content = f"地图如下：\n\n![扫地机地图]({svg_path})\n\n以上是地图。"
                filtered, images, files = adapter._filter_media(content)
                # 原图片标记应被替换为 ↑ 文件名
                assert "![扫地机地图]" not in filtered
                assert "↑ 扫地机地图" in filtered
        finally:
            os.unlink(svg_path)
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd /Users/lilei/tools/ai-bot && python -m pytest tests/test_feishu_image_format.py::TestFilterMediaFormatFallback -v`
Expected: FAIL（SVG 仍走图片路径，`mock_file.assert_called_once()` 失败）

- [ ] **Step 3: 修改 _filter_media 方法**

在 `im-adapters/feishu/src/niu_feishu_adapter/adapter.py` 的 `_filter_media` 方法中（L440-486），修改图片处理分支。

当前代码（L455-469）：
```python
            if is_image:
                # 本地路径检查：只要不是 URL/data URI 且文件存在就上传（与旧代码一致）
                if not path or path.startswith(("http://", "https://", "ftp://", "data:")):
                    continue
                if not Path(path).exists():
                    replacements.append((start_idx, end_idx, ""))
                    continue
                img_key = upload_image(self._app_id, self._app_secret, path)
                if img_key:
                    images.append({"img_key": img_key, "alt": alt_text or "照片"})
                    replacements.append((start_idx, end_idx, "[PHOTO_SEP]"))
                else:
                    # 上传失败，记录为 failed_image，终结后发独立图片消息重试
                    images.append({"img_key": None, "alt": alt_text or "照片", "path": path, "failed": True})
                    replacements.append((start_idx, end_idx, ""))
```

改为：
```python
            if is_image:
                # 本地路径检查：只要不是 URL/data URI 且文件存在就上传（与旧代码一致）
                if not path or path.startswith(("http://", "https://", "ftp://", "data:")):
                    continue
                if not Path(path).exists():
                    replacements.append((start_idx, end_idx, ""))
                    continue
                # 格式检查：不支持图片格式（如 SVG）自动降级为文件传输
                if not is_supported_image(Path(path)):
                    file_key = upload_file(self._app_id, self._app_secret, path, alt_text or Path(path).name)
                    if file_key:
                        display_name = alt_text or Path(path).name
                        files.append({"file_key": file_key, "filename": display_name})
                        replacements.append((start_idx, end_idx, f"↑ {display_name}"))
                    else:
                        replacements.append((start_idx, end_idx, f"[文件上传失败: {alt_text or Path(path).name}]"))
                    continue
                img_key = upload_image(self._app_id, self._app_secret, path)
                if img_key:
                    images.append({"img_key": img_key, "alt": alt_text or "照片"})
                    replacements.append((start_idx, end_idx, "[PHOTO_SEP]"))
                else:
                    # 上传失败，记录为 failed_image，终结后发独立图片消息重试
                    images.append({"img_key": None, "alt": alt_text or "照片", "path": path, "failed": True})
                    replacements.append((start_idx, end_idx, ""))
```

同时修改 import 行（L446），在 `upload_image, upload_file, extract_md_refs` 后加 `is_supported_image`：

当前：
```python
        from niu_feishu_adapter.feishu_api import upload_image, upload_file, extract_md_refs
```
改为：
```python
        from niu_feishu_adapter.feishu_api import upload_image, upload_file, extract_md_refs, is_supported_image
```

- [ ] **Step 4: 运行测试验证通过**

Run: `cd /Users/lilei/tools/ai-bot && python -m pytest tests/test_feishu_image_format.py -v`
Expected: 全部 PASSED

- [ ] **Step 5: ruff 检查**

Run: `cd /Users/lilei/tools/ai-bot && ruff check im-adapters/feishu/src/niu_feishu_adapter/adapter.py`
Expected: OK

- [ ] **Step 6: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add im-adapters/feishu/src/niu_feishu_adapter/adapter.py tests/test_feishu_image_format.py
git commit -m "feat(feishu): 不支持图片格式自动降级为文件传输"
```

---

### Task 3: 运行环境验证

**Files:** 无（手动验证）

- [ ] **Step 1: 重启应用**

```bash
cd /Users/lilei/tools/ai-bot && ./niu
```

- [ ] **Step 2: 在飞书中测试**

通过飞书向 Agent 发送消息："帮我看看扫地机的地图"

预期行为：
1. Agent 调 `ha_get_image` 下载 SVG 地图到本地
2. Agent 回复包含 `![扫地机地图](/path/to/map.svg)`
3. 飞书 adapter 的 `_filter_media` 检测到 `.svg` 不在支持列表
4. 走 `upload_file` 路径，上传为文件
5. 飞书中显示 `↑ 扫地机地图` 文本 + 可下载的文件

- [ ] **Step 3: 验证飞书收到文件**

- 飞书中应显示文件消息（可点击下载）
- 不应出现空白卡片或缺失图片

- [ ] **Step 4: 确认提交历史**

```bash
cd /Users/lilei/tools/ai-bot
git log --oneline -3
```
确认 2 个实现提交都在历史中。

---

## Self-Review

### 1. Spec coverage
- ✅ SVG 等不支持格式自动转文件传输 → Task 2 `_filter_media` 格式判断
- ✅ 白名单方式判断支持格式 → Task 1 `is_supported_image` + `_SUPPORTED_IMAGE_EXTS`
- ✅ 支持的格式（PNG/JPG/GIF/WEBP/BMP）仍走图片路径 → Task 2 测试验证
- ✅ 文件上传失败有错误提示 → Task 2 `[文件上传失败: ...]` 替换
- ✅ 运行环境验证 → Task 3
- ⚠️ 已知限制：无卡片 else 分支不覆盖（预存缺口，非本次引入）

### 2. Placeholder scan
- 无 TBD/TODO
- 所有代码块完整
- 所有命令含 expected output

### 3. Type consistency
- `is_supported_image(path: Path) -> bool` → Task 2 中 `is_supported_image(Path(path))` ✓
- `_SUPPORTED_IMAGE_EXTS` 常量名一致 ✓
- `upload_file(self._app_id, self._app_secret, path, alt_text or Path(path).name)` 签名与 `feishu_api.py:152` 一致 ✓
- `files.append({"file_key": ..., "filename": ...})` 与 `_on_send` 中 `file_info["file_key"]` / `file_info["filename"]` 一致 ✓

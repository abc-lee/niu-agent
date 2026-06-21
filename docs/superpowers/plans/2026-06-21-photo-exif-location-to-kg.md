# 照片 EXIF 位置信息写入知识图谱 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将照片 EXIF 中的 GPS 坐标和逆地理编码后的位置名同时写入知识图谱，让用户可以通过地点查询照片。

**Architecture:** 三步走——(1) 新增逆地理编码模块：GPS坐标→地名，带本地缓存；(2) 修改照片入库流程：exif 传入 KG 同步函数，照片实体 description 包含位置信息，新建位置实体并建关系；(3) 修改 `_generate_stable_description()` 将位置信息纳入照片描述。

**Tech Stack:** Python (photo-server), HTTP (Nominatim API), SQLite (本地缓存)

---

## 文件结构

| 文件 | 操作 | 职责 |
|------|------|------|
| `mcp-servers/photo-server/src/niu_photo_server/geocode.py` | **新建** | 逆地理编码模块：GPS→地名，含本地缓存 |
| `mcp-servers/photo-server/src/niu_photo_server/__init__.py` | 修改 | 传递 exif 到 KG、修改 description 生成、新建位置实体 |
| `mcp-servers/photo-server/tests/test_geocode.py` | **新建** | 逆地理编码模块的单元测试 |
| `scripts/ingest_unified.py` | 修改 | 补传 exif 参数到 sync_photo_to_kg |

---

### Task 1: 新建逆地理编码模块

**Files:**
- Create: `mcp-servers/photo-server/src/niu_photo_server/geocode.py`
- Create: `mcp-servers/photo-server/tests/test_geocode.py`

- [ ] **Step 1: 创建 geocode.py**

```python
"""逆地理编码模块：GPS 坐标 → 人可读地名，带本地缓存"""

import sqlite3
from pathlib import Path
from loguru import logger

# 缓存数据库路径（与 photos.db 同目录）
_cache_db_path: str | None = None


def _get_cache_db_path() -> str:
    """获取缓存数据库路径（延迟初始化）"""
    global _cache_db_path
    if _cache_db_path is None:
        from . import get_workspace_path
        ws = get_workspace_path()
        if ws:
            _cache_db_path = str(Path(ws) / "geocode_cache.db")
        else:
            _cache_db_path = str(Path.home() / ".niu" / "geocode_cache.db")
    return _cache_db_path


def _ensure_cache_db():
    """确保缓存数据库和表存在"""
    db_path = _get_cache_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS geocode_cache (
                lat_key TEXT NOT NULL,
                lon_key TEXT NOT NULL,
                location_name TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (lat_key, lon_key)
            )
        """)
        conn.commit()


def _round_coord(value: float, decimals: int = 2) -> str:
    """将坐标四舍五入到指定小数位（用于缓存键，约 1km 精度）"""
    return f"{value:.{decimals}f}"


def reverse_geocode(lat: float, lon: float) -> str | None:
    """将 GPS 坐标转换为人可读地名

    优先查本地缓存，未命中则调用 Nominatim API（OpenStreetMap）。

    Args:
        lat: 纬度
        lon: 经度

    Returns:
        位置名字符串（如"河北省石家庄市平山县西柏坡"），失败返回 None
    """
    if lat == 0.0 and lon == 0.0:
        return None

    lat_key = _round_coord(lat)
    lon_key = _round_coord(lon)

    # 1. 查缓存
    try:
        _ensure_cache_db()
        with sqlite3.connect(_get_cache_db_path()) as conn:
            cursor = conn.execute(
                "SELECT location_name FROM geocode_cache WHERE lat_key = ? AND lon_key = ?",
                (lat_key, lon_key),
            )
            row = cursor.fetchone()
        if row:
            logger.info(f"[Geocode] Cache hit: {lat_key},{lon_key} → {row[0]}")
            return row[0]
    except Exception as e:
        logger.warning(f"[Geocode] Cache lookup failed: {e}")

    # 2. 调用 Nominatim API
    try:
        import urllib.request
        import urllib.parse
        import json

        url = (
            f"https://nominatim.openstreetmap.org/reverse?"
            f"lat={lat}&lon={lon}&format=json&accept-language=zh&zoom=12"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "NiuPhotoServer/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        address = data.get("address", {})
        # 从最具体到最宽泛，拼接位置名
        parts = []
        for key in ["village", "town", "suburb", "city_district", "city", "county", "state"]:
            if key in address and address[key] not in parts:
                parts.append(address[key])

        if not parts and "country" in address:
            parts.append(address["country"])

        if not parts:
            display_name = data.get("display_name", "")
            parts = display_name.split(",")[:3] if display_name else []

        location_name = "".join(parts) if parts else None

        # 3. 写入缓存
        if location_name:
            try:
                _ensure_cache_db()
                with sqlite3.connect(_get_cache_db_path()) as conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO geocode_cache (lat_key, lon_key, location_name) VALUES (?, ?, ?)",
                        (lat_key, lon_key, location_name),
                    )
                    conn.commit()
                logger.info(f"[Geocode] Cached: {lat_key},{lon_key} → {location_name}")
            except Exception as e:
                logger.warning(f"[Geocode] Cache write failed: {e}")

        return location_name

    except Exception as e:
        logger.warning(f"[Geocode] Nominatim API failed for {lat},{lon}: {e}")
        return None
```

- [ ] **Step 2: 创建测试文件**

```python
"""逆地理编码模块测试"""
import pytest
import os
import sqlite3
import tempfile
from unittest.mock import patch, MagicMock


def test_round_coord():
    from niu_photo_server.geocode import _round_coord
    assert _round_coord(39.904200, 2) == "39.90"
    assert _round_coord(116.407396, 2) == "116.41"
    assert _round_coord(0.0, 2) == "0.00"


def test_reverse_geocode_zero_coords():
    from niu_photo_server.geocode import reverse_geocode
    result = reverse_geocode(0.0, 0.0)
    assert result is None


def test_reverse_geocode_cache_hit():
    """缓存命中时不调用 API"""
    from niu_photo_server import geocode

    with tempfile.TemporaryDirectory() as tmpdir:
        cache_path = os.path.join(tmpdir, "geocode_cache.db")
        # 手动写入缓存
        conn = sqlite3.connect(cache_path)
        conn.execute(
            "CREATE TABLE geocode_cache (lat_key TEXT, lon_key TEXT, location_name TEXT, PRIMARY KEY (lat_key, lon_key))"
        )
        conn.execute(
            "INSERT INTO geocode_cache VALUES (?, ?, ?)",
            ("39.90", "116.41", "北京市东城区"),
        )
        conn.commit()
        conn.close()

        # 替换缓存路径
        geocode._cache_db_path = cache_path
        try:
            result = geocode.reverse_geocode(39.904, 116.407)
            assert result == "北京市东城区"
        finally:
            geocode._cache_db_path = None


def test_reverse_geocode_api_call():
    """缓存未命中时调用 Nominatim API"""
    from niu_photo_server import geocode

    mock_response = MagicMock()
    mock_response.read.return_value = '{"address": {"city": "石家庄市", "state": "河北省"}, "display_name": "test"}'.encode("utf-8")
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

    with tempfile.TemporaryDirectory() as tmpdir:
        cache_path = os.path.join(tmpdir, "geocode_cache.db")
        geocode._cache_db_path = cache_path
        try:
            with patch("urllib.request.urlopen", return_value=mock_response):
                result = geocode.reverse_geocode(38.345, 114.234)
            assert result is not None
            assert "石家庄市" in result
        finally:
            geocode._cache_db_path = None
```

- [ ] **Step 3: 运行测试**

Run: `cd mcp-servers/photo-server && python -m pytest tests/test_geocode.py -v`
Expected: 3 个测试通过

- [ ] **Step 4: Commit**

```bash
git add mcp-servers/photo-server/src/niu_photo_server/geocode.py mcp-servers/photo-server/tests/test_geocode.py
git commit -m "feat: add reverse geocoding module with local cache"
```

---

### Task 2: 修改照片入库流程——exif 传入 KG 同步

**Files:**
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py`
- Modify: `scripts/ingest_unified.py`

**核心改动**：当前 `sync_photo_to_kg(file_path, abstract, detected_persons)` 不接收 exif 参数。需要将 exif 传入，让 KG 写入位置信息。同时 `ingest_unified.py` 中的调用也需补传 exif。

- [ ] **Step 1: 修改 `format_photo_ingest_data` 签名，接收 exif 参数**

将第 536 行：
```python
def format_photo_ingest_data(
    file_path: str, abstract: str, detected_persons: list
) -> dict:
```
改为：
```python
def format_photo_ingest_data(
    file_path: str, abstract: str, detected_persons: list, exif: dict | None = None
) -> dict:
```

- [ ] **Step 2: 解析 exif 中的位置信息（在 entities 构建之前）**

在第 559 行（`photo_entity_name = normalized_stem`）之后、第 562 行（`entities = [...]`）之前，添加位置解析逻辑：

```python
    # 位置信息：从 EXIF GPS 坐标逆地理编码获取地名
    location_name = None
    location_info = None  # "地名 (lat,lon)" 格式，用于 description
    if exif and exif.get("location"):
        try:
            parts = exif["location"].split(",")
            lat, lon = float(parts[0]), float(parts[1])
            from .geocode import reverse_geocode
            location_name = reverse_geocode(lat, lon)
            if location_name:
                location_info = f"{location_name} ({lat:.4f},{lon:.4f})"
            else:
                location_info = f"{lat:.4f},{lon:.4f}"
        except Exception as e:
            logger.warning(f"[KG] Geocode failed for {exif.get('location')}: {e}")
            location_info = exif["location"]
```

注意：`location_info` 必须在 `entities = [...]` 构建之前解析，因为照片实体的 description 需要引用它（Step 4）。

- [ ] **Step 3: 在 entities 构建后添加位置实体和关系**

在第 570 行（照片实体 `entities = [...]` 的构建结束）之后、`relationships = []` 之后（约第 572 行），添加位置实体和关系的构建：

```python
    if location_name:
        entities.append({
            "entity_name": location_name,
            "entity_type": "location",
            "description": f"地点：{location_name}，GPS坐标：{exif['location']}",
            "file_path": normalized_path,
            "source_id": normalized_path,
        })
        relationships.append({
            "src_id": photo_entity_name,
            "tgt_id": location_name,
            "keywords": "拍摄于",
            "file_path": normalized_path,
            "source_id": normalized_path,
        })
```

- [ ] **Step 4: 修改 `_generate_stable_description` 签名和实现**

将第 490 行：
```python
def _generate_stable_description(normalized_stem: str, abstract: str) -> str:
```
改为：
```python
def _generate_stable_description(normalized_stem: str, abstract: str, location_info: str | None = None) -> str:
```

在第 511-515 行（`parts = [...]` 构建部分），在 `if date_part:` 之后添加位置：

```python
    parts = [f"照片 {normalized_stem}"]
    if date_part:
        parts.append(f"拍摄于{date_part}")
    if location_info:
        parts.append(location_info)

    return "，".join(parts)
```

- [ ] **Step 5: 更新 `_generate_stable_description` 的调用点**

在 `format_photo_ingest_data` 中（约第 566 行），将：
```python
            "description": _generate_stable_description(normalized_stem, abstract),
```
改为：
```python
            "description": _generate_stable_description(normalized_stem, abstract, location_info),
```

`location_info` 变量在 Step 2 中定义，位于 entities 构建之前，所以此处引用时已定义。

- [ ] **Step 6: 修改 `sync_photo_to_kg` 签名，接收 exif 参数**

将第 642 行：
```python
def sync_photo_to_kg(file_path: str, abstract: str, detected_persons: list, force: bool = False) -> dict:
```
改为：
```python
def sync_photo_to_kg(file_path: str, abstract: str, detected_persons: list, force: bool = False, exif: dict | None = None) -> dict:
```

将第 677 行：
```python
    return _do_sync_photo_to_kg_sync(file_path, abstract, detected_persons)
```
改为：
```python
    return _do_sync_photo_to_kg_sync(file_path, abstract, detected_persons, exif)
```

- [ ] **Step 7: 修改 `_do_sync_photo_to_kg_sync` 签名和调用**

将第 680 行：
```python
def _do_sync_photo_to_kg_sync(file_path: str, abstract: str, detected_persons: list) -> dict:
```
改为：
```python
def _do_sync_photo_to_kg_sync(file_path: str, abstract: str, detected_persons: list, exif: dict | None = None) -> dict:
```

将第 685 行：
```python
        data = format_photo_ingest_data(file_path, abstract, detected_persons)
```
改为：
```python
        data = format_photo_ingest_data(file_path, abstract, detected_persons, exif)
```

- [ ] **Step 8: 修改 `ingest_photo` 中的 `sync_photo_to_kg` 调用**

将约第 2098 行：
```python
        kg_result = sync_photo_to_kg(final_path_resolved, abstract, detected_persons)
```
改为：
```python
        kg_result = sync_photo_to_kg(final_path_resolved, abstract, detected_persons, exif=exif)
```

- [ ] **Step 9: 修改 `ingest_unified.py` 中的 `sync_photo_to_kg` 调用和 `detected_persons` 构建**

将 `scripts/ingest_unified.py` 第 252 行：
```python
        kg_result = ps.sync_photo_to_kg(str(final_path), abstract, detected_persons)
```
改为：
```python
        kg_result = ps.sync_photo_to_kg(str(final_path), abstract, detected_persons, exif=exif)
```

该脚本在 line 119 已提取 `exif = ps.extract_exif(str(source))`，此处补传即可。

同时修复 `detected_persons` 缺少 `auto_label` 字段的既有 bug。将第 160-166 行：
```python
                detected_persons.append({
                    "id": person_id,
                    "name": person_name,
                    "similarity": similarity,
                    "bbox": bbox,
                    "confidence": confidence,
                })
```
改为：
```python
                detected_persons.append({
                    "id": person_id,
                    "name": person_name,
                    "auto_label": row[1] if row else "",
                    "similarity": similarity,
                    "bbox": bbox,
                    "confidence": confidence,
                })
```

`auto_label` 在 line 156 已查询（`SELECT name, auto_label`），`row[1]` 即为 `auto_label`。缺少此字段会导致 `format_photo_ingest_data` 中未命名人物实体名解析为空字符串，被 `if not entity_name: continue` 跳过，KG 中丢失未命名人物。

- [ ] **Step 10: 修改 `chunk_text` 构建逻辑，加入位置信息**

在 `_do_sync_photo_to_kg_sync` 中，`all_entity_names` 和 `all_person_names` 在 person 去重过滤之前构建（约第 686-687 行）。`all_location_names` 应在同一位置构建，保持一致。

将第 686-687 行：
```python
        all_entity_names = [e["entity_name"] for e in data["entities"]]
        all_person_names = [e["entity_name"] for e in data["entities"] if e.get("entity_type") == "person"]
```
改为：
```python
        all_entity_names = [e["entity_name"] for e in data["entities"]]
        all_person_names = [e["entity_name"] for e in data["entities"] if e.get("entity_type") == "person"]
        all_location_names = [e["entity_name"] for e in data["entities"] if e.get("entity_type") == "location"]
```

然后在约第 707-712 行的 `chunk_text` 构建中，在 `if all_person_names:` 之后添加：
```python
        if all_location_names:
            chunk_text += f"地点：{', '.join(all_location_names)}\n"
```

- [ ] **Step 11: 确认 `kg_result` 返回值自动包含位置实体名**

无需修改代码。`kg_entities`（约第 774-777 行）遍历 `data["entities"]` 构建，而 person 去重过滤（第 692-696 行）只过滤 `entity_type == "person"` 的实体，location 实体会被自动包含在返回值中。

- [ ] **Step 12: 验证语法**

Run: `python -c "import ast; ast.parse(open('mcp-servers/photo-server/src/niu_photo_server/__init__.py').read()); print('OK')"`
Expected: OK

- [ ] **Step 13: Commit**

```bash
git add mcp-servers/photo-server/src/niu_photo_server/__init__.py scripts/ingest_unified.py
git commit -m "feat: write GPS coordinates and location names to knowledge graph on photo ingest"
```

---

### Task 3: 修改 camera 信息写入照片描述

**前置条件**：Task 2 已全部 commit。

**Files:**
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py`

- [ ] **Step 1: 在 `_generate_stable_description` 中加入 camera 信息**

将 `_generate_stable_description` 的签名再增加 `camera` 参数：

```python
def _generate_stable_description(normalized_stem: str, abstract: str, location_info: str | None = None, camera: str | None = None) -> str:
```

在 `parts` 构建中，`if location_info:` 之后添加：

```python
    if camera:
        parts.append(camera)
```

- [ ] **Step 2: 更新 `format_photo_ingest_data` 中的调用**

找到 Task 2 Step 5 修改后的调用：
```python
            "description": _generate_stable_description(normalized_stem, abstract, location_info),
```
改为：
```python
            "description": _generate_stable_description(normalized_stem, abstract, location_info, exif.get("camera") if exif else None),
```

- [ ] **Step 3: 验证语法**

Run: `python -c "import ast; ast.parse(open('mcp-servers/photo-server/src/niu_photo_server/__init__.py').read()); print('OK')"`
Expected: OK

- [ ] **Step 4: Commit**

```bash
git add mcp-servers/photo-server/src/niu_photo_server/__init__.py
git commit -m "feat: include camera model in photo entity description"
```

---

### Task 4: 修复 EXIF 提取——改用公开 API + HEIC 兼容

**Files:**
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py`

**问题**：
1. `extract_exif()` 使用 `img._getexif()`（非公开 API，Pillow 未来版本可能移除），应改用 `img.getexif()`（公开 API）。但 `getexif()` 返回的 `Exif` 对象与 `_getexif()` 返回的 dict 接口不同——`.items()` 只迭代 IFD0 顶层标签，DateTimeOriginal 在 Exif 子 IFD 中，GPSInfo 的值是整数偏移量而非 dict。需要用 `get_ifd()` 分别获取子 IFD。
2. HEIC/HEIF 格式在无 `pillow-heif` 插件时，`Image.open()` 会抛出 `UnidentifiedImageError`，当前被 `except Exception` 静默吞掉，用户无感知。

- [ ] **Step 1: 重写 EXIF 提取逻辑，改用 `getexif()` + `get_ifd()`**

将第 1170-1220 行的整个 try 块内容替换为：

```python
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS, GPSTAGS, IFD as ExifIFD

        img = Image.open(file_path)
        exif_data = img.getexif()

        if not exif_data:
            return result

        # IFD0 顶层标签（Make, Model 等）
        for tag_id, value in exif_data.items():
            tag = TAGS.get(tag_id, tag_id)
            if tag == "Model":
                result["camera"] = value
            elif tag == "Make":
                if result["camera"]:
                    result["camera"] = f"{value} {result['camera']}"
                else:
                    result["camera"] = value

        # Exif 子 IFD（DateTimeOriginal 等）
        exif_ifd = exif_data.get_ifd(ExifIFD.Exif)
        if exif_ifd:
            for tag_id, value in exif_ifd.items():
                tag = TAGS.get(tag_id, tag_id)
                if tag == "DateTimeOriginal":
                    result["taken_at"] = value
                elif tag == "DateTimeDigitized":
                    if not result["taken_at"]:
                        result["taken_at"] = value

        # GPS 子 IFD
        gps_ifd = exif_data.get_ifd(ExifIFD.GPSInfo)
        if gps_ifd:
            gps_data: dict[str, Any] = {}
            for gps_tag, gps_val in gps_ifd.items():
                gps_tag_name = str(GPSTAGS.get(gps_tag, gps_tag))
                gps_data[gps_tag_name] = gps_val

            lat = gps_data.get("GPSLatitude")
            lat_ref = gps_data.get("GPSLatitudeRef")
            lon = gps_data.get("GPSLongitude")
            lon_ref = gps_data.get("GPSLongitudeRef")

            if lat and lat_ref and lon and lon_ref:
                lat_val = lat[0] + lat[1] / 60 + lat[2] / 3600
                if lat_ref == "S":
                    lat_val = -lat_val
                lon_val = lon[0] + lon[1] / 60 + lon[2] / 3600
                if lon_ref == "W":
                    lon_val = -lon_val
                result["location"] = f"{lat_val:.6f},{lon_val:.6f}"
```

关键变化：
- `_getexif()` → `getexif()`（公开 API）
- DateTimeOriginal/DateTimeDigitized 从 `exif_data.get_ifd(ExifIFD.Exif)` 子 IFD 获取（`_getexif()` 内部合并了子 IFD，`getexif()` 需要手动获取）
- GPS 从 `exif_data.get_ifd(ExifIFD.GPSInfo)` 子 IFD 获取（`_getexif()` 返回的 GPSInfo 值是已解析的 dict，`getexif()` 返回的是整数偏移量，必须用 `get_ifd()` 解析）
- 新增 `from PIL.ExifTags import IFD as ExifIFD` 导入

- [ ] **Step 2: 添加 HEIC/HEIF 格式提示**

将 except 块改为分别捕获。注意 `UnidentifiedImageError` 必须在 `extract_exif` 的 try 块内部与 `Image` 一起导入，不能放到文件顶部——因为 PIL 是延迟加载的，顶部导入会导致 PIL 缺失时整个模块无法 import：

```python
    try:
        from PIL import Image, UnidentifiedImageError
        from PIL.ExifTags import TAGS, GPSTAGS, IFD as ExifIFD
        # ... (Step 1 的代码) ...
    except UnidentifiedImageError:
        logger.info(f"EXIF extraction skipped for {file_path} (unsupported format, possibly HEIC/HEIF)")
    except ImportError:
        logger.warning("PIL not installed, EXIF extraction disabled")
    except Exception as e:
        logger.warning(f"EXIF extraction failed: {e}")
```

`UnidentifiedImageError` 是 Pillow 7.0+ 的公开异常类。与 `Image` 一起在函数内部延迟导入是安全的——如果 PIL 缺失，`ImportError` 会在 `UnidentifiedImageError` 处理程序被评估之前被捕获。

- [ ] **Step 3: 验证语法**

Run: `python -c "import ast; ast.parse(open('mcp-servers/photo-server/src/niu_photo_server/__init__.py').read()); print('OK')"`
Expected: OK

- [ ] **Step 4: Commit**

```bash
git add mcp-servers/photo-server/src/niu_photo_server/__init__.py
git commit -m "fix: use public getexif() API with get_ifd() and add HEIC format hint"
```

---

## 自审记录

### Spec 覆盖检查

| 需求 | 对应 Task |
|------|----------|
| GPS 坐标解析为位置名 | Task 1（逆地理编码模块） |
| GPS 坐标写入知识图谱 | Task 2（照片实体 description 包含坐标） |
| 位置名写入知识图谱 | Task 2（新建 location 实体 + 拍摄于关系） |
| camera 信息写入描述 | Task 3 |
| 本地缓存避免重复 API 调用 | Task 1（SQLite 缓存，坐标四舍五入到小数点后2位做去重键） |
| `_getexif()` 非公开 API 修复 | Task 4 |
| HEIC/HEIF 格式兼容 | Task 4 |

### Placeholder 扫描

无 TBD/TODO/placeholder。

### 类型一致性

- `exif` 参数类型：`dict | None = None` — 在 `format_photo_ingest_data`、`sync_photo_to_kg`、`_do_sync_photo_to_kg_sync` 中一致
- `location_info` 格式：`"地名 (lat,lon)"` 或 `"lat,lon"` — 在 `_generate_stable_description` 和 `format_photo_ingest_data` 中一致使用
- location 实体的 `entity_type`：`"location"` — 与 LightRAG 已有的 location 类型一致

### 注意事项

1. **Nominatim API 速率限制**：1次/秒。照片入库通常是单张操作，不会超限。批量入库时需要在调用方加限流（当前不需要，因为入库是交互式的）
2. **离线场景**：首次没有网络时，reverse_geocode 返回 None，照片实体 description 中只有 GPS 坐标（无地名）。下次有网络时不会自动补查——但 dream-evolver 可以在后续进化中手动补充位置名
3. **现有照片**：此修改只影响新入库的照片。已入库的照片 KG 中没有位置信息。如果需要补录，需要写一个一次性脚本调用 `sync_photo_to_kg(..., force=True, exif=...)`
4. **无需新增依赖**：`geocode.py` 使用 Python 标准库（`sqlite3`、`urllib.request`、`json`），`loguru` 和 `Pillow` 已在 `requirements.txt` 中。Task 4 使用的 `getexif()`、`ExifIFD`、`UnidentifiedImageError` 均为 Pillow 12.2.0 已有 API。不需要修改 `requirements.txt`

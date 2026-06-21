# 高德地图逆地理编码 API 接入 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将逆地理编码从 Nominatim（国内超时）改为高德地图 API，API Key 放在配置文件，未配置时引导用户获取 Key，主Agent可通过浏览器帮用户完成注册。

**Architecture:** (1) geocode.py 改用高德逆地理编码 API + WGS-84→GCJ-02 坐标转换，Key 从 preferences.json 读取；(2) Key 未配置时返回提示文字引导读手册；(3) 写高德 Key 获取说明文档到 docs/manual-amap-setup.md，挂到系统管理手册；(4) 更新测试。

**Tech Stack:** Python (photo-server), HTTP (高德 Web 服务 API), SQLite (本地缓存)

---

## 文件结构

| 文件 | 操作 | 职责 |
|------|------|------|
| `mcp-servers/photo-server/src/niu_photo_server/geocode.py` | 修改 | 改用高德 API + WGS-84→GCJ-02 转换 + preferences.json 读取 Key |
| `mcp-servers/photo-server/src/niu_photo_server/__init__.py` | 修改 | location 解析逻辑：提示文字不创建实体，保留到 description |
| `mcp-servers/photo-server/tests/test_geocode.py` | 修改 | 更新测试适配高德 API |
| `docs/manual-amap-setup.md` | **新建** | 高德 API Key 获取说明（供主Agent通过浏览器帮用户注册） |
| `docs/SYSTEM_MANUAL.md` | 修改 | 分册索引中添加高德开通手册链接 |

---

### Task 1: 改用高德 API + 坐标转换 + 配置读取

**Files:**
- Modify: `mcp-servers/photo-server/src/niu_photo_server/geocode.py`
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py`

- [ ] **Step 1: 添加配置读取函数和 Key 未配置提示**

在 `geocode.py` 文件顶部（`from loguru import logger` 之后）添加：

```python
import json
import math
import urllib.request

PREFS_PATH = Path.home() / ".niu" / "preferences.json"

AMAP_KEY_HINT = (
    "高德地图 API Key 未配置。请告知主Agent，主Agent可帮您通过浏览器注册并获取 Key，"
    "写入 ~/.niu/preferences.json 的 amap.api_key 字段。"
)


class _AmapKeyNotConfigured:
    """Sentinel: reverse_geocode 在 Key 未配置时返回此对象"""
    def __str__(self):
        return AMAP_KEY_HINT
    def __bool__(self):
        return False  # if 判断中视为 False，不会创建 location 实体


AMAP_KEY_NOT_CONFIGURED = _AmapKeyNotConfigured()


def _get_amap_api_key() -> str | None:
    """从 preferences.json 读取高德 API Key"""
    try:
        if not PREFS_PATH.exists():
            return None
        prefs = json.loads(PREFS_PATH.read_text(encoding="utf-8"))
        return prefs.get("amap", {}).get("api_key", "").strip() or None
    except Exception:
        return None
```

- [ ] **Step 2: 添加 WGS-84→GCJ-02 坐标转换函数**

高德 API 使用 GCJ-02 坐标系，照片 EXIF 中是 WGS-84，需转换。在 `_round_coord` 函数之后添加：

```python

# ============== WGS-84 → GCJ-02 坐标转换 ==============

_PI = 3.1415926535897932384626
_A = 6378245.0
_EE = 0.00669342162296594323


def _out_of_china(lat: float, lon: float) -> bool:
    return not (72.004 <= lon <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _transform_lat(x: float, y: float) -> float:
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * abs(x) ** 0.5
    ret += (20.0 * (1 if math.sin(x * _PI) > 0 else -1) * math.sin(6.0 * x * _PI)
            + 20.0 * math.sin(2.0 * x * _PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * _PI) + 40.0 * math.sin(y / 3.0 * _PI)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * _PI) + 320.0 * math.sin(y * _PI / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lon(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * abs(x) ** 0.5
    ret += (20.0 * (1 if math.sin(x * _PI) > 0 else -1) * math.sin(6.0 * x * _PI)
            + 20.0 * math.sin(2.0 * x * _PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * _PI / 3.0) + 40.0 * math.sin(x / 4.0 * _PI)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * _PI) + 300.0 * math.sin(x / 30.0 * _PI)) * 2.0 / 3.0
    return ret


def _wgs84_to_gcj02(lat: float, lon: float) -> tuple[float, float]:
    """WGS-84 坐标转 GCJ-02（高德坐标）"""
    if _out_of_china(lat, lon):
        return lon, lat
    d_lat = _transform_lat(lon - 105.0, lat - 35.0)
    d_lon = _transform_lon(lon - 105.0, lat - 35.0)
    rad_lat = lat / 180.0 * _PI
    magic = 1 - _EE * math.sin(rad_lat) ** 2
    sqrt_magic = magic ** 0.5
    d_lat = (d_lat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrt_magic) * _PI)
    d_lon = (d_lon * 180.0) / (_A / sqrt_magic * math.cos(rad_lat) * _PI)
    return lon + d_lon, lat + d_lat
```

- [ ] **Step 3: 添加缓存版本检测，清空旧 Nominatim 缓存**

现有部署的用户已有 `geocode_cache.db`，里面存的是 Nominatim 格式的地名。切换高德后，缓存命中时返回旧格式地名，导致地名风格不一致。需要加版本检测，版本不匹配时重建缓存。

将 `_ensure_cache_db` 函数替换为：

```python
_CACHE_VERSION = 2  # Nominatim=1, Amap=2


def _ensure_cache_db():
    """确保缓存数据库和表存在，版本不匹配时重建"""
    db_path = _get_cache_db_path()
    with sqlite3.connect(db_path) as conn:
        # 检查缓存版本
        try:
            version = conn.execute("SELECT value FROM cache_meta WHERE key = 'version'").fetchone()
            if version and version[0] == str(_CACHE_VERSION):
                return  # 版号匹配，无需重建
        except sqlite3.OperationalError:
            pass  # 旧表没有 cache_meta
        # 版号不匹配或表不存在，重建
        conn.execute("DROP TABLE IF EXISTS geocode_cache")
        conn.execute("""
            CREATE TABLE geocode_cache (
                lat_key TEXT NOT NULL,
                lon_key TEXT NOT NULL,
                location_name TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (lat_key, lon_key)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cache_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("INSERT OR REPLACE INTO cache_meta VALUES (?, ?)", ("version", str(_CACHE_VERSION)))
        conn.commit()
```

- [ ] **Step 4: 替换 `reverse_geocode` 函数中的 API 调用**

将 `reverse_geocode` 函数中"2. 调用 Nominatim API"部分（从 `# 2. 调用 Nominatim API` 到 `return None`）替换为高德 API 调用：

```python
    # 2. 检查高德 API Key
    api_key = _get_amap_api_key()
    if not api_key:
        logger.warning(f"[Geocode] {AMAP_KEY_HINT}")
        return AMAP_KEY_NOT_CONFIGURED

    # 3. 调用高德逆地理编码 API
    gcj_lon, gcj_lat = _wgs84_to_gcj02(lat, lon)
    try:
        # 注意：api_key 在 URL 中，不要将此 URL 写入日志
        url = (
            f"https://restapi.amap.com/v3/geocode/regeo?"
            f"key={api_key}&location={gcj_lon:.6f},{gcj_lat:.6f}"
            f"&extensions=base&output=json"
        )
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        if data.get("status") != "1":
            logger.warning(f"[Geocode] Amap API error: {data.get('info', 'unknown')}")
            return None

        addr = data.get("regeocode", {}).get("addressComponent", {})
        # 从宽泛到具体拼接
        parts = []
        for key in ["province", "city", "district", "township", "neighborhood"]:
            val = addr.get(key, "")
            if val and val not in parts:
                # city 可能和 province 相同（直辖市）
                if key == "city" and val == addr.get("province", ""):
                    continue
                parts.append(val)

        location_name = "".join(parts) if parts else None

        # 4. 写入缓存
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
        logger.warning(f"[Geocode] Amap API failed for {lat},{lon}: {e}")
        return None
```

同时更新 `reverse_geocode` 的文档字符串：

将：
```python
    """将 GPS 坐标转换为人可读地名

    优先查本地缓存，未命中则调用 Nominatim API（OpenStreetMap）。

    Args:
        lat: 纬度
        lon: 经度

    Returns:
        位置名字符串（如"河北省石家庄市平山县西柏坡"），失败返回 None
    """
```
改为：
```python
    """将 GPS 坐标转换为人可读地名

    使用高德地图逆地理编码 API（国内可用）。需在 ~/.niu/preferences.json
    配置 amap.api_key。Key 未配置时返回 AMAP_KEY_NOT_CONFIGURED sentinel。

    Args:
        lat: 纬度（WGS-84）
        lon: 经度（WGS-84）

    Returns:
        位置名字符串（如"河北省石家庄市"），失败返回 None，
        Key 未配置返回 AMAP_KEY_NOT_CONFIGURED（__bool__ 为 False）。
    """
```

同时更新函数签名，将返回类型注解改为：

```python
def reverse_geocode(lat: float, lon: float) -> str | _AmapKeyNotConfigured | None:
```

- [ ] **Step 5: 修改 `format_photo_ingest_data` 中的 location 处理逻辑**

当 `reverse_geocode` 返回 `AMAP_KEY_NOT_CONFIGURED` sentinel 时，`location_name` 不为 None，会导致创建一个以提示文字为名称的 location 实体。需要修改 `__init__.py` 中 `format_photo_ingest_data` 的 location 解析逻辑。

将 `__init__.py` 第 573-577 行：

```python
            location_name = reverse_geocode(lat, lon)
            if location_name:
                location_info = f"{location_name} ({lat:.4f},{lon:.4f})"
            else:
                location_info = f"GPS {lat:.4f},{lon:.4f}"
```

改为：

```python
            from .geocode import reverse_geocode, AMAP_KEY_NOT_CONFIGURED
            geocode_result = reverse_geocode(lat, lon)
            if geocode_result is AMAP_KEY_NOT_CONFIGURED:
                # Key 未配置：提示文字写入 description，但不创建 location 实体
                location_name = None
                location_info = str(geocode_result)
            elif geocode_result:
                location_name = geocode_result
                location_info = f"{location_name} ({lat:.4f},{lon:.4f})"
            else:
                location_name = None
                location_info = f"GPS {lat:.4f},{lon:.4f}"
```

- [ ] **Step 6: 验证语法**

Run: `cd mcp-servers/photo-server && python -c "import ast; ast.parse(open('src/niu_photo_server/geocode.py').read()); ast.parse(open('src/niu_photo_server/__init__.py').read()); print('OK')"`
Expected: OK

- [ ] **Step 7: Commit**

```bash
git add mcp-servers/photo-server/src/niu_photo_server/geocode.py mcp-servers/photo-server/src/niu_photo_server/__init__.py
git commit -m "feat: switch reverse geocoding to Amap API with GCJ-02 transform and config-based key"
```

---

### Task 2: 更新测试适配高德 API

**Files:**
- Modify: `mcp-servers/photo-server/tests/test_geocode.py`

- [ ] **Step 1: 更新 `test_reverse_geocode_cache_hit` 适配缓存版本检测**

新 `_ensure_cache_db` 会检查 `cache_meta` 表中的版本号，版本不匹配时重建缓存。现有测试只创建了 `geocode_cache` 表，没有 `cache_meta` 表，导致 `_ensure_cache_db` 会清空测试数据。需要在测试中同时创建 `cache_meta` 表并写入版本号。

将现有 `test_reverse_geocode_cache_hit` 替换为：

```python
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
        # 写入缓存版本号，避免 _ensure_cache_db 重建
        conn.execute(
            "CREATE TABLE cache_meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        conn.execute(
            "INSERT INTO cache_meta VALUES (?, ?)",
            ("version", str(geocode._CACHE_VERSION)),
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
```

- [ ] **Step 2: 添加坐标转换测试**

在测试文件中添加 WGS-84→GCJ-02 转换测试和 Key 未配置提示测试：

```python

def test_wgs84_to_gcj02_in_china():
    """中国境内坐标需要偏移"""
    from niu_photo_server.geocode import _wgs84_to_gcj02
    # 北京天安门 WGS-84 坐标
    gcj_lon, gcj_lat = _wgs84_to_gcj02(39.9087, 116.3975)
    # GCJ-02 坐标应该与 WGS-84 不同（有偏移）
    assert gcj_lon != 116.3975 or gcj_lat != 39.9087


def test_wgs84_to_gcj02_outside_china():
    """境外坐标不需要偏移"""
    from niu_photo_server.geocode import _wgs84_to_gcj02
    # 纽约 WGS-84 坐标
    gcj_lon, gcj_lat = _wgs84_to_gcj02(40.7128, -74.0060)
    # 境外坐标不变
    assert gcj_lon == pytest.approx(-74.0060, abs=1e-6)
    assert gcj_lat == pytest.approx(40.7128, abs=1e-6)


def test_reverse_geocode_no_api_key():
    """API Key 未配置时返回提示文字"""
    from niu_photo_server import geocode
    import json
    import tempfile
    import os
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        cache_path = os.path.join(tmpdir, "geocode_cache.db")
        tmp_prefs = os.path.join(tmpdir, "preferences.json")
        with open(tmp_prefs, "w") as f:
            json.dump({"version": "1.0"}, f)

        geocode._cache_db_path = cache_path
        geocode.PREFS_PATH = Path(tmp_prefs)
        try:
            result = geocode.reverse_geocode(39.904, 116.407)
            assert result is geocode.AMAP_KEY_NOT_CONFIGURED
            assert "高德地图 API Key 未配置" in str(result)
        finally:
            geocode._cache_db_path = None
            geocode.PREFS_PATH = Path.home() / ".niu" / "preferences.json"
```

- [ ] **Step 3: 更新 API 调用测试为高德格式**

将 `test_reverse_geocode_api_call` 修改为高德 API 格式：

```python
def test_reverse_geocode_api_call():
    """缓存未命中时调用高德逆地理编码 API"""
    from niu_photo_server import geocode

    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({
        "status": "1",
        "regeocode": {
            "addressComponent": {
                "province": "河北省",
                "city": "石家庄市",
                "district": "平山县",
            }
        }
    }).encode("utf-8")
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

    with tempfile.TemporaryDirectory() as tmpdir:
        cache_path = os.path.join(tmpdir, "geocode_cache.db")
        # 写入含 amap key 的 preferences.json
        tmp_prefs = os.path.join(tmpdir, "preferences.json")
        with open(tmp_prefs, "w") as f:
            json.dump({"amap": {"api_key": "test_key"}}, f)

        geocode._cache_db_path = cache_path
        geocode.PREFS_PATH = Path(tmp_prefs)
        try:
            with patch("niu_photo_server.geocode.urllib.request.urlopen", return_value=mock_response):
                result = geocode.reverse_geocode(38.345, 114.234)
            assert result is not None
            assert "河北省石家庄市平山县" in result
        finally:
            geocode._cache_db_path = None
            geocode.PREFS_PATH = Path.home() / ".niu" / "preferences.json"
```

注意：测试文件头部需要添加 `import json` 和 `from pathlib import Path`（已有 `os`、`tempfile`）。

- [ ] **Step 4: 运行测试**

Run: `cd mcp-servers/photo-server && PYTHONPATH=src python -m pytest tests/test_geocode.py -v`
Expected: 7 个测试通过（3 更新 + 1 cache_hit + 3 新增）

- [ ] **Step 5: Commit**

```bash
git add mcp-servers/photo-server/tests/test_geocode.py
git commit -m "test: update geocode tests for Amap API and add coordinate transform tests"
```

---

### Task 3: 写高德 Key 获取说明文档

**Files:**
- Create: `docs/manual-amap-setup.md`
- Modify: `docs/SYSTEM_MANUAL.md`

- [ ] **Step 1: 创建 `docs/manual-amap-setup.md`**

照飞书开通手册的风格，写高德 API Key 获取说明：

```markdown
# 高德地图 API Key 获取手册

> 本文档从 SYSTEM_MANUAL.md 拆分而来，供主Agent通过浏览器帮用户获取高德地图 API Key。
> 主Agent拥有 browser-server MCP 工具，可直接操作网页。

## 获取步骤

### 步骤1：注册高德开放平台账号

主Agent用浏览器打开：

```
https://lbs.amap.com/
```

- 点击页面右上角「注册」按钮
- 用户用手机号注册账号（用户在屏幕上自行输入手机号和验证码）
- 注册完成后自动登录

### 步骤2：完成开发者认证

- 登录后进入控制台：`https://console.amap.com/`
- 如果提示需要开发者认证，按页面指引完成个人开发者认证
- 用户需输入姓名和身份证号（用户在屏幕上自行输入）

### 步骤3：创建应用并获取 Key

- 在控制台中点击「应用管理」→「我的应用」
- 点击右上角「创建新应用」按钮
  - 应用名称：妞妞 AI 助理（或用户指定的名字）
  - 应用类型：出行（或其他合适类型）
- 创建应用后，在应用下点击「添加 Key」
  - Key 名称：逆地理编码（或任意名称）
  - 服务平台：选择 **Web服务**
  - 其他选项保持默认
- 点击「提交」，系统生成 Key（格式如 `xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`）
- 主Agent复制此 Key

### 步骤4：写入配置

主Agent将 Key 写入 `~/.niu/preferences.json`：

```json
{
  "amap": {
    "api_key": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
  }
}
```

写入方式：读取现有 preferences.json → 合并 amap 段 → 原子写入（临时文件 + os.replace）。

写入后无需重启，下次照片入库时逆地理编码会自动使用高德 API。

**整个流程用户只需输入手机号/验证码/身份证号，其余全由主Agent完成。**

---

## 故障排查

### 问题：API 返回 INVALID_USER_KEY

- 检查 `~/.niu/preferences.json` 中 `amap.api_key` 是否正确复制（无多余空格）
- 检查 Key 的服务平台是否选择了 **Web服务**（不是 Web端(JS API)）

### 问题：API 返回 DAILY_QUERY_OVER_LIMIT

- 高德 Web服务免费额度为每天 5000 次
- 照片入库通常单张操作，不会超限
- 如需更多额度，可在高德控制台中升级配额

### 问题：坐标偏移（位置不准）

- 高德使用 GCJ-02 坐标系，EXIF 中是 WGS-84
- 系统已内置 WGS-84→GCJ-02 自动转换，无需手动处理
- 境外坐标不做偏移（境外直接使用 WGS-84 原值调用高德）

### 问题：境外照片无法获取地名

- 高德逆地理编码主要覆盖中国境内
- 境外照片可能返回空结果，此时照片描述中只显示 GPS 坐标（无地名）
```

- [ ] **Step 2: 在 `SYSTEM_MANUAL.md` 分册索引中添加链接**

在 `docs/SYSTEM_MANUAL.md` 的分册索引表中（约第 275 行飞书开通之后）添加一行：

```
| 高德开通 | [manual-amap-setup.md](manual-amap-setup.md) | 高德地图 API Key 获取流程、浏览器操作步骤、配置写入、故障排查 |
```

- [ ] **Step 3: Commit**

```bash
git add docs/manual-amap-setup.md docs/SYSTEM_MANUAL.md
git commit -m "docs: add Amap API Key setup guide and link in system manual"
```

---

## 自审记录

### Spec 覆盖检查

| 需求 | 对应 Task |
|------|----------|
| Nominatim 改为高德 API | Task 1 |
| WGS-84→GCJ-02 坐标转换 | Task 1 |
| API Key 从 preferences.json 读取 | Task 1 |
| Key 未配置时返回 sentinel 对象 | Task 1 |
| Key 未配置时不创建 location 实体 | Task 1 Step 5 |
| 旧 Nominatim 缓存自动清除 | Task 1 Step 3 |
| 坐标转换测试 | Task 2 |
| Key 未配置提示测试 | Task 2 |
| 高德 API 调用测试 | Task 2 |
| 高德 Key 获取说明文档 | Task 3 |
| 系统管理手册链接 | Task 3 |

### Placeholder 扫描

无 TBD/TODO/placeholder。

### 类型一致性

- `PREFS_PATH`: `Path` 对象 — 在 `_get_amap_api_key` 和测试中一致使用 `Path`
- `AMAP_KEY_NOT_CONFIGURED`: sentinel 对象 — `__bool__` 返回 False，`__str__` 返回 `AMAP_KEY_HINT`；`__init__.py` 用 `is` 判断，测试用 `is` + `in str()` 双重断言
- `_wgs84_to_gcj02` 返回 `tuple[float, float]` — `(gcj_lon, gcj_lat)` 格式，高德 API 参数为 `location=lon,lat` — 一致
- 测试中 `geocode.PREFS_PATH` 和 `geocode._cache_db_path` 临时替换后恢复 — 与飞书测试模式一致
- `urllib.request` 在模块顶部导入 — 测试 patch 路径为 `niu_photo_server.geocode.urllib.request.urlopen`
- `math` 在模块顶部导入 — 坐标转换函数不再内部 import
- `_CACHE_VERSION = 2` — 缓存版本号，Nominatim=1（隐含），Amap=2

### 注意事项

1. **高德免费额度**：每天 5000 次，照片入库单张操作不会超限
2. **境外照片**：高德主要覆盖中国境内，境外可能返回空结果，此时照片描述只有 GPS 坐标
3. **sentinel 对象设计**：`AMAP_KEY_NOT_CONFIGURED` 的 `__bool__` 返回 False，即使调用方忘了 `is` 检查，`if geocode_result:` 也不会把 sentinel 当真地名。`is` 判断是编译期保障，`__bool__` 是运行期兜底
4. **缓存版本检测**：`_ensure_cache_db` 检查 `cache_meta` 表中的版本号，版本不匹配时 DROP + 重建，确保旧 Nominatim 缓存不会污染新高德结果
5. **API Key 安全**：URL 中包含 api_key，代码中加了注释提醒不要将 URL 写入日志

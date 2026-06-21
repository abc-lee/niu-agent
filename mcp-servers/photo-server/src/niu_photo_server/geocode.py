"""逆地理编码模块：GPS 坐标 → 人可读地名，带本地缓存"""

import sqlite3
from pathlib import Path
from loguru import logger

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


def _round_coord(value: float, decimals: int = 2) -> str:
    """将坐标四舍五入到指定小数位（用于缓存键，约 1km 精度）"""
    return f"{value:.{decimals}f}"


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
    """WGS-84 坐标转 GCJ-02（高德坐标）

    Returns:
        (gcj_lat, gcj_lon) — 与参数顺序一致
    """
    if _out_of_china(lat, lon):
        return lat, lon
    d_lat = _transform_lat(lon - 105.0, lat - 35.0)
    d_lon = _transform_lon(lon - 105.0, lat - 35.0)
    rad_lat = lat / 180.0 * _PI
    magic = 1 - _EE * math.sin(rad_lat) ** 2
    sqrt_magic = magic ** 0.5
    d_lat = (d_lat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrt_magic) * _PI)
    d_lon = (d_lon * 180.0) / (_A / sqrt_magic * math.cos(rad_lat) * _PI)
    return lat + d_lat, lon + d_lon


def reverse_geocode(lat: float, lon: float) -> str | _AmapKeyNotConfigured | None:
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

    # 2. 检查高德 API Key
    api_key = _get_amap_api_key()
    if not api_key:
        logger.warning(f"[Geocode] {AMAP_KEY_HINT}")
        return AMAP_KEY_NOT_CONFIGURED

    # 3. 调用高德逆地理编码 API
    gcj_lat, gcj_lon = _wgs84_to_gcj02(lat, lon)
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
        for key in ["province", "city", "district", "township"]:
            val = addr.get(key, "")
            if val and val not in parts:
                # city 可能和 province 相同（直辖市）
                if key == "city" and val == addr.get("province", ""):
                    continue
                parts.append(val)
        # neighborhood 返回 dict 或空列表，需提取 name
        neighborhood = addr.get("neighborhood", "")
        if isinstance(neighborhood, dict) and neighborhood.get("name"):
            parts.append(neighborhood["name"])

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
        # 不输出原始异常消息，可能包含 URL（含 API Key）
        logger.warning(f"[Geocode] Amap API failed for {lat},{lon}: {type(e).__name__}")
        return None

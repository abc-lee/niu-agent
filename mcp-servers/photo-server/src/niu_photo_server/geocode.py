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

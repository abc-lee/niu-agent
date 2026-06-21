"""逆地理编码模块测试"""
import pytest
import os
import sqlite3
import tempfile
import json
from pathlib import Path
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
                "township": "平山镇",
                "neighborhood": {"name": "中山广场", "type": "商业服务;商场"},
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
            assert result is not None and not isinstance(result, type(geocode.AMAP_KEY_NOT_CONFIGURED))
            assert "河北省石家庄市平山县" in str(result)
        finally:
            geocode._cache_db_path = None
            geocode.PREFS_PATH = Path.home() / ".niu" / "preferences.json"


def test_wgs84_to_gcj02_in_china():
    """中国境内坐标需要偏移"""
    from niu_photo_server.geocode import _wgs84_to_gcj02
    # 北京天安门 WGS-84 坐标
    gcj_lat, gcj_lon = _wgs84_to_gcj02(39.9087, 116.3975)
    # GCJ-02 坐标应该与 WGS-84 不同（有偏移）
    assert gcj_lon != 116.3975 or gcj_lat != 39.9087


def test_wgs84_to_gcj02_outside_china():
    """境外坐标不需要偏移"""
    from niu_photo_server.geocode import _wgs84_to_gcj02
    # 纽约 WGS-84 坐标
    gcj_lat, gcj_lon = _wgs84_to_gcj02(40.7128, -74.0060)
    # 境外坐标不变
    assert gcj_lat == pytest.approx(40.7128, abs=1e-6)
    assert gcj_lon == pytest.approx(-74.0060, abs=1e-6)


def test_reverse_geocode_no_api_key():
    """API Key 未配置时返回提示文字"""
    from niu_photo_server import geocode

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


def test_reverse_geocode_municipality():
    """直辖市 city 字段为空数组 []"""
    from niu_photo_server import geocode

    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({
        "status": "1",
        "regeocode": {
            "addressComponent": {
                "province": "北京市",
                "city": [],
                "district": "东城区",
                "township": "东华门街道",
                "neighborhood": {"name": "天安门广场", "type": "风景名胜"},
            }
        }
    }).encode("utf-8")
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

    with tempfile.TemporaryDirectory() as tmpdir:
        cache_path = os.path.join(tmpdir, "geocode_cache.db")
        tmp_prefs = os.path.join(tmpdir, "preferences.json")
        with open(tmp_prefs, "w") as f:
            json.dump({"amap": {"api_key": "test_key"}}, f)

        geocode._cache_db_path = cache_path
        geocode.PREFS_PATH = Path(tmp_prefs)
        try:
            with patch("niu_photo_server.geocode.urllib.request.urlopen", return_value=mock_response):
                result = geocode.reverse_geocode(39.908, 116.397)
            assert result is not None and not isinstance(result, type(geocode.AMAP_KEY_NOT_CONFIGURED))
            assert "北京市东城区东华门街道天安门广场" in str(result)
        finally:
            geocode._cache_db_path = None
            geocode.PREFS_PATH = Path.home() / ".niu" / "preferences.json"


def test_reverse_geocode_empty_arrays():
    """township 和 neighborhood 为空数组 []"""
    from niu_photo_server import geocode

    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({
        "status": "1",
        "regeocode": {
            "addressComponent": {
                "province": "广东省",
                "city": "深圳市",
                "district": "南山区",
                "township": [],
                "neighborhood": [],
            }
        }
    }).encode("utf-8")
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

    with tempfile.TemporaryDirectory() as tmpdir:
        cache_path = os.path.join(tmpdir, "geocode_cache.db")
        tmp_prefs = os.path.join(tmpdir, "preferences.json")
        with open(tmp_prefs, "w") as f:
            json.dump({"amap": {"api_key": "test_key"}}, f)

        geocode._cache_db_path = cache_path
        geocode.PREFS_PATH = Path(tmp_prefs)
        try:
            with patch("niu_photo_server.geocode.urllib.request.urlopen", return_value=mock_response):
                result = geocode.reverse_geocode(22.534, 113.932)
            assert result is not None and not isinstance(result, type(geocode.AMAP_KEY_NOT_CONFIGURED))
            assert "广东省深圳市南山区" in str(result)
        finally:
            geocode._cache_db_path = None
            geocode.PREFS_PATH = Path.home() / ".niu" / "preferences.json"

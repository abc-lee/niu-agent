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

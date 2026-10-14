"""`media_ingest.enabled` 开关**唯一**读取点（方案 D-2/D-3；spec §5.4），同时是 `max_pixels`/`downscale_long_side` 的唯一读取点。

语义：「入库时是否为媒体（图片）计算并保存向量」的**默认值**；ingest 的
`None` 参数跟随本开关，`True`/`False` 参数本次强制。

读取遵守既有配方（`agent/image_channel.py::_vision_section` 为范例）：
CONFIG_PATH **函数级 import**（保持 `monkeypatch("niu_api.config.CONFIG_PATH")`
注入点生效）、**每次现读**（改配置免重启热生效）、坏 JSON / 类型错**不抛**
（回默认 `False`）。
"""
from __future__ import annotations

import json
from pathlib import Path


def media_ingest_enabled() -> bool:
    """现读 user-config.json 的 `media_ingest.enabled`（缺省 `False`）。

    无段 / 无键 / 文件缺失 / JSON 坏 / 段或键类型不对 → `False`，绝不抛。
    只有显式的布尔 `true` 为真（`"yes"`、`1` 等类型错一律按关处理）。
    """
    from niu_api.config import CONFIG_PATH

    try:
        data = json.loads(Path(CONFIG_PATH).read_text(encoding="utf-8"))
        sec = data.get("media_ingest") if isinstance(data, dict) else None
        if not isinstance(sec, dict):
            return False
        return sec.get("enabled") is True
    except Exception:  # noqa: BLE001 —— 坏输入回默认，开关读取不得炸调用方
        return False


#: 直送门槛默认（W×H 像素数）。保守侧取值 = 改动前像素护栏的初始值：≤ 此值的图走
#: 原图直送路径（与现状逐字节一致），> 此值的图从「拒收无向量」变为「缩放后出向量」
#: ——默认形态下不存在比现状更差/更贵的行为（D 节口径）。
DEFAULT_MAX_PIXELS = 8_000_000
#: 缩放目标长边默认（像素）。保守侧取小长边：让服务端编码窗口在任何纯 CPU 机器上都短，
#: 单张代价低；用户机器若足够快，按手册标定流程与用户商量后调大（免重启生效）。
DEFAULT_DOWNSCALE_LONG_SIDE = 1024


def media_ingest_max_pixels() -> int:
    """现读 user-config.json 的 `media_ingest.max_pixels`（缺省 `DEFAULT_MAX_PIXELS`）。

    无段 / 无键 / 文件缺失 / JSON 坏 / 类型错（含布尔、小数、非正整数）→ 默认，绝不抛。
    每次调用现读：改配置免重启生效（下一队列行即按新门槛判定，D 节口径）。
    """
    from niu_api.config import CONFIG_PATH

    try:
        data = json.loads(Path(CONFIG_PATH).read_text(encoding="utf-8"))
        sec = data.get("media_ingest") if isinstance(data, dict) else None
        if isinstance(sec, dict):
            v = sec.get("max_pixels")
            if isinstance(v, int) and not isinstance(v, bool) and v > 0:
                return v
    except Exception:  # noqa: BLE001 —— 坏输入回默认，门槛读取不得炸调用方
        pass
    return DEFAULT_MAX_PIXELS


def media_ingest_downscale_long_side() -> int:
    """现读 user-config.json 的 `media_ingest.downscale_long_side`（缺省 `DEFAULT_DOWNSCALE_LONG_SIDE`）。

    兼底契约与 `media_ingest_max_pixels` 逐字同型（无段/无键/缺失/坏 JSON/类型错 → 默认，绝不抛）；
    每次调用现读：改配置免重启生效。
    """
    from niu_api.config import CONFIG_PATH

    try:
        data = json.loads(Path(CONFIG_PATH).read_text(encoding="utf-8"))
        sec = data.get("media_ingest") if isinstance(data, dict) else None
        if isinstance(sec, dict):
            v = sec.get("downscale_long_side")
            if isinstance(v, int) and not isinstance(v, bool) and v > 0:
                return v
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT_DOWNSCALE_LONG_SIDE

"""`media_ingest.enabled` 开关的**唯一**读取点（方案 D-2/D-3；spec §5.4）。

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

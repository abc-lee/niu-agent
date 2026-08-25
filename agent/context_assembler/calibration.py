"""token 校准倍率（spec §3.1 / D9）——桥接本地估算与服务端真值。

本地 TokenCalculator 计数与服务端 prompt_tokens 之间存在系统性偏差
（tokenizer 差异、中英文比例漂移）。倍率 = 服务端真值 ÷ 同消息集本地估算，
每次响应回来覆盖更新；发送前的窗口裁剪与预算判定全部用「估算 × 倍率」。

状态：进程内单例缓存 + 持久化 ~/.niu/token_calibration.json（原子写 +
flock 单 helper 纪律——复用 blocks._flock，不另造锁实现）。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

from loguru import logger

from agent.context_assembler.blocks import _flock, _funlock

DEFAULT_RATIO = 1.15  # 首轮回退安全系数（spec §3.1）

# 倍率合理区间：越界视为异常样本（消息集错位/计数失败），拒绝采纳
MIN_SANE_RATIO = 0.2
MAX_SANE_RATIO = 10.0

CALIBRATION_FILE = "token_calibration.json"

_lock = threading.Lock()
_cached_ratio: float | None = None  # 进程内缓存；None = 未加载


def default_file_path() -> Path:
    """默认持久化路径 ~/.niu/token_calibration.json。"""
    return Path.home() / ".niu" / CALIBRATION_FILE


def _load(path: Path | None = None) -> float:
    """从磁盘读倍率；文件缺失/损坏/越界时返回默认值。"""
    p = path or default_file_path()
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        ratio = float(data.get("ratio", DEFAULT_RATIO))
        if MIN_SANE_RATIO <= ratio <= MAX_SANE_RATIO:
            return ratio
        logger.warning(f"[Calibration] Invalid persisted ratio {ratio}, using default {DEFAULT_RATIO}")
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"[Calibration] Failed to load {p}: {e}, using default {DEFAULT_RATIO}")
    return DEFAULT_RATIO


def _save(ratio: float, path: Path | None = None) -> None:
    """原子写倍率文件（tmp + os.replace），写操作经 flock 单 helper 纪律。"""
    p = path or default_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lock_path = p.with_suffix(p.suffix + ".lock")
    tmp_path = p.with_suffix(p.suffix + ".tmp")
    try:
        with open(lock_path, "w") as lock_f:
            _flock(lock_f)
            try:
                payload = {"ratio": ratio, "updated_at": datetime.now().isoformat()}
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, p)  # 原子替换
            finally:
                _funlock(lock_f)
    except Exception as e:
        logger.warning(f"[Calibration] Failed to persist ratio to {p}: {e}")


def get_ratio(path: Path | None = None) -> float:
    """当前校准倍率（懒加载磁盘，进程内缓存）。"""
    global _cached_ratio
    if _cached_ratio is None:
        with _lock:
            if _cached_ratio is None:
                _cached_ratio = _load(path)
    return _cached_ratio


def estimate(n_local: int | float, path: Path | None = None) -> float:
    """本地估算 → 校准后估算：n_local × ratio。"""
    return n_local * get_ratio(path)


def update_ratio(truth_tokens: int | float, local_estimate: int | float,
                 path: Path | None = None) -> float | None:
    """以一次真实响应更新倍率：ratio = truth ÷ local（同消息集对齐）。

    防零/异常防护：任一入参非正、结果越界 [MIN_SANE_RATIO, MAX_SANE_RATIO]
    时拒绝采纳并保留旧值（返回 None）。每次响应回来覆盖更新（D9）。
    """
    global _cached_ratio
    try:
        truth = float(truth_tokens)
        local = float(local_estimate)
    except (TypeError, ValueError):
        logger.warning(f"[Calibration] Bad inputs: truth={truth_tokens!r}, local={local_estimate!r}")
        return None
    if truth <= 0 or local <= 0 or local != local or truth != truth:  # 含 NaN 防护
        return None
    ratio = truth / local
    if not (MIN_SANE_RATIO <= ratio <= MAX_SANE_RATIO):
        logger.warning(
            f"[Calibration] Ratio out of sane range, rejected: "
            f"truth={truth:.0f} local={local:.0f} -> {ratio:.3f}"
        )
        return None
    with _lock:
        _cached_ratio = ratio
        _save(ratio, path)
    return ratio


def reset(path: Path | None = None) -> None:
    """复位为安全默认（/new 清理面挂点，Task 8 接线）。"""
    global _cached_ratio
    with _lock:
        _cached_ratio = DEFAULT_RATIO
        try:
            p = path or default_file_path()
            if p.exists():
                p.unlink()
        except Exception as e:
            logger.warning(f"[Calibration] Failed to remove calibration file: {e}")

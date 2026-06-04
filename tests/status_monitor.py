#!/usr/bin/env python3
"""
LightRAG 入库状态机 — 独立测试

作为 API 端点挂载到 FastAPI，每 3 秒轮询一次 _shared_dicts，
将 pipeline_status 和 doc_status 的原始数据 + 计算出的百分比写入日志。

用法：
  1. 在 niu_api/main.py 中 import 并注册此路由（或单独启动）
  2. 触发入库（单文件或目录）
  3. 查看 /api/kg/test_status_monitor 获取当前状态
  4. 日志写入 tests/status_monitor.log

测试通过标准：
  - 目录入库时 ingesting=True
  - 进度百分比只增不减
  - 入库完成后 progress=100%, ingesting=False
"""

import os
import time
from datetime import datetime
from fastapi import APIRouter

router = APIRouter(prefix="/api/kg", tags=["status-monitor"])

LOG_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tests", "status_monitor.log")

# 单调递增进度
_max_progress = 0


def _read_shared_dicts():
    """直接从 _shared_dicts 读取 pipeline_status 和 doc_status"""
    from lightrag.kg.shared_storage import _shared_dicts, get_final_namespace
    from niu_api.internal.lightrag_manager import get_lightrag

    rag = get_lightrag()
    if rag is None:
        return None, None

    workspace = rag.workspace

    ps_key = get_final_namespace("pipeline_status", workspace)
    ps = _shared_dicts.get(ps_key, {})

    ds_key = get_final_namespace("doc_status", workspace)
    ds = _shared_dicts.get(ds_key, {})

    return ps, ds


def _calc_progress(ps, ds):
    """计算入库进度"""
    global _max_progress

    busy = ps.get("busy", False) if ps else False
    cur_batch = ps.get("cur_batch", 0) if ps else 0
    batchs = ps.get("batchs", 0) if ps else 0
    latest_message = ps.get("latest_message", "") if ps else ""

    # 统计 doc_status
    pending = 0
    processing = 0
    completed = 0
    total = 0

    if ds:
        for v in ds.values():
            if isinstance(v, dict):
                st = v.get("status", "")
            else:
                st = getattr(v, "status", "")
            st = str(st) if st else ""
            total += 1
            if st in ("pending", "processing"):
                if st == "pending":
                    pending += 1
                else:
                    processing += 1
            elif st in ("completed", "processed", "preprocessed", "failed"):
                completed += 1

    # 判断是否正在入库
    ingesting = busy or processing > 0 or pending > 0

    # 计算进度
    if total == 0 and not busy:
        progress = 0
    elif total > 0 and completed == total:
        progress = 100
    else:
        doc_pct = (completed / total * 100) if total > 0 else 0
        batch_pct = (cur_batch / batchs * 100) if batchs > 0 else 0
        progress = max(doc_pct, batch_pct)
        if progress >= 99:
            progress = 99

    # 只增不减
    if progress > _max_progress:
        _max_progress = progress
    display_progress = _max_progress

    # 入库完成时重置
    if not ingesting and total > 0 and completed == total:
        _max_progress = 0

    return {
        "ingesting": ingesting,
        "progress": display_progress,
        "busy": busy,
        "cur_batch": cur_batch,
        "batchs": batchs,
        "latest_message": latest_message,
        "doc_total": total,
        "doc_pending": pending,
        "doc_processing": processing,
        "doc_completed": completed,
    }


def _log_status(result):
    """写入日志"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = (
        f"{ts} | ingesting={result['ingesting']} | progress={result['progress']}% | "
        f"busy={result['busy']} | batch={result['cur_batch']}/{result['batchs']} | "
        f"docs={result['doc_total']} (P:{result['doc_pending']} R:{result['doc_processing']} D:{result['doc_completed']}) | "
        f"msg={result['latest_message'][:60]}\n"
    )
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line)
    except Exception:
        pass


@router.get("/test_status_monitor")
def get_status():
    """测试端点：直接读 _shared_dicts 返回入库状态"""
    try:
        ps, ds = _read_shared_dicts()
        if ps is None:
            return {"error": "LightRAG not initialized", "ingesting": False, "progress": 0}
        result = _calc_progress(ps, ds)
        _log_status(result)
        return result
    except Exception as e:
        return {"error": str(e), "ingesting": False, "progress": 0}


@router.get("/test_status_raw")
def get_raw():
    """调试端点：返回 _shared_dicts 原始数据"""
    try:
        ps, ds = _read_shared_dicts()
        if ps is None:
            return {"error": "LightRAG not initialized"}

        # 将 doc_status 中的对象转为可序列化格式
        doc_list = {}
        if ds:
            for k, v in ds.items():
                if isinstance(v, dict):
                    doc_list[k] = v
                else:
                    doc_list[k] = {
                        "status": str(getattr(v, "status", "")),
                        "content_summary": str(getattr(v, "content_summary", "")),
                    }

        return {
            "pipeline_status": dict(ps) if ps else {},
            "doc_status": doc_list,
            "doc_count": len(ds) if ds else 0,
        }
    except Exception as e:
        return {"error": str(e)}

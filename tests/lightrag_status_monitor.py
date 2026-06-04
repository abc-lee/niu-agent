#!/usr/bin/env python3
"""
LightRAG 入库状态机 — 独立程序

直接读取 LightRAG 的 _shared_dicts，不依赖我们的任何 API 调用链路。
每 3 秒轮询一次，将入库状态和百分比写入日志。

使用方式：
  1. 启动: python tests/lightrag_status_monitor.py
  2. 在 UI 中触发入库（单文件或目录）
  3. 观察日志输出
  4. Ctrl+C 停止
"""

import os
import sys
import time
from datetime import datetime

LOG_FILE = "tests/lightrag_status_monitor.log"
POLL_INTERVAL = 3  # seconds


def get_workspace():
    return os.environ.get("WORKSPACE_PATH", os.path.expanduser("~/.niu/lightrag"))


def read_pipeline_status():
    """直接从 _shared_dicts 读取 pipeline_status"""
    from lightrag.kg.shared_storage import _shared_dicts, get_final_namespace

    workspace = get_workspace()
    key = get_final_namespace("pipeline_status", workspace)
    return _shared_dicts.get(key, {})


def read_doc_status():
    """直接从 _shared_dicts 读取 doc_status"""
    from lightrag.kg.shared_storage import _shared_dicts, get_final_namespace

    workspace = get_workspace()
    key = get_final_namespace("doc_status", workspace)
    return _shared_dicts.get(key, {})


def calc_progress(pipeline_status, doc_status):
    """
    计算入库进度百分比。

    优先使用 pipeline_status 的 batch 粒度，
    结合 doc_status 中各文档的状态做补充判断。
    进度只增不减。
    """
    busy = pipeline_status.get("busy", False)
    cur_batch = pipeline_status.get("cur_batch", 0)
    batchs = pipeline_status.get("batchs", 0)
    latest_message = pipeline_status.get("latest_message", "")

    # 从 doc_status 统计文档状态
    total_docs = 0
    completed_docs = 0
    processing_docs = 0
    pending_docs = 0
    failed_docs = 0

    for doc_id, status_obj in doc_status.items():
        total_docs += 1
        # status_obj 可能是 DocProcessingStatus 对象或 dict
        if hasattr(status_obj, "status"):
            st = status_obj.status
            # DocStatus 枚举值
            if hasattr(st, "name"):
                st = st.name
            else:
                st = str(st)
        elif isinstance(status_obj, dict):
            st = str(status_obj.get("status", ""))
        else:
            continue

        if st in ("COMPLETED", "completed"):
            completed_docs += 1
        elif st in ("PROCESSING", "processing"):
            processing_docs += 1
        elif st in ("PENDING", "pending"):
            pending_docs += 1
        elif st in ("FAILED", "failed"):
            failed_docs += 1

    # 计算百分比
    if total_docs == 0:
        # 没有任何文档记录，说明没在入库
        return 0, False, "idle"

    # 基于 doc_status 的完成度
    doc_progress = (completed_docs / total_docs) * 100 if total_docs > 0 else 0

    # 基于 pipeline_status 的 batch 进度
    batch_progress = 0
    if batchs > 0 and cur_batch > 0:
        batch_progress = (cur_batch / batchs) * 100

    # 综合：取两者中较大的值，但不超过 99%（完成才显示 100%）
    progress = max(doc_progress, batch_progress)
    if progress >= 99:
        progress = 99
    if completed_docs == total_docs and total_docs > 0:
        progress = 100

    # 判断是否正在入库
    is_ingesting = busy or processing_docs > 0 or pending_docs > 0

    # 状态描述
    if is_ingesting:
        if busy and latest_message:
            msg = latest_message
        elif processing_docs > 0:
            msg = f"processing {processing_docs}/{total_docs}"
        else:
            msg = f"pending {pending_docs}/{total_docs}"
    elif completed_docs == total_docs and total_docs > 0:
        msg = "completed"
    elif failed_docs > 0 and completed_docs + failed_docs == total_docs:
        msg = f"done ({failed_docs} failed)"
    else:
        msg = "idle"

    return progress, is_ingesting, msg


def main():
    max_progress = 0

    with open(LOG_FILE, "w") as f:
        f.write(f"=== LightRAG Status Monitor started {datetime.now()} ===\n")
        f.write(f"Polling every {POLL_INTERVAL}s\n")
        f.write(f"Workspace: {get_workspace()}\n\n")

        try:
            while True:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                try:
                    pipeline_status = read_pipeline_status()
                    doc_status = read_doc_status()
                    progress, is_ingesting, msg = calc_progress(pipeline_status, doc_status)

                    # 只增不减
                    if progress > max_progress:
                        max_progress = progress
                    display_progress = max_progress

                    busy = pipeline_status.get("busy", False)
                    cur_batch = pipeline_status.get("cur_batch", 0)
                    batchs = pipeline_status.get("batchs", 0)
                    latest_message = pipeline_status.get("latest_message", "")

                    total_docs = len(doc_status)

                    line = (
                        f"{ts} | ingesting={is_ingesting} | progress={display_progress}% | "
                        f"busy={busy} | batch={cur_batch}/{batchs} | "
                        f"docs={total_docs} | msg={msg}\n"
                    )

                except Exception as e:
                    line = f"{ts} | ERROR | {type(e).__name__}: {e}\n"
                    display_progress = 0
                    is_ingesting = False

                f.write(line)
                f.flush()
                print(line.strip())
                time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            summary = f"\n=== Monitor stopped {datetime.now()} ===\n"
            summary += f"Max progress reached: {max_progress}%\n"
            f.write(summary)
            print(summary)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
触发目录入库 — 直接调用 LightRAG 的入库方法
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from niu_api.internal.lightrag_manager import get_lightrag, call_async, fire_and_forget


def ingest_directory(dir_path):
    rag = get_lightrag()
    if rag is None:
        print("LightRAG not initialized")
        return

    files = sorted(f for f in os.listdir(dir_path) if os.path.isfile(os.path.join(dir_path, f)))
    print(f"Found {len(files)} files in {dir_path}")

    for i, fname in enumerate(files):
        fpath = os.path.join(dir_path, fname)
        with open(fpath, "r") as f:
            content = f.read()

        print(f"[{i+1}/{len(files)}] Enqueuing {fname}...")
        call_async(rag.apipeline_enqueue_documents(content, file_paths=fpath), timeout=60)

    print("All files enqueued, firing pipeline...")
    fire_and_forget(rag.apipeline_process_enqueue_documents(), context="test-directory-ingest")
    print("Pipeline started!")


if __name__ == "__main__":
    dir_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/niu_test_ingest2"
    ingest_directory(dir_path)

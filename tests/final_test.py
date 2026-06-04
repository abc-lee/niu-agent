#!/usr/bin/env python3
"""最终测试：触发目录入库并同时监控 pipeline_status 端点"""
import json
import time
import urllib.request
from datetime import datetime

PIPELINE_URL = "http://127.0.0.1:9876/api/kg/pipeline_status"
MONITOR_URL = "http://127.0.0.1:9876/api/kg/test_status_monitor"
INGEST_URL = "http://127.0.0.1:9876/api/kg/test_ingest?dir_path=/tmp/niu_final_test"
LOG_FILE = "tests/final_test.log"


def fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def main():
    with open(LOG_FILE, "w") as f:
        f.write(f"=== Final Test {datetime.now()} ===\n\n")

        # 先触发入库
        print("Triggering ingestion...")
        try:
            req = urllib.request.Request(INGEST_URL, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                print(f"Ingestion started: {result}")
                f.write(f"Trigger: {result}\n\n")
        except Exception as e:
            print(f"Trigger failed: {e}")
            f.write(f"Trigger failed: {e}\n")
            return

        # 立即开始高频轮询
        max_progress = 0
        regressions = 0
        poll_count = 0
        busy_count = 0

        while True:
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            pipeline = fetch(PIPELINE_URL)
            monitor = fetch(MONITOR_URL)
            poll_count += 1

            p_busy = pipeline.get("busy", False) if "error" not in pipeline else "?"
            p_progress = pipeline.get("progress", 0) if "error" not in pipeline else "?"

            m_ing = monitor.get("ingesting", False) if "error" not in monitor else "?"
            m_progress = monitor.get("progress", 0) if "error" not in monitor else "?"

            if p_busy == True:
                busy_count += 1

            # 只增不减检查
            reg = ""
            if isinstance(p_progress, (int, float)) and p_progress < max_progress and max_progress > 0 and p_busy:
                reg = f" REGRESS!{max_progress}->{p_progress}"
                regressions += 1
            if isinstance(p_progress, (int, float)) and p_progress > max_progress:
                max_progress = p_progress

            line = f"{ts} | pipeline:busy={p_busy} prg={p_progress}% | monitor:ing={m_ing} prg={m_progress}%{reg}\n"
            f.write(line)
            f.flush()
            print(line.strip())

            # 入库完成检测
            if m_ing == False and poll_count > 5:
                # 多轮确认
                time.sleep(1)
                m2 = fetch(MONITOR_URL)
                if m2.get("ingesting") == False:
                    print("\n=== INGESTION COMPLETE ===")
                    break

            time.sleep(1)

        summary = (
            f"\n=== Summary ===\n"
            f"Total polls: {poll_count}\n"
            f"busy=True polls: {busy_count}\n"
            f"Max progress: {max_progress}%\n"
            f"Regressions: {regressions}\n"
            f"PASS: {regressions == 0 and busy_count > 0}\n"
        )
        f.write(summary)
        print(summary)


if __name__ == "__main__":
    main()

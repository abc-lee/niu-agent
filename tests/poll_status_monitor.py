#!/usr/bin/env python3
"""
自动轮询测试脚本 — 每 3 秒读一次 test_status_monitor 端点，
将结果写入日志，供审查 Agent 验证。

用法：
  1. 确保 niu 程序正在运行
  2. python tests/poll_status_monitor.py
  3. 在 UI 中触发入库
  4. 观察日志输出
  5. Ctrl+C 停止
"""

import json
import time
import urllib.request
from datetime import datetime

MONITOR_URL = "http://127.0.0.1:9876/api/kg/test_status_monitor"
RAW_URL = "http://127.0.0.1:9876/api/kg/test_status_raw"
PIPELINE_URL = "http://127.0.0.1:9876/api/kg/pipeline_status"
LOG_FILE = "tests/poll_status_monitor.log"
POLL_INTERVAL = 3


def fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def main():
    with open(LOG_FILE, "w") as f:
        f.write(f"=== Status Monitor Poll started {datetime.now()} ===\n")
        f.write(f"Polling every {POLL_INTERVAL}s\n\n")

        max_progress = 0
        regressions = 0

        try:
            while True:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                # 读新端点
                monitor = fetch(MONITOR_URL)
                # 同时读旧端点做对比
                pipeline = fetch(PIPELINE_URL)

                if "error" in monitor:
                    line = f"{ts} | MONITOR ERROR | {monitor['error']}\n"
                else:
                    ingesting = monitor.get("ingesting", False)
                    progress = monitor.get("progress", 0)

                    # 检查进度回退
                    regression = ""
                    if ingesting and progress < max_progress and max_progress > 0:
                        regression = f" ⚠️ REGRESSION: {max_progress}% → {progress}%"
                        regressions += 1
                    if progress > max_progress:
                        max_progress = progress

                    line = (
                        f"{ts} | monitor: ingesting={ingesting} progress={progress}% "
                        f"busy={monitor.get('busy')} batch={monitor.get('cur_batch')}/{monitor.get('batchs')} "
                        f"docs={monitor.get('doc_total')}(P:{monitor.get('doc_pending')} R:{monitor.get('doc_processing')} D:{monitor.get('doc_completed')}) "
                        f"msg={str(monitor.get('latest_message',''))[:50]}"
                    )

                    # 旧端点对比
                    if "error" not in pipeline:
                        old_busy = pipeline.get("busy", False)
                        old_progress = pipeline.get("progress", 0)
                        line += f" | old_api: busy={old_busy} progress={old_progress}%"

                    line += regression + "\n"

                f.write(line)
                f.flush()
                print(line.strip())
                time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            summary = (
                f"\n=== Poll stopped {datetime.now()} ===\n"
                f"Max progress: {max_progress}%\n"
                f"Regressions: {regressions}\n"
            )
            f.write(summary)
            print(summary)


if __name__ == "__main__":
    main()

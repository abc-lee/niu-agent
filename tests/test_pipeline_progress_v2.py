#!/usr/bin/env python3
"""Test pipeline_status endpoint during directory ingestion.

Polls /api/kg/pipeline_status every 0.5s and logs results to
test_pipeline_progress_v2.log. Designed to capture both single-file
and directory ingestion scenarios.

Usage:
  1. Start this script: python tests/test_pipeline_progress_v2.py
  2. Trigger ingestion (single file or directory) from the UI
  3. Wait for ingestion to complete
  4. Kill this script (Ctrl+C)
  5. Review the log file
"""

import json
import time
import urllib.request
from datetime import datetime

API_URL = "http://127.0.0.1:9876/api/kg/pipeline_status"
LOG_FILE = "tests/test_pipeline_progress_v2.log"
POLL_INTERVAL = 0.5  # seconds

def poll():
    try:
        with urllib.request.urlopen(API_URL, timeout=3) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"busy": None, "progress": None, "error": str(e)}

def main():
    with open(LOG_FILE, "w") as f:
        f.write(f"=== pipeline_status v2 poll log started {datetime.now()} ===\n")
        f.write(f"Polling {API_URL} every {POLL_INTERVAL}s\n\n")

        busy_count = 0
        not_busy_count = 0
        error_count = 0
        max_progress = 0
        progress_history = []

        try:
            while True:
                data = poll()
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

                if data.get("error"):
                    error_count += 1
                    line = f"{ts} | ERROR | {data['error']}\n"
                else:
                    busy = data.get("busy", False)
                    progress = data.get("progress", 0)
                    cur_batch = data.get("cur_batch", 0)
                    batchs = data.get("batchs", 0)
                    msg = data.get("message", "")

                    if busy:
                        busy_count += 1
                    else:
                        not_busy_count += 1

                    if progress > max_progress:
                        max_progress = progress

                    progress_history.append(progress)

                    # Check for progress regression (should only increase)
                    regression = ""
                    if len(progress_history) >= 2 and busy:
                        prev = progress_history[-2]
                        curr = progress_history[-1]
                        if curr < prev and prev > 0:
                            regression = f" ⚠️ REGRESSION: {prev}% → {curr}%"

                    line = f"{ts} | busy={busy} | progress={progress}% | cur_batch={cur_batch} | batchs={batchs} | msg={msg[:80]}{regression}\n"

                f.write(line)
                f.flush()
                time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            summary = (
                f"\n=== Summary ===\n"
                f"Total polls: {busy_count + not_busy_count + error_count}\n"
                f"busy=True: {busy_count}\n"
                f"busy=False: {not_busy_count}\n"
                f"Errors: {error_count}\n"
                f"Max progress: {max_progress}%\n"
                f"Progress regressions: check ⚠️ markers above\n"
            )
            f.write(summary)
            print(summary)

if __name__ == "__main__":
    main()

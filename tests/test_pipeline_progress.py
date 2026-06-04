#!/usr/bin/env python3
"""
Poll kg_api pipeline_status endpoint every 0.5s and log progress.

Usage:
    python test_pipeline_progress.py

Stop with Ctrl+C or it auto-stops 5s after busy transitions from True to False.
"""

import json
import time
import urllib.request
from datetime import datetime

URL = "http://127.0.0.1:9876/api/kg/pipeline_status"
POLL_INTERVAL = 0.5
IDLE_STOP_SECONDS = 5  # stop after this many seconds of busy=False following busy=True

LOG_PATH = "REDACTED_USER_PATH/tools/ai-bot/tests/test_pipeline_progress.log"


def poll():
    """Return parsed JSON from pipeline_status, or None on error."""
    try:
        req = urllib.request.Request(URL, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"busy": False, "progress": 0, "cur_batch": 0, "batchs": 0,
                "message": f"POLL_ERROR: {e}"}


def main():
    records = []
    saw_busy = False
    idle_since = None
    prev_progress = None
    progress_decreased = False

    print(f"Polling {URL} every {POLL_INTERVAL}s ...")
    print(f"Log file: {LOG_PATH}")
    print("Press Ctrl+C to stop.\n")

    with open(LOG_PATH, "w", encoding="utf-8") as log:
        log.write(f"=== pipeline_status poll log started {datetime.now()} ===\n")

        try:
            while True:
                data = poll()
                busy = bool(data.get("busy", False))
                progress = int(data.get("progress", 0))
                cur_batch = int(data.get("cur_batch", 0))
                batchs = int(data.get("batchs", 0))
                message = str(data.get("message", ""))
                job_name = str(data.get("job_name", ""))

                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                line = (f"{ts} | busy={busy} | progress={progress}% "
                        f"| cur_batch={cur_batch} | batchs={batchs} "
                        f"| message={message}")
                log.write(line + "\n")
                log.flush()
                print(line)

                # track progress regression
                if prev_progress is not None and progress < prev_progress and busy:
                    progress_decreased = True
                prev_progress = progress

                records.append({
                    "busy": busy,
                    "progress": progress,
                    "cur_batch": cur_batch,
                    "batchs": batchs,
                })

                # auto-stop logic
                if busy:
                    saw_busy = True
                    idle_since = None
                elif saw_busy and not busy:
                    if idle_since is None:
                        idle_since = time.monotonic()
                    elif time.monotonic() - idle_since >= IDLE_STOP_SECONDS:
                        print(f"\nPipeline idle for {IDLE_STOP_SECONDS}s after busy phase. Auto-stopping.")
                        break

                time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            print("\nStopped by user.")

        # --- statistics ---
        total = len(records)
        busy_count = sum(1 for r in records if r["busy"])
        idle_count = total - busy_count
        max_progress = max((r["progress"] for r in records), default=0)

        stats = [
            "",
            "=== STATISTICS ===",
            f"Total polls       : {total}",
            f"busy=True polls   : {busy_count}",
            f"busy=False polls  : {idle_count}",
            f"Max progress      : {max_progress}%",
            f"Progress decreased: {progress_decreased}",
        ]
        for s in stats:
            log.write(s + "\n")
            print(s)

        log.write(f"\n=== log ended {datetime.now()} ===\n")

    print(f"\nLog saved to {LOG_PATH}")


if __name__ == "__main__":
    main()

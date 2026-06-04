#!/usr/bin/env python3
"""高频轮询 — 每 0.5 秒读一次状态"""
import json
import time
import urllib.request
from datetime import datetime

MONITOR_URL = "http://127.0.0.1:9876/api/kg/test_status_monitor"
PIPELINE_URL = "http://127.0.0.1:9876/api/kg/pipeline_status"
LOG_FILE = "tests/poll_fast.log"
POLL_INTERVAL = 0.5


def fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def main():
    max_progress = 0
    with open(LOG_FILE, "w") as f:
        f.write(f"=== Fast poll started {datetime.now()} ===\n")
        try:
            while True:
                ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                monitor = fetch(MONITOR_URL)
                pipeline = fetch(PIPELINE_URL)

                m_ing = monitor.get("ingesting", False) if "error" not in monitor else "?"
                m_prg = monitor.get("progress", 0) if "error" not in monitor else "?"
                m_busy = monitor.get("busy", False) if "error" not in monitor else "?"
                m_docs = monitor.get("doc_total", 0) if "error" not in monitor else "?"

                p_busy = pipeline.get("busy", False) if "error" not in pipeline else "?"
                p_prg = pipeline.get("progress", 0) if "error" not in pipeline else "?"

                # 只增不减检查
                reg = ""
                if isinstance(m_prg, (int, float)) and m_prg < max_progress and max_progress > 0 and m_ing:
                    reg = f" REGRESS!{max_progress}->{m_prg}"
                if isinstance(m_prg, (int, float)) and m_prg > max_progress:
                    max_progress = m_prg

                line = f"{ts} | monitor:ing={m_ing} prg={m_prg}% busy={m_busy} docs={m_docs} | pipeline:busy={p_busy} prg={p_prg}%{reg}\n"
                f.write(line)
                f.flush()
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            f.write(f"\n=== Max progress: {max_progress}% ===\n")


if __name__ == "__main__":
    main()

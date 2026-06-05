#!/usr/bin/env python3
"""
LightRAG Pipeline Monitor

Monitors LightRAG's ingestion pipeline progress by polling the niu_api
HTTP endpoint. Works as a completely independent process — no LightRAG
imports or initialization needed.

Usage:
    python3 scripts/pipeline_monitor.py          # Continuous polling (default)
    python3 scripts/pipeline_monitor.py --once   # Poll once and exit
    python3 scripts/pipeline_monitor.py --watch  # Same as default (explicit)
"""

import argparse
import json
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from typing import Any, Dict, Optional

API_URL = "http://127.0.0.1:9876/api/kg/pipeline_status"


def fetch_pipeline_status() -> Optional[Dict[str, Any]]:
    """Poll the HTTP API for pipeline status. Returns None on connection error."""
    try:
        req = urllib.request.Request(API_URL, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError:
        return None
    except Exception:
        return None


def format_status_line(status: Dict[str, Any], start_time: Optional[float] = None) -> str:
    """Format a single-line status update."""
    timestamp = datetime.now().strftime("%H:%M:%S")

    if not status.get("busy", False):
        progress = status.get("progress", 0)
        message = status.get("message", "")
        if progress == 0 and "Completed" not in message:
            return f"[{timestamp}] Idle"
        elapsed = ""
        if start_time is not None:
            elapsed_sec = int(time.time() - start_time)
            elapsed = f" in {elapsed_sec}s"
        batchs = status.get("batchs", 0)
        return f"[{timestamp}] Done -- {batchs} file(s) processed{elapsed}"

    progress = status.get("progress", 0)
    cur_batch = status.get("cur_batch", 0)
    batchs = status.get("batchs", 0)
    message = status.get("message", "")

    # Truncate long messages
    if len(message) > 60:
        message = message[:57] + "..."

    batch_str = f"({cur_batch}/{batchs})" if batchs > 0 else "(?/?)"
    return f"[{timestamp}] >> {progress:3d}% {batch_str} {message}"


def monitor_loop(once: bool = False) -> None:
    """Main monitoring loop."""
    print("Monitoring LightRAG pipeline status via HTTP API...")
    print("Press Ctrl+C to stop\n")

    prev_busy = False
    start_time: Optional[float] = None
    connection_warned = False

    try:
        while True:
            data = fetch_pipeline_status()

            if data is None:
                if not connection_warned:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] Cannot connect to {API_URL}")
                    print("  Is the niu_api server running?")
                    connection_warned = True
                if once:
                    sys.exit(1)
                time.sleep(1)
                continue

            connection_warned = False
            busy = bool(data.get("busy", False))

            # Detect pipeline start
            if busy and not prev_busy:
                start_time = time.time()
                job_name = data.get("job_name", "")
                print(f"\n--- Pipeline started: {job_name} ---")

            # Print status line
            print(format_status_line(data, start_time), end="\r", flush=True)

            # Detect pipeline completion
            if not busy and prev_busy:
                print()  # New line after the status
                print("--- Pipeline completed ---")
                if start_time is not None:
                    elapsed = int(time.time() - start_time)
                    print(f"    Total time: {elapsed}s")
                print()
                start_time = None

            prev_busy = busy

            if once:
                print()  # New line for clean exit
                break

            time.sleep(1)

    except KeyboardInterrupt:
        print("\n\nMonitoring stopped.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Monitor LightRAG ingestion pipeline progress"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Poll once and exit (useful for scripting)",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Continuous polling (default, explicit flag)",
    )
    args = parser.parse_args()

    # --watch is the default, --once overrides
    monitor_loop(once=args.once)


if __name__ == "__main__":
    main()

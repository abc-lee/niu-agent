#!/usr/bin/env python3
"""
Trigger a file insert into the knowledge graph via the Agent's MCP tool.

Since there is no direct REST endpoint for file insertion (it goes through
the MCP tool `lightrag_insert_file`), this script sends a chat message to
the main API asking the Agent to insert a file.

Usage:
    python trigger_insert.py /path/to/file.pdf

Alternatively, just drag a file into the UI to trigger insertion manually,
then run test_pipeline_progress.py to monitor progress.
"""

import json
import sys
import urllib.request

CHAT_URL = "http://127.0.0.1:9876/api/chat"


def trigger_insert(file_path: str):
    """Send a chat message asking the agent to insert a file."""
    payload = json.dumps({
        "message": f"请将文件 {file_path} 入库到知识图谱",
        "session_id": "test-pipeline-progress",
    }).encode("utf-8")

    req = urllib.request.Request(
        CHAT_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"Request sent. HTTP {resp.status}")
            print("The agent will process the file in the background.")
            print("Now run test_pipeline_progress.py to monitor progress.")
    except Exception as e:
        print(f"Failed to send request: {e}")
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python trigger_insert.py /path/to/file")
        print("\nAlternatively, drag a file into the UI to trigger insertion,")
        print("then run test_pipeline_progress.py to monitor progress.")
        sys.exit(1)

    trigger_insert(sys.argv[1])

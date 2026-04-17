#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
清理 tool_scores.json 中的 hidden 工具分数

读取 mcp-servers.yaml 的 visibility 配置，删除所有 visibility=hidden 的工具分数条目。
"""

import json
import yaml
from pathlib import Path

def main():
    # 读取 visibility 配置
    config_path = Path(__file__).parent.parent / "config" / "mcp-servers.yaml"
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    hidden_tools = set()
    for server, server_cfg in config.items():
        if not isinstance(server_cfg, dict):
            continue
        for tool_name, tool_cfg in server_cfg.get("tools", {}).items():
            if tool_cfg.get("visibility") == "hidden":
                hidden_tools.add(f"{server}/{tool_name}")

    print(f"Found {len(hidden_tools)} hidden tools")

    # 清理分数文件
    scores_path = Path.home() / ".niu" / "tool_scores.json"
    if not scores_path.exists():
        print("No tool_scores.json found, nothing to clean")
        return

    scores = json.loads(scores_path.read_text(encoding="utf-8"))
    before = len(scores)
    scores = {k: v for k, v in scores.items() if k not in hidden_tools}
    scores_path.write_text(json.dumps(scores, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Cleaned {before - len(scores)} hidden tool scores")
    print(f"Remaining: {len(scores)} tool scores")

if __name__ == "__main__":
    main()

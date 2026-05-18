"""飞书 MCP 服务器入口点（stdio 模式，同进程架构下不使用）"""

from niu_feishu_server import get_tool_schemas


def main():
    """MCP stdio 入口 — 同进程架构下由 ToolRegistry 直接 import，不走 stdio"""
    import json
    import sys

    schemas = get_tool_schemas()
    for s in schemas:
        print(json.dumps(s), flush=True)

    sys.exit(0)


if __name__ == "__main__":
    main()

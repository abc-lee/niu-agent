"""MCP stdio entry point for ha-server (backup, same-process mode is primary)."""
from niu_ha_server import run_server

if __name__ == "__main__":
    run_server()

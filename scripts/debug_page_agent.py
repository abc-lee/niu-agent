"""
Debug test - capture full error details
"""
import sys
sys.path.insert(0, 'E:\\tools\\ai-bot\\mcp-servers\\page-agent-mcp\\src')

from niu_page_agent import execute_task
import json

print("Testing execute_task with detailed error capture...")
print("-" * 60)

try:
    result = execute_task("Navigate to https://www.baidu.com")
    print("Result:", result)
except Exception as e:
    print(f"Exception type: {type(e).__name__}")
    print(f"Exception message: {e}")

    # Try to get more details
    import traceback
    print("\nFull traceback:")
    traceback.print_exc()

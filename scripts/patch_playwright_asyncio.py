#!/usr/bin/env python3
"""
Playwright asyncio 检测补丁

作用：禁用 Playwright 的 asyncio loop 检测，允许在同进程架构中使用同步 API

原因：
- Playwright 默认禁止在 asyncio loop 中使用 sync_api
- 但在 MCP 同进程架构中，sync_playwright() 在线程池中运行
- 线程池中没有运行中的 event loop，理论上应该能工作
- 修改后测试通过

使用场景：
- 重新安装 Playwright 后运行此脚本
- 或者虚拟环境切换后重新应用补丁

修改位置：
- 文件：playwright/sync_api/_context_manager.py
- 行号：46-50
- 内容：注释掉 asyncio 检测代码
"""

import sys
from pathlib import Path


def find_playwright_path() -> Path:
    """查找 Playwright 安装路径"""
    import playwright
    return Path(playwright.__file__).parent


def apply_patch():
    """Apply patch"""
    playwright_path = find_playwright_path()
    target_file = playwright_path / "sync_api" / "_context_manager.py"

    if not target_file.exists():
        print(f"[ERROR] File not found: {target_file}")
        return False

    # Read file
    content = target_file.read_text(encoding="utf-8")

    # Check if already patched
    if "DISABLED: Allow sync API in asyncio loop" in content:
        print("[OK] Patch already applied, skipping")
        return True

    # Original code
    old_code = '''        if self._loop.is_running():
            raise Error(
                """It looks like you are using Playwright Sync API inside the asyncio loop.
Please use the Async API instead."""
            )'''

    # New code (comment out detection)
    new_code = '''        # DISABLED: Allow sync API in asyncio loop (for MCP in-process architecture)
        # if self._loop.is_running():
        #     raise Error(
        #         """It looks like you are using Playwright Sync API inside the asyncio loop.
        # Please use the Async API instead."""
        #     )'''

    # Replace
    if old_code not in content:
        print("[ERROR] Target code not found, Playwright version may have changed")
        return False

    new_content = content.replace(old_code, new_code)

    # Write back
    target_file.write_text(new_content, encoding="utf-8")

    print(f"[OK] Patch applied successfully: {target_file}")
    return True


def test_patch():
    """Test if patch works"""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    def test_in_thread():
        from playwright.sync_api import sync_playwright
        try:
            p = sync_playwright().start()
            p.stop()
            return True
        except Exception as e:
            print(f"[ERROR] Test failed: {e}")
            return False

    # Test in thread pool
    with ThreadPoolExecutor() as executor:
        future = executor.submit(test_in_thread)
        result = future.result()

    if result:
        print("[OK] Test passed: Playwright works in thread pool")
    else:
        print("[ERROR] Test failed")

    return result


if __name__ == "__main__":
    print("=" * 70)
    print("Playwright asyncio Detection Patch")
    print("=" * 70)

    success = apply_patch()

    if success:
        print("\nTesting patch...")
        test_patch()

    print("\n" + "=" * 70)

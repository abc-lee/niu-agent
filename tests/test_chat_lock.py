#!/usr/bin/env python3
"""Test that chat requests are serialized via asyncio.Lock"""

import asyncio
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))


async def test_chat_lock_serializes():
    """Two concurrent requests should be serialized, not parallel"""
    from niu_api.compat import _chat_lock

    results = []

    async def simulated_request(name: str, duration: float):
        async with _chat_lock:
            results.append(f"{name}-start")
            await asyncio.sleep(duration)
            results.append(f"{name}-end")

    # Launch two requests concurrently
    await asyncio.gather(
        simulated_request("A", 0.05),
        simulated_request("B", 0.05),
    )

    # If serialized: A-start, A-end, B-start, B-end (or B first, then A)
    # If parallel: A-start, B-start, A-end, B-end (interleaved)
    starts = [i for i, r in enumerate(results) if r.endswith("-start")]
    ends = [i for i, r in enumerate(results) if r.endswith("-end")]

    # First start must come before first end
    assert starts[0] < ends[0], f"Not serialized: {results}"
    # Second start must come after first end
    assert starts[1] > ends[0], f"Not serialized: {results}"
    print(f"PASS: Requests serialized: {results}")


async def test_chat_lock_uncontended():
    """Single request should work normally"""
    from niu_api.compat import _chat_lock

    async with _chat_lock:
        pass  # Should complete immediately

    assert not _chat_lock.locked()
    print("PASS: Uncontended lock works")


async def test_shared_lock_across_modules():
    """compat.py and chat.py should share the same lock object"""
    from niu_api.compat import _chat_lock as lock1
    from niu_api.chat import _chat_lock as lock2

    assert lock1 is lock2, "Locks are different objects!"
    print("PASS: Same lock object shared across modules")


if __name__ == "__main__":
    asyncio.run(test_chat_lock_serializes())
    asyncio.run(test_chat_lock_uncontended())
    asyncio.run(test_shared_lock_across_modules())

"""快速验证 P0 修复"""
import subprocess
import sys

def quick_verify_p0():
    """快速验证 P0 修复是否成功"""

    print("=" * 60)
    print("Quick P0 Verification")
    print("=" * 60)

    tests = [
        ("P0-1: History field cleanup",
         "tests/test_p0/test_llmcore_fixes.py::TestContentBlocksInitialization"),  # P0-1 已在此测试中验证
        ("P0-2: MessageStore sorting",
         "tests/test_p0/test_session.py::TestMessageStoreSorting"),
        ("P0-3: Context length limit",
         "tests/test_p0/test_compat.py::TestContextLengthLimit"),
        ("P0-4: Type validation",
         "tests/test_p0/test_llmcore_fixes.py::TestTypeValidation"),
        ("P0-5: content_blocks init",
         "tests/test_p0/test_llmcore_fixes.py::TestContentBlocksInitialization"),
        ("P0-6: JSON parsing",
         "tests/test_p0/test_agent_loop.py::TestToolCallJSONParsing"),
        ("P0-7: Database connection",
         "tests/test_p0/test_handler.py::TestDatabaseConnectionManagement"),
    ]

    results = []

    for name, test_path in tests:
        print(f"\nTesting: {name}")
        print("-" * 60)

        result = subprocess.run(
            [sys.executable, "-m", "pytest", test_path, "-v", "-m", "p0"],
            cwd="E:/tools/ai-bot",
            capture_output=True,
            text=True
        )

        if result.returncode == 0:
            print(f"[PASS] PASSED")
            results.append((name, "PASSED"))
        else:
            print(f"[FAIL] FAILED")
            print(result.stdout)
            print(result.stderr)
            results.append((name, "FAILED"))

    print()
    print("=" * 60)
    print("P0 Verification Summary")
    print("=" * 60)

    for name, status in results:
        symbol = "[OK]" if status == "PASSED" else "[X]"
        print(f"{symbol} {name}: {status}")

    passed = sum(1 for _, status in results if status == "PASSED")
    total = len(results)

    print()
    print(f"Result: {passed}/{total} tests passed")

    return 0 if passed == total else 1

if __name__ == "__main__":
    sys.exit(quick_verify_p0())

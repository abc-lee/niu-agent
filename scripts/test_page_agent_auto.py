"""
Page-Agent 自动化测试
使用 Playwright 自动测试 Page-Agent 的功能
"""
import asyncio
import json
import sys
from pathlib import Path
from playwright.async_api import async_playwright

# Force UTF-8 encoding
sys.stdout.reconfigure(encoding='utf-8')

# 测试配置
TEST_URL = "file:///E:/tools/ai-bot/scripts/test-page-agent.html"
RESULTS_FILE = Path(__file__).parent.parent / "docs" / "page-agent-test-results.json"


async def test_page_agent():
    """自动化测试 Page-Agent"""

    results = {
        "test_date": "2026-04-10",
        "environment": {
            "os": "Windows 11",
            "browser": "Chromium",
            "test_url": TEST_URL,
        },
        "tests": [],
        "summary": {}
    }

    async with async_playwright() as p:
        # 启动浏览器
        browser = await p.chromium.launch(headless=False)  # headless=False 可以看到浏览器
        context = await browser.new_context()
        page = await context.new_page()

        print("=" * 60)
        print("Page-Agent 自动化测试开始")
        print("=" * 60)

        try:
            # 1. 打开测试页面
            print("\n[1/6] 打开测试页面...")
            await page.goto(TEST_URL)
            await page.wait_for_timeout(2000)  # 等待 Page-Agent 初始化
            print("✅ 页面加载成功")

            # 2. 检查 Page-Agent 是否加载
            print("\n[2/6] 检查 Page-Agent 是否加载...")
            agent_exists = await page.evaluate("""() => {
                return typeof PageAgent !== 'undefined' &&
                       typeof PageAgent.PageAgent !== 'undefined';
            }""")

            if agent_exists:
                print("✅ Page-Agent 已加载")

                # 获取 Page-Agent 版本
                version = await page.evaluate("""() => {
                    if (window.pageAgentDemo) return 'Demo';
                    if (window.PageAgent) return 'Full';
                    return 'Unknown';
                }""")
                print(f"   版本类型: {version}")
            else:
                print("⚠️ Page-Agent 未加载，尝试刷新...")
                await page.reload()
                await page.wait_for_timeout(3000)
                agent_exists = await page.evaluate("""() => {
                    return typeof PageAgent !== 'undefined';
                }""")

                if agent_exists:
                    print("✅ 刷新后 Page-Agent 已加载")
                else:
                    print("❌ Page-Agent 加载失败")
                    results["tests"].append({
                        "name": "Page-Agent 加载",
                        "status": "FAIL",
                        "error": "Page-Agent 未定义"
                    })

            # 3. 测试 1：点击按钮
            print("\n[3/6] 测试 1：点击按钮...")
            try:
                # 点击测试按钮
                await page.click("text=测试 1: 点击按钮")

                # 等待执行完成
                await page.wait_for_timeout(10000)  # 等待最多 10 秒

                # 检查结果
                log_content = await page.inner_text("#log-container")
                success = "点击成功" in log_content or "✅" in log_content

                results["tests"].append({
                    "name": "测试 1：点击按钮",
                    "status": "PASS" if success else "FAIL",
                    "log": log_content[-500:] if len(log_content) > 500 else log_content
                })

                print(f"{'✅' if success else '❌'} 测试 1 {'成功' if success else '失败'}")

            except Exception as e:
                print(f"❌ 测试 1 出错: {e}")
                results["tests"].append({
                    "name": "测试 1：点击按钮",
                    "status": "ERROR",
                    "error": str(e)
                })

            # 4. 测试 2：填写表单
            print("\n[4/6] 测试 2：填写表单...")
            try:
                await page.click("text=测试 2: 填写表单")
                await page.wait_for_timeout(10000)

                # 检查表单是否被填写
                name_value = await page.input_value("#name")
                email_value = await page.input_value("#email")

                success = name_value == "张三" and email_value == "test@example.com"

                results["tests"].append({
                    "name": "测试 2：填写表单",
                    "status": "PASS" if success else "FAIL",
                    "expected": {"name": "张三", "email": "test@example.com"},
                    "actual": {"name": name_value, "email": email_value}
                })

                print(f"{'✅' if success else '❌'} 测试 2 {'成功' if success else '失败'}")
                if not success:
                    print(f"   预期: 姓名=张三, 邮箱=test@example.com")
                    print(f"   实际: 姓名={name_value}, 邮箱={email_value}")

            except Exception as e:
                print(f"❌ 测试 2 出错: {e}")
                results["tests"].append({
                    "name": "测试 2：填写表单",
                    "status": "ERROR",
                    "error": str(e)
                })

            # 5. 测试 3：提取信息
            print("\n[5/6] 测试 3：提取信息...")
            try:
                await page.click("text=测试 3: 提取信息")
                await page.wait_for_timeout(10000)

                result_content = await page.inner_text("#result-display")

                results["tests"].append({
                    "name": "测试 3：提取信息",
                    "status": "PASS" if len(result_content) > 50 else "FAIL",
                    "result": result_content[:500] if len(result_content) > 500 else result_content
                })

                print(f"✅ 测试 3 完成，结果长度: {len(result_content)} 字符")

            except Exception as e:
                print(f"❌ 测试 3 出错: {e}")
                results["tests"].append({
                    "name": "测试 3：提取信息",
                    "status": "ERROR",
                    "error": str(e)
                })

            # 6. 测试 4：复杂任务
            print("\n[6/6] 测试 4：复杂任务...")
            try:
                await page.click("text=测试 4: 复杂任务")
                await page.wait_for_timeout(15000)  # 复杂任务需要更长时间

                # 检查最终状态
                name_value = await page.input_value("#name")

                results["tests"].append({
                    "name": "测试 4：复杂任务",
                    "status": "PASS" if name_value == "李四" else "PARTIAL",
                    "expected_name": "李四",
                    "actual_name": name_value
                })

                print(f"{'✅' if name_value == '李四' else '⚠️'} 测试 4 {'成功' if name_value == '李四' else '部分完成'}")
                print(f"   预期姓名: 李四, 实际姓名: {name_value}")

            except Exception as e:
                print(f"❌ 测试 4 出错: {e}")
                results["tests"].append({
                    "name": "测试 4：复杂任务",
                    "status": "ERROR",
                    "error": str(e)
                })

            # 汇总统计
            passed = sum(1 for t in results["tests"] if t["status"] == "PASS")
            failed = sum(1 for t in results["tests"] if t["status"] == "FAIL")
            errors = sum(1 for t in results["tests"] if t["status"] == "ERROR")

            results["summary"] = {
                "total": len(results["tests"]),
                "passed": passed,
                "failed": failed,
                "errors": errors,
                "success_rate": f"{(passed / len(results["tests"]) * 100):.1f}%" if results["tests"] else "0%"
            }

        except Exception as e:
            print(f"\n❌ 测试过程出错: {e}")
            results["error"] = str(e)

        finally:
            # 保存结果
            with open(RESULTS_FILE, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=2)

            print("\n" + "=" * 60)
            print("测试完成！")
            print(f"结果已保存到: {RESULTS_FILE}")
            print("\n汇总:")
            print(f"  总测试数: {results['summary'].get('total', 0)}")
            print(f"  通过: {results['summary'].get('passed', 0)}")
            print(f"  失败: {results['summary'].get('failed', 0)}")
            print(f"  错误: {results['summary'].get('errors', 0)}")
            print(f"  成功率: {results['summary'].get('success_rate', '0%')}")
            print("=" * 60)

            # 保持浏览器打开，让用户查看
            print("\n浏览器保持打开状态，可以手动检查测试结果...")
            print("按 Ctrl+C 或关闭浏览器窗口退出...")

            # 等待用户手动关闭或超时
            await asyncio.sleep(60)

            await browser.close()


if __name__ == "__main__":
    asyncio.run(test_page_agent())

"""
检查 Page-Agent Demo 版本的状态和可用性
"""
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright


async def check_page_agent():
    """检查 Page-Agent 的状态"""

    print("=" * 60)
    print("Page-Agent 状态检查")
    print("=" * 60)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_context().new_page()

        # 监听控制台消息
        console_messages = []
        page.on("console", lambda msg: console_messages.append({
            "type": msg.type,
            "text": msg.text
        }))

        # 监听网络请求
        network_requests = []
        page.on("request", lambda req: network_requests.append({
            "url": req.url,
            "status": req.response.status if req.response else "pending"
        }))

        print("\n[1/3] 打开测试页面...")
        await page.goto("file:///E:/tools/ai-bot/scripts/test-page-agent.html")
        await page.wait_for_timeout(3000)

        print("\n[2/3] 检查 Page-Agent 状态...")

        # 获取所有诊断信息
        status = await page.evaluate("""() => {
            const result = {
                // PageAgent 全局对象
                hasPageAgentGlobal: typeof PageAgent !== 'undefined',
                hasPageAgentConstructor: typeof PageAgent?.PageAgent !== 'undefined',
                hasPageAgentInstance: typeof pageAgentInstance !== 'undefined',

                // window 对象中的相关属性
                windowKeys: Object.keys(window).filter(k =>
                    k.toLowerCase().includes('page') ||
                    k.toLowerCase().includes('agent') ||
                    k.toLowerCase().includes('llm')
                ),

                // 检查全局变量
                globals: {
                    PageAgent: typeof PageAgent,
                    pageAgentDemo: typeof window?.pageAgentDemo,
                    pageAgentInstance: typeof window?.pageAgentInstance,
                },

                // 脚本加载状态
                scriptsLoaded: Array.from(document.scripts).map(s => ({
                    src: s.src,
                    loaded: s.readyState || 'unknown'
                }))
            };

            return result;
        }""")

        print(json.dumps(status, indent=2, ensure_ascii=False))

        print("\n[3/3] 控制台消息:")
        for msg in console_messages:
            print(f"  [{msg['type']}] {msg['text']}")

        print("\n[额外] 网络请求（Page-Agent CDN）:")
        for req in network_requests:
            if "page-agent" in req["url"].lower() or "jsdelivr" in req["url"].lower():
                print(f"  [{req['status']}] {req['url']}")

        # 保存完整日志
        output = {
            "status": status,
            "console_messages": console_messages,
            "network_requests": [r for r in network_requests if "page-agent" in r["url"].lower() or "jsdelivr" in r["url"].lower()]
        }

        output_file = Path("E:/tools/ai-bot/docs/page-agent-status-check.json")
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)

        print(f"\n状态检查完成，详情保存到: {output_file}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(check_page_agent())

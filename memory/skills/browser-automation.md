# 浏览器自动化 Skill

**触发关键词**：浏览器、网页、填表、截图、网页操作、表单填写、自动答题

**L1 摘要**：Browser automation|browser,form filling,web operation|Use browser_navigate + code_run(CDP) to execute Playwright code for browser automation|browser_navigate,Playwright,CDP,code_run|skill|memory/skills/browser-automation.md

## 概述

本项目提供浏览器自动化能力，基于 Playwright 实现。采用 **MCP 工具 + code_run(CDP)** 架构：

1. **`browser_navigate` MCP 工具**：启动浏览器并导航到 URL
2. **`code_run` 工具**：通过 CDP 协议连接已有浏览器，执行 Playwright 代码

## 架构说明

```
browser_navigate (MCP 工具)
    ↓ 启动浏览器，开放 CDP 端口 (9222)
code_run (基础工具)
    ↓ connect_over_cdp("http://127.0.0.1:9222")
    ↓ 连接到同一个浏览器实例
完成浏览器操作（点击、填充、截图、提取内容等）
```

**关键**：code_run 是子进程，无法直接访问主进程的 BrowserManager。通过 CDP（Chrome DevTools Protocol）连接到同一个浏览器实例，和 Playwright CLI 的架构一样。

## 使用流程

### 1. 导航到网页

**调用 `browser_navigate` 工具**：

```json
{
  "url": "https://example.com",
  "wait_until": "networkidle"
}
```

参数说明：
- `url`: 目标 URL
- `wait_until`: 等待策略
  - `"load"`: 等待 load 事件
  - `"domcontentloaded"`: 等待 DOMContentLoaded（默认）
  - `"networkidle"`: 等待网络空闲
  - `"commit"`: 收到响应头即返回

### 2. 通过 code_run 操作浏览器

**使用 `code_run` 工具，通过 CDP 连接已有浏览器**：

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    # 通过 CDP 连接到 browser_navigate 启动的浏览器
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    page = browser.contexts[0].pages[0]

    # 执行操作
    page.click('button:has-text("提交")')
    page.fill('input[name="username"]', '张三')
    page.screenshot(path='screenshot.png')
```

## 常用操作示例

### 提取页面内容

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    page = browser.contexts[0].pages[0]

    # 提取文本
    text = page.inner_text('body')
    title = page.title()
    print(f"标题: {title}")
    print(f"内容: {text[:500]}")
```

### 点击元素

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    page = browser.contexts[0].pages[0]

    # 通过文本点击
    page.click('button:has-text("登录")')

    # 通过 CSS 选择器点击
    page.click('#submit-button')

    # 通过 role 点击
    page.click('button[name="submit"]')
```

### 填充表单

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    page = browser.contexts[0].pages[0]

    # 填充输入框
    page.fill('input[name="name"]', '李四')
    page.fill('input[name="email"]', 'lisi@example.com')

    # 选择下拉框
    page.select_option('select#country', 'China')

    # 勾选复选框
    page.check('input[type="checkbox"]')

    # 点击提交
    page.click('button[type="submit"]')
```

### 截图

```python
from playwright.sync_api import sync_playwright
import base64

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    page = browser.contexts[0].pages[0]

    # 截取整个页面
    screenshot_bytes = page.screenshot()

    # 转换为 base64
    screenshot_b64 = base64.b64encode(screenshot_bytes).decode()
    print(f"截图已保存（{len(screenshot_b64)} bytes）")
```

### 提取数据

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    page = browser.contexts[0].pages[0]

    # 提取文本
    text = page.inner_text('body')
    title = page.title()

    # 提取链接
    links = page.query_selector_all('a')
    for link in links:
        href = link.get_attribute('href')
        text = link.inner_text()
        print(f"{text}: {href}")
```

### 等待元素

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    page = browser.contexts[0].pages[0]

    # 等待元素出现
    page.wait_for_selector('.result', timeout=5000)

    # 等待元素消失
    page.wait_for_selector('.loading', state='hidden')

    # 等待导航完成
    page.wait_for_load_state('networkidle')
```

## 高级用法

### 智能表单填充（结合知识库）

**场景**：从知识库中读取用户信息，自动填充表单。

```python
from playwright.sync_api import sync_playwright

# 1. 从知识库获取用户信息（伪代码）
user_info = {
    "name": "张三",
    "email": "zhangsan@example.com",
    "phone": "13800138000"
}

# 2. 打开表单页面（通过 browser_navigate 工具完成）
# ...

# 3. 填充表单
with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    page = browser.contexts[0].pages[0]

    for field, value in user_info.items():
        selectors = [
            f'input[name="{field}"]',
            f'input[placeholder*="{field}"]',
            f'[aria-label*="{field}"]'
        ]
        for selector in selectors:
            try:
                page.fill(selector, value, timeout=1000)
                print(f"✓ 填充 {field}: {value}")
                break
            except:
                continue
```

### 自动答题（结合向量搜索）

**场景**：从网页提取问题，在知识库中搜索答案，自动填写。

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    page = browser.contexts[0].pages[0]

    # 1. 提取问题
    question_element = page.query_selector('.question')
    if question_element:
        question = question_element.inner_text()

        # 2. 在知识库中搜索答案（伪代码）
        # answer = search_in_knowledge_base(question)

        # 3. 填写答案
        answer_input = page.query_selector('textarea.answer')
        if answer_input:
            answer_input.fill("答案内容")
            page.click('button:has-text("提交")')
```

## 注意事项

### 1. 浏览器生命周期

- **自动启动**：首次调用 `browser_navigate` 时自动启动浏览器
- **浏览器可见**：默认 `headless=False`，会显示浏览器窗口
- **自动关闭**：浏览器空闲 5 分钟后自动关闭
- **单例模式**：全局只有一个浏览器实例，通过 `BrowserManager` 管理
- **持久化登录**：用户数据保存在 `~/.niu/browser_data/`，包括 cookies、登录状态、浏览历史
- **非无痕模式**：关闭浏览器后重新打开，仍保持登录状态
- **CDP 端口**：浏览器启动后开放 CDP 端口 9222，供 code_run 子进程连接

### 2. 强制规则（必须遵守）

**code_run 中连接浏览器的唯一正确方式**：
- ✅ `p.chromium.connect_over_cdp("http://127.0.0.1:9222")` — 通过 CDP 连接已有浏览器
- ❌ 禁止 `chromium.launch()` — 会启动新浏览器实例，导致冲突
- ❌ 禁止 `chromium.launch(headless=True)` — headless 模式会被反爬虫系统检测
- ❌ 禁止 `BrowserManager().get_page()` — code_run 是子进程，无法访问主进程对象
- ✅ 必须使用 `browser_navigate` 工具进行导航

**原因**：code_run 是子进程，和主进程不共享内存。通过 CDP 协议连接同一个浏览器实例，和 Playwright CLI 的架构一样。

### 3. 并发保护

- 使用 `threading.Lock` 保护浏览器实例
- 超时时间：30 秒
- 如果超时，返回错误 `"Browser busy"`

### 4. 错误重试

- 浏览器启动失败会自动重试（最多 3 次）
- 每次操作成功后重置错误计数

### 5. 限制

- **无反爬虫绕过**：CAPTCHA、Cloudflare 可能阻止访问
- **无代理支持**：不支持 IP 轮换
- **会话持久化**：✅ 支持持久化，cookies 和登录状态保存在 `~/.niu/browser_data/`

## Playwright 选择器语法

### Role-based 选择器（推荐）

```python
page.click('button[name="submit"]')
page.click('link:has-text("登录")')
page.fill('textbox[name="email"]', 'test@example.com')
```

### 文本选择器

```python
page.click('text=登录')
page.click('button:has-text("提交")')
```

### CSS 选择器

```python
page.click('#submit-button')
page.fill('input.username', '张三')
```

### 组合选择器

```python
page.click('article >> .title')
page.fill('form >> input[name="email"]', 'test@example.com')
```

## 完整示例

### 场景：自动填写注册表单

**步骤 1**：导航到注册页面

```json
{
  "name": "browser_navigate",
  "arguments": {
    "url": "https://example.com/register",
    "wait_until": "networkidle"
  }
}
```

**步骤 2**：使用 `code_run` 填充表单

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    page = browser.contexts[0].pages[0]

    # 填充用户信息
    page.fill('input[name="username"]', 'zhangsan')
    page.fill('input[name="email"]', 'zhangsan@example.com')
    page.fill('input[name="password"]', 'SecurePassword123')
    page.fill('input[name="confirm_password"]', 'SecurePassword123')

    # 同意条款
    page.check('input[type="checkbox"]')

    # 提交表单
    page.click('button[type="submit"]')

    # 等待跳转
    page.wait_for_load_state('networkidle')

    # 截图确认
    screenshot_bytes = page.screenshot()
    print(f"✓ 注册完成（截图 {len(screenshot_bytes)} bytes）")
```

## 关键词

浏览器、网页、填表、截图、自动化、Playwright、表单填写、自动答题、CDP

# 浏览器自动化 Skill

**触发关键词**：浏览器、网页、填表、截图、网页操作、表单填写、自动答题

**L1 摘要**：Browser automation|browser,form filling,web operation|Use browser_navigate + code_run to execute Playwright code for browser automation|browser_navigate,Playwright,BrowserManager,code_run|skill|memory/skills/browser-automation.md

## 概述

本项目提供浏览器自动化能力，基于 Playwright 实现。采用 **MCP 工具 + code_run** 架构：

1. **`browser_navigate` MCP 工具**：启动浏览器并导航到 URL
2. **`code_run` 工具**：执行 Playwright Python 代码进行其他操作（点击、填充、截图等）

## 架构说明

```
browser_navigate (MCP 工具)
    ↓ 启动浏览器
BrowserManager (单例)
    ↓ 提供共享 Page 实例
code_run (基础工具)
    ↓ 执行 Playwright 代码
完成浏览器操作
```

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

### 2. 执行浏览器操作

**使用 `code_run` 工具执行 Playwright 代码**：

```python
from niu_browser_server import BrowserManager

# 获取浏览器页面对象
page, error = BrowserManager().get_page()
if error:
    print(f"Error: {error}")
    exit(1)

if not page:
    print("Failed to get page")
    exit(1)

# 执行操作
page.click('button:has-text("提交")')
page.fill('input[name="username"]', '张三')
page.screenshot(path='screenshot.png')
```

## 常用操作示例

### 点击元素

```python
from niu_browser_server import BrowserManager

page, _ = BrowserManager().get_page()
if page:
    # 通过文本点击
    page.click('button:has-text("登录")')

    # 通过 CSS 选择器点击
    page.click('#submit-button')

    # 通过 role 点击
    page.click('button[name="submit"]')
```

### 填充表单

```python
from niu_browser_server import BrowserManager

page, _ = BrowserManager().get_page()
if page:
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
from niu_browser_server import BrowserManager
import base64

page, _ = BrowserManager().get_page()
if page:
    # 截取整个页面
    screenshot_bytes = page.screenshot()

    # 转换为 base64
    screenshot_b64 = base64.b64encode(screenshot_bytes).decode()

    # 返回给用户
    print(f"截图已保存（{len(screenshot_b64)} bytes）")
```

### 提取数据

```python
from niu_browser_server import BrowserManager

page, _ = BrowserManager().get_page()
if page:
    # 提取文本
    text = page.inner_text('body')

    # 提取特定元素
    title = page.title()
    headings = page.query_selector_all('h1, h2, h3')

    # 提取链接
    links = page.query_selector_all('a')
    for link in links:
        href = link.get_attribute('href')
        text = link.inner_text()
        print(f"{text}: {href}")
```

### 等待元素

```python
from niu_browser_server import BrowserManager

page, _ = BrowserManager().get_page()
if page:
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
from niu_browser_server import BrowserManager

# 1. 从知识库获取用户信息（伪代码）
user_info = {
    "name": "张三",
    "email": "zhangsan@example.com",
    "phone": "13800138000"
}

# 2. 打开表单页面（通过 browser_navigate 工具完成）
# ...

# 3. 填充表单
page, _ = BrowserManager().get_page()
if page:
    for field, value in user_info.items():
        try:
            # 尝试多种选择器策略
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
        except Exception as e:
            print(f"✗ 填充 {field} 失败: {e}")
```

### 自动答题（结合向量搜索）

**场景**：从网页提取问题，在知识库中搜索答案，自动填写。

```python
from niu_browser_server import BrowserManager

# 1. 提取问题
page, _ = BrowserManager().get_page()
if not page:
    exit(1)

question_element = page.query_selector('.question')
if question_element:
    question = question_element.inner_text()

    # 2. 在知识库中搜索答案（伪代码）
    # answer = search_in_knowledge_base(question)

    # 3. 填写答案
    answer_input = page.query_selector('textarea.answer')
    if answer_input:
        answer_input.fill("答案内容")

        # 4. 提交
        page.click('button:has-text("提交")')
```

## 注意事项

### 1. 浏览器生命周期

- **自动启动**：首次调用 `browser_navigate` 时自动启动浏览器
- **自动关闭**：浏览器空闲 5 分钟后自动关闭
- **单例模式**：全局只有一个浏览器实例，通过 `BrowserManager` 管理

### 2. 并发保护

- 使用 `threading.Lock` 保护浏览器实例
- 超时时间：30 秒
- 如果超时，返回错误 `"Browser busy"`

### 3. 错误重试

- 浏览器启动失败会自动重试（最多 3 次）
- 每次操作成功后重置错误计数

### 4. 限制

- **无反爬虫绕过**：CAPTCHA、Cloudflare 可能阻止访问
- **无代理支持**：不支持 IP 轮换
- **无会话持久化**：浏览器关闭后 cookies 丢失

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
from niu_browser_server import BrowserManager

page, _ = BrowserManager().get_page()
if not page:
    print("Error: Failed to get page")
    exit(1)

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

浏览器、网页、填表、截图、自动化、Playwright、表单填写、自动答题

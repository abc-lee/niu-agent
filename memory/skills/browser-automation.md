---
name: browser-automation
description: Use when user asks to browse websites, fill web forms, click buttons, automate browser tasks, or interact with web pages
status: active
created: 2026-04-26
last_tested: 2026-04-26
---

# Browser Automation

## Overview

通过虚拟磁盘工具 `disk(command)` 操作浏览器。所有浏览器命令在 `/browser/` 目录下。

## Quick Start

```
cat /browser/readme.txt           # 先看目录说明，了解所有工具用法
/browser/browser_navigate https://example.com   # 打开网页
```

## Core Workflow

### 1. 打开网页

```
/browser/browser_navigate https://www.example.com
```

**注意**：URL 必须以 `http://` 或 `https://` 开头。不要写 `url=https://...`。

### 2. 查看页面状态

导航后自动返回页面状态：
- `elements` — 编号的交互元素列表（可点击/输入的元素）
- `tabSummary` — 所有标签页列表
- `currentTabId` — 当前标签页 ID

### 3. 与页面交互

```
/browser/browser_interact click --index 5        # 点击第 5 号元素
/browser/browser_interact input --index 3 --text "搜索内容"  # 在第 3 号元素输入文本
/browser/browser_interact select --index 2 --option "选项值"  # 选择下拉选项
/browser/browser_interact scroll --direction down --amount 1.5  # 向下滚动 1.5 页
/browser/browser_interact get_state              # 获取当前页面状态
```

**重要**：每次交互后元素会重新编号，始终使用上一次返回结果中的最新编号。

### 4. 多标签页管理

```
/browser/browser_new_tab https://example.com     # 新标签页打开
/browser/browser_switch_tab --tab_id 42          # 切换到标签页 42
/browser/browser_close_tab --tab_id 42           # 关闭标签页 42
```

`tab_id` 来自之前响应中的 `tabSummary`。不能关闭初始标签页。

## Common Patterns

### 搜索并点击结果

```
/browser/browser_navigate https://www.google.com
/browser/browser_interact input --index 1 --text "搜索关键词"
/browser/browser_interact click --index 2        # 点击搜索按钮
```

### 对比两个网页

```
/browser/browser_navigate https://site-a.com
/browser/browser_new_tab https://site-b.com
/browser/browser_switch_tab --tab_id <id>        # 切回 site-a
```

### 填写表单

```
/browser/browser_navigate https://example.com/form
/browser/browser_interact input --index 1 --text "姓名"
/browser/browser_interact input --index 2 --text "邮箱"
/browser/browser_interact select --index 3 --option "选项A"
/browser/browser_interact click --index 4        # 提交
```

## Common Mistakes

| 问题 | 解决方案 |
|------|---------|
| URL 变成 extension://... | URL 必须以 `https://` 开头，不要写 `url=` 前缀 |
| 元素编号找不到 | 用 `get_state` 获取最新编号，每次交互后编号会变 |
| 标签页操作失败 | 检查 `tabSummary` 中的 tabId 是否正确 |
| 页面加载慢 | 等待导航返回后再操作，不要重复发送导航命令 |

## Tips

- 先 `cat /browser/readme.txt` 了解所有工具的完整参数
- 位置参数直接写，flag 参数用 `--key value` 格式
- 每次交互后检查返回的 `elements` 和 `tabSummary`
- 不要猜测元素编号，始终基于最新返回结果操作

<!-- 执行提醒 -->
<!-- 此区域用于重申已有规则，不引入新规则。规则没错但没被遵守时在这里添加提醒。 -->

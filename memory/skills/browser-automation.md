# 浏览器自动化 Skill

**触发关键词**：浏览器、网页、填表、网页操作、表单填写、自动答题、上网查

**L1 摘要**：Browser automation|browser,form filling,web operation|Use browser_navigate + browser_interact + browser_new_tab to automate browser tasks|browser_navigate,browser_interact,browser_new_tab,Chrome Extension|skill|memory/skills/browser-automation.md

## 工具

| 工具 | 用途 |
|------|------|
| `browser_navigate(url)` | 导航到 URL，返回页面状态（编号的交互元素） |
| `browser_interact(action, index, ...)` | 操作页面元素，返回新页面状态 |
| `browser_new_tab(url)` | 在新标签页打开 URL，返回新标签页状态 |

**action 类型**：`click` | `input` | `select` | `scroll` | `get_state`

## 工作循环

```
get_state → 看当前页面 → 操作 → 看新编号 → 操作 → ... → 汇报
```

**核心**：操作串行，每次用上一步返回的新索引。

## 接管用户浏览器（最重要）

**扩展共享用户浏览器，不要覆盖用户当前页面！**

1. **先获取当前页面状态**：`browser_interact(action="get_state")` — 返回用户当前页面的 URL、标题和编号元素
2. **根据当前页面决定操作**：如果用户已经在目标页面，直接操作；如果需要导航，再用 `browser_navigate`
3. **禁止无脑 navigate**：不要上来就 `browser_navigate(url)`，这会覆盖用户正在看的页面

正确流程：
```
browser_interact(action="get_state")  ← 先看用户当前页面
  → 用户已在百度搜索页
browser_interact(action="input", index=12, text="查询内容")  ← 直接操作
```

错误流程：
```
❌ browser_navigate("https://www.baidu.com")  ← 覆盖了用户当前页面！
```

## 首次使用（只需一次）

如果扩展未加载，首次调用时会提示你手动加载扩展：

1. 打开 `edge://extensions/`（或 `chrome://extensions/`）
2. 开启右上角的「开发者模式」
3. 点击「加载解压的扩展」
4. 选择目录：`extensions/niu-browser-ext/`

之后每次打开浏览器，扩展都会自动在后台运行，共享你的所有登录状态。

## 规则

### 串行操作（最重要）

操作必须串行，禁止并行。每次操作改变页面状态，并行会导致索引错乱。

❌ 同时发两个 `browser_interact`
✅ 等上一个返回，用新索引再发下一个

### 元素编号

- `[N]` = 可交互元素，N 是操作索引
- `*[N]` = 新出现的元素（页面变化后新增）
- 缩进 = DOM 父子关系
- **索引不保证稳定**：页面变化后重新分配，必须用最新返回的索引
- "Element not found" = 索引过期，先 `get_state` 刷新

### 只用编号索引，不用 CSS 选择器

**必须用 `index` 编号操作元素，禁止自己构造 CSS 选择器、XPath 或 nth-child 定位。**

- 页面上同 class 的元素很多（如 60 道题的选项），CSS 选择器会匹配到错误元素
- 编号索引是精确的，每个编号对应唯一 DOM 元素
- ❌ `document.querySelector('div.spanner:nth-child(3)')` — 可能匹配错
- ✅ `browser_interact(action="click", index=15)` — 精确操作第 15 号元素

### 操作策略

- **输入后要主动完成**：填完输入框后，按回车/点搜索按钮/选下拉选项，不要等页面自己响应
- **输入后注意页面变化**：输入可能触发自动补全、下拉建议、弹窗，检查是否需要与新出现的元素交互
- **不要重复同一操作超过 3 次**：重复无效说明策略错误，换方式或求助
- **不要点 `target=_blank` 链接**：会开新标签页，脱离当前页面控制
- **不要随意登录**：没有凭据不要尝试登录
- **验证操作结果**：不要假设操作成功，检查返回的页面状态是否如预期变化

### 滚动

- 只在 `pixelsBelow > 0` 或 `pixelsAbove > 0` 时才滚动
- 可滚动区域标记为 `data-scrollable`，带滚动距离信息
- `amount` 支持小数：0.5 = 半页，2.0 = 两页

### 遇到困难

- 找不到元素 → 先滚动查找，或 `get_state` 刷新
- CAPTCHA → 告知用户无法解决，请求人工处理
- 页面未加载完 → 等待后 `get_state`
- 卡住 → 换思路（滚动、返回、换路径），或向用户求助
- 任务不可能完成 → 诚实告知，不要硬试

### 限制

- 无反爬虫绕过（CAPTCHA、Cloudflare）
- Canvas/WebGL 页面无 DOM 控件
- 跨域 iframe 内容无法检测

## 示例

填写登录表单：
```
browser_navigate("https://example.com/login")
  → [0]<input name=username />  [1]<input name=password />  [2]<button>登录 />

browser_interact(action="input", index=0, text="zhangsan")
  → 返回新状态（索引可能变了）

browser_interact(action="input", index=1, text="mypass")
  → 返回新状态

browser_interact(action="click", index=2)
  → 登录成功，返回新页面
```

上网查信息：
```
browser_navigate("https://www.baidu.com")
browser_interact(action="input", index=12, text="Claude AI")
browser_interact(action="click", index=17)   ← 点搜索按钮完成输入
  → 搜索结果页面
```

新标签页打开链接：
```
browser_new_tab("https://news.ycombinator.com")
  → 新标签页打开，返回页面状态
```

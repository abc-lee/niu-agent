# 子 Agent Thinking Chain 展开/收起修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复子 Agent thinking chain 展开/收起按钮两个问题：内容不到 200px 时不应显示按钮；内容超过 200px 时按钮点击无效。

**Architecture:** 将按钮从 `overflow-y: auto` 的 div 内部移到外部作为兄弟元素，避免 `position: sticky` 在可滚动容器内不可靠。用 JS 判断 `scrollHeight > clientHeight` 控制按钮显示。

**Tech Stack:** JavaScript, CSS, Electron

---

## 两个问题

### 问题一：内容不到 200px 时也显示展开按钮

CSS L866 `.thinking-expand-btn { display: block }` 是无条件的，JS L2865-2873 对所有 `cssClass === 'thinking'` 的消息都创建按钮，没有判断内容高度。

### 问题二：内容超过 200px 时展开无效

按钮放在 div（`max-height: 200px` + `overflow-y: auto`）内部，用 `position: sticky; bottom: 0` 定位。sticky 在可滚动容器内行为不可靠——按钮可能被滚动到不可见位置或点击区域不对。`toggle('expanded')` 和 CSS 优先级本身正确，但按钮可见性和可交互性有问题。

---

## 修改前代码

### CSS（L848-879）

```css
    .message.thinking {
      font-size: 12px;
      color: #888;
      border-left: 2px solid #ccc;
      padding-left: 8px;
      margin: 4px 0;
      white-space: pre-wrap;
      max-height: 200px;
      overflow-y: auto;
      position: relative;
    }
    .message.thinking.expanded {
      max-height: none;
    }
    .thinking-expand-btn {
      position: sticky;
      bottom: 0;
      display: block;
      width: 100%;
      padding: 16px 0 4px 0;
      border: none;
      cursor: pointer;
      font-size: 12px;
      color: #40a0a0;
      text-align: center;
      background: linear-gradient(transparent, #faf8f0 70%);
    }
    .message.thinking.expanded .thinking-expand-btn {
      background: none;
    }
```

### JS（L2863-2874）

```javascript
      container.appendChild(div);
      // thinking chain：创建展开/收起按钮（CSS 已有 .thinking-expand-btn + .expanded 样式）
      if (cssClass === 'thinking') {
        const expandBtn = document.createElement('button');
        expandBtn.className = 'thinking-expand-btn';
        expandBtn.textContent = '展开全部';
        expandBtn.addEventListener('click', () => {
          const expanded = div.classList.toggle('expanded');
          expandBtn.textContent = expanded ? '收起' : '展开全部';
        });
        div.appendChild(expandBtn);
      }
```

---

## 修改后代码

### CSS 修改

```css
    .message.thinking {
      font-size: 12px;
      color: #888;
      border-left: 2px solid #ccc;
      padding-left: 8px;
      margin: 4px 0;
      white-space: pre-wrap;
      max-height: 200px;
      overflow-y: auto;
      position: relative;
    }
    .message.thinking.expanded {
      max-height: none;
    }
    /* 展开按钮：作为 thinking div 的兄弟元素，不受 overflow-y:auto 裁剪 */
    .thinking-expand-btn {
      display: block;
      width: 100%;
      padding: 4px 0;
      border: none;
      cursor: pointer;
      font-size: 12px;
      color: #40a0a0;
      text-align: center;
      background: linear-gradient(transparent, #faf8f0 70%);
      margin-top: -4px;
    }
```

变更点：
- 移除 `position: sticky; bottom: 0`（sticky 在 overflow-y:auto 容器内不可靠）
- 移除 `.message.thinking.expanded .thinking-expand-btn { background: none }`（按钮已不在 div 内部，不需要）
- `padding` 从 `16px 0 4px 0` 改为 `4px 0`（不需要 sticky 遮罩的留白）
- `margin-top: -4px`（紧贴 thinking div 底部，视觉连贯）

### JS 修改

```javascript
      container.appendChild(div);
      // thinking chain：内容超过 200px（max-height 限制）时显示展开按钮
      if (cssClass === 'thinking' && div.scrollHeight > div.clientHeight) {
        const expandBtn = document.createElement('button');
        expandBtn.className = 'thinking-expand-btn';
        expandBtn.textContent = '展开全部';
        expandBtn.addEventListener('click', () => {
          const expanded = div.classList.toggle('expanded');
          expandBtn.textContent = expanded ? '收起' : '展开全部';
        });
        container.appendChild(expandBtn);  // 兄弟元素，不在 overflow-y:auto 的 div 内
      }
```

变更点：
- 加 `div.scrollHeight > div.clientHeight` 条件：内容不超过 200px 时不显示按钮
- `div.appendChild(expandBtn)` → `container.appendChild(expandBtn)`：按钮从 div 内部移到外部兄弟元素

---

## 场景验证

| # | 场景 | scrollHeight vs clientHeight | 按钮显示 | 默认折叠 | 点击展开 | 点击收起 | 结果 |
|---|------|------------------------------|---------|---------|---------|---------|------|
| 1 | 内容 1 行（<200px） | scrollHeight <= clientHeight | 不显示 | 不折叠 | N/A | N/A | ✓ |
| 2 | 内容 1 行（刚好 200px） | scrollHeight <= clientHeight | 不显示 | 不折叠 | N/A | N/A | ✓ |
| 3 | 内容超过 200px | scrollHeight > clientHeight | 显示"展开全部" | 折叠到 200px | 展开为全部 | 折叠回 200px | ✓ |
| 4 | 超长内容展开后 | N/A | 显示"收起" | 全部展开 | N/A | 折叠回 200px | ✓ |

---

### Task 1: 修改 CSS

**Files:**
- Modify: `ui/main/windows/assistant/chat.html:848-879`

- [ ] **Step 1: 备份**

```bash
cd /Users/lilei/tools/ai-bot
git add -A && git commit -m "backup: before thinking chain expand/collapse fix"
```

- [ ] **Step 2: 替换 CSS L864-879**

将 L864-879 的 `.thinking-expand-btn` 和 `.message.thinking.expanded .thinking-expand-btn` 规则替换为：

```css
    /* 展开按钮：作为 thinking div 的兄弟元素，不受 overflow-y:auto 裁剪 */
    .thinking-expand-btn {
      display: block;
      width: 100%;
      padding: 4px 0;
      border: none;
      cursor: pointer;
      font-size: 12px;
      color: #40a0a0;
      text-align: center;
      background: linear-gradient(transparent, #faf8f0 70%);
      margin-top: -4px;
    }
```

- [ ] **Step 3: 确认 CSS 语法正确**

### Task 2: 修改 JS

**Files:**
- Modify: `ui/main/windows/assistant/chat.html:2863-2874`

- [ ] **Step 1: 替换 JS L2864-2874**

将 L2864-2874 的 thinking chain 按钮创建逻辑替换为：

```javascript
      // thinking chain：内容超过 200px（max-height 限制）时显示展开按钮
      if (cssClass === 'thinking' && div.scrollHeight > div.clientHeight) {
        const expandBtn = document.createElement('button');
        expandBtn.className = 'thinking-expand-btn';
        expandBtn.textContent = '展开全部';
        expandBtn.addEventListener('click', () => {
          const expanded = div.classList.toggle('expanded');
          expandBtn.textContent = expanded ? '收起' : '展开全部';
        });
        container.appendChild(expandBtn);  // 兄弟元素，不在 overflow-y:auto 的 div 内
      }
```

- [ ] **Step 2: 确认 JS 语法正确**

### Task 3: 提交

- [ ] **Step 1: 提交**

```bash
git add ui/main/windows/assistant/chat.html
git commit -m "fix: thinking chain expand/collapse — only show button when content overflows + move button outside overflow-y:auto container"
```

### Task 4: 端到端验证

- [ ] **Step 1: 启动应用**

```bash
cd /Users/lilei/tools/ai-bot
./niu
```

- [ ] **Step 2: 测试短 thinking chain**

触发子 Agent 输出短 thinking chain（<200px），确认不显示展开按钮。

- [ ] **Step 3: 测试长 thinking chain**

触发子 Agent 输出长 thinking chain（>200px），确认：
- 默认折叠到 200px 高度
- 显示"展开全部"按钮
- 点击展开 → 内容全部展开 → 按钮变"收起"
- 点击收起 → 内容折叠回 200px → 按钮变"展开全部"

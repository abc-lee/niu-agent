# 飞书机器人开通手册

> 本文档从 SYSTEM_MANUAL.md 拆分而来，供主Agent通过浏览器帮用户开通飞书机器人。
> 主Agent拥有 browser-server MCP 工具，可直接操作网页。

## 方式一：扫码快捷开通（推荐）

这是最简单的开通方式，用户只需扫一次二维码，系统自动完成机器人创建、权限开通、事件订阅。

### 步骤1：打开快捷开通页面

主Agent用浏览器打开以下链接：

```
https://open.feishu.cn/page/openclaw?user_code=DDLT-FLXv&from=hermes&tp=hermes
```

页面会显示一个二维码。

**注意**：此链接包含项目专属的 user_code，后续如飞书变更机制，需关注此链接是否仍然有效。如果链接失效，回退到方式二手动开通。

### 步骤2：用户扫码

用户用飞书客户端扫描页面上的二维码。

扫码后飞书自动完成：
- 创建自建应用
- 开通机器人能力
- 配置事件订阅（长连接模式）
- 开通消息收发等核心权限

### 步骤3：获取 App ID 和 App Secret

扫码完成后，页面自动跳转到应用详情页。

- 在页面中找到 **App ID**（格式：`cli_xxx`），复制
- 找到 **App Secret**，点击显示后复制
- 主Agent记录这两个值

### 步骤4：写入配置并重启

主Agent将 app_id 和 app_secret 写入 `~/.niu/preferences.json`：

```json
{
  "feishu": {
    "app_id": "cli_xxx",
    "app_secret": "xxx",
    "enabled": true
  }
}
```

写入方式：读取现有 preferences.json → 合并 feishu 段 → 原子写入（临时文件 + os.replace）。

写入后重启服务，日志中应出现 "Feishu channel starting (WebSocket)" 字样。

**整个流程用户只需扫一次码，其余全由主Agent完成。**

## 方式二：浏览器手动开通（备选）

主Agent通过浏览器帮用户操作飞书开放平台网页，全程用户只需扫码登录+确认发布。

### 步骤1：打开飞书开发者后台

- URL: `https://open.feishu.cn/app`
- 用户需要用飞书客户端扫码登录
- 登录后进入应用列表页面

### 步骤2：创建自建应用

- 点击「创建企业自建应用」按钮
- 应用名称：妞妞 AI 助理（或用户指定的名字）
- 应用描述：个人AI助理，支持消息收发与知识管理
- 点击「创建」按钮

### 步骤3：开启机器人能力

- 进入刚创建的应用详情页
- 左侧导航 →「添加应用能力」
- 找到「机器人」，点击其下方的「添加能力」按钮

### 步骤4：配置事件订阅（长连接模式）

- 左侧导航 →「开发配置」→「事件与回调」→「事件配置」
- 请求方式选择 **长连接（WebSocket）**
- 添加事件：搜索「接收消息」，选择 `im.message.receive_v1`
- 不需要配置 Encrypt Key（SDK 自动处理）
- 不需要配置 Webhook URL（长连接模式无需公网IP）

### 步骤5：配置权限（最小权限集）

- 左侧导航 →「开发配置」→「权限管理」→「API 权限」
- 搜索并开通以下3个核心权限：

| 权限 | scope | 用途 |
|------|-------|------|
| 读取用户发给机器人的单聊消息 | `im:message.p2p_msg:readonly` | 接收私聊消息 |
| 以应用的身份发消息 | `im:message:send_as_bot` | 发送回复 |
| 获取群组中用户@机器人消息 | `im:message.group_at_msg:readonly` | 接收群聊@消息 |

- 可选扩展权限（按需开通）：

| 权限 | scope | 用途 |
|------|-------|------|
| 日历读写 | `calendar:calendar` | 日程管理 |
| 日历只读 | `calendar:calendar:readonly` | 日程查询 |
| 文档读写 | `docx:document` | 文档操作 |
| 云盘读写 | `drive:drive` | 文件管理 |
| 云盘只读 | `drive:drive:readonly` | 文件查询 |
| 通讯录只读 | `contact:user.base:readonly` | 人员查询 |

### 步骤6：获取 App ID 和 App Secret

- 左侧导航 →「基础信息」→「凭证与基础信息」
- 复制 **App ID**（格式：`cli_xxx`）
- 点击 **App Secret** 旁的「显示」按钮，复制 App Secret
- 主Agent记录这两个值

### 步骤7：发布应用

- 左侧导航 →「应用发布」→「版本管理与发布」
- 点击「创建版本」
- 填写版本号（如 1.0.0）和版本描述
- 点击「保存」→「申请线上发布」
- 企业自建应用通常自动通过审核

### 步骤8：写入配置并重启

主Agent将获取的 app_id 和 app_secret 写入 `~/.niu/preferences.json`：

```json
{
  "feishu": {
    "app_id": "cli_xxx",
    "app_secret": "xxx",
    "enabled": true
  }
}
```

写入方式：读取现有 preferences.json → 合并 feishu 段 → 原子写入（临时文件 + os.replace）。

写入后重启服务，日志中应出现 "Feishu channel starting (WebSocket)" 字样。

---

## 方式二：SDK 扫码快捷创建（实验性）

飞书开放平台提供了 SDK 方式的"一键创建智能体应用"，通过 Web 扫码完成创建，自动预置30+权限和事件订阅。

**当前状态**：妞妞尚未集成此 SDK，记录此方式供未来参考。

**未来集成方向**：
1. 集成 `lark-oapi` 的快速创建 SDK
2. 主Agent生成创建链接 → 用户扫码 → 自动获取 app_id/app_secret → 写入配置
3. 文档参考：`https://open.feishu.cn/document/mcp_open_tools/integrating-agents-with-feishu/overview`

**限制**：创建链接10分钟内有效，仅支持一位用户使用。

---

## 故障排查

### 问题：创建应用时提示"没有企业"

需要先注册飞书企业账号：访问 `https://www.feishu.cn/` →「免费试用」→ 注册并创建企业。

### 问题：发布后审核未通过

- 联系企业管理员审核
- 或使用测试版本：左侧导航 →「测试企业和人员」→「创建测试企业」→ 关联应用
- 测试版本权限和配置变更直接生效，无需审核

### 问题：配置后机器人不回复消息

排查步骤：
1. 检查事件订阅是否选择了「长连接」模式（非 Webhook）
2. 检查 `im.message.receive_v1` 事件是否已添加
3. 检查 `~/.niu/preferences.json` 中 `feishu.enabled` 是否为 `true`
4. 检查服务日志中是否有 "Feishu channel starting" 字样
5. 检查 `feishu.app_id` 和 `feishu.app_secret` 是否正确（app_id 格式应为 `cli_xxx`）

### 问题：飞书国际版（Lark）用户

- 开发者后台 URL 改为 `https://open.larksuite.com/app`
- 其余步骤完全相同

---

## 未来可能的变化

1. **快捷开通链接变化**：`user_code` 和页面路径可能随飞书平台升级而变化，主Agent应先尝试快捷链接，如果页面无法打开或二维码不显示，自动回退到方式二手动开通。
2. **飞书开放平台界面改版**：按钮位置和名称可能变化，主Agent应根据页面文本内容灵活定位元素，不要依赖固定的按钮位置
3. **权限名称变化**：飞书可能调整权限 scope 名称或描述，主Agent应根据功能描述搜索对应权限，而非硬编码 scope 名称
4. **长连接模式可能成为默认**：未来飞书可能默认使用长连接，事件订阅步骤可能简化
5. **SDK 扫码方式成熟**：未来集成 SDK 扫码方式后，可实现完全自动化的开通流程，用户只需扫码一步
6. **权限策略变化**：飞书可能收紧或放宽默认权限范围，最小权限集可能需要调整

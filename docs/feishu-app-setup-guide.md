# 飞书应用创建指南

## 1. 创建自建应用

1. 登录 [飞书开放平台](https://open.feishu.cn/app)
2. 点击「创建企业自建应用」
3. 填写应用名称（如"妞妞 AI 助理"）和描述
4. 记录 `App ID` 和 `App Secret`

## 2. 开启机器人能力

1. 进入应用 → 「添加应用能力」→ 勾选「机器人」
2. 在「事件与回调」→「事件配置」中：
   - 请求方式选择 **长连接（WebSocket）**
   - 不需要配置 Encrypt Key（SDK 自动处理）

## 3. 配置权限

在「权限管理」→「API 权限」中开通以下 scope：

| scope | 用途 |
|-------|------|
| `im:message` | 接收消息 |
| `im:message:send_as_bot` | 发送消息 |
| `calendar:calendar` | 日历读写 |
| `calendar:calendar:readonly` | 日历只读 |
| `docx:document` | 文档读写 |
| `drive:drive` | 云盘读写 |
| `drive:drive:readonly` | 云盘只读 |
| `mail:mail` | 邮件读写发送 |
| `contact:user.base:readonly` | 通讯录只读 |

## 4. 发布应用

1. 点击「版本管理与发布」→「创建版本」
2. 提交审核（企业自建应用通常自动通过）
3. 审核通过后，应用即可使用

## 5. 配置妞妞

在 `~/.niu/preferences.json` 中添加：

```json
{
  "feishu": {
    "app_id": "cli_xxx",
    "app_secret": "xxx",
    "enabled": true,
    "sync": {
      "calendar": true,
      "task": true
    }
  }
}
```

重启妞妞即可生效。
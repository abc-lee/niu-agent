# 高德地图 API Key 获取手册

> 本文档从 SYSTEM_MANUAL.md 拆分而来，供主Agent通过浏览器帮用户获取高德地图 API Key。
> 主Agent拥有 browser-server MCP 工具，可直接操作网页。

## 获取步骤

### 步骤1：注册高德开放平台账号

主Agent用浏览器打开：

```
https://lbs.amap.com/
```

- 点击页面右上角「注册」按钮
- 用户用手机号注册账号（用户在屏幕上自行输入手机号和验证码）
- 注册完成后自动登录

### 步骤2：完成开发者认证

- 登录后进入控制台：`https://console.amap.com/`
- 如果提示需要开发者认证，按页面指引完成个人开发者认证
- 用户需输入姓名和身份证号（用户在屏幕上自行输入）

### 步骤3：创建应用并获取 Key

- 在控制台中点击「应用管理」→「我的应用」
- 点击右上角「创建新应用」按钮
  - 应用名称：妞妞 AI 助理（或用户指定的名字）
  - 应用类型：出行（或其他合适类型）
- 创建应用后，在应用下点击「添加 Key」
  - Key 名称：逆地理编码（或任意名称）
  - 服务平台：选择 **Web服务**
  - 其他选项保持默认
- 点击「提交」，系统生成 Key（格式如 `xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`）
- 主Agent复制此 Key

### 步骤4：写入配置

主Agent将 Key 写入 `~/.niu/preferences.json`：

```json
{
  "amap": {
    "api_key": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
  }
}
```

写入方式：读取现有 preferences.json → 合并 amap 段 → 原子写入（临时文件 + os.replace）。

写入后无需重启，下次照片入库时逆地理编码会自动使用高德 API。

**整个流程用户只需输入手机号/验证码/身份证号，其余全由主Agent完成。**

---

## 故障排查

### 问题：API 返回 INVALID_USER_KEY

- 检查 `~/.niu/preferences.json` 中 `amap.api_key` 是否正确复制（无多余空格）
- 检查 Key 的服务平台是否选择了 **Web服务**（不是 Web端(JS API)）

### 问题：API 返回 DAILY_QUERY_OVER_LIMIT

- 高德 Web服务免费额度为每天 5000 次
- 照片入库通常单张操作，不会超限
- 如需更多额度，可在高德控制台中升级配额

### 问题：坐标偏移（位置不准）

- 高德使用 GCJ-02 坐标系，EXIF 中是 WGS-84
- 系统已内置 WGS-84→GCJ-02 自动转换，无需手动处理
- 境外坐标不做偏移（境外直接使用 WGS-84 原值调用高德）

### 问题：境外照片无法获取地名

- 高德逆地理编码主要覆盖中国境内
- 境外照片可能返回空结果，此时照片描述中只显示 GPS 坐标（无地名）

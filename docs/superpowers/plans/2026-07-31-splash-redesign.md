# Splash 启动页美化实现计划

> **日期**: 2026-07-31
> **目标**: 把 Iced splash 从"黑窗口白字"改为贴合主 UI 的暖米白圆角卡片 + 柔和投影
> **约束**: 零新依赖（只用 iced 0.13 已有 API）；兼容缺失依赖时窗口动态增高；多平台兼容（macOS/Windows/Linux）

## 背景与根因

当前 splash（`launcher/src/main.rs`）：
- 窗口：`window::Settings { size: 320×80, decorations: false, transparent: true }`，`.theme(|_| Theme::Dark)`（L2311-2328）
- view：白色文字直接放在根 container 上，根 container 无背景样式（L850-925）
- **黑底根因**：iced_winit 0.13 渲染时用 `Appearance.background_color = theme.palette().background` 做表面清除色（`program.rs:168`），`transparent: true` 不改变清除色 → 深色主题把窗口填满成黑底

关键 API（已在本地 cargo registry 源码确认存在）：
- `.style()` builder（`iced-0.13/src/application.rs:353`）：`impl Fn(&State, &Theme) -> Appearance`，可把清除色改为 `Color::TRANSPARENT`
- `iced_core::Shadow`（`iced_core-0.13/src/shadow.rs`）+ `container::Style.shadow`（`iced_widget-0.13/src/container.rs:584`）：阴影
- `container::Style.border.radius`：圆角
- `iced::Background::Gradient`：卡片微渐变

GitNexus impact（CLI）：`Splash.view` LOW risk，0 依赖方。改动全部在 `launcher/src/main.rs` 单文件内。

## 多平台兼容性（已逐项核实）

| 平台关切 | 结论 |
|---------|------|
| wgpu 透明合成 | `iced_wgpu-0.13/src/window/compositor.rs:121-131` 优先选 PostMultiplied/PreMultiplied alpha mode——macOS Metal（PreMultiplied）与 Windows DX12（PostMultiplied）均支持 DWM/系统合成透明 |
| winit 透明窗口 | 现代码已设 `transparent: true`，平台适配早已存在；本改动只是把清除色从 opaque 改透明 |
| 圆角/阴影/渐变 | 纯 wgpu 矢量绘制，平台无关 |
| CJK 字体 | 已有 cfg 分支（PingFang SC / Microsoft YaHei / Noto Sans CJK SC），不动 |
| 高分屏 | 尺寸均为逻辑像素，iced 自动乘 scale factor |
| 降级风险 | 个别 Linux 无合成器环境下四角可能不透明（显示清除色），可接受；主目标 macOS/Windows 正常 |

## 设计

### 视觉结构

```
window（透明、无边框）
└── 外层 container（Fill，透明背景，padding = SHADOW_MARGIN 四边留白给阴影）
    └── 卡片 container（Fill，暖米渐变背景，radius 16，柔和投影）
        └── 内容（居中）
```

### 常量（新增到 main.rs Splash 区）

| 常量 | 值 | 说明 |
|------|-----|------|
| `CARD_WIDTH` | 340.0 | 卡片宽（比现 320 略宽，呼吸感） |
| `CARD_HEIGHT` | 96.0 | 卡片高（比现 80 略高） |
| `SHADOW_MARGIN` | 20.0 | 阴影留白，blur 24 + offset 8 需要 ~20px |
| 初始窗口尺寸 | `Size::new(380, 136)` = CARD + 2×MARGIN | 替代现 320×80 |

### 配色（贴合主 UI #faf8f0 暖米体系）

| 元素 | 颜色 |
|------|------|
| 卡片背景 | 线性渐变 `#fffdf7`（顶）→ `#f4efe2`（底），竖直方向 |
| 阴影 | rgba(0,0,0,0.22)，offset (0, 8)，blur 24 |
| 主文字（stage/closing） | `#3d3833` 暖深灰 |
| 动画点 "..." | `#d35400` 暖橙（点缀色） |
| 缺失依赖标题 | `#9a6a0a` 深琥珀（原暖黄 [1.0,0.85,0.4] 在浅底上看不清） |
| 缺失依赖条目 | `#6b6357` 暖中灰 |

### 字号

- stage 文字 16（原 18 在卡片里偏大）
- closing 长文案 13（保持）
- 动画点 16，MONOSPACE 不变

## 修改点（全部在 launcher/src/main.rs）

### 1. 新增常量 + Appearance style

- Splash 常量区加 `CARD_WIDTH/CARD_HEIGHT/SHADOW_MARGIN`
- `iced::application(...)` 链上 `.theme(|_| Theme::Dark)` 之后加：
  ```rust
  .style(|_state, _theme| iced::program::Appearance {
      background_color: iced::Color::TRANSPARENT,
      text_color: iced::Color::from_rgb(0.24, 0.22, 0.20),
  })
  ```
  （`iced::program::Appearance` 若不可达则用 `iced::Appearance`，以编译结果为准）

### 2. 初始窗口尺寸

`window::Settings { size: Size::new(320.0, 80.0) }` → `Size::new(CARD_WIDTH + 2.0*SHADOW_MARGIN, CARD_HEIGHT + 2.0*SHADOW_MARGIN)`

### 3. view() 重构

- 文字颜色白 → `#3d3833`；dots 颜色 → `#d35400`；stage 字号 18→16
- 缺失依赖标题色 → `#9a6a0a`，条目色 → `#6b6357`
- 定义卡片样式闭包（渐变背景 + radius 16 + shadow），有/无缺失依赖两分支共用
- 根返回结构：外层透明 container（Fill + padding(SHADOW_MARGIN)）包卡片 container（Fill + style），原内容居中逻辑（含 missing_deps 分支）原样放入卡片内

### 4. 缺失依赖动态增高（WindowOpened，L561-567）

现状：`window::resize(id, Size::new(320.0, 80.0 + extra))`

改为：`window::resize(id, Size::new(CARD_WIDTH + 2.0*SHADOW_MARGIN, CARD_HEIGHT + extra + 2.0*SHADOW_MARGIN))`

extra 计算逻辑（每条 20px + 标题 20px + 内边距）不变——它本来就是按内容高度算的，现在加在卡片高度上再补阴影 margin。

## 验证

1. `./launcher/build.sh`（铁律 8，禁止直接 cargo build）
2. 运行 `./niu`，启动数秒后 `screencapture -x /tmp/splash.png` 截屏，read 图片确认：圆角、阴影、米白卡片、文字清晰、四边透明
3. 截屏后杀掉启动的进程组（niu + Python API + Electron），恢复正常状态
4. 缺失依赖路径：代码审查 resize 数学（本机无缺失依赖，不易现场触发）
5. Windows 平台：本机无法验证，靠 wgpu alpha mode 调研结论 + 代码审查保证；后续 Windows 打包时实测

## 提交

- 一个提交：`feat(launcher): splash 暖米白圆角卡片 + 阴影 + 真透明窗口`

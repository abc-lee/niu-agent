# niu-natives 桌面层 ↔ 上游 omp 对照台账

> 用途：记录 `niu-natives/src/desktop/**` 与上游 `oh-my-pi/crates/pi-natives/src/desktop/**` 的**逐文件关系**与**全部偏差**，
> 使 OMP 升级时的跟版动作可机械重放（re-cp + 重放偏差）。
>
> **上游基线**：`oh-my-pi` @ `883c9507ff`（2026-09-13 核对）。
> 核对方法：`diff <上游文件> <本仓文件>`（逐行），差异行数记录如下表。
> 下表数字按同一基线重跑三态 diff 全表校准（基线 commit 不变，2026-09-13）。

## 1. 文件三态表

| 文件 | 上游行数 | 本仓行数 | 差异行 | 状态 |
|---|---:|---:|---:|---|
| `macos/ax.rs` | 538 | 538 | 0 | **逐字节一致** |
| `macos/capture.rs` | 337 | 337 | 0 | **逐字节一致** |
| `macos/skylight.rs` | 414 | 414 | 0 | **逐字节一致** |
| `win32/ax.rs` | 325 | 325 | 8 | 适配（类 B：let-chain 展平，2026-09-13） |
| `win32/capture.rs` | 328 | 328 | 0 | **逐字节一致** |
| `win32/delivery.rs` | 156 | 156 | 0 | **逐字节一致** |
| `ax.rs` | 802 | 802 | 6 | 适配（类 A：符号改名） |
| `keys.rs` | 320 | 320 | 8 | 适配（类 B：let-chain 展平） |
| `macos/input.rs` | 1008 | 1008 | 8 | 适配（类 B） |
| `win32/input.rs` | 865 | 865 | 48 | 适配（类 B：6 处 let-chain 展平；`raise_window` 已照抄上游补回，2026-09-13） |
| `backend.rs` | 128 | 128 | 0 | **逐字节一致**（`raise_window` 已补回，2026-09-13） |
| `macos/mod.rs` | 150 | 155 | 5 | 适配（类 D：process_type；`raise_window` 与 objc2 import 已补回，2026-09-13） |
| `win32/mod.rs` | 119 | 119 | 0 | **逐字节一致**（`raise_window` 已补回，2026-09-13） |
| `error.rs` | 124 | 131 | 11 | 适配（类 A：napi→PyO3） |
| `frame.rs` | 289 | 714 | 485 | 适配（类 D：region 裁剪 + wire） |
| `types.rs` | 206 | 573 | 423 | 适配（类 D：wire 结构） |
| `mod.rs` | 1222 | 1577 | 969 | 适配（类 A 为主 + 类 D；`RaiseWindow` 变体/处理 arm/导出方法已补回，2026-09-13） |
| `macos/process_type.rs` | — | 36 | — | **本仓新增**（类 D） |
| `linux/**`（9 文件） | 3809 | — | — | **有意排除**（类 C） |

## 2. 偏差全集（4 类，共 8 项）

### 类 A：napi → PyO3（绑定层，必需，机械可重放）

| # | 位置 | 上游 | 本仓 | 说明 |
|---|---|---|---|---|
| A1 | `error.rs:118` | `impl From<DesktopError> for napi::Error` → `Error::from_reason(error.to_string())` | `impl From<DesktopError> for PyErr` → `PyRuntimeError::new_err(error.to_string())` | 保持 `{code}: {message}` 前缀不变，Python 侧可分段 |
| A2 | `ax.rs:269/411/429` | `fn node_to_napi(...)` | `fn node_from_props(...)`（3 处调用点同步） | 纯改名 |
| A3 | `mod.rs` 全文 | `#[napi]` / `napi_derive` / `task::blocking` / `Uint8Array` / `Reply` 通道 | `#[pymethods]` / `pyo3` / 同步直调 / `Vec<u8>` / `py.detach` | 本仓 MCP 为同进程同步调用；**重放方式 = 重新执行当初的 PyO3 化变换，逐块核对** |

### 类 B：Rust edition 2021 兼容（机械可重放）

上游用 `edition 2024`（`Cargo.toml`），let-chain（`if let … && cond`）在本仓 `edition = "2021"` 下不合法。

| # | 位置 | 已处理 |
|---|---|---|
| B1 | `keys.rs:245` | ✅ 已展平为嵌套 `if` |
| B2 | `macos/input.rs:690` | ✅ 已展平 |
| B3 | `win32/input.rs:62/159/515/545/748/785` | ✅ 6 处已展平 |
| B4 | `win32/ax.rs:317–319` | ✅ 已展平为嵌套 `if`（2026-09-13）；`cargo check --target x86_64-pc-windows-msvc` 通过，门禁命令零命中，**类 B 无遗留** |

**重放规则**：上游更新后，对 `niuu-natives` 内所有 `if let … \n && …` 形态统一展平；
门禁命令（跨行匹配，单行 grep 抓不到）：
```bash
grep -rn -Pzo 'if let [^\n]*\n\s*&&' niu-natives/src | tr '\0' '\n'
```

### 类 C：有意裁剪（能力缺口，需按需补回）

| # | 位置 | 内容 | 后果 / 状态 |
|---|---|---|---|
| C1 | `mod.rs` / `backend.rs:101` / `macos/mod.rs` / `win32/mod.rs` / `win32/input.rs` | （曾）删除 `Request::RaiseWindow` 与 `Backend::raise_window`（macOS 侧含 `NSRunningApplication.activateWithOptions` 激活窗口、Windows 侧含 `ShowWindow(SW_RESTORE)` + `SetForegroundWindow`） | **更正**：裁剪只影响 `.raise()`，**不影响** `delivery:"foreground"`——macOS foreground 投递走 `skylight.rs` 自带的激活/恢复焦点链（与上游逐字节一致）。**2026-09-13 已照抄上游补回全部落点**（含 `macos/mod.rs` 的 `NSRunningApplication`/`NSApplicationActivationOptions` import）并接线 `agent/computer/objects.py:Win.raise_()` → **本项关闭，类 C 仅剩 C2** |
| C2 | `mod.rs` | 删除 `mod linux;` 与 `create_backend` 的 linux 分支（上游 9 文件 3809 行） | Linux 平台不支持（已决：本仓范围 = macOS + Windows） |

### 类 D：本仓新增（上游没有；跟版时必须保留）

| # | 位置 | 内容 | 来源 |
|---|---|---|---|
| D1 | `types.rs`：`CaptureRegion`/`GeometryWire`/`GeometryKindWire`/`RegionWire`；`frame.rs`：`crop_to_logical_region`；`mod.rs`：`Request::Capture{region}` + 仅 `Target::Desktop` 允许 + `to_wire()` | 逻辑桌面区域裁剪（`screenshot(region_ratio=[…])`） | 用户 Phase 3 需求；**上游无对应实现**（`grep` 零命中）→ `frame.rs` 289→714、`types.rs` 206→573 的增长几乎全来自此。**语义约束（2026-09-13 用户拍板 fail-loud，跟版勿删）**：region 裁剪仅用于看图——捕获路径中 **region 捕获使该 target 的整屏帧失效**（`frames.remove`），整屏捕获才写帧；裁剪图是不同视口，其像素不是该 target 的有效指针坐标，若保留旧整屏帧会让模型拿裁剪图像素静默点错，故失效后坐标输入报 `InvalidCoordinateFrame`（提示先整屏截图）直至下一次整屏捕获。测试锚点：`mod.rs` capture_tests `region_capture_invalidates_fullscreen_frame` |
| D2 | `macos/process_type.rs` + `macos/mod.rs:4,7-8` | `demote_to_background_only()`（`TransformProcessType` 降级），在 `DesktopSession::new` 早期调用 | 修 macOS Dock 出现 Python 火箭图标；**上游无对应**（上游宿主是原生进程，无此问题） |

## 3. 上游已有、本仓因裁剪而未暴露的能力

| 能力 | 上游位置 | 本仓状态 |
|---|---|---|
| `elementAt(x, y)` 全局坐标取元素 | `mod.rs:440`（`ax::element_at_node`）、`:941`（`ax_element_at`） | 原生层**已移植**，MCP 层未暴露 |
| `focusedElement()` | `mod.rs:443` | 同上 |
| 缺帧坐标输入拒绝 | `mod.rs:257`（`InvalidCoordinateFrame`） | 已移植 |
| 每 target 单帧缓存 | `mod.rs:228`（`frames: HashMap<String, FrameGeometry>`，key = `Target::key()`） | 已移植 |
| `.raise()` | `mod.rs::raise_window`、`macos/mod.rs`、`win32/input.rs::raise_window` | **已补回**（2026-09-13，C1 关闭）；PyO3 方法 `DesktopSession.raise_window(window_id)`，Python 侧 `Win.raise_()` 已接线 |
| `delivery:"foreground"` | macOS：`skylight.rs` 激活链；Windows：`win32/input.rs` foreground 子模块 | **从未受 C1 影响**（原台账误记，2026-09-13 更正） |
| `read_only` 只读通道 | `computer.ts:52-58`（审批分级） | 无审批门（本仓不做审批） |
| 错误码全集（14） | `error.rs:24-37` | 已移植；MCP 层仅 3 码有中文映射 |

## 4. 跟版流程（OMP 升级时执行）

```bash
# 0. 立项前提：新基线 commit 已知（本台账 §1 表头即基线）
UP=/path/to/oh-my-pi/crates/pi-natives/src/desktop
NI=niu-natives/src/desktop
# 1. 逐文件三态报告（0 = 可整文件 cp 覆盖）
for f in $(cd $UP && find . -name '*.rs' | sed 's|^\./||'); do
  [ -f "$NI/$f" ] && echo "$(diff "$UP/$f" "$NI/$f" | grep -c '^[<>]')	$f" || echo "NEW	$f"
done
# 2. 按类重放：
#    - 状态=0 的文件：直接 cp 上游覆盖
#    - 全部文件：重放类 A（PyO3 化）与类 B（let-chain 展平）
#    - 类 C 的裁剪点：确认上游是否新增了依赖 raise/linux 的调用，按需补
#    - 类 D 的两处新增：确认上游是否已原生支持 region 裁剪或进程降级（若已支持则删本仓自造、改用上游）
# 3. 验收：cargo test（本仓）+ build.sh 装机 + 真机走通（见手册「桌面操作」节）
```

**判定原则**：能整文件 cp 的一律 cp（不手改）；只有类 A/B/C/D 允许偏离，且必须在 §2 登记新条目。

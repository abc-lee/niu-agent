//! Niu Launcher - Rust 启动器
//!
//! Windows release build 下编译为 GUI 子系统（不弹 cmd 窗口）。
//! debug build 保留 console 方便调试。macOS/Linux 不受影响。

#![cfg_attr(all(target_os = "windows", not(debug_assertions)), windows_subsystem = "windows")]

use std::env;
use std::fs;
use std::io::{BufRead, BufReader};
use std::path::Path;
use std::path::PathBuf;
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::Receiver;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

#[cfg(windows)]
use std::os::windows::process::CommandExt;

use clap::Parser;
use iced::widget::container;
use iced::window;
use iced::{Element, Font, Length, Subscription, Task, Theme};
use serde::Deserialize;
use tracing::{debug, error, info, warn};

// ---------------------------------------------------------------------------
// Splash — iced splash window shown during startup
// ---------------------------------------------------------------------------

/// CJK font for Chinese text display
/// macOS: "PingFang SC" (system default CJK font)
/// Windows: "Microsoft YaHei" (common CJK font)
#[cfg(target_os = "macos")]
const CJK_FONT: Font = Font::with_name("PingFang SC");

#[cfg(target_os = "windows")]
const CJK_FONT: Font = Font::with_name("Microsoft YaHei");

#[cfg(not(any(target_os = "macos", target_os = "windows")))]
const CJK_FONT: Font = Font::with_name("Noto Sans CJK SC");

// Splash 卡片视觉常量：暖米白圆角卡片 + 柔和投影 + 真透明窗口。
// 窗口实际尺寸 = 卡片 + 四边 SHADOW_MARGIN 留白（容纳 blur 24 + offset 8 的阴影）。
const CARD_WIDTH: f32 = 340.0;
const CARD_HEIGHT: f32 = 96.0;
const SHADOW_MARGIN: f32 = 20.0;

/// LightRAG status reported by `/api/kg/stats`.
/// Only the fields the launcher needs for alerting are decoded.
/// NOTE: API returns snake_case field names (Python convention), so we do NOT
/// use serde(rename_all = "camelCase") here. Using camelCase would make serde
/// expect `initFailed` / `totalErrors` while the API sends `init_failed` /
/// `total_errors`, causing "missing field" decode errors that silently break
/// the corruption-detection dialog flow.
#[derive(Debug, Clone, Deserialize)]
struct LightragStatus {
    init_failed: bool,
    #[allow(dead_code)]
    init_retry_in_seconds: Option<f64>,
    integrity: Option<IntegrityStatus>,
}

/// LightRAG data integrity summary reported by `/api/kg/stats`.
///
/// 字段策略（v4）：
/// - `total_errors`（必填，无 `#[serde(default)]`）：Python get_lightrag_status
///   必须返回此字段（= critical + major + minor 之和），删了会 "missing field"
///   decode 错误。StatusCheckResult 用它生成弹窗文案。
/// - `critical_errors`/`major_errors`/`minor_errors`（可选，`#[serde(default)]`）：
///   新版 Python 返回的三级 severity 字段。format_repair_summary 用它们
///   区分"残留 N 个严重、M 个主要错误"。旧版 Python 缺这些字段时默认 0。
///
/// 注意：字段名必须是 snake_case（Python 约定），不能用
/// `#[serde(rename_all = "camelCase")]`，否则 serde 会期望
/// `totalErrors`/`criticalErrors` 而 API 返回 `total_errors`/`critical_errors`，
/// 导致 "missing field" decode 错误。
#[derive(Debug, Clone, Deserialize)]
struct IntegrityStatus {
    ok: bool,
    total_errors: i32,
    #[serde(default)]
    critical_errors: i32,
    #[serde(default)]
    major_errors: i32,
    #[serde(default)]
    minor_errors: i32,
}

/// Splash window state
struct Splash {
    /// Receiver for the "ready" signal from the launcher background thread.
    /// Wrapped in Mutex for Sync compatibility with iced's runtime.
    ready_rx: Mutex<Receiver<()>>,
    /// Window ID captured from window open event
    window_id: Option<window::Id>,
    /// Whether Dock icon has been hidden
    dock_hidden: bool,
    /// Animation frame counter for the "..." dots (0..3 cycles)
    dot_frame: u8,
    /// Whether the one-shot LightRAG status check has been dispatched.
    /// Prevents repeated queries while the splash is polling the ready channel.
    status_checked: bool,
    /// Whether the Python API has reported ready at least once.
    /// Status check only fires after the API is reachable so that
    /// `/api/kg/stats` does not 404 during early boot.
    niu_api_ready: bool,
    /// Whether the LightRAG status check has completed AND the data is healthy.
    /// Splash close is gated on this flag to avoid a timing race where the
    /// ready signal arrives while the status check (or rfd dialog) is still
    /// in flight — closing the splash would drop the StatusCheckResult future
    /// and the user would never see the corruption dialog.
    /// Flipped to true only when status returns healthy; stays false while
    /// the rfd dialog is open or repair is in progress.
    status_check_completed: bool,
    /// Cached "ready signal received" flag.
    /// try_recv() consumes the ready signal from the channel, but we cannot
    /// close the splash until status_check_completed is also true. So we
    /// cache the consumed ready signal here and gate close on
    /// `ready_signal_seen && status_check_completed`.
    ready_signal_seen: bool,
    /// Python API port, used to send /api/shutdown when exiting due to
    /// LightRAG data corruption. Set at construction time from Args::port.
    api_port: u16,
    /// Shared cancellation flag. When ExitApp fires we flip this so the
    /// background thread breaks out of its monitor loop and tears down the
    /// Python API child process (HTTP shutdown -> SIGTERM -> SIGKILL).
    cancelled: Arc<AtomicBool>,
    /// Shared "integrity failed" flag. Set true the moment the status check
    /// reports corruption so the background thread does NOT proceed to
    /// launch the assistant window. Without this the bg thread would
    /// unconditionally launch the main UI even while the rfd dialog is
    /// still on screen.
    integrity_failed: Arc<AtomicBool>,
    /// Whether a repair is in progress (UserDialogChoice(true) fired).
    /// Drives the splash view to show "正在修复..." text instead of the
    /// default "正在启动..." animation.
    repairing: bool,
    /// Whether the launcher has entered the "closing" phase.
    /// Flipped to true after the settings test passes (or settings window
    /// is closed) so the splash stays on screen showing
    /// "正在关闭所有进程，关闭后请重新启动程序" while the background
    /// thread tears down the Python API child (HTTP shutdown ->
    /// SIGTERM -> SIGKILL). Stays visible until `cleanup_done` arrives.
    closing: bool,
    /// Receiver for lifecycle signals from the background thread:
    /// - `SplashPhase::Closing` -> enter closing state
    /// - `SplashPhase::CleanupDone` -> all processes reaped, call iced::exit()
    /// Wrapped in Mutex for Sync compatibility with iced's runtime.
    phase_rx: Mutex<Receiver<SplashPhase>>,
    /// Missing optional dependencies detected at startup (license-excluded
    /// packages like cv2/insightface/pillow_heif/igraph/leidenalg + buffalo_l
    /// model). Empty = all present. Shown on splash as a hint to read README.
    missing_deps: Vec<String>,
    /// Receiver for startup stage text from the preload-status polling thread.
    /// Wrapped in Mutex for Sync compatibility with iced's runtime.
    stage_rx: Mutex<Receiver<String>>,
    /// Current startup stage text, updated from preload-status polling.
    /// Displayed instead of the static "正在启动" label.
    stage: String,
}

/// Lifecycle signals sent from the launcher background thread to the splash
/// window after the settings flow completes.
#[derive(Debug, Clone, Copy)]
enum SplashPhase {
    /// Enter the "closing" phase — show the shutdown message.
    Closing,
    /// All child processes have been reaped — exit the application.
    CleanupDone,
}

#[derive(Debug, Clone)]
enum SplashMessage {
    /// Periodic tick — check if the launcher is ready
    Tick,
    /// Window opened — capture the window ID
    WindowOpened(window::Id),
    /// First tick after window opened — hide Dock icon
    HideDockIcon,
    /// /health probe succeeded — API is reachable, fire status check.
    /// Dispatched from a background thread to avoid blocking the UI thread.
    ApiReady,
    /// Result of the one-shot `/api/kg/stats` query dispatched at startup.
    StatusCheckResult(Result<LightragStatus, String>),
    /// User's choice from the rfd native dialog (true=try repair, false=exit).
    UserDialogChoice(bool),
    /// Result of the repair API call.
    RepairResult(Result<String, String>),
    /// Exit the application (user chose "No" in the native dialog).
    ExitApp,
}

/// 把 /api/kg/lightrag/repair 的响应文本格式化成弹窗摘要。
///
/// 响应格式：{"status":"ok","result":{"repaired":bool,"check_ok":bool,
///   "repair_result":{"vdb_entities.json":{"status":"ok","rebuilt_count":5,...},...}}}
///
/// 列出每个 vdb 的 status/message/rebuilt_count，并提示用户程序将退出。
/// 解析失败时退回原始文本（截断到 500 字符避免超长）。
///
/// vdb_relationships.json 走截断修复（source=vdb_truncate_repair）时，
/// 追加数据丢失风险提示（断点后的 relationship 永久丢失，GraphML 有但 vdb 无，
/// 当前无 check_relationship_sync 检测、无 GraphML 反向补齐）。
fn format_repair_summary(resp_text: &str) -> (String, rfd::MessageLevel, String) {
    // 返回 (title, level, body)：
    // - has_unrecoverable → "修复失败（不可恢复）" / Error
    // - repaired && minor_errors == 0 → "修复成功" / Info
    // - repaired && minor_errors > 0 → "修复完成（有警告）" / Warning
    // - !repaired → "修复失败" / Error（区分 critical/major 残留）
    // 区分标题和图标，避免后端部分失败时还显示绿色 Info 误导用户。
    match serde_json::from_str::<serde_json::Value>(resp_text) {
        Ok(v) => {
            let result = v.get("result");
            let repaired = result
                .and_then(|r| r.get("repaired"))
                .and_then(|b| b.as_bool())
                .unwrap_or(false);
            let critical_errors = result
                .and_then(|r| r.get("critical_errors"))
                .and_then(|c| c.as_u64())
                .unwrap_or(0);
            let major_errors = result
                .and_then(|r| r.get("major_errors"))
                .and_then(|m| m.as_u64())
                .unwrap_or(0);
            let minor_errors = result
                .and_then(|r| r.get("minor_errors"))
                .and_then(|m| m.as_u64())
                .unwrap_or(0);

            // 检测是否存在不可恢复项：
            // 1) 顶层 _unrecoverable（Python 端 run_repair_on_user_request 显式提升，
            //    覆盖 GraphML 损坏导致 repair_all 在备份/删除阶段就 return 的情况，
            //    此时 repair_result 无重建子项，只有顶层 _unrecoverable）
            // 2) repair_result 子项的 unrecoverable: true（向后兼容，逐项遍历）
            let top_unrecoverable = result
                .and_then(|r| r.get("_unrecoverable"))
                .and_then(|b| b.as_bool())
                .unwrap_or(false);
            let has_unrecoverable = top_unrecoverable
                || result
                    .and_then(|r| r.get("repair_result"))
                    .and_then(|r| r.as_object())
                    .map(|obj| {
                        obj.values()
                            .any(|d| d.get("unrecoverable").and_then(|b| b.as_bool()).unwrap_or(false))
                    })
                    .unwrap_or(false);

            let (title, level, overall) = if has_unrecoverable {
                (
                    "修复失败（不可恢复）".to_string(),
                    rfd::MessageLevel::Error,
                    "部分数据不可恢复，无法从其他文件重建。详见下方清单。".to_string(),
                )
            } else if repaired && minor_errors == 0 {
                (
                    "修复成功".to_string(),
                    rfd::MessageLevel::Info,
                    "修复成功，所有数据一致性检查通过。".to_string(),
                )
            } else if repaired && minor_errors > 0 {
                (
                    "修复完成（有警告）".to_string(),
                    rfd::MessageLevel::Warning,
                    format!(
                        "修复完成，但仍有 {} 个次要警告，不影响使用，详见下方清单。",
                        minor_errors
                    ),
                )
            } else {
                // 修复失败：区分 critical/major 残留
                let residual = if critical_errors > 0 {
                    format!("残留 {} 个严重、{} 个主要错误", critical_errors, major_errors)
                } else if major_errors > 0 {
                    format!("残留 {} 个主要错误", major_errors)
                } else {
                    "部分项目失败".to_string()
                };
                (
                    "修复失败".to_string(),
                    rfd::MessageLevel::Error,
                    format!("修复未全部成功，{}（详见下方清单）。", residual),
                )
            };

            let mut lines: Vec<String> = vec![overall, String::new()];

            if let Some(repair_result) = result
                .and_then(|r| r.get("repair_result"))
                .and_then(|r| r.as_object())
            {
                lines.push("修复清单：".to_string());
                for (name, detail) in repair_result {
                    let status = detail
                        .get("status")
                        .and_then(|s| s.as_str())
                        .unwrap_or("unknown");
                    let message = detail
                        .get("message")
                        .and_then(|m| m.as_str())
                        .unwrap_or("");
                    let rebuilt_count = detail
                        .get("rebuilt_count")
                        .and_then(|c| c.as_u64());
                    let source = detail
                        .get("source")
                        .and_then(|s| s.as_str())
                        .unwrap_or("");
                    let expected = detail.get("expected").and_then(|e| e.as_u64());
                    let actual = detail.get("actual").and_then(|a| a.as_u64());
                    let lost = detail.get("lost").and_then(|l| l.as_u64()).unwrap_or(0);
                    let unrecoverable = detail
                        .get("unrecoverable")
                        .and_then(|b| b.as_bool())
                        .unwrap_or(false);

                    let status_marker = if status == "ok" { "成功" } else { "失败" };

                    // 优先用 expected/actual/lost 展示数量差额，其次用 message
                    let detail_text = if let (Some(exp), Some(act)) = (expected, actual) {
                        let lost_marker = if lost > 0 { " ⚠️" } else { "" };
                        format!(
                            "应 {} / 实 {} / 丢失 {}{}",
                            exp, act, lost, lost_marker
                        )
                    } else if !message.is_empty() {
                        message.to_string()
                    } else {
                        status.to_string()
                    };

                    let mut line = format!("  {} [{}]: {}", name, status_marker, detail_text);
                    if let Some(cnt) = rebuilt_count {
                        line.push_str(&format!("（重建 {} 条）", cnt));
                    }
                    lines.push(line);

                    // vdb_relationships.json 走截断修复时，追加数据丢失风险提示
                    // （断点后的 relationship 永久丢失，GraphML 有但 vdb 重建后缺失，
                    //  当前无 check_relationship_sync 检测、无 GraphML 反向补齐）
                    if name == "vdb_relationships.json"
                        && status == "ok"
                        && source == "vdb_truncate_repair"
                    {
                        lines.push(
                            "    注意：截断修复可能丢失部分关系数据（GraphML 有但 vdb 重建后缺失），详情见日志".to_string()
                        );
                    }

                    // 不可恢复项加显著提示
                    if unrecoverable {
                        lines.push(
                            "    ⛔ 不可恢复：建议从原始文档重新 ingest".to_string()
                        );
                    }
                }
            }

            lines.push(String::new());
            lines.push("确定后程序将完全退出，请重新启动程序。".to_string());

            (title, level, lines.join("\n"))
        }
        Err(_) => {
            // JSON 解析失败：退回原始文本（截断到 500 字符避免超长）
            // 用 chars().take() 而非字节切片，避免 UTF-8 多字节字符边界 panic
            let truncated: String = resp_text.chars().take(500).collect();
            let truncated = if resp_text.chars().count() > 500 {
                format!("{}...(已截断)", truncated)
            } else {
                truncated
            };
            let body = format!(
                "修复已完成（无法解析详细结果）。\n\n原始响应：\n{}\n\n确定后程序将完全退出，请重新启动程序。",
                truncated
            );
            ("修复结果（无法解析）".to_string(), rfd::MessageLevel::Warning, body)
        }
    }
}

impl Splash {
    fn new(
        ready_rx: Receiver<()>,
        api_port: u16,
        cancelled: Arc<AtomicBool>,
        integrity_failed: Arc<AtomicBool>,
        phase_rx: Receiver<SplashPhase>,
        missing_deps: Vec<String>,
        stage_rx: Receiver<String>,
    ) -> Self {
        Self {
            ready_rx: Mutex::new(ready_rx),
            window_id: None,
            dock_hidden: false,
            dot_frame: 0,
            status_checked: false,
            niu_api_ready: false,
            status_check_completed: false,
            ready_signal_seen: false,
            api_port,
            cancelled,
            integrity_failed,
            repairing: false,
            closing: false,
            phase_rx: Mutex::new(phase_rx),
            missing_deps,
            stage_rx: Mutex::new(stage_rx),
            stage: "正在启动服务".to_string(),
        }
    }

    fn update(&mut self, message: SplashMessage) -> Task<SplashMessage> {
        match message {
            SplashMessage::Tick => {
                // Advance animation frame
                self.dot_frame = (self.dot_frame + 1) % 30;
                // On first tick after window is open, hide Dock icon
                if !self.dock_hidden && self.window_id.is_some() {
                    self.dock_hidden = true;
                    return Task::done(SplashMessage::HideDockIcon);
                }

                // Probe Python API readiness on a background thread (non-blocking).
                // 目的：API /health 能响应就立即触发 status check，不等 preload
                // 全完成（preload 加载 embedding ~9s + MCP ~5s，用户不用等）。
                // 不能在主线程做同步 HTTP——60fps 每帧发请求会卡死 UI 动画。
                // 节流：每 15 帧（约 250ms）才 spawn 一次 probe，避免线程爆炸。
                if !self.niu_api_ready && self.dot_frame % 15 == 0 {
                    let port = self.api_port;
                    let (tx, rx) = iced::futures::channel::oneshot::channel::<bool>();
                    std::thread::spawn(move || {
                        let ok = reqwest::blocking::Client::builder()
                            .timeout(Duration::from_millis(500))
                            .build()
                            .map(|client| {
                                let url = format!("http://127.0.0.1:{}/health", port);
                                client
                                    .get(&url)
                                    .send()
                                    .map(|resp| resp.status().is_success())
                                    .unwrap_or(false)
                            })
                            .unwrap_or(false);
                        let _ = tx.send(ok);
                    });
                    return Task::perform(
                        async move { rx.await.unwrap_or(false) },
                        |ok| {
                            if ok {
                                SplashMessage::ApiReady
                            } else {
                                // 用 Tick 占位——不影响逻辑，只是让 Task::perform 有消息可发
                                SplashMessage::Tick
                            }
                        },
                    );
                }

                // One-shot LightRAG status check: dispatch as soon as the API
                // is reachable. Run the blocking request on a std thread and
                // bridge back to the iced runtime via iced::futures::oneshot
                // so we do not stall the UI thread.
                if !self.status_checked && self.niu_api_ready {
                    self.status_checked = true;
                    let port = self.api_port;
                    let (tx, rx) = iced::futures::channel::oneshot::channel::<Result<LightragStatus, String>>();
                    std::thread::spawn(move || {
                        let result = reqwest::blocking::Client::builder()
                            .timeout(Duration::from_secs(2))
                            .build()
                            .map_err(|e| e.to_string())
                            .and_then(|client| {
                                let url = format!("http://127.0.0.1:{}/api/kg/stats", port);
                                client
                                    .get(&url)
                                    .send()
                                    .map_err(|e| e.to_string())
                                    .and_then(|resp| {
                                        resp.json::<LightragStatus>().map_err(|e| e.to_string())
                                    })
                            });
                        let _ = tx.send(result);
                    });
                    return Task::perform(
                        async move { rx.await.unwrap_or(Err("channel closed".into())) },
                        SplashMessage::StatusCheckResult,
                    );
                }

                // Poll the phase channel for lifecycle signals from the background
                // thread. SplashPhase::Closing flips `closing=true` (entered after
                // the settings test passes); SplashPhase::CleanupDone triggers
                // iced::exit() once cleanup finishes. Drained greedily so we never
                // miss a signal due to Tick scheduling.
                loop {
                    match self.phase_rx.lock().unwrap().try_recv() {
                        Ok(SplashPhase::Closing) => {
                            self.closing = true;
                        }
                        Ok(SplashPhase::CleanupDone) => {
                            // Cleanup finished — exit immediately regardless of
                            // whether we saw Closing first (defensive).
                            return iced::exit();
                        }
                        Err(std::sync::mpsc::TryRecvError::Empty) => break,
                        Err(std::sync::mpsc::TryRecvError::Disconnected) => {
                            // Background thread dropped the sender. If we are in the
                            // closing phase, exit to avoid hanging; otherwise ignore.
                            if self.closing {
                                return iced::exit();
                            }
                            break;
                        }
                    }
                }
                // Drain any pending stage updates from the preload-status polling
                // thread. Keeps the latest stage text; empties the channel each tick.
                while let Ok(stage) = self.stage_rx.lock().unwrap().try_recv() {
                    self.stage = stage;
                }

                // Non-blocking check: if the launcher thread sent the ready signal,
                // AND the LightRAG status check has completed (healthy), close the window.
                // Gating on status_check_completed avoids a timing race where the ready
                // signal arrives while the status check / rfd dialog is still in flight:
                // closing the splash would cancel the StatusCheckResult future and the
                // user would never see the corruption dialog.
                // try_recv() consumes the signal; cache it in ready_signal_seen so we
                // can still decide to close once status_check_completed flips later.
                // NOTE: when `closing` is true (settings flow has completed and we are
                // waiting for child process cleanup), the splash MUST stay open to show
                // the "正在关闭所有进程" message — skip the ready-signal close path.
                if !self.closing {
                    if !self.ready_signal_seen {
                        if self.ready_rx.lock().unwrap().try_recv().is_ok() {
                            self.ready_signal_seen = true;
                        }
                    }
                    if self.ready_signal_seen && self.status_check_completed {
                        // 检测完成且健康，可以关 splash
                        if let Some(id) = self.window_id {
                            return window::close(id);
                        } else {
                            // Fallback: get the oldest window ID and close it
                            return window::get_oldest().then(|oldest_id| {
                                if let Some(id) = oldest_id {
                                    window::close::<SplashMessage>(id)
                                } else {
                                    Task::none()
                                }
                            });
                        }
                    }
                }

                Task::none()
            }
            SplashMessage::WindowOpened(id) => {
                // Capture the window ID when the window opens
                self.window_id = Some(id);
                // 窗口打开后，如有缺失依赖提示，动态加大窗口高度容纳列表
                if !self.missing_deps.is_empty() {
                    // 每条缺失项约 20px 高度 + 标题 20px + 内边距，加在卡片高度上再补四边阴影留白
                    let extra = (self.missing_deps.len() as f32 + 1.0) * 22.0;
                    let new_height = CARD_HEIGHT + extra + 2.0 * SHADOW_MARGIN;
                    return window::resize(id, iced::Size::new(CARD_WIDTH + 2.0 * SHADOW_MARGIN, new_height));
                }
                Task::none()
            }
            SplashMessage::ApiReady => {
                // /health probe 成功 —— API 可达，立即触发 status check。
                // 这个消息由 Tick 节流 spawn 的后台线程通过 oneshot 推回，
                // 避免在主线程做同步 HTTP 阻塞 UI 动画。
                self.niu_api_ready = true;
                // 不直接调 status check —— 让下一个 Tick 走
                // `if !self.status_checked && self.niu_api_ready` 分支触发。
                // 这样保持单一触发路径，避免重复 dispatch。
                Task::none()
            }
            SplashMessage::HideDockIcon => {
                // winit overrides activation policy during EventLoop init.
                // Re-set to Accessory after the window is created to hide the Dock icon.
                #[cfg(target_os = "macos")]
                {
                    use std::ffi::c_void;
                    extern "C" {
                        fn objc_getClass(name: *const u8) -> *mut c_void;
                        fn sel_registerName(name: *const u8) -> *mut c_void;
                        fn objc_msgSend(obj: *mut c_void, sel: *mut c_void, ...) -> *mut c_void;
                    }
                    unsafe {
                        let nsapp_class = objc_getClass("NSApplication\0".as_ptr());
                        let shared_sel = sel_registerName("sharedApplication\0".as_ptr());
                        let app = objc_msgSend(nsapp_class, shared_sel);
                        let set_policy_sel = sel_registerName("setActivationPolicy:\0".as_ptr());
                        objc_msgSend(app, set_policy_sel, 1i64);
                        // Accessory policy 应用不会自动成为 frontmost，必须显式
                        // activateIgnoringOtherApps:YES 才能让窗口显示到前台。
                        let activate_sel = sel_registerName("activateIgnoringOtherApps:\0".as_ptr());
                        objc_msgSend(app, activate_sel, true as u8 as std::ffi::c_uint);
                    }
                }
                Task::none()
            }
            SplashMessage::StatusCheckResult(result) => {
                match result {
                    Ok(status) => {
                        if status.init_failed
                            || status.integrity.as_ref().map_or(false, |i| !i.ok)
                        {
                            // 检测到损坏：立即设 integrity_failed=true，阻止后台线程
                            // 继续启动主 UI 窗口（assistant）。后台线程在
                            // `splash_tx.send(())` 之前会检查此标志，若为 true
                            // 则不发 ready 信号、不启动 assistant 窗口，直接
                            // 进入 cancelled cleanup loop。
                            self.integrity_failed.store(true, Ordering::SeqCst);
                            // v4: 优先读 total_errors（Python 必填字段，无
                            // #[serde(default)]），但用 critical + major + minor
                            // 求和做 fallback——如果 Python 未来误删 total_errors
                            // 或字段值不一致，用更细粒度的 sum 防止弹窗显示 0。
                            let total_errors = status
                                .integrity
                                .as_ref()
                                .map_or(0, |i| {
                                    let sum =
                                        i.critical_errors + i.major_errors + i.minor_errors;
                                    if i.total_errors != sum && sum > 0 {
                                        warn!(
                                            "integrity total_errors({}) != critical+major+minor({}), using sum",
                                            i.total_errors, sum
                                        );
                                        sum
                                    } else {
                                        i.total_errors
                                    }
                                });
                            let message = if status.init_failed {
                                format!(
                                    "LightRAG 初始化失败\n\n检测到数据损坏，请选择：\n\n是 - 尝试修复（修复未必成功，可能会丢失数据）\n否 - 直接退出（请自行从备份恢复）"
                                )
                            } else {
                                format!(
                                    "检测到 {} 个数据一致性问题\n\n请选择：\n\n是 - 尝试修复（修复未必成功，可能会丢失数据）\n否 - 直接退出（请自行从备份恢复）",
                                    total_errors
                                )
                            };
                            // 弹 rfd 原生对话框（独立线程，避免阻塞 iced executor）
                            // 不设 status_check_completed=true：splash 关闭需等用户决策
                            // (UserDialogChoice / RepairResult 会推进状态机)
                            let (tx, rx) =
                                iced::futures::channel::oneshot::channel::<bool>();
                            std::thread::spawn(move || {
                                let choice = rfd::MessageDialog::new()
                                    .set_title("LightRAG 数据异常")
                                    .set_description(&message)
                                    .set_buttons(rfd::MessageButtons::YesNo)
                                    .set_level(rfd::MessageLevel::Warning)
                                    .show();
                                let _ = tx.send(choice == rfd::MessageDialogResult::Yes);
                            });
                            return Task::perform(
                                async move { rx.await.unwrap_or(false) },
                                SplashMessage::UserDialogChoice,
                            );
                        }
                        // 健康：标记检测完成，可以关 splash（如果 ready 信号也已到）
                        self.status_check_completed = true;
                        Task::none()
                    }
                    Err(_) => {
                        // Status query failed (e.g. endpoint not yet mounted).
                        // 视为检测完成（失败不阻塞启动），允许 splash 关闭
                        self.status_check_completed = true;
                        Task::none()
                    }
                }
            }
            SplashMessage::UserDialogChoice(try_repair) => {
                if try_repair {
                    // 用户选"是"=尝试修复，调 /api/kg/lightrag/repair?target=all
                    // 修完后无论成功失败都退出（用户重启做下一轮检测）
                    self.repairing = true;
                    let port = self.api_port;
                    let (tx, rx) =
                        iced::futures::channel::oneshot::channel::<Result<String, String>>();
                    std::thread::spawn(move || {
                        // 不设超时 —— embedding 重算几千个向量需要数分钟，
                        // 数据量大了更久。靠"正在修复..."动画让用户知道在干活，
                        // 不由程序自动断开。如真卡死，用户可强杀进程。
                        //
                        // macOS 用 curl 子进程：reqwest::blocking 在长 HTTP 请求
                        // 上会卡死在 kevent（tokio IO driver 永久阻塞），
                        // 详见 802b9d71 提交的 9 场景实测试记录。
                        // Windows 用 PowerShell Invoke-WebRequest：curl.exe 在
                        // Win10 1803 之前可能未预装，PowerShell 跨版本可用。
                        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(move || {
                            let url = format!(
                                "http://127.0.0.1:{}/api/kg/lightrag/repair?target=all",
                                port
                            );
                            if cfg!(target_os = "windows") {
                                // Windows: PowerShell Invoke-WebRequest
                                // -TimeoutSec 0 = 无超时（embedding 重算需数分钟）
                                // Invoke-WebRequest 在 HTTP 4xx/5xx 时抛异常进 catch，
                                // catch 中 exit 1 让 Rust 侧判定为失败
                                let ps_script = format!(
                                    "try {{ $r = Invoke-WebRequest -Uri '{}' -Method POST -UseBasicParsing -TimeoutSec 0; $r.Content }} catch {{ if ($_.Exception.Response) {{ \"HTTP $([int]$_.Exception.Response.StatusCode)\" }} else {{ $_.Exception.Message }}; exit 1 }}",
                                    url
                                );
                                let output = std::process::Command::new("powershell")
                                    .arg("-NoProfile")
                                    .arg("-Command")
                                    .arg(&ps_script)
                                    .output()
                                    .map_err(|e| format!("PowerShell 启动失败: {}", e))?;
                                let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
                                let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
                                if !output.status.success() {
                                    return Err(if stderr.is_empty() { stdout.clone() } else { stderr });
                                }
                                Ok(stdout)
                            } else {
                                // macOS/Linux: curl 子进程（避免 reqwest kevent 卡死）
                                // -s 静默进度条，-f HTTP 4xx/5xx 返回非 0 exit code，
                                // --max-time 3600 安全网（1 小时兜底，数据量大修复时间长）
                                let output = std::process::Command::new("curl")
                                    .arg("-s")
                                    .arg("-f")
                                    .arg("--max-time")
                                    .arg("3600")
                                    .arg("-X")
                                    .arg("POST")
                                    .arg(&url)
                                    .output()
                                    .map_err(|e| format!("curl 启动失败: {}", e))?;
                                // curl 连接失败或 HTTP 错误时 exit code 非 0，
                                // stderr 可能为空（-s 模式），用 exit code 判断
                                if !output.status.success() {
                                    let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
                                    let exit_code = output.status.code().unwrap_or(-1);
                                    return Err(if stderr.is_empty() {
                                        if exit_code == 28 {
                                            "修复超时（超过 1 小时），可能是数据量过大。请重试或从备份恢复。".to_string()
                                        } else {
                                            format!("curl 退出码 {}", exit_code)
                                        }
                                    } else {
                                        stderr
                                    });
                                }
                                Ok(String::from_utf8_lossy(&output.stdout).to_string())
                            }
                        }));
                        let result = match result {
                            Ok(r) => r,
                            Err(panic_payload) => {
                                let msg = if let Some(s) = panic_payload.downcast_ref::<&str>() {
                                    format!("修复线程 panic: {}", s)
                                } else if let Some(s) = panic_payload.downcast_ref::<String>() {
                                    format!("修复线程 panic: {}", s)
                                } else {
                                    "修复线程发生未知 panic（无法提取消息）".to_string()
                                };
                                Err(msg)
                            }
                        };
                        let _ = tx.send(result);
                    });
                    return Task::perform(
                        async move { rx.await.unwrap_or(Err("channel closed".into())) },
                        SplashMessage::RepairResult,
                    );
                } else {
                    // 用户选"否"=立即退出
                    return Task::done(SplashMessage::ExitApp);
                }
            }
            SplashMessage::RepairResult(result) => {
                // 无论修复成功或失败都退出（用户重启做下一轮检测）
                match result {
                    Err(e) => {
                        // 修复失败：弹"修复失败"对话框（仅"确定"按钮）告知用户，
                        // 关闭后立即退出。不再给"重试"选项 —— 用户重启后再次检测，
                        // 如仍损坏会再次弹"是/否"对话框。
                        let (tx, rx) =
                            iced::futures::channel::oneshot::channel::<()>();
                        std::thread::spawn(move || {
                            rfd::MessageDialog::new()
                                .set_title("修复失败")
                                .set_description(&format!(
                                    "修复失败：{}\n\n请从备份恢复后重启程序。",
                                    e
                                ))
                                .set_buttons(rfd::MessageButtons::Ok)
                                .set_level(rfd::MessageLevel::Error)
                                .show();
                            let _ = tx.send(());
                        });
                        return Task::perform(
                            async move { rx.await.unwrap_or(()) },
                            |_| SplashMessage::ExitApp,
                        );
                    }
                    Ok(resp_text) => {
                        // 修复请求 HTTP 200 返回。解析 JSON 列清单，
                        // 根据 repaired + check_ok 决定对话框标题和图标：
                        // - 都为 true → "修复成功" / Info（绿色）
                        // - repaired=true 但 check_ok=false → "修复完成（有警告）" / Warning（黄色）
                        // - repaired=false → "修复失败" / Error（红色）
                        // HTTP 200 不等于修复成功——后端可能 200 但 repaired=false。
                        // API 返回格式：{"status":"ok","result":{"repaired":bool,
                        //   "check_ok":bool,"repair_result":{"vdb_entities.json":
                        //   {"status":"ok","rebuilt_count":5,...},...}}}
                        let (title, level, summary) = format_repair_summary(&resp_text);
                        let (tx, rx) =
                            iced::futures::channel::oneshot::channel::<()>();
                        std::thread::spawn(move || {
                            rfd::MessageDialog::new()
                                .set_title(&title)
                                .set_description(&summary)
                                .set_buttons(rfd::MessageButtons::Ok)
                                .set_level(level)
                                .show();
                            let _ = tx.send(());
                        });
                        return Task::perform(
                            async move { rx.await.unwrap_or(()) },
                            |_| SplashMessage::ExitApp,
                        );
                    }
                }
            }
            SplashMessage::ExitApp => {
                // 用户已决策退出（"否" / 修复完成）：
                // 1) 立即设 cancelled=true，让后台线程跳出 monitor loop
                //    并清理 Python API 子进程（HTTP /api/shutdown ->
                //    SIGTERM -> SIGKILL）。
                // 2) 调 /api/shutdown 通知 Python API 优雅关闭（后台线程
                //    也会调一次，但提前调可让 Python API 尽早开始清理）。
                // 3) 返回 iced::exit() 关闭 splash 窗口，主线程随后
                //    while !cancelled 会立即返回（cancelled=true），
                //    join 后台线程完成清理后退出整个程序。
                self.cancelled.store(true, Ordering::SeqCst);
                let port = self.api_port;
                // 优雅通知——搬到后台线程，避免 2 秒同步超时阻塞 UI。
                // 后台 cleanup loop（L1716）也会调一次，这里 fire-and-forget。
                std::thread::spawn(move || {
                    let _ = notify_shutdown(port);
                });
                iced::exit()
            }
        }
    }

    fn view(&self) -> Element<'_, SplashMessage> {
        let dots = match (self.dot_frame / 10) % 3 {
            0 => ".",
            1 => "..",
            _ => "...",
        };
        // Two separate text elements: CJK label + fixed-width container for dots
        // The dots container has a fixed width so the row's total width never changes,
        // preventing layout shift when the number of dots changes.
        let label_text: &str = if self.closing {
            "正在关闭所有进程，关闭后请重新启动程序"
        } else if self.repairing {
            "正在修复"
        } else {
            &self.stage
        };
        // Closing message is long (~18 CJK glyphs); shrink font so it fits in
        // the 280px-wide splash without being truncated.
        let label_size = if self.closing { 13 } else { 16 };
        let label = iced::widget::text(label_text)
            .size(label_size)
            .font(CJK_FONT)
            .color(iced::Color::from_rgb(0.24, 0.22, 0.20)); // #3d3833 暖深灰
        let dots_text = iced::widget::text(dots)
            .size(16)
            .font(Font::MONOSPACE)
            .color(iced::Color::from_rgb(0.83, 0.33, 0.0)); // #d35400 暖橙
        let dots_container = container(dots_text).width(Length::Fixed(36.0));
        let top_row = iced::widget::row![label, dots_container]
            .align_y(iced::alignment::Vertical::Center);

        // 卡片样式（两分支共用）：暖米白竖向微渐变 + 圆角 16 + 柔和投影。
        let card_style = |_theme: &Theme| container::Style {
            background: Some(iced::Background::from(
                iced::gradient::Linear::new(std::f32::consts::FRAC_PI_2)
                    .add_stop(0.0, iced::Color::from_rgb(1.0, 0.992, 0.969)) // #fffdf7
                    .add_stop(1.0, iced::Color::from_rgb(0.957, 0.937, 0.886)), // #f4efe2
            )),
            border: iced::Border {
                radius: 16.0.into(),
                ..iced::Border::default()
            },
            shadow: iced::Shadow {
                color: iced::Color::from_rgba(0.0, 0.0, 0.0, 0.22),
                offset: iced::Vector::new(0.0, 8.0),
                blur_radius: 24.0,
            },
            ..container::Style::default()
        };

        // 缺失依赖提示（如有）
        if self.missing_deps.is_empty() || self.closing {
            // 无缺失 / closing 状态（重启/关闭中）：保持原布局，不显示缺失提示
            // closing 时用户要重启了，提示已无意义
            let card = container(top_row)
                .width(Length::Fill)
                .height(Length::Fill)
                .align_x(iced::alignment::Horizontal::Center)
                .align_y(iced::alignment::Vertical::Center)
                .style(card_style);
            // 外层透明 container：Fill 全窗口，四边留白给阴影
            container(card)
                .width(Length::Fill)
                .height(Length::Fill)
                .padding(SHADOW_MARGIN)
                .into()
        } else {
            // 有缺失：顶部 label 行 + 分隔 + 提示标题 + 缺失项列表
            let hint_title = iced::widget::text("以下功能因依赖缺失暂不可用，请读 README 安装说明：")
                .size(11)
                .font(CJK_FONT)
                .color(iced::Color::from_rgb(0.60, 0.42, 0.04)); // #9a6a0a 深琥珀
            let items: Vec<Element<SplashMessage>> = self
                .missing_deps
                .iter()
                .map(|s| {
                    iced::widget::text(format!("• {}", s))
                        .size(11)
                        .font(CJK_FONT)
                        .color(iced::Color::from_rgb(0.42, 0.39, 0.34)) // #6b6357 暖中灰
                        .into()
                })
                .collect();
            let items_column = iced::widget::column![hint_title]
                .push(iced::widget::Space::new(Length::Fixed(2.0), Length::Fixed(2.0)))
                .extend(items)
                .spacing(2);
            let card = container(
                iced::widget::column![top_row]
                    .push(iced::widget::Space::new(Length::Fixed(0.0), Length::Fixed(8.0)))
                    .push(items_column)
                    .align_x(iced::alignment::Horizontal::Center),
            )
            .width(Length::Fill)
            .height(Length::Fill)
            .padding(8)
            .align_x(iced::alignment::Horizontal::Center)
            .align_y(iced::alignment::Vertical::Top)
            .style(card_style);
            // 外层透明 container：Fill 全窗口，四边留白给阴影
            container(card)
                .width(Length::Fill)
                .height(Length::Fill)
                .padding(SHADOW_MARGIN)
                .into()
        }
    }

    fn subscription(&self) -> Subscription<SplashMessage> {
        // Use window redraw frames as a periodic tick to poll the channel
        // Also subscribe to window open events to capture the window ID
        Subscription::batch([
            window::frames().map(|_| SplashMessage::Tick),
            window::open_events().map(SplashMessage::WindowOpened),
        ])
    }
}

// ---------------------------------------------------------------------------
// detectPython — corresponds to Go's detectPython()
// ---------------------------------------------------------------------------

/// detectPython finds the project's self-contained Python executable.
/// Primary: based on executable directory (works when running built binary from any cwd).
/// Fallback: current working directory (supports development).
/// Detect project's self-contained Python executable.
/// Looks in `<resources_root>/python/bin/python3` (macOS/Linux) or
/// `<resources_root>/python/Scripts/python.exe` (Windows).
///
/// Resources root from detect_resources_root():
/// - macOS bundle: niu.app/Contents/Resources/
/// - macOS dev / Windows / Linux: exe parent
///
/// cargo run fallback: current_exe() = launcher/target/debug/niu-launcher,
/// resources root = launcher/target/debug/ — python/ not there. Fall back
/// to cwd (only when resources_root didn't find it).
fn detect_python() -> String {
    let python_rel_path: PathBuf = if cfg!(target_os = "windows") {
        PathBuf::from("python").join("Scripts").join("python.exe")
    } else {
        PathBuf::from("python").join("bin").join("python3")
    };

    // Primary: resources root
    let resources_root = detect_resources_root();
    let candidate = resources_root.join(&python_rel_path);
    if Command::new(&candidate)
        .arg("--version")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
    {
        let abs_path = dunce::canonicalize(&candidate).unwrap_or_else(|_| candidate.clone());
        info!("Found project Python (resources): {}", abs_path.display());
        return abs_path.to_string_lossy().to_string();
    }

    // Dev fallback: cargo run scenario (cwd=project root)
    let cwd = env::current_dir()
        .map(|d| d.to_string_lossy().to_string())
        .unwrap_or_else(|_| ".".to_string());
    let cwd_candidate = PathBuf::from(&cwd).join(&python_rel_path);
    if Command::new(&cwd_candidate)
        .arg("--version")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
    {
        let abs_path = dunce::canonicalize(&cwd_candidate).unwrap_or_else(|_| cwd_candidate.clone());
        info!("Found project Python (cwd fallback): {}", abs_path.display());
        return abs_path.to_string_lossy().to_string();
    }

    error!(
        "Project Python not found. Checked resources: {}, cwd: {}",
        candidate.display(),
        cwd_candidate.display()
    );
    log_fatal_error(&format!(
        "Project Python not found. Checked resources: {}, cwd: {}",
        candidate.display(),
        cwd_candidate.display()
    ));
    std::process::exit(1);
}

// ---------------------------------------------------------------------------
// initNiuDir — corresponds to Go's initNiuDir()
// ---------------------------------------------------------------------------

/// initNiuDir ensures ~/.niu/ directory exists and copies template files if needed
fn init_niu_dir(project_root: &str) {
    let home_dir = match dirs::home_dir() {
        Some(d) => d,
        None => {
            error!("Failed to get home directory");
            return;
        }
    };

    let niu_dir = home_dir.join(".niu");

    // Create ~/.niu/ directory if it doesn't exist
    if let Err(e) = fs::create_dir_all(&niu_dir) {
        error!("Failed to create ~/.niu directory: {}", e);
        return;
    }

    // Template files to copy if they don't exist in ~/.niu/
    let template_files = ["memory.json", "preferences.json"];
    let template_dir = PathBuf::from(project_root).join("memory");

    for filename in &template_files {
        let dst_path = niu_dir.join(filename);
        // Only copy if destination doesn't exist
        if dst_path.exists() {
            continue;
        }
        let src_path = template_dir.join(filename);
        let src_data = match fs::read(&src_path) {
            Ok(d) => d,
            Err(e) => {
                warn!("Template file not found, skipping: {} ({})", filename, e);
                continue;
            }
        };
        if let Err(e) = fs::write(&dst_path, &src_data) {
            error!(
                "Failed to copy template file: src={}, dst={}, error={}",
                src_path.display(),
                dst_path.display(),
                e
            );
            continue;
        }
        info!("Copied template file: {} -> {}", filename, dst_path.display());
    }

    // Copy config/mcp-servers.yaml (file-level, don't overwrite existing)
    // 与 skills/ 同范式：dst.exists() 跳过，保护用户修改
    // 源是 project_root/config/mcp-servers.yaml，目标是 ~/.niu/config/mcp-servers.yaml
    // 必须放在 skills 块之前：skills 块内部有多处提前 return，
    // 放它之后会被跳过，违背"启动即保证"目标
    let src_mcp_yaml = PathBuf::from(project_root).join("config").join("mcp-servers.yaml");
    let dst_config_dir = niu_dir.join("config");
    let dst_mcp_yaml = dst_config_dir.join("mcp-servers.yaml");

    if dst_mcp_yaml.exists() {
        info!(
            "mcp-servers.yaml already exists, skipping: {}",
            dst_mcp_yaml.display()
        );
    } else if !src_mcp_yaml.exists() {
        warn!(
            "Template mcp-servers.yaml not found, skipping: {}",
            src_mcp_yaml.display()
        );
    } else if let Err(e) = fs::create_dir_all(&dst_config_dir) {
        error!(
            "Failed to create config directory: {}, error={}",
            dst_config_dir.display(),
            e
        );
    } else {
        match fs::read(&src_mcp_yaml) {
            Ok(data) => {
                if let Err(e) = fs::write(&dst_mcp_yaml, &data) {
                    error!(
                        "Failed to copy mcp-servers.yaml: src={}, dst={}, error={}",
                        src_mcp_yaml.display(),
                        dst_mcp_yaml.display(),
                        e
                    );
                } else {
                    info!(
                        "Copied mcp-servers.yaml: {} -> {}",
                        src_mcp_yaml.display(),
                        dst_mcp_yaml.display()
                    );
                }
            }
            Err(e) => {
                warn!(
                    "Failed to read mcp-servers.yaml: {}, error={}",
                    src_mcp_yaml.display(),
                    e
                );
            }
        }
    }

    // Copy skills/ directory (individual .md files, don't overwrite existing)
    // Triggered when: dir missing / dir exists but empty / specific .md missing
    // Protects user modifications by skipping existing files
    let src_skills_dir = template_dir.join("skills");
    let dst_skills_dir = niu_dir.join("skills");

    if !src_skills_dir.exists() {
        warn!(
            "Template skills directory not found, skipping: {}",
            src_skills_dir.display()
        );
        return;
    }

    if let Err(e) = fs::create_dir_all(&dst_skills_dir) {
        error!(
            "Failed to create skills directory: {}, error={}",
            dst_skills_dir.display(),
            e
        );
        return;
    }

    let entries = match fs::read_dir(&src_skills_dir) {
        Ok(e) => e,
        Err(e) => {
            warn!(
                "Failed to read template skills directory: {}, error={}",
                src_skills_dir.display(),
                e
            );
            return;
        }
    };

    let mut copied_count = 0u32;
    let mut skipped_count = 0u32;
    for entry in entries {
        let entry = match entry {
            Ok(e) => e,
            Err(e) => {
                warn!("Failed to read directory entry: {}", e);
                continue;
            }
        };

        let file_name = entry.file_name();
        let file_name_str = match file_name.to_str() {
            Some(s) => s,
            None => continue,
        };

        // Only copy .md files, skip .DS_Store / hidden files
        if !file_name_str.ends_with(".md") {
            continue;
        }

        let dst_path = dst_skills_dir.join(file_name_str);
        if dst_path.exists() {
            debug!(
                "Skill file already exists, skipping: {}",
                dst_path.display()
            );
            skipped_count += 1;
            continue;
        }

        let src_path = entry.path();
        let src_data = match fs::read(&src_path) {
            Ok(d) => d,
            Err(e) => {
                warn!(
                    "Failed to read template skill file: {}, error={}",
                    src_path.display(),
                    e
                );
                continue;
            }
        };

        if let Err(e) = fs::write(&dst_path, &src_data) {
            error!(
                "Failed to copy skill file: src={}, dst={}, error={}",
                src_path.display(),
                dst_path.display(),
                e
            );
            continue;
        }

        info!(
            "Copied skill file: {} -> {}",
            file_name_str,
            dst_path.display()
        );
        copied_count += 1;
    }

    info!(
        "Skills directory sync: copied={}, skipped(existing)={}, dst={}",
        copied_count,
        skipped_count,
        dst_skills_dir.display()
    );
}

// ---------------------------------------------------------------------------
// loadMemory — corresponds to Go's loadMemory()
// ---------------------------------------------------------------------------

/// loadMemory loads user memory from ~/.niu/memory.json
fn load_memory() -> Option<serde_json::Value> {
    let home_dir = dirs::home_dir()?;

    let memory_path = home_dir.join(".niu").join("memory.json");
    let data = fs::read_to_string(&memory_path).ok()?;

    serde_json::from_str(&data).ok()
}

// ---------------------------------------------------------------------------
// notifyShutdown — corresponds to Go's notifyShutdown()
// ---------------------------------------------------------------------------

/// notifyShutdown sends shutdown request to Python API
fn notify_shutdown(port: u16) -> Result<(), Box<dyn std::error::Error>> {
    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(2))
        .build()?;
    let url = format!("http://127.0.0.1:{}/api/shutdown", port);
    client.post(&url).header("Content-Type", "application/json").send()?;
    Ok(())
}

// ---------------------------------------------------------------------------
// isAPIRunning — corresponds to Go's isAPIRunning()
// ---------------------------------------------------------------------------

/// isAPIRunning checks if the Python API is already running on the given port
fn is_api_running(port: u16) -> bool {
    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(2))
        .build();
    let client = match client {
        Ok(c) => c,
        Err(_) => return false,
    };
    let url = format!("http://127.0.0.1:{}/health", port);
    match client.get(&url).send() {
        Ok(resp) => resp.status().is_success(),
        Err(_) => false,
    }
}

// ---------------------------------------------------------------------------
// killStaleAPIProcess — corresponds to Go's killStaleAPIProcess()
// ---------------------------------------------------------------------------

/// killStaleAPIProcess checks if the API port is occupied and kills the stale process
fn kill_stale_api_process(port: u16) {
    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(3))
        .build()
        .expect("Failed to build HTTP client");

    // Check if port is occupied by trying to connect
    let url = format!("http://127.0.0.1:{}/health", port);
    let resp = client.get(&url).send();
    let conn = match resp {
        Ok(r) => r,
        Err(_) => {
            // Port not occupied, safe to start
            return;
        }
    };
    // Drain the body (Go's resp.Body.Close())
    let _ = conn.text();

    warn!("Port already occupied, attempting to stop stale API process (port={})", port);

    // Try graceful shutdown via HTTP endpoint
    if notify_shutdown(port).is_ok() {
        info!("Sent shutdown request to stale API process, waiting 2s...");
        thread::sleep(Duration::from_secs(2));
        // Check if port is now free
        let url2 = format!("http://127.0.0.1:{}/health", port);
        match client.get(&url2).send() {
            Err(_) => {
                info!("Stale API process exited gracefully");
                return;
            }
            Ok(r) => {
                let _ = r.text();
            }
        }
    }

    #[cfg(unix)]
    {
        warn!("Stale API process still alive, force killing with pkill");
        let kill_result = Command::new("pkill")
            .arg("-f")
            .arg("python.*niu_api")
            .status();
        match kill_result {
            Ok(status) if status.success() => {
                info!("Sent SIGTERM to stale API process via pkill");
            }
            _ => {
                warn!("pkill failed (process may already be gone)");
            }
        }
    }
    #[cfg(windows)]
    {
        warn!("Stale API process still alive, force killing with PowerShell");
        // Use PowerShell to find and kill Python processes running niu_api
        // Python background processes have no window title, so WINDOWTITLE filter won't work
        let kill_result = Command::new("powershell")
            .args([
                "-Command",
                "Get-Process python* | Where-Object {$_.CommandLine -match 'niu_api'} | Stop-Process -Force",
            ])
            .status();
        match kill_result {
            Ok(status) if status.success() => {
                info!("Sent kill to stale API process via PowerShell");
            }
            _ => {
                warn!("PowerShell kill failed (process may already be gone)");
            }
        }
    }
    // Wait for process to exit
    thread::sleep(Duration::from_secs(1));
}

// ---------------------------------------------------------------------------
// launchWindow — corresponds to Go's launchWindow()
// ---------------------------------------------------------------------------

/// launchWindow launches an Electron window.
///
/// In macOS/Linux bundle mode, we directly invoke the Electron binary
/// located at `<window_dir>/node_modules/electron/dist/Electron.app/Contents/MacOS/Electron`
/// to avoid depending on PATH (Finder-launched processes inherit a minimal
/// PATH that does not include `/usr/local/bin` where npm lives).
/// In dev mode (no node_modules), we fall back to `npm start`.
/// Windows always uses `cmd /C npm start` (users must install npm).
fn launch_window(name: &str, port: u16) -> Result<std::process::Child, Box<dyn std::error::Error>> {
    let resources_root = detect_resources_root();
    let window_dir = resources_root.join("ui").join("main");
    #[cfg(windows)]
    {
        let mut cmd = Command::new("cmd");
        cmd.args(["/C", "npm", "start"])
            .env("NIU_WINDOW", name)
            .env("NIU_API_PORT", port.to_string())
            .current_dir(&window_dir);
        // Windows: CREATE_NO_WINDOW 避免弹 cmd 控制台窗口
        cmd.creation_flags(0x08000000);
        cmd.stdout(std::process::Stdio::null());
        cmd.stderr(std::process::Stdio::null());
        cmd.stdin(std::process::Stdio::null());
        Ok(cmd.spawn()?)
    }

    #[cfg(target_os = "macos")]
    {
        // Locate the bundled Electron binary (absolute path, no PATH lookup).
        let electron_bin = window_dir.join(
            "node_modules/electron/dist/Electron.app/Contents/MacOS/Electron",
        );

        if electron_bin.exists() {
            // Bundle mode: run Electron directly with the window_dir as the
            // app entry point ("." resolves to package.json -> main field).
            let mut cmd = Command::new(&electron_bin);
            cmd.arg(".")
                .env("NIU_WINDOW", name)
                .env("NIU_API_PORT", port.to_string())
                .current_dir(&window_dir);
            cmd.stdout(std::process::Stdio::null());
            cmd.stderr(std::process::Stdio::null());
            cmd.stdin(std::process::Stdio::null());
            Ok(cmd.spawn()?)
        } else {
            // Dev mode fallback: no bundled Electron, rely on `npm start`.
            let mut cmd = Command::new("npm");
            cmd.arg("start")
                .env("NIU_WINDOW", name)
                .env("NIU_API_PORT", port.to_string())
                .current_dir(&window_dir);
            cmd.stdout(std::process::Stdio::null());
            cmd.stderr(std::process::Stdio::null());
            cmd.stdin(std::process::Stdio::null());
            Ok(cmd.spawn()?)
        }
    }

    #[cfg(all(unix, not(target_os = "macos")))]
    {
        // Linux: no .app bundle, fall back to `npm start`.
        let mut cmd = Command::new("npm");
        cmd.arg("start")
            .env("NIU_WINDOW", name)
            .env("NIU_API_PORT", port.to_string())
            .current_dir(&window_dir);
        cmd.stdout(std::process::Stdio::null());
        cmd.stderr(std::process::Stdio::null());
        cmd.stdin(std::process::Stdio::null());
        Ok(cmd.spawn()?)
    }
}

// ---------------------------------------------------------------------------
// CLI args — corresponds to Go's flag package
// ---------------------------------------------------------------------------

#[derive(Parser, Debug)]
#[command(name = "niu-launcher", about = "Niu launcher")]
struct Args {
    /// path to configuration directory (kept for compatibility)
    #[arg(long, default_value = "./config")]
    config: String,

    /// open settings window
    #[arg(long)]
    settings: bool,

    /// open knowledge graph window
    #[arg(long)]
    graph: bool,

    /// port for Python API server
    #[arg(long, default_value_t = 9876)]
    port: u16,
}

// ---------------------------------------------------------------------------
// Project root detection / logging gate / fatal error log
// ---------------------------------------------------------------------------

/// Pure function: compute resources root from a given exe path.
/// Extracted from detect_resources_root() for testability.
fn resources_root_from_exe(exe_path: &std::path::Path) -> PathBuf {
    #[cfg(target_os = "macos")]
    {
        let exe_str = exe_path.to_string_lossy();
        if exe_str.contains(".app/Contents/MacOS/") {
            let contents_dir = exe_path
                .parent()
                .and_then(|p| p.parent())
                .map(|p| p.to_path_buf());
            return match contents_dir {
                Some(contents) => contents.join("Resources"),
                None => PathBuf::from("."),
            };
        }
    }
    exe_path
        .parent()
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| PathBuf::from("."))
}

/// Detect the resources root directory (where python/, ui/, config/, models/, memory/ live).
/// macOS bundle mode: niu.app/Contents/MacOS/niu → niu.app/Contents/Resources/
/// macOS dev mode / Windows / Linux: exe parent directory
fn detect_resources_root() -> PathBuf {
    let exe_path = env::current_exe().unwrap_or_else(|_| PathBuf::from("."));
    resources_root_from_exe(&exe_path)
}

/// Check for optional dependencies excluded from the DMG for license compliance.
/// Returns a list of human-readable hint strings for missing components.
/// Each hint names the affected feature and points the user to README.
/// Empty Vec = all optional deps present (developer build).
///
/// 检查位置：bundle 内 resources_root 下
///   - site-packages/ 下 cv2/insightface/easydict/pillow_heif/igraph/leidenalg
///   - models/models/buffalo_l/ 下是否有 .onnx
/// 不查用户家目录（~/.insightface/）——程序内禁止下载，本地没有直接报错，
/// 用户按 README 把包和模型装到 bundle 内 + 重签名。
///
/// 注意：本列表只查主包目录（cv2/insightface/easydict/pillow_heif/igraph/leidenalg），
/// 不需要与 build.sh 的 rsync --exclude 完全一致——exclude 还含 texttable（igraph 依赖）、
/// _pillow_heif*.so（松散 .so）、*-*.dist-info（元数据）等，此处不查。
/// 两处列表不同步是 by design（打包侧 vs 运行时侧职责不同）。
fn check_missing_deps(resources_root: &Path) -> Vec<String> {
    // 动态找 site-packages 目录（不硬编码版本号，Python 升级时无需改此处）
    // macOS bundle: python/lib/python3.x/site-packages/
    // Windows venv: python/Lib/site-packages/（大写 Lib，无 python3.x 子目录）
    let python_lib = resources_root.join("python").join("lib");
    let python_dir = fs::read_dir(&python_lib)
        .ok()
        .and_then(|entries| {
            entries
                .filter_map(|e| e.ok())
                .map(|e| e.file_name())
                .filter(|name| {
                    name.to_str().map(|s| s.starts_with("python3.")).unwrap_or(false)
                })
                .next()
        })
        .map(|name| python_lib.join(name));
    let site_packages = match python_dir {
        Some(dir) => dir.join("site-packages"),
        None => {
            // Windows venv fallback: python/Lib/site-packages/
            let win_path = resources_root.join("python").join("Lib").join("site-packages");
            if win_path.exists() {
                win_path
            } else {
                return Vec::new();  // 找不到 site-packages，无缺失可查
            }
        }
    };
    let mut missing: Vec<String> = Vec::new();

    // 人脸识别全套（cv2 + insightface + easydict 一起检查，任一缺失=人脸识别不可用）
    let has_cv2 = site_packages.join("cv2").exists();
    let has_insightface = site_packages.join("insightface").exists();
    let has_easydict = site_packages.join("easydict").exists();
    if !has_cv2 || !has_insightface || !has_easydict {
        missing.push(
            "照片处理 / 人脸识别（缺 cv2 + insightface + easydict，见 README\"可选：启用照片处理\"）"
                .to_string(),
        );
    }

    // HEIC 照片支持
    if !site_packages.join("pillow_heif").exists() {
        missing.push(
            "iPhone HEIC 照片（缺 pillow-heif，见 README\"可选：启用照片处理\"）".to_string(),
        );
    }

    // 脑区社区检测
    let has_igraph = site_packages.join("igraph").exists();
    let has_leidenalg = site_packages.join("leidenalg").exists();
    if !has_igraph || !has_leidenalg {
        missing.push(
            "脑区社区检测（缺 igraph + leidenalg，见 README\"可选：启用脑区功能\"）".to_string(),
        );
    }

    // 人脸模型（只查 bundle 内 models/models/buffalo_l/，与 get_face_model 读取路径一致；
    // 不查 ~/.insightface/——程序内禁止下载，本地没有直接报错，让用户按 README 装到 bundle 内）
    let bundle_model = resources_root
        .join("models")
        .join("models")
        .join("buffalo_l");
    let has_onnx = |dir: &Path| -> bool {
        fs::read_dir(dir)
            .map(|entries| entries.filter_map(|e| e.ok()).any(|e| {
                e.path()
                    .extension()
                    .map(|ext| ext == "onnx")
                    .unwrap_or(false)
            }))
            .unwrap_or(false)
    };
    if !has_onnx(&bundle_model) {
        missing.push(
            "人脸识别模型（缺 buffalo_l 模型，见 README\"可选：启用照片处理\"）".to_string(),
        );
    }

    missing
}

/// Detect user data root (~/.niu/). All writable runtime data lives here.
fn detect_niu_home() -> Result<PathBuf, std::io::Error> {
    let home = dirs::home_dir()
        .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::NotFound, "home_dir not found"))?;
    Ok(home.join(".niu"))
}

/// Detect project root directory (= resources root). All path detection
/// now goes through detect_resources_root() — no cwd fallback.
fn detect_project_root() -> String {
    detect_resources_root().to_string_lossy().to_string()
}

/// Read ~/.niu/config/user-config.json `logging.enabled` field.
/// Returns false (conservative default) on any failure.
///
/// IMPORTANT: config file must live in ~/.niu/config/, not bundle.
/// Bundle files are read-only at runtime (macOS Gatekeeper enforces).
/// Rust and Python and Electron all read the same ~/.niu/config/user-config.json
/// so logging flag is consistent across processes.
fn should_enable_logging() -> bool {
    let config_path = match detect_niu_home() {
        Ok(home) => home.join("config").join("user-config.json"),
        Err(_) => return false,
    };
    match std::fs::read_to_string(&config_path) {
        Ok(content) => match serde_json::from_str::<serde_json::Value>(&content) {
            Ok(v) => v
                .get("logging")
                .and_then(|l| l.get("enabled"))
                .and_then(|e| e.as_bool())
                .unwrap_or(false),
            Err(_) => false,
        },
        Err(_) => false,
    }
}

/// Write a fatal error message to `~/.niu/logs/launcher_error.log`.
/// Independent of tracing/logging flag — guarantees diagnostic availability.
///
/// IMPORTANT: log file must NOT live in the .app bundle — bundle files are
/// read-only at runtime (macOS Gatekeeper enforces). Write to ~/.niu/logs/.
/// Fallback to std::env::temp_dir() if home_dir unavailable (cross-platform writable temp).
fn log_fatal_error(msg: &str) {
    let log_path = match detect_niu_home() {
        Ok(home) => home.join("logs").join("launcher_error.log"),
        Err(_) => std::env::temp_dir().join("niu_launcher_error.log"),
    };
    let _ = std::fs::create_dir_all(log_path.parent().unwrap_or(std::path::Path::new(".")));
    use time::macros::format_description;
    let format = format_description!("[year]-[month]-[day] [hour]:[minute]:[second]");
    let timestamp = match time::OffsetDateTime::now_local() {
        Ok(t) => t.format(format).unwrap_or_else(|_| "unknown".to_string()),
        Err(_) => "unknown".to_string(),
    };
    let line = format!("[{}] FATAL: {}\n", timestamp, msg);
    let _ = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .and_then(|mut f| std::io::Write::write_all(&mut f, line.as_bytes()));
}

// ---------------------------------------------------------------------------
// main — corresponds to Go's main()
// ---------------------------------------------------------------------------

fn main() {
    // Initialize tracing with local timezone (Asia/Shanghai UTC+8)
    // Default uses UTC with "Z" suffix which is confusing for Chinese users
    // Gate: respect config/user-config.json `logging.enabled` — when false,
    // skip init so tracing calls are silently dropped (Windows GUI release mode).
    if should_enable_logging() {
        tracing_subscriber::fmt()
            .with_timer(tracing_subscriber::fmt::time::LocalTime::new(
                time::macros::format_description!(
                    "[year]-[month]-[day]T[hour]:[minute]:[second].[subsecond digits:6]+08:00"
                ),
            ))
            .init();
    }
    // else: tracing_subscriber not initialized — tracing calls (info!/warn!/error!) silently dropped.

    // Parse args (replaces Go's flag)
    let args = Args::parse();

    // Context cancellation via AtomicBool (replaces Go's context.WithCancel)
    let cancelled = Arc::new(AtomicBool::new(false));
    let cancelled_clone = cancelled.clone();

    // Handle shutdown signals (replaces Go's signal.Notify)
    ctrlc::set_handler(move || {
        info!("Shutdown signal received (SIGINT)");
        cancelled_clone.store(true, Ordering::SeqCst);
    })
    .expect("Failed to set Ctrl-C handler");

    // Also handle SIGTERM on Unix (ctrlc only handles SIGINT)
    #[cfg(unix)]
    {
        use nix::sys::signal::{self, Signal};
        // Block SIGTERM in the main thread BEFORE spawning any threads.
        // All subsequently created threads inherit this blocked mask,
        // so the kernel can only deliver SIGTERM to the sigwait thread below.
        let mut sigset = signal::SigSet::empty();
        sigset.add(Signal::SIGTERM);
        signal::pthread_sigmask(signal::SigmaskHow::SIG_BLOCK, Some(&sigset), None).unwrap();

        let cancelled_term = cancelled.clone();
        thread::spawn(move || {
            // SIGTERM is already blocked (inherited from main thread).
            // sigwait will synchronously catch it.
            let mut sigset = signal::SigSet::empty();
            sigset.add(Signal::SIGTERM);
            sigset.wait().unwrap();
            info!("Shutdown signal received (SIGTERM)");
            cancelled_term.store(true, Ordering::SeqCst);
        });
    }

    info!("Niu launcher starting...");

    // --settings and --graph modes: connect to existing API, do NOT start a new one
    // This prevents orphan Python API processes when these modes exit without shutdown
    if args.settings || args.graph {
        if is_api_running(args.port) {
            let window_name = if args.graph { "graph" } else { "settings" };
            info!(
                "API already running, launching window: window={}, port={}",
                window_name, args.port
            );
            if let Err(e) = launch_window(window_name, args.port) {
                error!("Failed to launch window: window={}, error={}", window_name, e);
                std::process::exit(1);
            }
            return;
        }
        error!(
            "API is not running, please start the main program first (port={})",
            args.port
        );
        println!(
            "Error: API is not running on port {}. Please start the main program (niu) first.",
            args.port
        );
        // Independent file diagnostic (not subject to logging flag — available
        // even when logging.enabled=false / Windows GUI release mode).
        log_fatal_error(&format!("API is not running on port {}", args.port));
        std::process::exit(1);
    }

    // Detect Python
    let python_path = detect_python();
    info!("Using Python path: {}", python_path);

    // Check for missing optional dependencies (license-excluded packages).
    // Non-blocking: just shown on splash as a hint to read README.
    let resources_root = detect_resources_root();
    let missing_deps = check_missing_deps(&resources_root);
    if !missing_deps.is_empty() {
        info!("Missing optional dependencies: {:?}", missing_deps);
    }

    // Get project root (needed for template file paths and config loading)
    // Delegates to detect_project_root() (shared with should_enable_logging / log_fatal_error).
    let project_root = detect_project_root();

    // Initialize ~/.niu/ directory and copy template files if needed
    init_niu_dir(&project_root);

    // Load memory for injection (passed to Python API via environment)
    let memory = load_memory();

    // Extract workspace.path from memory and set as WORKSPACE_PATH env var
    // so all child processes (Python API, MCP servers) use the correct workspace path
    // Skip placeholder values like "请询问用户指定工作目录" which are not real paths
    let mut workspace_path = String::new();
    if let Some(mem) = &memory {
        if let Some(ws) = mem.get("workspace").and_then(|v| v.as_object()) {
            if let Some(path) = ws.get("path").and_then(|v| v.as_str()) {
                if !path.is_empty() && !path.starts_with("请询问") {
                    workspace_path = path.to_string();
                }
            }
        }
    }

    // --- Splash window + background launcher ---
    // macOS requires GUI to run on the main thread, so we:
    // 1. Spawn a background thread for Python API startup + health/preload checks
    // 2. Run the iced splash window on the main thread
    // 3. When preload is ready, background thread signals the splash to close
    // 4. After splash closes, main thread continues with process monitoring

    let (splash_tx, splash_rx) = std::sync::mpsc::channel::<()>();
    // Lifecycle channel: background thread -> splash window. Used after the
    // settings test passes to (1) flip the splash into the "closing" state
    // showing the shutdown message, and (2) trigger iced::exit() once all
    // child processes have been reaped. See SplashPhase enum for details.
    let (phase_tx, phase_rx) = std::sync::mpsc::channel::<SplashPhase>();
    let (stage_tx, stage_rx) = std::sync::mpsc::channel::<String>();
    let cancelled_bg = cancelled.clone();
    let port = args.port;

    // Shared "integrity failed" flag — Splash sets this true the moment the
    // LightRAG status check reports corruption. The background thread reads
    // it before sending the ready signal / launching the assistant window,
    // and aborts startup if true (skips splash_tx.send, skips
    // launch_window("assistant"), falls through to the cancelled cleanup
    // loop which tears down the Python API child).
    let integrity_failed = Arc::new(AtomicBool::new(false));
    let integrity_failed_bg = integrity_failed.clone();

    // Shared "LLM config failed" flag — set true by the background thread
    // itself when /api/llm-status reports not-ready OR the real /api/test-llm
    // fails twice. Once true, the bg thread launches the settings window
    // (instead of the assistant), waits for either the test to pass or the
    // settings window to be closed without passing, then sets cancelled=true
    // to tear down the Python API and exit the process (so user can restart).
    let llm_config_failed = Arc::new(AtomicBool::new(false));
    let llm_config_failed_bg = llm_config_failed.clone();

    // Build environment vars for Python API (computed on main thread for simplicity)
    let mut env_vars: Vec<(String, String)> = Vec::new();
    env_vars.push((
        "NIU_API_PORT".to_string(),
        args.port.to_string(),
    ));
    env_vars.push(("PYTHONUNBUFFERED".to_string(), "1".to_string()));
    env_vars.push(("LITELLM_LOCAL_MODEL_COST_MAP".to_string(), "True".to_string()));
    env_vars.push(("LITELLM_NO_AIOHTTP_TRANSPORT".to_string(), "True".to_string()));

    if !workspace_path.is_empty() {
        if !PathBuf::from(&workspace_path).exists() {
            error!(
                "WORKSPACE_PATH directory does not exist, skipping: path={}, error=directory not found",
                workspace_path
            );
            workspace_path = String::new();
        }
    }
    if !workspace_path.is_empty() {
        env_vars.push(("WORKSPACE_PATH".to_string(), workspace_path.clone()));
        info!("Setting WORKSPACE_PATH for Python API: {}", workspace_path);
    }

    // Kill stale Python API process occupying the port before starting a new one
    kill_stale_api_process(args.port);

    // Spawn background thread: start Python API, health check, preload check, launch Electron
    let python_path_bg = python_path.clone();
    let project_root_bg = project_root.clone();
    let env_vars_bg = env_vars.clone();
    let bg_handle = thread::spawn(move || {
        // Start Python API server as background process
        info!("Starting Python API server...");
        let mut api_server_cmd = Command::new(&python_path_bg);
        api_server_cmd.args(["-m", "niu_api"]);
        // cwd: ~/.niu/ (writable, not bundle). Bundle is read-only at runtime
        // (macOS Gatekeeper enforces). Python API writes session/db/logs to cwd.
        let api_server_cwd = match detect_niu_home() {
            Ok(home) => home.to_string_lossy().to_string(),
            Err(_) => project_root_bg.clone(),
        };
        api_server_cmd.current_dir(&api_server_cwd);
        // Windows: CREATE_NO_WINDOW 避免 Python 进程弹控制台窗口
        #[cfg(windows)]
        api_server_cmd.creation_flags(0x08000000);

        // Set environment: inherit current env first, then override with our vars
        // IMPORTANT: env::vars() must come BEFORE PYTHONHOME, otherwise inherited
        // PYTHONHOME (e.g. from user shell) would override our bundle-internal path.
        for (key, value) in env::vars() {
            api_server_cmd.env(&key, &value);
        }
        for (key, value) in &env_vars_bg {
            api_server_cmd.env(key, value);
        }

        // PYTHONHOME: 让 python3 找到 bundle 内 stdlib + site-packages
        // macOS 必须：python3 链接的 dylib 内部硬编码系统 framework stdlib 路径，
        // PYTHONHOME 覆盖让 base_prefix 指向 bundle 内。
        // Windows 不能设：venv 的 stdlib 在系统 Python（C:\Python311\Lib）里，
        // 设 PYTHONHOME 会导致 "No module named encodings" 致命错误。
        if !cfg!(target_os = "windows") {
            let python_home = PathBuf::from(&python_path_bg)
                .parent()
                .and_then(|p| p.parent())
                .map(|p| p.to_string_lossy().to_string())
                .unwrap_or_default();
            api_server_cmd.env("PYTHONHOME", &python_home);
        }

        // PYTHONPATH: 让 Python 找到 niu_api 和 agent 模块
        // cwd 是 ~/.niu/（可写），但 niu_api/agent 模块在 bundle 内 Resources/
        // 必须把 Resources/ 加入 PYTHONPATH，否则 "No module named niu_api"
        let resources_root = detect_resources_root();
        let python_path_env = resources_root.to_string_lossy().to_string();
        api_server_cmd.env("PYTHONPATH", &python_path_env);

        // Capture output
        let mut api_server_child = api_server_cmd
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .spawn()
            .expect("Failed to start Python API server command");

        // stdout thread
        if let Some(stdout) = api_server_child.stdout.take() {
            thread::spawn(move || {
                let reader = BufReader::new(stdout);
                for line in reader.lines() {
                    match line {
                        Ok(text) => info!("niu_api output: {}", text),
                        Err(_) => break,
                    }
                }
            });
        }

        // stderr thread
        if let Some(stderr) = api_server_child.stderr.take() {
            thread::spawn(move || {
                let reader = BufReader::new(stderr);
                for line in reader.lines() {
                    match line {
                        Ok(line_text) => {
                            // Filter tqdm progress bar lines (Batches:, \r lines)
                            if line_text.starts_with("Batches:") || line_text.starts_with('\r') {
                                continue;
                            }
                            // Skip lines that are only ANSI escape sequences (tqdm control chars)
                            let stripped = line_text.replace('\x1b', "");
                            if stripped.is_empty() {
                                continue;
                            }
                            // Route log level based on Python logger markers
                            if line_text.contains("| INFO") || line_text.contains("| DEBUG") {
                                info!("niu_api stderr: {}", line_text);
                            } else if line_text.contains("| WARNING") || line_text.contains("| WARN") || line_text.contains(":WARNING") || line_text.contains(":WARN") {
                                warn!("niu_api stderr: {}", line_text);
                            } else if line_text.contains("Error") || line_text.contains("Exception") || line_text.contains("Traceback") {
                                error!("niu_api stderr: {}", line_text);
                            } else {
                                info!("niu_api stderr: {}", line_text);
                            }
                        }
                        Err(_) => break,
                    }
                }
            });
        }

        // Wait for API server to be ready
        // Use a 3-second timeout client for polling to avoid 30s default * 30 retries = 15min hang
        let check_client = reqwest::blocking::Client::builder()
            .timeout(Duration::from_secs(3))
            .build()
            .expect("Failed to build HTTP check client");

        let mut api_ready = false;
        for _ in 0..30 {
            thread::sleep(Duration::from_secs(1));

            // Read startup stage from file (works before HTTP is available —
            // uvicorn doesn't accept connections until lifespan completes).
            if let Ok(stage) = std::fs::read_to_string(
                dirs::home_dir().unwrap_or_default().join(".niu").join(".startup_stage"),
            ) {
                let stage = stage.trim().to_string();
                if !stage.is_empty() {
                    let _ = stage_tx.send(stage);
                }
            }

            let url = format!("http://127.0.0.1:{}/health", port);
            match check_client.get(&url).send() {
                Ok(resp) => {
                    if resp.status().is_success() {
                        api_ready = true;
                        break;
                    }
                }
                Err(_) => {}
            }
        }

        if !api_ready {
            warn!("Python API server may not be ready");
        }
        info!("Python API server started");

        // Wait for preload to complete (embedding-service, MCP tools)
        info!("Waiting for preload to complete...");
        let mut preload_ready = false;
        // 3600 轮 × 0.5s ≈ 30min 墙钟（lifespan 阻塞时连接拒绝使每轮 ≈0.5s）——
        // 覆盖 lifespan 最长 ~335s（LLM 门控阻塞探测 210s + 初始化 ~125s），
        // 防慢模型启动时 preload 耗尽致启动器过早弹配置页（无限重启循环）。
        // v2.6.1：同步更新既有注释（原"180s 超时（原 60s）：脑区 gate 40s+90s"依据
        //   已过时——新依据为 LLM 门控阻塞 150-210s + 初始化 ~125s）
        for i in 0..3600 {
            thread::sleep(Duration::from_millis(500));
            let url = format!("http://127.0.0.1:{}/api/preload-status", port);
            match check_client.get(&url).send() {
                Ok(resp) => {
                    let body = match resp.text() {
                        Ok(b) => b,
                        Err(_) => continue,
                    };

                    // Parse JSON
                    #[derive(Debug, Deserialize)]
                    struct PreloadStatus {
                        ready: bool,
                        uptime: String,
                        #[serde(default)]
                        stage: Option<String>,
                    }
                    let status: PreloadStatus = match serde_json::from_str(&body) {
                        Ok(s) => s,
                        Err(e) => {
                            warn!("Failed to parse preload status: error={}, body={}", e, body);
                            continue;
                        }
                    };
                    // Forward stage text to the splash window for live display
                    if let Some(stage) = &status.stage {
                        let _ = stage_tx.send(stage.clone());
                    }

                    // Log first response or when ready
                    if i == 0 || status.ready {
                        info!(
                            "Preload status check: ready={}, uptime={}, stage={:?}, attempt={}",
                            status.ready, status.uptime, status.stage, i + 1
                        );
                    }

                    if status.ready {
                        preload_ready = true;
                        info!("Preload complete, launching window...");
                        break;
                    }
                }
                Err(_) => {}
            }
        }

        if !preload_ready {
            warn!("Preload may not be complete, proceeding anyway");
        }

        // 损坏检测守卫：若 LightRAG 数据已损坏，主 UI 窗口绝不能启动。
        // integrity_failed 由 Splash 的 StatusCheckResult 处理分支在检测到
        // 损坏时设为 true。这里检查后：
        //   - 不发 splash_tx.send(())（splash 由 ExitApp 路径关闭）
        //   - 不调 launch_window("assistant")（主 UI 不启动）
        //   - 不进入 Electron 监控
        // 直接 fall through 到 cancelled cleanup loop，由 ExitApp 设置的
        // cancelled=true 触发 Python API 子进程的优雅+强制清理。
        if integrity_failed_bg.load(Ordering::SeqCst) {
            info!("LightRAG integrity failed — skipping LLM verification and assistant window launch");
        } else {
            // --- LLM 配置验证 ---
            info!("Checking LLM configuration...");

            #[derive(Debug, Deserialize)]
            struct LlmStatus { ready: bool, #[allow(dead_code)] error: Option<String> }
            #[derive(Debug, Deserialize)]
            struct TestLlmResult { success: bool, message: Option<String>, error: Option<String> }

            let llm_status_url = format!("http://127.0.0.1:{}/api/llm-status", port);
            let llm_configured = match check_client.get(&llm_status_url).send() {
                Ok(resp) if resp.status().is_success() => {
                    match resp.json::<LlmStatus>() {
                        Ok(s) => s.ready,
                        Err(e) => { warn!("Failed to deserialize llm-status response: {}", e); false }
                    }
                }
                _ => false,
            };

            // 判断是否需要打开 settings 窗口让用户重配 LLM
            // - llm_configured=false：直接打开
            // - llm_configured=true 但真实 test 失败（且重试也失败）：打开
            let mut need_settings = false;
            if !llm_configured {
                info!("LLM not configured (llm-status.ready=false), opening settings...");
                need_settings = true;
            } else {
                info!("LLM configured, running real test...");
                // 对齐后端 /api/test-llm 端点 wait_for 上限 220s（resolve_probe_budget
                // 钳制 llm.read_timeout ≤ 190 → wait ≤ 220；逃生口全范围覆盖）——
                // 25s 会让慢首响推理模型（20-120s）在此超时并落入 proceed-anyway 分支
                // 或配置页轮询永久超时，与后端 LLM 门控判定分歧（静默降级/配置页卡死）。
                // 230s 与 settings 前端 socket 超时一致（v2.5：三方统一 230）。
                let test_client = reqwest::blocking::Client::builder()
                    .timeout(Duration::from_secs(230)).build().unwrap_or_else(|_| check_client.clone());
                let test_url = format!("http://127.0.0.1:{}/api/test-llm", port);

                match test_client.post(&test_url)
                    .header("Content-Type", "application/json").body("{}").send() {
                    Ok(resp) if resp.status().is_success() => {
                        if let Ok(v) = resp.json::<TestLlmResult>() {
                            if !v.success {
                                warn!("LLM test failed: {}", v.error.unwrap_or_default());
                                // 重试一次（5 秒后），避免瞬时网络错误误判
                                thread::sleep(Duration::from_secs(5));
                                let retry = test_client.post(&test_url)
                                    .header("Content-Type", "application/json").body("{}").send();
                                let retry_passed = match retry {
                                    Ok(resp) if resp.status().is_success() => {
                                        resp.json::<TestLlmResult>().ok().map_or(false, |v| v.success)
                                    }
                                    _ => false
                                };
                                if retry_passed {
                                    info!("LLM test passed on retry (transient error)");
                                } else {
                                    // 连续两次失败 → 打开配置页面
                                    info!("LLM test failed twice, opening settings...");
                                    need_settings = true;
                                }
                            } else {
                                info!("LLM test passed: {}", v.message.unwrap_or_default());
                            }
                        }
                    }
                    _ => {
                        // 端点出错 → 降级继续启动
                        warn!("LLM test endpoint failed, proceeding anyway");
                    }
                }
            }

            if need_settings {
                // 设共享标志，让后续 launch_window("assistant") 守卫跳过
                llm_config_failed_bg.store(true, Ordering::SeqCst);
                // settings 模式下不关 splash（splash_tx.send(())）—— 测试通过后
                // splash 需要继续显示 "正在关闭所有进程..." 提示，直到 cleanup
                // 完成由 phase_tx 通知 splash 退出。这里启动 settings 窗口，
                // splash 仍留在屏幕底层作为遮罩。
                let settings_result = launch_window("settings", port);
                if let Ok(mut settings_child) = settings_result {
                    let test_url = format!("http://127.0.0.1:{}/api/test-llm", port);
                    // 对齐后端 /api/test-llm 端点 wait_for 上限 220s（resolve_probe_budget
                    // 钳制 llm.read_timeout ≤ 190 → wait ≤ 220；逃生口全范围覆盖）——
                    // 25s 会让慢首响推理模型（20-120s）在此超时并落入 proceed-anyway 分支
                    // 或配置页轮询永久超时，与后端 LLM 门控判定分歧（静默降级/配置页卡死）。
                    // 230s 与 settings 前端 socket 超时一致（v2.5：三方统一 230）。
                    let poll_client = reqwest::blocking::Client::builder()
                        .timeout(Duration::from_secs(230))
                        .build().unwrap_or_else(|_| check_client.clone());
                    // 轮询：直到 settings 测试通过 OR settings 窗口被关闭
                    // 删除 reopen_count>3 重开死循环——settings 一关就 break
                    loop {
                        if cancelled_bg.load(Ordering::SeqCst) { break; }
                        // settings 窗口退出？
                        if let Ok(Some(exit_status)) = settings_child.try_wait() {
                            info!("Settings window closed (exit_status={})", exit_status);
                            break;
                        }
                        thread::sleep(Duration::from_secs(3));
                        if let Ok(resp) = poll_client.post(&test_url)
                            .header("Content-Type", "application/json").body("{}").send() {
                            if let Ok(v) = resp.json::<TestLlmResult>() {
                                if v.success {
                                    info!("LLM test passed after reconfiguration, exiting for restart");
                                    break;
                                }
                            }
                        }
                    }
                } else {
                    error!("Failed to launch settings window: {:?}", settings_result.err());
                }
                // 不论测试通过还是窗口被关，都退出整个进程（让用户重启）
                // 先通知 splash 切到 closing 状态，显示 "正在关闭所有进程..."
                // 提示。splash 会保持显示直到 cleanup 完成由 phase_tx 的
                // CleanupDone 信号触发 iced::exit()。
                let _ = phase_tx.send(SplashPhase::Closing);
                cancelled_bg.store(true, Ordering::SeqCst);
                let _ = notify_shutdown(port);
                info!("LLM settings flow complete, exiting process for user restart");
            } else {
                // LLM 配置正常 → 关 splash + 启 assistant
                let _ = splash_tx.send(());

                // Launch assistant window
                let mut electron_child = match launch_window("assistant", port) {
                    Ok(child) => Some(child),
                    Err(e) => {
                        error!("Failed to launch assistant window: {}", e);
                        println!("\nPlease run manually: cd ui/main && NIU_WINDOW=assistant npm start");
                        // Independent file diagnostic (not subject to logging flag).
                        log_fatal_error(&format!("Failed to launch assistant window: {}", e));
                        None
                    }
                };

                // Monitor Electron process - when it exits, trigger shutdown
                if let Some(mut child) = electron_child.take() {
                    let cancelled_ref = cancelled_bg.clone();
                    thread::spawn(move || {
                        let result = child.wait();
                        match result {
                            Err(e) => {
                                info!("Electron window exited: error={}", e);
                            }
                            Ok(status) => {
                                if status.success() {
                                    info!("Electron window closed normally");
                                } else {
                                    info!("Electron window exited with status: {}", status);
                                }
                            }
                        }
                        cancelled_ref.store(true, Ordering::SeqCst); // Trigger shutdown
                    });
                }
            }
        }

        // Keep the api_server_child alive until the process exits or we get cancelled
        // Wait for cancellation or child exit
        loop {
            if cancelled_bg.load(Ordering::SeqCst) {
                break;
            }
            // Try non-blocking wait on the child
            match api_server_child.try_wait() {
                Ok(Some(status)) => {
                    info!("Python API server exited with status: {}", status);
                    cancelled_bg.store(true, Ordering::SeqCst);
                    break;
                }
                Ok(None) => {
                    // Still running
                }
                Err(e) => {
                    warn!("Error checking API server status: {}", e);
                    break;
                }
            }
            thread::sleep(Duration::from_millis(100));
        }

        // If we're here because of cancellation (not child exit), do the shutdown
        if api_server_child.try_wait().ok().flatten().is_none() {
            // Child is still running, need to shut it down

            // Step 1: Send HTTP shutdown notification first (graceful)
            info!("Notifying Python API to shutdown via HTTP...");
            if let Err(e) = notify_shutdown(port) {
                warn!("Failed to notify Python API shutdown: {}", e);
            }

            // Step 2: Wait for graceful HTTP shutdown to take effect
            thread::sleep(Duration::from_secs(2));

            // Check if it exited after HTTP notification
            if api_server_child.try_wait().ok().flatten().is_some() {
                info!("API server exited gracefully after HTTP notification");
            } else {
                // Step 3: Send SIGTERM (Unix) or kill (Windows)
                #[cfg(unix)]
                {
                    use nix::sys::signal::{self, Signal};
                    use nix::unistd::Pid;
                    let pid = Pid::from_raw(api_server_child.id() as i32);
                    let _ = signal::kill(pid, Signal::SIGTERM);
                }
                #[cfg(windows)]
                {
                    let _ = api_server_child.kill();
                }

                // Step 4: Wait up to 5 seconds for graceful exit after SIGTERM
                let start = std::time::Instant::now();
                loop {
                    match api_server_child.try_wait() {
                        Ok(Some(_)) => {
                            info!("API server exited gracefully after SIGTERM");
                            break;
                        }
                        Ok(None) => {
                            if start.elapsed() >= Duration::from_secs(5) {
                                warn!("API server did not exit in 5s, force killing");
                                // Step 5: SIGKILL
                                #[cfg(unix)]
                                {
                                    use nix::sys::signal::{self, Signal};
                                    use nix::unistd::Pid;
                                    let pid = Pid::from_raw(api_server_child.id() as i32);
                                    let _ = signal::kill(pid, Signal::SIGKILL);
                                }
                                #[cfg(windows)]
                                {
                                    let _ = api_server_child.kill();
                                }

                                // Step 6: Reap the child process after SIGKILL
                                match api_server_child.wait() {
                                    Ok(status) => {
                                        info!("API server reaped after SIGKILL, exit status: {}", status);
                                    }
                                    Err(e) => {
                                        warn!("Failed to reap API server after SIGKILL: {}", e);
                                    }
                                }
                                break;
                            }
                        }
                        Err(_) => break,
                    }
                    thread::sleep(Duration::from_millis(100));
                }
            }
        }

        // All child processes have been reaped. Notify the splash window so it
        // can call iced::exit() and let the main thread return. In the normal
        // (non-settings) flow the splash has already been closed via
        // splash_tx — phase_tx.send will return Err (receiver dropped), which
        // we ignore. In the settings flow the splash is still open showing
        // "正在关闭所有进程..." and needs this signal to exit.
        let _ = phase_tx.send(SplashPhase::CleanupDone);
    });

    // --- Run iced splash window on the main thread (required by macOS) ---
    let splash = Splash::new(
        splash_rx,
        port,
        cancelled.clone(),
        integrity_failed.clone(),
        phase_rx,
        missing_deps,
        stage_rx,
    );
    let window_settings = window::Settings {
        size: iced::Size::new(CARD_WIDTH + 2.0 * SHADOW_MARGIN, CARD_HEIGHT + 2.0 * SHADOW_MARGIN),
        position: window::Position::Centered,
        decorations: false,
        transparent: true,
        resizable: false,
        exit_on_close_request: true,
        ..window::Settings::default()
    };

    if let Err(e) = iced::application(
        "启动中",
        Splash::update,
        Splash::view,
    )
    .window(window_settings)
    .default_font(CJK_FONT)
    .theme(|_| Theme::Dark)
    // 真透明窗口：iced_winit 用 Appearance.background_color 做表面清除色，
    // 深色主题默认是 opaque 黑底；设为 TRANSPARENT 后由系统合成器实现真透明。
    .style(|_state, _theme| iced::daemon::Appearance {
        background_color: iced::Color::TRANSPARENT,
        text_color: iced::Color::from_rgb(0.24, 0.22, 0.20),
    })
    .subscription(|splash: &Splash| splash.subscription())
    .run_with(|| (splash, Task::none()))
    {
        warn!("Splash window error (non-fatal): {}", e);
    }

    // --- After splash closes: wait for Electron window to close or Ctrl-C ---
    info!("Splash window closed, waiting for Electron window to close...");

    // Wait for cancelled flag (set by Electron exit monitor or Ctrl-C)
    while !cancelled.load(Ordering::SeqCst) {
        thread::sleep(Duration::from_millis(100));
    }

    // Shutdown is handled by the background thread:
    // notify_shutdown HTTP -> sleep 2s -> SIGTERM -> sleep 5s -> SIGKILL -> wait()
    // Wait for the background thread to finish cleanup
    let _ = bg_handle.join();

    info!("Niu launcher shutdown complete");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_resources_root_from_exe_macos_bundle() {
        let exe = std::path::Path::new("/Applications/niu.app/Contents/MacOS/niu");
        let result = resources_root_from_exe(exe);
        assert_eq!(result, PathBuf::from("/Applications/niu.app/Contents/Resources"));
    }

    #[test]
    fn test_resources_root_from_exe_macos_bundle_arbitrary() {
        for exe in [
            "REDACTED_USER_PATH/Desktop/niu.app/Contents/MacOS/niu",
            "/Applications/niu.app/Contents/MacOS/niu",
            "/Volumes/USB/niu.app/Contents/MacOS/niu",
        ] {
            let p = std::path::Path::new(exe);
            let result = resources_root_from_exe(p);
            assert!(result.to_string_lossy().ends_with("niu.app/Contents/Resources"));
        }
    }

    #[test]
    fn test_resources_root_from_exe_dev_mode() {
        let exe = std::path::Path::new("REDACTED_USER_PATH/tools/ai-bot/niu");
        let result = resources_root_from_exe(exe);
        assert_eq!(result, PathBuf::from("REDACTED_USER_PATH/tools/ai-bot"));
    }

    #[test]
    fn test_detect_niu_home_smoke() {
        let result = detect_niu_home();
        assert!(result.is_ok());
        assert!(result.unwrap().to_string_lossy().ends_with("/.niu"));
    }
}

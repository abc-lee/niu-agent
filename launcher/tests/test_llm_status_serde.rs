// 测试 LlmStatus 三态反序列化（llm-status 端点新契约）。
// 复制 main.rs 的 LlmStatus 定义（与 IntegrityStatus 范式一致）。
// 注意（R1-P1 修正）：复制定义与 main.rs **解耦**——本测试的价值是锁定 serde 契约
// 语义（probe_failed 缺省 false 兼容旧后端），不是漂移检测（main.rs 改字段本测试
// 照过）；main.rs 决策段语法/类型错误的真正闸门是 Task 2 Step 4 `cargo test`
// 全目标编译（会编译 main.rs bin）。

use serde::Deserialize;

#[derive(Debug, Deserialize)]
struct LlmStatus {
    ready: bool,
    #[serde(default)]
    probe_failed: bool,
    #[allow(dead_code)]
    error: Option<String>,
}

#[test]
fn test_ready_true() {
    let s: LlmStatus = serde_json::from_str(r#"{"ready": true, "probe_failed": false, "error": null}"#).unwrap();
    assert!(s.ready);
    assert!(!s.probe_failed);
}

#[test]
fn test_ready_true_no_error_key() {
    // 新后端成功响应真实形态：无 error key（serde Option 缺省 None）
    let s: LlmStatus = serde_json::from_str(r#"{"ready": true, "probe_failed": false}"#).unwrap();
    assert!(s.ready);
    assert!(!s.probe_failed);
}

#[test]
fn test_probe_failed() {
    let s: LlmStatus = serde_json::from_str(r#"{"ready": false, "probe_failed": true, "error": "LLM connectivity probe failed at startup"}"#).unwrap();
    assert!(!s.ready);
    assert!(s.probe_failed);
}

#[test]
fn test_not_ready_missing_probe_failed_field_defaults_false() {
    // 旧后端/缺字段兼容：probe_failed 缺省 → false（not_ready 语义）
    let s: LlmStatus = serde_json::from_str(r#"{"ready": false, "error": "API key not configured"}"#).unwrap();
    assert!(!s.ready);
    assert!(!s.probe_failed);
}

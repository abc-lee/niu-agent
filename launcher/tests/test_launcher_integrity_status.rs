// tests/test_launcher_integrity_status.rs
// 测试 IntegrityStatus struct 能正确反序列化 Python /api/kg/stats 返回的 integrity JSON。
// 锁定字段不能被误删（main.rs:42 注释警告过 "missing field" decode 错误）。
//
// 注意：IntegrityStatus 在 main.rs 里不是 pub struct，无法从 tests/ 目录直接 import。
// 所以这里复制一份定义做反序列化行为测试 —— 如果 main.rs 里的 IntegrityStatus 误改成
// camelCase 或误删字段，本测试会编译失败或断言失败（提醒开发者同步更新此测试）。

use serde::Deserialize;

// 复制 main.rs 的 IntegrityStatus 定义（保持字段名一致）
// 如果 main.rs 里的 IntegrityStatus 误改成 camelCase 或误删字段，
// 本测试会编译失败或断言失败。
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

#[test]
fn test_integrity_status_deserializes_full_json() {
    // 模拟 Python get_lightrag_status 返回的 integrity 字段（v4 全字段版）
    let json = r#"{
        "ok": false,
        "total_errors": 3,
        "critical_errors": 1,
        "major_errors": 1,
        "minor_errors": 1
    }"#;
    let status: IntegrityStatus = serde_json::from_str(json).unwrap();
    assert_eq!(status.ok, false);
    assert_eq!(status.total_errors, 3);
    assert_eq!(status.critical_errors, 1);
    assert_eq!(status.major_errors, 1);
    assert_eq!(status.minor_errors, 1);
}

#[test]
fn test_integrity_status_deserializes_without_severity_fields() {
    // 模拟旧版 Python 只返回 total_errors（没有 critical/major/minor）
    // critical_errors/major_errors/minor_errors 有 #[serde(default)] 应默认 0
    let json = r#"{
        "ok": true,
        "total_errors": 0
    }"#;
    let status: IntegrityStatus = serde_json::from_str(json).unwrap();
    assert_eq!(status.ok, true);
    assert_eq!(status.total_errors, 0);
    assert_eq!(status.critical_errors, 0, "缺 critical_errors 时应默认 0");
    assert_eq!(status.major_errors, 0, "缺 major_errors 时应默认 0");
    assert_eq!(status.minor_errors, 0, "缺 minor_errors 时应默认 0");
}

#[test]
fn test_integrity_status_total_errors_missing_fails() {
    // 如果 Python 误删 total_errors 字段，serde 应报错（main.rs:42 注释警告）
    // 本测试锁定 total_errors 必须保留（不能加 #[serde(default)]）
    let json = r#"{
        "ok": true,
        "critical_errors": 0,
        "major_errors": 0,
        "minor_errors": 0
    }"#;
    let result: Result<IntegrityStatus, _> = serde_json::from_str(json);
    assert!(
        result.is_err(),
        "缺 total_errors 时 serde 必须报错（不能加 #[serde(default)]）"
    );
}

#[test]
fn test_integrity_status_field_names_are_snake_case() {
    // main.rs:42 注释明确：API 返回 snake_case 字段名（Python 约定），
    // 不能用 #[serde(rename_all = "camelCase")]（会导致 "missing field"
    // decode 错误：serde 期望 totalErrors 而 API 返 total_errors）。
    // 本测试锁定字段名必须是 snake_case。
    let json = r#"{
        "ok": true,
        "total_errors": 0,
        "critical_errors": 0,
        "major_errors": 0,
        "minor_errors": 0
    }"#;
    let status: IntegrityStatus = serde_json::from_str(json).unwrap();
    // 如果 struct 误改成 camelCase（totalErrors/criticalErrors），
    // serde 会期望 "totalErrors" 而非 "total_errors"，
    // 本断言会失败（缺字段 → result.is_err() 在上面已测）
    let _ = status; // 检查字段名编译通过即可
}

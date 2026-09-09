//! niu-natives — Niu 桌面采集/输入 native 绑定（PyO3）。
//!
//! T1 空骨架：仅声明从 oh-my-pi pi-natives `desktop` 模块原样搬运的 `desktop` 子模块。
//! 源码仍含 omp 原生的 napi / crate::task / ax 全量引用，**故意暂不可编译**——
//! T2 手术（ax_lite 裁切 + PyO3 化 + task 链删除）后补 `#[pymodule]`。
//! 见 docs/superpowers/plans/2026-09-09-vision-phase1-niu-natives-plan.md。

mod desktop;

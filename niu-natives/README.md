# niu-natives

Niu 桌面采集/输入 native 绑定（Rust + PyO3，maturin 构建 wheel 装入 `python/`）。

## 出处声明

本 crate 的 `src/desktop/` 源码移植自 [oh-my-pi](https://github.com/can1357/oh-my-pi)
（MIT License）的 `crates/pi-natives/src/desktop` 模块：macOS/Windows 屏幕采集、
窗口枚举、坐标映射与输入注入。版权与许可见本目录 `LICENSE`（含 Niu 修改声明）。

## 构建

T1 阶段为纯搬运脚手架（源码仍含 omp napi 引用，**故意不编译**）；
T2 手术（ax_lite + PyO3 化）后启用 maturin 构建链，见
`docs/superpowers/plans/2026-09-09-vision-phase1-niu-natives-plan.md`。

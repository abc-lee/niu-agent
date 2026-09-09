//! niu-natives — Niu 桌面采集/输入 native 绑定（PyO3）。
//!
//! 移植自 oh-my-pi `pi-natives/src/desktop`（MIT，can1357/oh-my-pi）。
//! T2a 手术（plan v0.4）：删 omp 原生绑定层与 Promise 任务链、ax 全量树 →
//! PyO3 `#[pyclass] DesktopSession`（同步方法，每方法 `py.detach` 释放 GIL）；
//! macOS 仅保留 ax_lite 聚焦子集（`desktop::macos::ax`）。region/geometry
//! wire format 由 T2b 补齐。

mod desktop;
mod version_sentinel {
	include!(concat!(env!("OUT_DIR"), "/version_sentinel.rs"));
}

use pyo3::prelude::*;

/// `niu_natives` — desktop capture/input native bindings for Niu.
#[pymodule]
fn niu_natives(module: &Bound<'_, PyModule>) -> PyResult<()> {
	module.add_class::<desktop::DesktopSession>()?;
	module.add("__version__", env!("CARGO_PKG_VERSION"))?;
	module.add(version_sentinel::SENTINEL, true)?;
	Ok(())
}

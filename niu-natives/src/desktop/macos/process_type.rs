//! Demote this process to a background-only application (macOS).
//!
//! Why: the first call into macOS's capture/display APIs (xcap) makes LaunchServices
//! promote the *same* process (PID unchanged — verified on this machine) from
//! ApplicationType=NULL to **Foreground**, which puts a Python rocket icon in the Dock.
//! Calling `TransformProcessType(..., kProcessTransformToBackgroundApplication)` before
//! any xcap call demotes the process to BackgroundOnly; verified that subsequent
//! `list_displays()` / `capture()` calls do NOT re-promote it and capture still works.

// The crate flattens its generated modules: HIServices items live at the crate root.
use objc2_application_services::{kCurrentProcess, kProcessTransformToBackgroundApplication, TransformProcessType};

/// C-ABI mirror of HIServices' `ProcessSerialNumber` (a `{ u32; 2 }`). The generated
/// binding's struct is crate-private with private fields, so we mirror the layout and
/// cast the pointer — both are `#[repr(C)]` structs of two `u32`, hence identical ABI.
#[repr(C)]
struct Psn {
	high_long_of_psn: u32,
	low_long_of_psn: u32,
}

/// Demote the current process to a background-only application so that using the
/// desktop capture APIs does not make macOS show a Dock icon for it.
///
/// Best-effort by design: never fails. This crate has no logging facility, so a
/// non-zero status (e.g. already background-only) is silently ignored — the demotion
/// is cosmetic and must not break capture.
pub(crate) fn demote_to_background_only() {
	// PSN `{ .highLongOfPSN = 0, .lowLongOfPSN = kCurrentProcess }` targets this process.
	let psn = Psn { high_long_of_psn: 0, low_long_of_psn: kCurrentProcess as u32 };
	// `.cast()` reinterprets the pointer as `*const ProcessSerialNumber` (identical layout).
	let status = unsafe {
		TransformProcessType((&psn as *const Psn).cast(), kProcessTransformToBackgroundApplication)
	};
	let _ = status; // 0 == noErr; anything else is ignored on purpose (see above).
}

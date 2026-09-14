mod ax;
mod capture;
mod input;
mod process_type;
mod skylight;

pub(crate) use self::process_type::demote_to_background_only;

use image::RgbaImage;
use objc2_app_kit::{NSApplicationActivationOptions, NSRunningApplication};
use objc2_core_foundation::{CFDictionary, CFString, CFRetained};
use objc2_foundation::{NSArray, NSString};
use objc2::rc::Retained;

use std::{
	ffi::c_void,
	ptr::NonNull,
	sync::atomic::{AtomicU32, Ordering},
	thread::sleep,
	time::{Duration, Instant},
};

use self::{ax::MacAx, capture::MacCapture, input::MacInput};
use super::{
	backend::{
		AxBackend, Backend, DeliveryMode, DisplayWakeState, PointerEvent, ScreensaverState,
		SessionLockState, SessionReadyState,
	},
	error::{CoreResult, DesktopError},
	frame::FrameGeometry,
	keys::KeyName,
	types::{
		CaptureCaps, DesktopCapabilities, DesktopDisplay, DesktopWindow, DisplaySelector, Target,
	},
};

pub struct MacosBackend {
	capture: MacCapture,
	input:   MacInput,
	ax:      MacAx,
}

impl MacosBackend {
	pub(crate) fn new(display: DisplaySelector) -> CoreResult<Self> {
		Ok(Self {
			capture: MacCapture::new(display),
			input:   MacInput::new()?,
			ax:      MacAx::new(),
		})
	}

	fn require_input_permission() -> CoreResult<()> {
		if ax::is_trusted() {
			Ok(())
		} else {
			Err(DesktopError::permission_denied(
				"macOS Accessibility permission is required for native input",
			))
		}
	}
}

impl Backend for MacosBackend {
	fn capabilities(&mut self) -> DesktopCapabilities {
		let capture_permission = capture::capture_permission();
		let input_permission = ax::is_trusted();
		let display_count = if capture_permission {
			self
				.capture
				.displays()
				.map_or(0, |displays| u32::try_from(displays.len()).unwrap_or(u32::MAX))
		} else {
			0
		};
		DesktopCapabilities {
			backend: "quartz".to_string(),
			display_server: Some("Quartz WindowServer".to_string()),
			capture: capture_permission && display_count > 0,
			input: input_permission,
			// The full AX backend (macos::ax) is gated on the same Accessibility
			// trust as native input.
			ax: input_permission,
			background_window_input: input_permission && skylight::is_available(),
			delivery_modes: vec!["background".to_string(), "foreground".to_string()],
			capture_permission: permission_label(capture_permission),
			input_permission: permission_label(input_permission),
			ax_permission: permission_label(input_permission),
			display_count,
		}
	}

	fn displays(&mut self) -> CoreResult<Vec<DesktopDisplay>> {
		self.capture.displays()
	}

	fn windows(&mut self) -> CoreResult<Vec<DesktopWindow>> {
		self.capture.windows()
	}

	fn capture(
		&mut self,
		target: &Target,
		_caps: &CaptureCaps,
	) -> CoreResult<(RgbaImage, FrameGeometry)> {
		self.capture.capture(target)
	}

	fn pointer(
		&mut self,
		target: &Target,
		event: PointerEvent,
		_frame: &FrameGeometry,
		mode: DeliveryMode,
	) -> CoreResult<()> {
		Self::require_input_permission()?;
		self.input.pointer(target, event, mode, &self.capture)
	}

	fn type_text(&mut self, target: &Target, text: &str, mode: DeliveryMode) -> CoreResult<()> {
		Self::require_input_permission()?;
		self.input.type_text(target, text, mode, &self.capture)
	}

	fn key_chord(
		&mut self,
		target: &Target,
		keys: &[KeyName],
		mode: DeliveryMode,
	) -> CoreResult<()> {
		Self::require_input_permission()?;
		self.input.key_chord(target, keys, mode, &self.capture)
	}

	fn raise_window(&mut self, id: &str) -> CoreResult<()> {
		Self::require_input_permission()?;
		let window = self.capture.window(id)?;
		self.ax.raise(&window)?;
		let pid = window.pid.ok_or_else(|| {
			DesktopError::input_failed(format!("window {id} has no owning process id"))
		})?;
		let pid = i32::try_from(pid).map_err(|_| {
			DesktopError::input_failed(format!("window {id} has an invalid process id"))
		})?;
		let app =
			NSRunningApplication::runningApplicationWithProcessIdentifier(pid).ok_or_else(|| {
				DesktopError::window_not_found(format!(
					"application for window '{id}' is no longer running"
				))
			})?;
		if !app.activateWithOptions(NSApplicationActivationOptions::empty()) {
			return Err(DesktopError::input_failed(format!(
				"activation request for window '{id}' was rejected"
			)));
		}
		Ok(())
	}

	fn session_ready(&mut self) -> SessionReadyState {
		let deadline = Instant::now() + SESSION_READY_WAIT;
		// ① Dismiss a running screensaver (standard quit Apple event via AppKit).
		let mut screensaver = ScreensaverState::None;
		if screensaver_running() {
			screensaver = if dismiss_screensaver() && wait_until(deadline, || !screensaver_running())
			{
				ScreensaverState::Dismissed
			} else {
				ScreensaverState::DismissFailed
			};
		}
		// ② Wake an asleep display (IOKit user-activity assertion).
		let displays_up = || self.capture.displays().is_ok_and(|displays| !displays.is_empty());
		let mut display = DisplayWakeState::Awake;
		if !displays_up() {
			wake_display();
			display = if wait_until(deadline, &displays_up) {
				DisplayWakeState::Woken
			} else {
				DisplayWakeState::StillAsleep
			};
		}
		// ③ Report the lock state — a fact only; it is never acted on here.
		SessionReadyState { screensaver, display, locked: session_locked() }
	}

	fn ax(&mut self) -> Option<&mut dyn AxBackend> {
		Some(&mut self.ax)
	}
}

fn permission_label(granted: bool) -> String {
	if granted {
		"granted".to_string()
	} else {
		"denied".to_string()
	}
}

// --- Session readiness (screensaver dismissal + display wake + lock state) ---
//
// macOS standard approach: quit the screensaver app via AppKit's public API
// (`NSRunningApplication.terminate()` — a standard quit Apple event), wake an
// asleep display with IOKit `IOPMAssertionDeclareUserActivity` (the same
// primitive as `caffeinate -u`), and read the session lock state from the
// CoreGraphics session dictionary. A real lock screen is reported, never
// bypassed.

const SCREENSAVER_BUNDLE_ID: &str = "com.apple.ScreenSaver.Engine";
/// Bounded wait for dismiss/wake to take effect (plan D-A ③).
const SESSION_READY_WAIT: Duration = Duration::from_millis(1500);
const SESSION_READY_POLL: Duration = Duration::from_millis(100);

/// The assertion name must be a non-NULL CFString (IOPMLib.h); it is minted per
/// call because CFString is not `Sync`. The *assertion id* is what gets reused
/// across calls so repeated readiness checks do not mint a new assertion (R7).
static WAKE_ASSERTION_ID: AtomicU32 = AtomicU32::new(0);

#[link(name = "IOKit", kind = "framework")]
extern "C" {
	/// Declares user activity (wakes the display). `assertion_name` must not be
	/// NULL; passing a previously returned assertion id in `assertion_id` reuses
	/// that assertion instead of creating a new one. Returns kern_return_t.
	fn IOPMAssertionDeclareUserActivity(
		assertion_name: *const CFString,
		type_of_user_activity: i32,
		assertion_id: *mut u32,
	) -> i32;
}

/// kIOPMUserActiveLocal — the first enumerator of IOPMUserActiveType
/// (IOKit/pwr_mgt/IOPMLib.h); `kIOPMUserActiveRemote` is 1.
const K_IOPM_USER_ACTIVE_LOCAL: i32 = 0;

fn wake_display() {
	let mut id = WAKE_ASSERTION_ID.load(Ordering::Relaxed);
	let name = CFString::from_str("niu-desktop-session-ready");
	// SAFETY: `name` stays retained for the synchronous IOKit call; `id` is
	// in/out per the IOKit contract.
	let kr = unsafe {
		IOPMAssertionDeclareUserActivity(
			CFRetained::as_ptr(&name).as_ptr(),
			K_IOPM_USER_ACTIVE_LOCAL,
			&mut id,
		)
	};
	if kr == 0 {
		WAKE_ASSERTION_ID.store(id, Ordering::Relaxed);
	}
}

fn screensaver_apps() -> Retained<NSArray<NSRunningApplication>> {
	let key = NSString::from_str(SCREENSAVER_BUNDLE_ID);
	NSRunningApplication::runningApplicationsWithBundleIdentifier(&key)
}

fn screensaver_running() -> bool {
	!screensaver_apps().is_empty()
}

/// Sends the standard quit request to every running screensaver instance.
/// Returns whether the request was accepted (the app may still linger — the
/// caller polls for its disappearance).
fn dismiss_screensaver() -> bool {
	let apps = screensaver_apps();
	if apps.is_empty() {
		return false;
	}
	// ScreenSaverEngine is a single-instance app; quit it.
	apps.objectAtIndex(0).terminate()
}

#[link(name = "CoreGraphics", kind = "framework")]
extern "C" {
	/// Undocumented SPI: returns a +1 CFDictionary describing the current GUI
	/// session, or NULL when there is no session. The `CGSSessionScreenIsLocked`
	/// key (itself an undocumented SPI) holds a boolean while the screen is
	/// locked and is absent when it is not.
	fn CGSessionCopyCurrentDictionary() -> *const c_void;
}

fn session_locked() -> SessionLockState {
	use objc2_core_foundation::kCFBooleanTrue;
	let dict_ptr = unsafe { CGSessionCopyCurrentDictionary() };
	if dict_ptr.is_null() {
		return SessionLockState::Unknown; // no session dictionary → unknown
	}
	// SAFETY: the SPI returned a +1 reference we now own; from_raw takes it.
	let dict = unsafe { CFRetained::from_raw(NonNull::new_unchecked(dict_ptr as *mut CFDictionary)) };
	let key = CFString::from_str("CGSSessionScreenIsLocked");
	// SAFETY: `key` is a valid CFString; value() returns a borrowed pointer.
	let value = unsafe { dict.value(CFRetained::as_ptr(&key).as_ptr().cast()) };
	if value.is_null() {
		SessionLockState::Unlocked // key absent → not locked
	} else if (unsafe { kCFBooleanTrue }).is_some_and(|t| t as *const _ as *const c_void == value) {
		SessionLockState::Locked
	} else {
		SessionLockState::Unlocked // explicit False (or non-boolean) → not locked
	}
}

/// Polls `probe` until it returns true or the deadline passes.
fn wait_until(deadline: Instant, mut probe: impl FnMut() -> bool) -> bool {
	loop {
		if probe() {
			return true;
		}
		let now = Instant::now();
		if now >= deadline {
			return false;
		}
		sleep((deadline - now).min(SESSION_READY_POLL));
	}
}

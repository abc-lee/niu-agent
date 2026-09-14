#[cfg(target_os = "windows")]
mod ax;
#[cfg(target_os = "windows")]
mod capture;
pub mod delivery;
#[cfg(target_os = "windows")]
mod input;

#[cfg(target_os = "windows")]
use enigo::Enigo;
#[cfg(target_os = "windows")]
use image::RgbaImage;

#[cfg(target_os = "windows")]
use std::{
	ffi::c_void,
	mem::size_of,
	thread::sleep,
	time::{Duration, Instant},
};
#[cfg(target_os = "windows")]
use windows_sys::Win32::{
	System::{
		Power::{ES_DISPLAY_REQUIRED, SetThreadExecutionState},
		StationsAndDesktops::{
			CloseDesktop, DESKTOP_READOBJECTS, GetUserObjectInformationW, HDESK, OpenInputDesktop,
			UOI_NAME,
		},
	},
	UI::{
		Input::KeyboardAndMouse::{
			INPUT, INPUT_0, INPUT_MOUSE, MOUSEEVENTF_MOVE, MOUSEINPUT, SendInput,
		},
		WindowsAndMessaging::{SPI_GETSCREENSAVERRUNNING, SystemParametersInfoW},
	},
};

#[cfg(target_os = "windows")]
use self::ax::Win32Ax;
#[cfg(target_os = "windows")]
use super::backend::{
	AxBackend, Backend, DeliveryMode, DisplayWakeState, PointerEvent, ScreensaverState,
	SessionLockState, SessionReadyState,
};
#[cfg(target_os = "windows")]
use super::error::CoreResult;
#[cfg(target_os = "windows")]
use super::frame::FrameGeometry;
#[cfg(target_os = "windows")]
use super::keys::KeyName;
#[cfg(target_os = "windows")]
use super::types::{
	CaptureCaps, DesktopCapabilities, DesktopDisplay, DesktopWindow, DisplaySelector, Target,
};

#[cfg(target_os = "windows")]
pub(crate) struct Win32Backend {
	display:      DisplaySelector,
	global_input: Enigo,
	ax:           Win32Ax,
}

#[cfg(target_os = "windows")]
impl Win32Backend {
	pub(crate) fn new(display: DisplaySelector) -> CoreResult<Self> {
		// Initialize DPI awareness before xcap or input observes desktop geometry,
		// keeping both APIs in the same per-monitor physical coordinate regime.
		let global_input = input::create_global_input()?;
		let _ = capture::displays(&display)?;
		Ok(Self { display, global_input, ax: Win32Ax::new() })
	}
}

#[cfg(target_os = "windows")]
impl Backend for Win32Backend {
	fn capabilities(&mut self) -> DesktopCapabilities {
		let display_count = capture::displays(&self.display)
			.map(|displays| displays.len().min(u32::MAX as usize) as u32)
			.unwrap_or(0);
		DesktopCapabilities {
			backend: "win32".to_string(),
			display_server: Some("win32".to_string()),
			capture: display_count > 0,
			input: true,
			ax: true,
			background_window_input: true,
			delivery_modes: vec!["background".to_string(), "foreground".to_string()],
			capture_permission: if display_count > 0 {
				"granted"
			} else {
				"unknown"
			}
			.to_string(),
			input_permission: "granted".to_string(),
			ax_permission: "granted".to_string(),
			display_count,
		}
	}

	fn displays(&mut self) -> CoreResult<Vec<DesktopDisplay>> {
		capture::displays(&self.display)
	}

	fn windows(&mut self) -> CoreResult<Vec<DesktopWindow>> {
		capture::windows()
	}

	fn capture(
		&mut self,
		target: &Target,
		_caps: &CaptureCaps,
	) -> CoreResult<(RgbaImage, FrameGeometry)> {
		capture::capture(&self.display, target)
	}

	fn pointer(
		&mut self,
		target: &Target,
		event: PointerEvent,
		_frame: &FrameGeometry,
		mode: DeliveryMode,
	) -> CoreResult<()> {
		input::pointer(&mut self.global_input, target, event, mode)
	}

	fn type_text(&mut self, target: &Target, text: &str, mode: DeliveryMode) -> CoreResult<()> {
		input::type_text(&mut self.global_input, target, text, mode)
	}

	fn key_chord(
		&mut self,
		target: &Target,
		keys: &[KeyName],
		mode: DeliveryMode,
	) -> CoreResult<()> {
		input::key_chord(&mut self.global_input, target, keys, mode)
	}

	fn raise_window(&mut self, id: &str) -> CoreResult<()> {
		input::raise_window(id)
	}

	fn session_ready(&mut self) -> SessionReadyState {
		let deadline = Instant::now() + session::SESSION_READY_WAIT;
		// ① Dismiss a running screensaver (synthesized 1px relative mouse move,
		//    immediately reset — the only input this path ever sends).
		let mut screensaver = ScreensaverState::None;
		let mut input_blocked = false;
		if session::screensaver_running() {
			if session::nudge_mouse() {
				screensaver = if session::wait_until(deadline, || !session::screensaver_running()) {
					ScreensaverState::Dismissed
				} else {
					ScreensaverState::DismissFailed
				};
			} else {
				// SendInput short count: input is blocked (secure desktop/UIPI).
				// Not retryable; also a strong "not the default desktop" signal.
				screensaver = ScreensaverState::DismissFailed;
				input_blocked = true;
			}
		}
		// ② Wake an asleep display (one-shot SetThreadExecutionState — never
		//    ES_CONTINUOUS, which would keep the display awake indefinitely).
		let displays_up = || {
			capture::displays(&DisplaySelector::All)
				.is_ok_and(|displays| !displays.is_empty())
		};
		let mut display = DisplayWakeState::Awake;
		if !displays_up() {
			// SAFETY: state-setting call with no preconditions.
			unsafe { SetThreadExecutionState(ES_DISPLAY_REQUIRED) };
			display = if session::wait_until(deadline, &displays_up) {
				DisplayWakeState::Woken
			} else {
				DisplayWakeState::StillAsleep
			};
		}
		// ③ Report the lock state (fact only — never acted on here).
		let mut locked = session::session_desktop_locked();
		if input_blocked && !matches!(locked, SessionLockState::Locked) {
			locked = SessionLockState::Unknown;
		}
		SessionReadyState { screensaver, display, locked }
	}

	fn ax(&mut self) -> Option<&mut dyn AxBackend> {
		Some(&mut self.ax)
	}
}

#[cfg(target_os = "windows")]
mod session {
	use super::*;

	/// Bounded wait for dismiss/wake to take effect (plan D-A ③).
	pub(super) const SESSION_READY_WAIT: Duration = Duration::from_millis(1500);
	const SESSION_READY_POLL: Duration = Duration::from_millis(100);

	/// Whether the Windows screensaver is currently running.
	pub(super) fn screensaver_running() -> bool {
		// SAFETY: SPI_GETSCREENSAVERRUNNING has no preconditions; `value` is a
		// writable u32 as the API requires.
		unsafe {
			let mut value: u32 = 0;
			let ok = SystemParametersInfoW(
				SPI_GETSCREENSAVERRUNNING,
				0,
				&mut value as *mut u32 as *mut c_void,
				0,
			);
			ok != 0 && value != 0
		}
	}

	fn mouse_nudge(dx: i32) -> INPUT {
		INPUT {
			r#type:    INPUT_MOUSE,
			Anonymous: INPUT_0 {
				mi: MOUSEINPUT {
					dx,
					dy:          0,
					mouseData:   0,
					dwFlags:     MOUSEEVENTF_MOVE,
					time:        0,
					dwExtraInfo: 0,
				},
			},
		}
	}

	/// Sends a 1px relative mouse move followed by its reset (the move doubles
	/// as a display wake). Returns whether every event was accepted; a short
	/// count means input was blocked and must not be retried.
	pub(super) fn nudge_mouse() -> bool {
		let events = [mouse_nudge(1), mouse_nudge(-1)];
		// SAFETY: fixed, fully initialized INPUTs copied synchronously.
		let sent = unsafe { SendInput(events.len() as u32, events.as_ptr(), size_of::<INPUT>() as i32) };
		sent == events.len() as u32
	}

	/// Reads the name of the current input desktop. A desktop other than
	/// `default` (case-insensitive) means a secure desktop (lock/logon). The
	/// handle is always closed.
	pub(super) fn session_desktop_locked() -> SessionLockState {
		// SAFETY: access rights limited to reading object names; no preconditions.
		let desk: HDESK = unsafe { OpenInputDesktop(0, 0, DESKTOP_READOBJECTS) };
		if desk.is_null() {
			return SessionLockState::Unknown;
		}
		const NAME_LEN: usize = 128; // desktop names are short ("default", "Winlogon")
		let mut name = [0u16; NAME_LEN];
		let mut needed = 0u32;
		// SAFETY: `name` is a writable buffer sized for any real desktop name.
		let ok = unsafe {
			GetUserObjectInformationW(
				desk as *mut c_void,
				UOI_NAME,
				name.as_mut_ptr() as *mut c_void,
				(NAME_LEN * size_of::<u16>()) as u32,
				&mut needed,
			)
		};
		// SAFETY: `desk` is a valid HDESK from OpenInputDesktop.
		unsafe { CloseDesktop(desk) };
		if ok == 0 {
			return SessionLockState::Unknown;
		}
		let len = name.iter().position(|unit| *unit == 0).unwrap_or(NAME_LEN);
		let mut desktop = String::new();
		for unit in &name[..len] {
			if let Some(ch) = char::from_u32(u32::from(*unit)) {
				desktop.push(ch);
			}
		}
		if desktop.eq_ignore_ascii_case("default") {
			SessionLockState::Unlocked
		} else {
			SessionLockState::Locked
		}
	}

	/// Polls `probe` until it returns true or the deadline passes.
	pub(super) fn wait_until(deadline: Instant, mut probe: impl FnMut() -> bool) -> bool {
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
}

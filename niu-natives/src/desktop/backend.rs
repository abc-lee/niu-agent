use image::RgbaImage;

use super::{
	ax::{AxHandle, AxProps},
	error::{CoreResult, DesktopError},
	frame::FrameGeometry,
	keys::KeyName,
	types::{CaptureCaps, DesktopCapabilities, DesktopDisplay, DesktopWindow, Target},
};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum DeliveryMode {
	#[default]
	Background,
	Foreground,
}

impl DeliveryMode {
	pub(crate) fn parse(value: Option<&str>) -> Self {
		if value.is_some_and(|value| value.trim().eq_ignore_ascii_case("foreground")) {
			Self::Foreground
		} else {
			Self::Background
		}
	}
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum MouseButton {
	#[default]
	Left,
	Right,
	Middle,
}

impl MouseButton {
	pub(crate) fn parse(value: Option<&str>) -> CoreResult<Self> {
		match value.map(str::trim) {
			None => Ok(Self::Left),
			Some(value) if value.eq_ignore_ascii_case("left") => Ok(Self::Left),
			Some(value) if value.eq_ignore_ascii_case("right") => Ok(Self::Right),
			Some(value) if value.eq_ignore_ascii_case("middle") => Ok(Self::Middle),
			Some(value) => Err(DesktopError::input_failed(format!("unknown button '{value}'"))),
		}
	}
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Modifiers {
	pub ctrl:  bool,
	pub alt:   bool,
	pub shift: bool,
	pub meta:  bool,
}

#[derive(Debug, Clone)]
pub enum PointerEvent {
	Click {
		x:         f64,
		y:         f64,
		button:    MouseButton,
		count:     u32,
		modifiers: Modifiers,
	},
	Move {
		x: f64,
		y: f64,
	},
	Drag {
		path:      Vec<(f64, f64)>,
		button:    MouseButton,
		modifiers: Modifiers,
	},
	Scroll {
		x:  f64,
		y:  f64,
		dx: f64,
		dy: f64,
	},
}

/// Screensaver phase reported by [`Backend::session_ready`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ScreensaverState {
	/// No screensaver was running.
	None,
	/// A screensaver was running and its dismissal was verified.
	Dismissed,
	/// A screensaver was running but could not be verified as dismissed.
	DismissFailed,
}

impl ScreensaverState {
	pub(crate) const fn as_str(self) -> &'static str {
		match self {
			Self::None => "none",
			Self::Dismissed => "dismissed",
			Self::DismissFailed => "dismiss_failed",
		}
	}
}

/// Display wake phase reported by [`Backend::session_ready`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DisplayWakeState {
	/// Displays were already available.
	Awake,
	/// Displays were unavailable and a wake was verified as effective.
	Woken,
	/// Displays were unavailable and a wake could not be verified as effective.
	StillAsleep,
}

impl DisplayWakeState {
	pub(crate) const fn as_str(self) -> &'static str {
		match self {
			Self::Awake => "awake",
			Self::Woken => "woken",
			Self::StillAsleep => "still_asleep",
		}
	}
}

/// Session lock state reported by [`Backend::session_ready`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SessionLockState {
	Unlocked,
	Locked,
	/// The lock state could not be determined; callers must treat this as
	/// "not locked" (fail-open) — only [`Self::Locked`] triggers user-facing
	/// unlock requests.
	Unknown,
}

impl SessionLockState {
	pub(crate) const fn as_str(self) -> &'static str {
		match self {
			Self::Unlocked => "false",
			Self::Locked   => "true",
			Self::Unknown  => "unknown",
		}
	}
}

/// Facts about the desktop session after a readiness pass.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SessionReadyState {
	pub screensaver: ScreensaverState,
	pub display:   DisplayWakeState,
	pub locked:    SessionLockState,
}

pub trait Backend: Send {
	fn capabilities(&mut self) -> DesktopCapabilities;
	fn displays(&mut self) -> CoreResult<Vec<DesktopDisplay>>;
	fn windows(&mut self) -> CoreResult<Vec<DesktopWindow>>;
	fn capture(
		&mut self,
		target: &Target,
		caps: &CaptureCaps,
	) -> CoreResult<(RgbaImage, FrameGeometry)>;
	fn pointer(
		&mut self,
		target: &Target,
		ev: PointerEvent,
		frame: &FrameGeometry,
		mode: DeliveryMode,
	) -> CoreResult<()>;
	fn type_text(&mut self, target: &Target, text: &str, mode: DeliveryMode) -> CoreResult<()>;
	fn key_chord(&mut self, target: &Target, keys: &[KeyName], mode: DeliveryMode)
	-> CoreResult<()>;
	fn raise_window(&mut self, id: &str) -> CoreResult<()>;
	/// Ensure the desktop session is ready for capture/input: dismiss a running
	/// screensaver, wake an asleep display (bounded wait for effect), and report
	/// the lock state. Never fails — it returns facts only; a real lock screen is
	/// reported (`locked`), never bypassed.
	fn session_ready(&mut self) -> SessionReadyState;
	fn ax(&mut self) -> Option<&mut dyn AxBackend>;
}

pub trait AxBackend {
	fn window_root(&mut self, win: &DesktopWindow) -> CoreResult<AxHandle>;
	fn props(&mut self, h: &AxHandle) -> CoreResult<AxProps>;
	fn children(&mut self, h: &AxHandle) -> CoreResult<Vec<AxHandle>>;
	fn parent(&mut self, h: &AxHandle) -> CoreResult<Option<AxHandle>>;
	fn perform(&mut self, h: &AxHandle, action: &str) -> CoreResult<()>;
	fn set_value(&mut self, h: &AxHandle, value: &str) -> CoreResult<()>;
	fn focus(&mut self, h: &AxHandle) -> CoreResult<()>;
	fn element_at(&mut self, x: f64, y: f64) -> CoreResult<Option<AxHandle>>;
	fn focused_element(&mut self) -> CoreResult<Option<AxHandle>>;
	fn attributes(&mut self, h: &AxHandle) -> CoreResult<Vec<(String, String)>>;
}

#[cfg(test)]
mod tests {
	use super::*;

	#[test]
	fn delivery_only_escalates_explicit_foreground() {
		assert_eq!(DeliveryMode::parse(None), DeliveryMode::Background);
		assert_eq!(DeliveryMode::parse(Some("garbage")), DeliveryMode::Background);
		assert_eq!(DeliveryMode::parse(Some(" FoReGrOuNd ")), DeliveryMode::Foreground);
	}
}

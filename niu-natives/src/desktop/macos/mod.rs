mod ax;
mod capture;
mod input;
mod process_type;
mod skylight;

pub(crate) use self::process_type::demote_to_background_only;

use image::RgbaImage;

use self::{capture::MacCapture, input::MacInput};
use super::{
	backend::{Backend, DeliveryMode, PointerEvent},
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
}

impl MacosBackend {
	pub(crate) fn new(display: DisplaySelector) -> CoreResult<Self> {
		Ok(Self {
			capture: MacCapture::new(display),
			input:   MacInput::new()?,
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
			// ax_lite keeps only the focus subset — full accessibility traversal
			// is not exposed.
			ax: false,
			background_window_input: input_permission && skylight::is_available(),
			delivery_modes: vec!["background".to_string(), "foreground".to_string()],
			capture_permission: permission_label(capture_permission),
			input_permission: permission_label(input_permission),
			ax_permission: "unavailable".to_string(),
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
}

fn permission_label(granted: bool) -> String {
	if granted {
		"granted".to_string()
	} else {
		"denied".to_string()
	}
}

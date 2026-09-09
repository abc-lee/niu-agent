//! macOS Accessibility focus subset (ax_lite).
//!
//! T2a trim of the omp `MacAx` accessibility tree code: only the window-focus
//! helpers survive as free functions — `is_trusted` / `prepare_foreground_input`
//! / `window_root` / `perform` — with the handle type localized to
//! `CFRetained<AXUIElement>`. Everything tree-shaped (props/children/parent/
//! hit-test/snapshot/registry) was removed with the desktop `ax` module.

use std::{
	collections::HashSet,
	ffi::c_void,
	mem,
	ptr::{self, NonNull},
	sync::{LazyLock, Mutex},
	thread,
	time::Duration,
};

use objc2_application_services::{AXError, AXIsProcessTrusted, AXUIElement, AXValue, AXValueType};
use objc2_core_foundation::{CFArray, CFBoolean, CFRetained, CFString, CFType, CGPoint, CGSize};

use super::super::{
	error::{CoreResult, DesktopError},
	types::DesktopWindow,
};

const AX_TIMEOUT_SECONDS: f32 = 2.0;

/// Localized accessibility handle: a retained application/window element.
type Handle = CFRetained<AXUIElement>;

type GetWindowIdFn = unsafe extern "C" fn(&AXUIElement, *mut u32) -> AXError;

static GET_WINDOW_ID: LazyLock<Option<GetWindowIdFn>> = LazyLock::new(|| {
	// SAFETY: The symbol name is a static NUL-terminated string for process-wide
	// lookup.
	let symbol = unsafe { libc::dlsym(libc::RTLD_DEFAULT, c"_AXUIElementGetWindow".as_ptr()) };
	if symbol.is_null() {
		None
	} else {
		// SAFETY: `_AXUIElementGetWindow` has the exact AXUIElementRef, CGWindowID* ->
		// AXError ABI above.
		Some(unsafe { mem::transmute::<*mut c_void, GetWindowIdFn>(symbol) })
	}
});

/// Processes already asked to expose their renderer accessibility tree.
static MANUAL_ACCESSIBILITY: LazyLock<Mutex<HashSet<libc::pid_t>>> =
	LazyLock::new(|| Mutex::new(HashSet::new()));

#[link(name = "ApplicationServices", kind = "framework")]
unsafe extern "C" {
	fn AXUIElementCreateApplication(pid: libc::pid_t) -> *mut AXUIElement;
}

/// Whether this process holds macOS Accessibility (TCC) trust — the gate for
/// native input injection.
pub(super) fn is_trusted() -> bool {
	// SAFETY: This non-prompting TCC query takes no arguments and only reads
	// current trust state.
	unsafe { AXIsProcessTrusted() }
}

/// Resolve the accessibility root of `window` and raise it, making the
/// addressed window the app's main/focused window while foreground delivery
/// has deliberately activated the app. Best-effort at the input callsite
/// because keyboard delivery must still work without AX trust.
pub(super) fn prepare_foreground_input(window: &DesktopWindow) -> CoreResult<()> {
	let root = window_root(window)?;
	let element: &AXUIElement = &root;
	for attribute in ["AXMain", "AXFocused"] {
		let attribute = CFString::from_str(attribute);
		// SAFETY: The retained element, attribute, and singleton CFBoolean remain
		// valid for the synchronous setter call.
		let _ = unsafe { element.set_attribute_value(&attribute, CFBoolean::new(true)) };
	}
	perform(&root, "AXRaise")
}

/// Resolve the accessibility element for the native `window`, by window-id SPI
/// when available, otherwise by title+bounds matching.
pub(super) fn window_root(win: &DesktopWindow) -> CoreResult<Handle> {
	ensure_trusted()?;
	let pid = win.pid.ok_or_else(|| {
		DesktopError::ax_failed(format!("window {} has no owning process id", win.id))
	})?;
	let pid = i32::try_from(pid).map_err(|_| {
		DesktopError::ax_failed(format!("window {} has an invalid process id", win.id))
	})?;
	let app = create_application(pid)?;
	set_timeout(&app)?;
	enable_web_accessibility(pid, &app);
	let windows = copy_elements(&app, "AXWindows")?;
	let expected_id = win.id.parse::<u32>().ok();
	if let (Some(get_id), Some(expected_id)) = (*GET_WINDOW_ID, expected_id) {
		for element in &windows {
			let mut actual_id = 0u32;
			// SAFETY: `actual_id` is writable and this retained AX element remains alive
			// for the call.
			if unsafe { get_id(element, &mut actual_id) } == AXError::Success
				&& actual_id == expected_id
			{
				set_timeout(element)?;
				return Ok(element.clone());
			}
		}
	}
	// Older systems may hide the private window-id SPI. Match title and global
	// frame together, then title alone only when it is unique.
	let mut title_match = None;
	for element in windows {
		let title = copy_string(&element, "AXTitle").unwrap_or_default();
		if title != win.title {
			continue;
		}
		if bounds(&element).is_some_and(|bounds| bounds_matches_window(bounds, win)) {
			set_timeout(&element)?;
			return Ok(element);
		}
		if title_match.is_some() {
			title_match = None;
			break;
		}
		title_match = Some(element);
	}
	let element = title_match.ok_or_else(|| {
		DesktopError::ax_failed(format!(
			"accessibility window for native window {} ('{}') was not found",
			win.id, win.title,
		))
	})?;
	set_timeout(&element)?;
	Ok(element)
}

/// Perform a named AX action (e.g. `"AXRaise"`, `"AXPress"`) on `handle`.
pub(super) fn perform(handle: &Handle, action: &str) -> CoreResult<()> {
	let element: &AXUIElement = handle;
	let native = action_name(action);
	let action = CFString::from_str(&native);
	// SAFETY: The retained element and action CFString remain valid for the
	// synchronous AX request.
	let error = unsafe { element.perform_action(&action) };
	ax_result(error, format!("AX action '{native}' failed"))
}

fn ensure_trusted() -> CoreResult<()> {
	if is_trusted() {
		Ok(())
	} else {
		Err(DesktopError::permission_denied(
			"macOS Accessibility permission is not granted for this process",
		))
	}
}

fn create_application(pid: libc::pid_t) -> CoreResult<CFRetained<AXUIElement>> {
	// SAFETY: AXUIElementCreateApplication accepts any process id and returns a +1
	// retained CF object.
	let raw = unsafe { AXUIElementCreateApplication(pid) };
	let pointer = NonNull::new(raw).ok_or_else(|| {
		DesktopError::ax_failed(format!("AXUIElementCreateApplication({pid}) returned null"))
	})?;
	// SAFETY: Create-rule ownership transfers the +1 AXUIElement reference into
	// CFRetained.
	Ok(unsafe { CFRetained::from_raw(pointer) })
}

/// Chromium-family apps build their renderer accessibility tree lazily. Reading
/// the application role activates modern Chrome's native AX mode, while older
/// Chromium/Electron builds also honor `AXManualAccessibility`. A process that
/// rejects the manual setter incurs no readiness delay.
fn enable_web_accessibility(pid: libc::pid_t, app: &AXUIElement) {
	// Modern Chromium treats an assistive client's role query as the activation
	// signal. Older Chromium/Electron builds use the manual setter below.
	let _ = copy_string(app, "AXRole");
	{
		let mut enabled = MANUAL_ACCESSIBILITY
			.lock()
			.unwrap_or_else(|error| error.into_inner());
		if !enabled.insert(pid) {
			return;
		}
		let attribute = CFString::from_str("AXManualAccessibility");
		// SAFETY: The retained element, attribute, and singleton CFBoolean remain
		// valid for the synchronous setter call.
		let error = unsafe { app.set_attribute_value(&attribute, CFBoolean::new(true)) };
		if error != AXError::Success {
			// Manual activation is unsupported; leave no stale pid marker.
			enabled.remove(&pid);
			return;
		}
	}
	// The renderers publish their trees over IPC after the switch flips, so the
	// first snapshot would otherwise race a still-empty web area.
	thread::sleep(Duration::from_millis(500));
}

fn set_timeout(element: &AXUIElement) -> CoreResult<()> {
	// SAFETY: The retained AX element remains valid for the synchronous timeout
	// update.
	let error = unsafe { element.set_messaging_timeout(AX_TIMEOUT_SECONDS) };
	ax_result(error, "AXUIElementSetMessagingTimeout(2.0) failed")
}

fn copy_attribute_result(
	element: &AXUIElement,
	attribute: &str,
) -> Result<Option<CFRetained<CFType>>, AXError> {
	let attribute = CFString::from_str(attribute);
	let mut output: *const CFType = ptr::null();
	let slot = NonNull::from(&mut output);
	// SAFETY: `slot` is writable and receives a create-rule retained CF object on
	// success.
	let error = unsafe { element.copy_attribute_value(&attribute, slot) };
	if error != AXError::Success {
		return Err(error);
	}
	let Some(pointer) = NonNull::new(output.cast_mut()) else {
		return Ok(None);
	};
	// SAFETY: AXUIElementCopyAttributeValue returns a +1 object on success.
	Ok(Some(unsafe { CFRetained::from_raw(pointer) }))
}

fn copy_attribute(element: &AXUIElement, attribute: &str) -> Option<CFRetained<CFType>> {
	copy_attribute_result(element, attribute).ok().flatten()
}

fn copy_string(element: &AXUIElement, attribute: &str) -> Option<String> {
	let value = copy_attribute(element, attribute)?;
	if let Ok(value) = value.downcast::<CFString>() {
		Some(value.to_string())
	} else {
		None
	}
}

fn copy_elements(
	element: &AXUIElement,
	attribute: &str,
) -> CoreResult<Vec<CFRetained<AXUIElement>>> {
	copy_elements_optional(element, attribute)
		.ok_or_else(|| DesktopError::ax_failed(format!("copying {attribute} failed")))
}

fn copy_elements_optional(
	element: &AXUIElement,
	attribute: &str,
) -> Option<Vec<CFRetained<AXUIElement>>> {
	let array = copy_attribute(element, attribute)?
		.downcast::<CFArray>()
		.ok()?;
	// SAFETY: AXWindows is a documented CFArray<AXUIElement> value.
	let array = unsafe { CFRetained::cast_unchecked::<CFArray<CFType>>(array) };
	Some(
		array
			.iter()
			.filter_map(|value| value.downcast::<AXUIElement>().ok())
			.collect(),
	)
}

/// Minimal frame of an AX element, used only to disambiguate same-title
/// windows while resolving `window_root`.
#[derive(Debug, Clone, Copy)]
struct AxBounds {
	x:      f64,
	y:      f64,
	width:  f64,
	height: f64,
}

fn bounds(element: &AXUIElement) -> Option<AxBounds> {
	let position = copy_attribute(element, "AXPosition")?
		.downcast::<AXValue>()
		.ok()?;
	let size = copy_attribute(element, "AXSize")?
		.downcast::<AXValue>()
		.ok()?;
	let mut point = CGPoint { x: 0.0, y: 0.0 };
	let mut dimensions = CGSize { width: 0.0, height: 0.0 };
	// SAFETY: The output pointer targets a live CGPoint and the requested type
	// matches AXPosition.
	let got_point =
		unsafe { position.value(AXValueType::CGPoint, NonNull::from(&mut point).cast()) };
	// SAFETY: The output pointer targets a live CGSize and the requested type
	// matches AXSize.
	let got_size = unsafe { size.value(AXValueType::CGSize, NonNull::from(&mut dimensions).cast()) };
	if !got_point || !got_size {
		return None;
	}
	Some(AxBounds {
		x:      point.x,
		y:      point.y,
		width:  dimensions.width,
		height: dimensions.height,
	})
}

fn bounds_matches_window(bounds: AxBounds, window: &DesktopWindow) -> bool {
	(bounds.x - f64::from(window.x)).abs() <= 2.0
		&& (bounds.y - f64::from(window.y)).abs() <= 2.0
		&& (bounds.width - f64::from(window.width)).abs() <= 2.0
		&& (bounds.height - f64::from(window.height)).abs() <= 2.0
}

fn action_name(action: &str) -> String {
	match action.trim().to_ascii_lowercase().as_str() {
		"press" => "AXPress".to_string(),
		"raise" => "AXRaise".to_string(),
		"showmenu" | "show_menu" => "AXShowMenu".to_string(),
		_ if action.starts_with("AX") => action.to_string(),
		_ => format!("AX{action}"),
	}
}

fn ax_result(error: AXError, context: impl Into<String>) -> CoreResult<()> {
	if error == AXError::Success {
		Ok(())
	} else {
		Err(DesktopError::ax_failed(format!("{} ({error:?})", context.into())))
	}
}

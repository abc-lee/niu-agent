mod backend;
mod error;
mod frame;
mod keys;
#[cfg(target_os = "macos")]
mod macos;
mod types;
#[cfg(any(target_os = "windows", test))]
mod win32;

use std::{
	collections::HashMap,
	panic::AssertUnwindSafe,
	sync::Arc,
	thread::{self, JoinHandle},
	time::Duration,
};

use backend::{Backend, DeliveryMode, MouseButton, PointerEvent};
use error::{CoreResult, DesktopError};
use frame::{FrameGeometry, apply_capture_caps, encode_png};
use keys::{parse_keys, parse_modifiers};
use parking_lot::Mutex;
use pyo3::prelude::*;
use pyo3::types::PyDict;
pub use types::*;

const OPERATION_TIMEOUT: Duration = Duration::from_mins(1);

enum Response {
	Capabilities(DesktopCapabilities),
	Displays(Vec<DesktopDisplay>),
	Windows(Vec<DesktopWindow>),
	Capture(DesktopCapture),
	MapPoint { x: f64, y: f64 },
	Unit,
}

type Reply = flume::Sender<CoreResult<Response>>;

enum Request {
	Capabilities {
		reply: Reply,
	},
	ListDisplays {
		reply: Reply,
	},
	ListWindows {
		reply: Reply,
	},
	Capture {
		target: Target,
		caps:   CaptureCaps,
		reply:  Reply,
	},
	Click {
		target:  Target,
		x:       f64,
		y:       f64,
		options: ParsedPointerOptions,
		reply:   Reply,
	},
	MoveMouse {
		target: Target,
		x:      f64,
		y:      f64,
		mode:   DeliveryMode,
		reply:  Reply,
	},
	Drag {
		target:  Target,
		path:    Vec<(f64, f64)>,
		options: ParsedPointerOptions,
		reply:   Reply,
	},
	Scroll {
		target: Target,
		x:      f64,
		y:      f64,
		dx:     f64,
		dy:     f64,
		mode:   DeliveryMode,
		reply:  Reply,
	},
	TypeText {
		target: Target,
		text:   String,
		mode:   DeliveryMode,
		reply:  Reply,
	},
	KeyChord {
		target: Target,
		keys:   Vec<keys::KeyName>,
		mode:   DeliveryMode,
		reply:  Reply,
	},
	MapPoint {
		target: Target,
		x:      f64,
		y:      f64,
		reply:  Reply,
	},
	Close {
		reply: Reply,
	},
}

impl Request {
	fn reply(self, result: CoreResult<Response>) {
		let reply = match self {
			Self::Capabilities { reply }
			| Self::ListDisplays { reply }
			| Self::ListWindows { reply }
			| Self::Capture { reply, .. }
			| Self::Click { reply, .. }
			| Self::MoveMouse { reply, .. }
			| Self::Drag { reply, .. }
			| Self::Scroll { reply, .. }
			| Self::TypeText { reply, .. }
			| Self::KeyChord { reply, .. }
			| Self::MapPoint { reply, .. }
			| Self::Close { reply } => reply,
		};
		let _ = reply.send(result);
	}

	const fn is_close(&self) -> bool {
		matches!(self, Self::Close { .. })
	}
}

#[derive(Clone, Copy)]
struct ParsedPointerOptions {
	button:    MouseButton,
	count:     u32,
	modifiers: backend::Modifiers,
	mode:      DeliveryMode,
}
impl ParsedPointerOptions {
	fn parse(options: Option<PointerOptions>) -> CoreResult<Self> {
		let options = options.unwrap_or_default();
		Ok(Self {
			button:    MouseButton::parse(options.button.as_deref())?,
			count:     options.count.unwrap_or(1).max(1),
			modifiers: parse_modifiers(options.modifiers.as_deref().unwrap_or_default())?,
			mode:      DeliveryMode::parse(options.delivery_mode.as_deref()),
		})
	}
}

struct Worker {
	backend:      CoreResult<Box<dyn Backend>>,
	frames:       HashMap<String, FrameGeometry>,
	capabilities: Arc<Mutex<DesktopCapabilities>>,
}

impl Worker {
	fn new(selector: DisplaySelector, capabilities: Arc<Mutex<DesktopCapabilities>>) -> Self {
		let backend = create_backend(selector);
		Self { backend, frames: HashMap::new(), capabilities }
	}

	fn backend(&mut self) -> CoreResult<&mut Box<dyn Backend>> {
		self.backend.as_mut().map_err(|error| error.clone())
	}

	fn window(&mut self, target: &Target) -> CoreResult<DesktopWindow> {
		let windows = self.backend()?.windows()?;
		match target {
			Target::Window(id) => windows
				.into_iter()
				.find(|window| window.id == *id)
				.ok_or_else(|| DesktopError::window_not_found(format!("window '{id}' was not found"))),
			Target::Desktop => windows
				.into_iter()
				.find(|window| window.focused)
				.ok_or_else(|| DesktopError::window_not_found("no focused window was found")),
		}
	}

	fn frame(&self, target: &Target) -> CoreResult<FrameGeometry> {
		self.frames.get(target.key()).cloned().ok_or_else(|| {
			DesktopError::invalid_coordinate_frame(format!(
				"no capture of '{}' yet — take a screenshot of this target first; coordinate input is \
				 in pixels of that screenshot",
				target.key()
			))
		})
	}

	fn map_point(
		&mut self,
		target: &Target,
		x: f64,
		y: f64,
	) -> CoreResult<(f64, f64, FrameGeometry)> {
		let frame = self.frame(target)?;
		let current = if matches!(target, Target::Window(_)) {
			Some(self.window(target)?)
		} else {
			None
		};
		let (x, y) = frame.map_point(x, y, current.as_ref())?;
		Ok((x, y, frame))
	}

	fn process(&mut self, request: &Request) -> CoreResult<Response> {
		match request {
			Request::Capabilities { .. } => {
				let caps = match self.backend.as_mut() {
					Ok(backend) => backend.capabilities(),
					Err(_) => DesktopCapabilities::unavailable(),
				};
				*self.capabilities.lock() = caps.clone();
				Ok(Response::Capabilities(caps))
			},
			Request::ListDisplays { .. } => Ok(Response::Displays(self.backend()?.displays()?)),
			Request::ListWindows { .. } => Ok(Response::Windows(self.backend()?.windows()?)),
			Request::Capture { target, caps, .. } => {
				let (image, mut geometry) = self.backend()?.capture(target, caps)?;
				let source_width = image.width();
				let source_height = image.height();
				let image = apply_capture_caps(image, &mut geometry, caps)?;
				let width = image.width();
				let height = image.height();
				let source = match target {
					Target::Desktop => self.backend()?.displays()?,
					Target::Window(_) => {
						let w = self.window(target)?;
						vec![DesktopDisplay {
							id:           w.id,
							name:         format!("{} — {}", w.app, w.title),
							x:            w.x,
							y:            w.y,
							width:        w.width,
							height:       w.height,
							scale:        f64::from(width) / f64::from(w.width.max(1)),
							pixel_x:      0,
							pixel_y:      0,
							pixel_width:  width,
							pixel_height: height,
							is_primary:   false,
						}]
					},
				};
				let displays = geometry.display_metadata(&source);
				let png = encode_png(image)?;
				self.frames.insert(target.key().to_string(), geometry);
				let capabilities = self.backend()?.capabilities();
				*self.capabilities.lock() = capabilities.clone();
				Ok(Response::Capture(DesktopCapture {
					data: png,
					width,
					height,
					source_width,
					source_height,
					target: target.key().to_string(),
					displays,
					backend: capabilities.backend,
					display_server: capabilities.display_server,
				}))
			},
			Request::Click { target, x, y, options, .. } => {
				let (x, y, frame) = self.map_point(target, *x, *y)?;
				self.backend()?.pointer(
					target,
					PointerEvent::Click {
						x,
						y,
						button: options.button,
						count: options.count,
						modifiers: options.modifiers,
					},
					&frame,
					options.mode,
				)?;
				Ok(Response::Unit)
			},
			Request::MoveMouse { target, x, y, mode, .. } => {
				let (x, y, frame) = self.map_point(target, *x, *y)?;
				self
					.backend()?
					.pointer(target, PointerEvent::Move { x, y }, &frame, *mode)?;
				Ok(Response::Unit)
			},
			Request::Drag { target, path, options, .. } => {
				let frame = self.frame(target)?;
				let current = if matches!(target, Target::Window(_)) {
					Some(self.window(target)?)
				} else {
					None
				};
				let mapped = path
					.iter()
					.map(|(x, y)| frame.map_point(*x, *y, current.as_ref()))
					.collect::<CoreResult<Vec<_>>>()?;
				self.backend()?.pointer(
					target,
					PointerEvent::Drag {
						path:      mapped,
						button:    options.button,
						modifiers: options.modifiers,
					},
					&frame,
					options.mode,
				)?;
				Ok(Response::Unit)
			},
			Request::Scroll { target, x, y, dx, dy, mode, .. } => {
				let (x, y, frame) = self.map_point(target, *x, *y)?;
				self.backend()?.pointer(
					target,
					PointerEvent::Scroll { x, y, dx: *dx, dy: *dy },
					&frame,
					*mode,
				)?;
				Ok(Response::Unit)
			},
			Request::TypeText { target, text, mode, .. } => {
				self.backend()?.type_text(target, text, *mode)?;
				Ok(Response::Unit)
			},
			Request::KeyChord { target, keys, mode, .. } => {
				self.backend()?.key_chord(target, keys, *mode)?;
				Ok(Response::Unit)
			},
			Request::MapPoint { target, x, y, .. } => {
				let (x, y, _) = self.map_point(target, *x, *y)?;
				Ok(Response::MapPoint { x, y })
			},
			Request::Close { .. } => Ok(Response::Unit),
		}
	}
}

#[cfg(target_os = "macos")]
fn create_backend(selector: DisplaySelector) -> CoreResult<Box<dyn Backend>> {
	Ok(Box::new(macos::MacosBackend::new(selector)?))
}
#[cfg(target_os = "windows")]
fn create_backend(selector: DisplaySelector) -> CoreResult<Box<dyn Backend>> {
	Ok(Box::new(win32::Win32Backend::new(selector)?))
}
#[cfg(not(any(target_os = "macos", target_os = "windows")))]
fn create_backend(_: DisplaySelector) -> CoreResult<Box<dyn Backend>> {
	Err(DesktopError::capture_failed("desktop backend unavailable on this platform"))
}

struct Lifecycle {
	tx:     Option<flume::Sender<Request>>,
	done:   Option<flume::Receiver<()>>,
	join:   Option<JoinHandle<()>>,
	closed: bool,
}
struct SessionCore {
	selector:     DisplaySelector,
	lifecycle:    Mutex<Lifecycle>,
	capabilities: Arc<Mutex<DesktopCapabilities>>,
}
impl SessionCore {
	fn new(selector: DisplaySelector) -> Arc<Self> {
		Arc::new(Self {
			selector,
			lifecycle: Mutex::new(Lifecycle {
				tx:     None,
				done:   None,
				join:   None,
				closed: false,
			}),
			capabilities: Arc::new(Mutex::new(DesktopCapabilities::unavailable())),
		})
	}

	fn ensure_started(&self) -> CoreResult<flume::Sender<Request>> {
		let mut lifecycle = self.lifecycle.lock();
		if lifecycle.closed {
			return Err(DesktopError::closed());
		}
		if let Some(tx) = &lifecycle.tx {
			return Ok(tx.clone());
		}
		let (tx, rx) = flume::unbounded::<Request>();
		let (done_tx, done_rx) = flume::bounded(1);
		let selector = self.selector.clone();
		let caps = Arc::clone(&self.capabilities);
		let join = thread::Builder::new()
			.name("niu-natives-desktop-session".into())
			.spawn(move || {
				let mut worker = Worker::new(selector, caps);
				while let Ok(request) = rx.recv() {
					let close = request.is_close();
					let result = std::panic::catch_unwind(AssertUnwindSafe(|| worker.process(&request)))
						.unwrap_or_else(|_| {
							Err(DesktopError::internal("native desktop worker panicked"))
						});
					request.reply(result);
					if close {
						break;
					}
				}
				let _ = done_tx.send(());
			})
			.map_err(|e| {
				DesktopError::internal(format!("failed to start native desktop worker: {e}"))
			})?;
		lifecycle.tx = Some(tx.clone());
		lifecycle.done = Some(done_rx);
		lifecycle.join = Some(join);
		Ok(tx)
	}

	fn call(&self, make: impl FnOnce(Reply) -> Request) -> CoreResult<Response> {
		let (txr, rxr) = flume::bounded(1);
		self
			.ensure_started()?
			.send(make(txr))
			.map_err(|_| DesktopError::internal("native desktop worker stopped unexpectedly"))?;
		rxr.recv_timeout(OPERATION_TIMEOUT).map_err(|e| {
			DesktopError::timeout(format!("native desktop operation did not complete: {e}"))
		})?
	}
}
impl Drop for SessionCore {
	fn drop(&mut self) {
		let lifecycle = self.lifecycle.get_mut();
		if let Some(tx) = lifecycle.tx.take() {
			let (reply, _) = flume::bounded(1);
			let _ = tx.send(Request::Close { reply });
		}
		let _ = lifecycle.join.take();
	}
}

fn response_unit(response: Response) -> CoreResult<()> {
	if matches!(response, Response::Unit) {
		Ok(())
	} else {
		Err(DesktopError::internal("unexpected desktop worker response"))
	}
}

/// Persistent, serialized native desktop capture/input session.
///
/// All methods are synchronous: the call blocks until the native worker
/// replies (or the operation timeout elapses). The GIL is released for the
/// duration of each call (`py.detach`), so concurrent Python threads stay
/// schedulable while native capture/input runs.
#[pyclass]
pub struct DesktopSession {
	core: Arc<SessionCore>,
}

#[pymethods]
impl DesktopSession {
	/// Create a session. `options` is `None` or a dict with an optional
	/// `"display"` key (display id or `"all"`).
	#[new]
	#[pyo3(signature = (options=None))]
	fn new(options: Option<DesktopSessionOptions>) -> PyResult<Self> {
		Ok(Self { core: SessionCore::new(DisplaySelector::parse(options.and_then(|o| o.display))) })
	}

	/// Backend capabilities and permission state (never fails; falls back to
	/// the last known snapshot while the native worker is unavailable).
	#[getter]
	fn capabilities(&self, py: Python<'_>) -> PyResult<Py<DesktopCapabilities>> {
		let core = Arc::clone(&self.core);
		let cached = Arc::clone(&core.capabilities);
		let result: CoreResult<DesktopCapabilities> =
			py.detach(move || match core.call(|reply| Request::Capabilities { reply }) {
				Ok(Response::Capabilities(caps)) => Ok(caps),
				_ => Ok(cached.lock().clone()),
			});
		let caps = result?;
		Py::new(py, caps).map_err(Into::into)
	}

	/// List the monitors of the composite desktop.
	fn list_displays(&self, py: Python<'_>) -> PyResult<Vec<Py<DesktopDisplay>>> {
		let core = Arc::clone(&self.core);
		let result = py.detach(move || match core.call(|reply| Request::ListDisplays { reply }) {
			Ok(Response::Displays(displays)) => Ok(displays),
			Ok(_) => Err(DesktopError::internal("unexpected response")),
			Err(error) => Err(error),
		});
		let displays = result?;
		displays.into_iter().map(|display| Py::new(py, display)).collect()
	}

	/// List the top-level windows of the composite desktop.
	fn list_windows(&self, py: Python<'_>) -> PyResult<Vec<Py<DesktopWindow>>> {
		let core = Arc::clone(&self.core);
		let result = py.detach(move || match core.call(|reply| Request::ListWindows { reply }) {
			Ok(Response::Windows(windows)) => Ok(windows),
			Ok(_) => Err(DesktopError::internal("unexpected response")),
			Err(error) => Err(error),
		});
		let windows = result?;
		windows.into_iter().map(|window| Py::new(py, window)).collect()
	}

	/// Capture `target` (`"desktop"` or a window id from `list_windows`) and
	/// return the PNG bytes plus frame metadata.
	///
	/// Returns a dict: `{"png_bytes": bytes, "width": int, "height": int,
	/// "backend": str}`. (The geometry/displays wire fields are added in a
	/// later stage.)
	#[pyo3(signature = (target, caps=None))]
	fn capture(
		&self,
		py: Python<'_>,
		target: String,
		caps: Option<CaptureCaps>,
	) -> PyResult<Py<PyDict>> {
		let core = Arc::clone(&self.core);
		let target = Target::parse(&target);
		let caps = caps.unwrap_or_default();
		let result = py.detach(move || {
			match core.call(|reply| Request::Capture { target, caps, reply }) {
				Ok(Response::Capture(capture)) => Ok(capture),
				Ok(_) => Err(DesktopError::internal("unexpected response")),
				Err(error) => Err(error),
			}
		});
		let capture = result?;
		let dict = PyDict::new(py);
		dict.set_item("png_bytes", capture.data)?;
		dict.set_item("width", capture.width)?;
		dict.set_item("height", capture.height)?;
		dict.set_item("backend", capture.backend)?;
		Ok(dict.unbind())
	}

	/// Map capture-frame pixel coordinates (from the last capture of `target`)
	/// to global logical desktop coordinates.
	///
	/// Returns a dict `{"x": float, "y": float}` in global logical desktop
	/// pixels (window targets are re-anchored to the window's current origin).
	fn map_point(
		&self,
		py: Python<'_>,
		target: String,
		x: f64,
		y: f64,
	) -> PyResult<Py<PyDict>> {
		let core = Arc::clone(&self.core);
		let target = Target::parse(&target);
		let result = py.detach(move || {
			match core.call(|reply| Request::MapPoint { target, x, y, reply }) {
				Ok(Response::MapPoint { x, y }) => Ok((x, y)),
				Ok(_) => Err(DesktopError::internal("unexpected response")),
				Err(error) => Err(error),
			}
		});
		let (x, y) = result?;
		let dict = PyDict::new(py);
		dict.set_item("x", x)?;
		dict.set_item("y", y)?;
		Ok(dict.unbind())
	}

	/// Click at capture-frame pixel coordinates. `opts` may set `button`,
	/// `count`, `modifiers` and `delivery_mode`.
	#[pyo3(signature = (target, x, y, opts=None))]
	fn click(
		&self,
		py: Python<'_>,
		target: String,
		x: f64,
		y: f64,
		opts: Option<PointerOptions>,
	) -> PyResult<()> {
		let options = ParsedPointerOptions::parse(opts)?;
		let core = Arc::clone(&self.core);
		let target = Target::parse(&target);
		py.detach(move || {
			core.call(|reply| Request::Click { target, x, y, options, reply })
				.and_then(response_unit)
		})
		.map_err(PyErr::from)
	}

	/// Move the mouse to capture-frame pixel coordinates.
	#[pyo3(signature = (target, x, y, opts=None))]
	fn move_mouse(
		&self,
		py: Python<'_>,
		target: String,
		x: f64,
		y: f64,
		opts: Option<PointerOptions>,
	) -> PyResult<()> {
		let mode = ParsedPointerOptions::parse(opts)?.mode;
		let core = Arc::clone(&self.core);
		let target = Target::parse(&target);
		py.detach(move || {
			core.call(|reply| Request::MoveMouse { target, x, y, mode, reply })
				.and_then(response_unit)
		})
		.map_err(PyErr::from)
	}

	/// Drag through a path of capture-frame pixel coordinates.
	#[pyo3(signature = (target, path, opts=None))]
	fn drag(
		&self,
		py: Python<'_>,
		target: String,
		path: Vec<DesktopPoint>,
		opts: Option<PointerOptions>,
	) -> PyResult<()> {
		let options = ParsedPointerOptions::parse(opts)?;
		let path = path.into_iter().map(|point| (point.x, point.y)).collect();
		let core = Arc::clone(&self.core);
		let target = Target::parse(&target);
		py.detach(move || {
			core.call(|reply| Request::Drag { target, path, options, reply })
				.and_then(response_unit)
		})
		.map_err(PyErr::from)
	}

	/// Scroll at capture-frame pixel coordinates.
	#[pyo3(signature = (target, x, y, dx, dy, opts=None))]
	fn scroll(
		&self,
		py: Python<'_>,
		target: String,
		x: f64,
		y: f64,
		dx: f64,
		dy: f64,
		opts: Option<PointerOptions>,
	) -> PyResult<()> {
		let mode = ParsedPointerOptions::parse(opts)?.mode;
		let core = Arc::clone(&self.core);
		let target = Target::parse(&target);
		py.detach(move || {
			core.call(|reply| Request::Scroll { target, x, y, dx, dy, mode, reply })
				.and_then(response_unit)
		})
		.map_err(PyErr::from)
	}

	/// Type `text` into `target`.
	#[pyo3(signature = (target, text, opts=None))]
	fn type_text(
		&self,
		py: Python<'_>,
		target: String,
		text: String,
		opts: Option<PointerOptions>,
	) -> PyResult<()> {
		let mode = ParsedPointerOptions::parse(opts)?.mode;
		let core = Arc::clone(&self.core);
		let target = Target::parse(&target);
		py.detach(move || {
			core.call(|reply| Request::TypeText { target, text, mode, reply })
				.and_then(response_unit)
		})
		.map_err(PyErr::from)
	}

	/// Send a keyboard chord (e.g. `["cmd", "c"]`) to `target`.
	#[pyo3(signature = (target, keys, opts=None))]
	fn key_chord(
		&self,
		py: Python<'_>,
		target: String,
		keys: Vec<String>,
		opts: Option<PointerOptions>,
	) -> PyResult<()> {
		let keys = parse_keys(&keys)?;
		let mode = ParsedPointerOptions::parse(opts)?.mode;
		let core = Arc::clone(&self.core);
		let target = Target::parse(&target);
		py.detach(move || {
			core.call(|reply| Request::KeyChord { target, keys, mode, reply })
				.and_then(response_unit)
		})
		.map_err(PyErr::from)
	}
}

#[cfg(test)]
mod capture_tests {
	use image::RgbaImage;

	use super::*;
	use crate::desktop::{
		backend::Backend,
		error::ErrorCode,
		keys::KeyName,
	};

	const WAYLAND_ID: &str = "atspi::1.31:/org/a11y/atspi/accessible/1";

	/// Backend that mints a composite AT-SPI window id, mirroring the Wayland
	/// `AtSpiAx` path. Exists to exercise `Worker::process` without a display.
	struct FakeWaylandBackend {
		window: DesktopWindow,
	}

	impl FakeWaylandBackend {
		fn new() -> Self {
			Self {
				window: DesktopWindow {
					id:      WAYLAND_ID.to_string(),
					title:   "Obsidian".to_string(),
					app:     "obsidian".to_string(),
					pid:     Some(1234),
					x:       0,
					y:       0,
					width:   64,
					height:  48,
					focused: true,
				},
			}
		}
	}

	impl Backend for FakeWaylandBackend {
		fn capabilities(&mut self) -> DesktopCapabilities {
			DesktopCapabilities {
				backend: "wayland".to_string(),
				display_server: Some("wayland".to_string()),
				capture: true,
				..DesktopCapabilities::unavailable()
			}
		}

		fn displays(&mut self) -> CoreResult<Vec<DesktopDisplay>> {
			Ok(Vec::new())
		}

		fn windows(&mut self) -> CoreResult<Vec<DesktopWindow>> {
			Ok(vec![self.window.clone()])
		}

		fn capture(
			&mut self,
			target: &Target,
			_caps: &CaptureCaps,
		) -> CoreResult<(RgbaImage, FrameGeometry)> {
			match target {
				Target::Window(id) if id == &self.window.id => {
					let image = RgbaImage::new(self.window.width, self.window.height);
					let geometry =
						FrameGeometry::for_window(&self.window, image.width(), image.height());
					Ok((image, geometry))
				},
				Target::Window(id) => {
					Err(DesktopError::window_not_found(format!("Wayland window {id} not found")))
				},
				Target::Desktop => Err(DesktopError::capture_failed("desktop capture not exercised")),
			}
		}

		fn pointer(
			&mut self,
			_: &Target,
			_: PointerEvent,
			_: &FrameGeometry,
			_: DeliveryMode,
		) -> CoreResult<()> {
			unreachable!("pointer not exercised")
		}

		fn type_text(&mut self, _: &Target, _: &str, _: DeliveryMode) -> CoreResult<()> {
			unreachable!("type_text not exercised")
		}

		fn key_chord(&mut self, _: &Target, _: &[KeyName], _: DeliveryMode) -> CoreResult<()> {
			unreachable!("key_chord not exercised")
		}
	}

	fn worker_with(backend: impl Backend + 'static) -> Worker {
		Worker {
			backend:      Ok(Box::new(backend)),
			frames:       HashMap::new(),
			capabilities: Arc::new(Mutex::new(DesktopCapabilities::unavailable())),
		}
	}

	fn capture_request(target: Target) -> Request {
		let (reply, _rx) = flume::bounded(1);
		Request::Capture { target, caps: CaptureCaps::default(), reply }
	}

	/// Regression for #7701: a composite AT-SPI window id minted by the Wayland
	/// backend's own `windows()` must reach the backend, not be rejected by a
	/// `u64` pre-parse in the shared request path.
	#[test]
	fn capture_accepts_non_numeric_wayland_window_id() {
		let mut worker = worker_with(FakeWaylandBackend::new());
		let response = worker
			.process(&capture_request(Target::Window(WAYLAND_ID.to_string())))
			.expect("wayland window id should be accepted by capture");
		let Response::Capture(capture) = response else {
			panic!("expected a capture response");
		};
		assert_eq!(capture.target, WAYLAND_ID);
		assert_eq!(capture.width, 64);
		assert_eq!(capture.height, 48);
		assert_eq!(capture.backend, "wayland");
	}

	/// Unknown ids still fail — but as `WindowNotFound` from the backend lookup,
	/// never as an `InvalidTarget` pre-parse rejection of a non-`u64` id.
	#[test]
	fn capture_rejects_unknown_window_id_via_backend_lookup() {
		let mut worker = worker_with(FakeWaylandBackend::new());
		let Err(err) = worker.process(&capture_request(Target::Window("does-not-exist".to_string())))
		else {
			panic!("unknown window id should fail");
		};
		assert_eq!(err.code, ErrorCode::WindowNotFound);
	}
}

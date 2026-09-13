mod ax;
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

use ax::{AxRegistry, register_node};
use backend::{Backend, DeliveryMode, MouseButton, PointerEvent};
use error::{CoreResult, DesktopError};
use frame::{FrameGeometry, apply_capture_caps, encode_png};
use keys::{parse_keys, parse_modifiers};
use parking_lot::Mutex;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
pub use types::*;

const OPERATION_TIMEOUT: Duration = Duration::from_mins(1);

enum Response {
	Capabilities(DesktopCapabilities),
	Displays(Vec<DesktopDisplay>),
	Windows(Vec<DesktopWindow>),
	Capture(DesktopCapture),
	Unit,
	Snapshot(AxSnapshot),
	Nodes(Vec<AxNode>),
	Node(Option<AxNode>),
	Attributes(Vec<(String, String)>),
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
		/// Logical-desktop region crop for `Target::Desktop` captures; `None`
		/// captures the whole composite. Rejected for window targets.
		region: Option<CaptureRegion>,
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
	RaiseWindow {
		id:    String,
		reply: Reply,
	},
	AxSnapshot {
		target:  Target,
		options: AxSnapshotOptions,
		reply:   Reply,
	},
	AxQuery {
		target: Target,
		query:  AxQuery,
		reply:  Reply,
	},
	AxElementAt {
		target: Target,
		x:      f64,
		y:      f64,
		reply:  Reply,
	},
	AxFocused {
		reply: Reply,
	},
	AxNode {
		reference: String,
		reply:     Reply,
	},
	AxAttributes {
		reference: String,
		reply:     Reply,
	},
	AxChildren {
		reference: String,
		reply:     Reply,
	},
	AxParent {
		reference: String,
		reply:     Reply,
	},
	AxPerform {
		reference: String,
		action:    String,
		reply:     Reply,
	},
	AxSetValue {
		reference: String,
		value:     String,
		reply:     Reply,
	},
	AxFocus {
		reference: String,
		reply:     Reply,
	},
	AxClick {
		reference: String,
		options:   ParsedPointerOptions,
		reply:     Reply,
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
			| Self::RaiseWindow { reply, .. }
			| Self::AxSnapshot { reply, .. }
			| Self::AxQuery { reply, .. }
			| Self::AxElementAt { reply, .. }
			| Self::AxFocused { reply }
			| Self::AxNode { reply, .. }
			| Self::AxAttributes { reply, .. }
			| Self::AxChildren { reply, .. }
			| Self::AxParent { reply, .. }
			| Self::AxPerform { reply, .. }
			| Self::AxSetValue { reply, .. }
			| Self::AxFocus { reply, .. }
			| Self::AxClick { reply, .. }
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
	registry:     AxRegistry,
	frames:       HashMap<String, FrameGeometry>,
	capabilities: Arc<Mutex<DesktopCapabilities>>,
}

impl Worker {
	fn new(selector: DisplaySelector, capabilities: Arc<Mutex<DesktopCapabilities>>) -> Self {
		let backend = create_backend(selector);
		Self { backend, registry: AxRegistry::default(), frames: HashMap::new(), capabilities }
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

	fn ax(&mut self) -> CoreResult<&mut dyn backend::AxBackend> {
		self
			.backend()?
			.ax()
			.ok_or_else(DesktopError::ax_unsupported)
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
			Request::Capture { target, caps, region, .. } => {
				if region.is_some() && !matches!(target, Target::Desktop) {
					return Err(DesktopError::invalid_target(
						"capture region is only supported for the 'desktop' target (window captures \
						 are always full-window)",
					));
				}
				let (mut image, mut geometry) = self.backend()?.capture(target, caps)?;
				if let Some(region) = region {
					let (x, y, width, height) = geometry.crop_to_logical_region(&region)?;
					image = image::imageops::crop_imm(&image, x, y, width, height).to_image();
				}
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
				let geometry_wire = geometry.to_wire();
				// Frame semantics (fail-loud, 2026-09-13): pointer coordinates
				// belong to the most recent *full* capture of this target. A
				// region crop is a different viewport — its pixels are NOT valid
				// pointer coordinates for the target. Keeping the old full frame
				// after a crop would let a model feed crop pixels into input and
				// silently click the wrong place, so the crop invalidates the
				// stored frame: coordinate input fails with
				// InvalidCoordinateFrame ("take a screenshot of this target
				// first") until a fresh full capture is taken.
				if region.is_some() {
					self.frames.remove(target.key());
				} else {
					self.frames.insert(target.key().to_string(), geometry);
				}
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
					geometry: geometry_wire,
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
			Request::RaiseWindow { id, .. } => {
				self.backend()?.raise_window(id)?;
				Ok(Response::Unit)
			},
			Request::AxSnapshot { target, options, .. } => {
				let window = self.window(target)?;
				let (backend, registry) = (&mut self.backend, &mut self.registry);
				let ax = backend
					.as_mut()
					.map_err(|error| error.clone())?
					.ax()
					.ok_or_else(DesktopError::ax_unsupported)?;
				Ok(Response::Snapshot(ax::snapshot(ax, registry, &window, options)?))
			},
			Request::AxQuery { target, query, .. } => {
				let window = self.window(target)?;
				let (backend, registry) = (&mut self.backend, &mut self.registry);
				let ax = backend
					.as_mut()
					.map_err(|error| error.clone())?
					.ax()
					.ok_or_else(DesktopError::ax_unsupported)?;
				Ok(Response::Nodes(ax::query(ax, registry, &window, query)?))
			},
			Request::AxElementAt { target, x, y, .. } => {
				let (backend, registry) = (&mut self.backend, &mut self.registry);
				let backend = backend
					.as_mut()
					.map_err(|error| error.clone())?
					.ax()
					.ok_or_else(DesktopError::ax_unsupported)?;
				Ok(Response::Node(ax::element_at_node(backend, registry, target.key(), *x, *y)?))
			},
			Request::AxFocused { .. } => {
				let handle = self.ax()?.focused_element()?;
				let node = match handle {
					Some(h) => {
						let (backend, registry) = (&mut self.backend, &mut self.registry);
						let ax = backend
							.as_mut()
							.map_err(|error| error.clone())?
							.ax()
							.ok_or_else(DesktopError::ax_unsupported)?;
						Some(register_node(ax, registry, "desktop", h)?)
					},
					None => None,
				};
				Ok(Response::Node(node))
			},
			Request::AxNode { reference, .. } => {
				let h = self.registry.resolve(reference)?;
				let props = self.ax()?.props(&h)?;
				Ok(Response::Node(Some(axnode(reference.clone(), props))))
			},
			Request::AxAttributes { reference, .. } => {
				let h = self.registry.resolve(reference)?;
				let mut attributes = self.ax()?.attributes(&h)?;
				for (_, value) in &mut attributes {
					if value.chars().count() > 200 {
						*value = value
							.chars()
							.take(199)
							.chain(std::iter::once('…'))
							.collect();
					}
				}
				Ok(Response::Attributes(attributes))
			},
			Request::AxChildren { reference, .. } => {
				let h = self.registry.resolve(reference)?;
				let target = self.registry.target(reference)?;
				let handles = self.ax()?.children(&h)?;
				let mut nodes = Vec::with_capacity(handles.len());
				for h in handles {
					let (backend, registry) = (&mut self.backend, &mut self.registry);
					let ax = backend
						.as_mut()
						.map_err(|error| error.clone())?
						.ax()
						.ok_or_else(DesktopError::ax_unsupported)?;
					nodes.push(register_node(ax, registry, &target, h)?);
				}
				Ok(Response::Nodes(nodes))
			},
			Request::AxParent { reference, .. } => {
				let h = self.registry.resolve(reference)?;
				let target = self.registry.target(reference)?;
				let parent = self.ax()?.parent(&h)?;
				let node = match parent {
					Some(h) => {
						let (backend, registry) = (&mut self.backend, &mut self.registry);
						let ax = backend
							.as_mut()
							.map_err(|error| error.clone())?
							.ax()
							.ok_or_else(DesktopError::ax_unsupported)?;
						Some(register_node(ax, registry, &target, h)?)
					},
					None => None,
				};
				Ok(Response::Node(node))
			},
			Request::AxPerform { reference, action, .. } => {
				let h = self.registry.resolve(reference)?;
				if action.eq_ignore_ascii_case("press") {
					ax::ax_press(self.ax()?, &h)?;
				} else {
					self.ax()?.perform(&h, action)?;
				}
				Ok(Response::Unit)
			},
			Request::AxSetValue { reference, value, .. } => {
				let h = self.registry.resolve(reference)?;
				self.ax()?.set_value(&h, value)?;
				Ok(Response::Unit)
			},
			Request::AxFocus { reference, .. } => {
				let h = self.registry.resolve(reference)?;
				self.ax()?.focus(&h)?;
				Ok(Response::Unit)
			},
			Request::AxClick { reference, options, .. } => {
				let h = self.registry.resolve(reference)?;
				let bounds = self.ax()?.props(&h)?.bounds.ok_or_else(|| {
					DesktopError::ax_failed(format!("{reference} has no clickable bounds"))
				})?;
				let x = bounds.x + bounds.width / 2.0;
				let y = bounds.y + bounds.height / 2.0;
				let windows = self.backend()?.windows()?;
				let window = windows
					.into_iter()
					.find(|w| {
						x >= f64::from(w.x)
							&& x < f64::from(w.x + w.width as i32)
							&& y >= f64::from(w.y)
							&& y < f64::from(w.y + w.height as i32)
					})
					.ok_or_else(|| {
						DesktopError::window_not_found(format!("no window contains {reference}"))
					})?;
				let target = Target::Window(window.id);
				self.backend()?.pointer(
					&target,
					PointerEvent::Click {
						x,
						y,
						button: options.button,
						count: options.count,
						modifiers: options.modifiers,
					},
					&FrameGeometry::identity_global(),
					options.mode,
				)?;
				Ok(Response::Unit)
			},
			Request::Close { .. } => Ok(Response::Unit),
		}
	}
}

fn axnode(reference: String, props: ax::AxProps) -> AxNode {
	let (x, y, width, height) = props
		.bounds
		.map_or((None, None, None, None), |b| (Some(b.x), Some(b.y), Some(b.width), Some(b.height)));
	AxNode {
		ref_: reference,
		role: props.role,
		native_role: props.native_role,
		title: props.title,
		value: props.value,
		description: props.description,
		enabled: props.enabled,
		focused: props.focused,
		x,
		y,
		width,
		height,
		actions: (!props.actions.is_empty()).then_some(props.actions),
		child_count: props.child_count,
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

/// Serializes one geometry region into its wire dict.
fn region_to_dict<'py>(py: Python<'py>, region: &RegionWire) -> PyResult<Bound<'py, PyDict>> {
	let dict = PyDict::new(py);
	dict.set_item("x", region.x)?;
	dict.set_item("y", region.y)?;
	dict.set_item("width", region.width)?;
	dict.set_item("height", region.height)?;
	dict.set_item("pixel_x", region.pixel_x)?;
	dict.set_item("pixel_y", region.pixel_y)?;
	dict.set_item("pixel_width", region.pixel_width)?;
	dict.set_item("pixel_height", region.pixel_height)?;
	Ok(dict)
}

/// Serializes a `FrameGeometry` into the wire dict consumed by
/// `map_point(x, y, geometry)`: `{kind, width, height, regions, ...}`.
fn geometry_to_dict<'py>(py: Python<'py>, wire: &GeometryWire) -> PyResult<Bound<'py, PyDict>> {
	let dict = PyDict::new(py);
	match wire.kind {
		GeometryKindWire::Desktop => {
			dict.set_item("kind", "desktop")?;
		},
		GeometryKindWire::Window { captured_width, captured_height } => {
			dict.set_item("kind", "window")?;
			dict.set_item("captured_width", captured_width)?;
			dict.set_item("captured_height", captured_height)?;
		},
	}
	dict.set_item("width", wire.width)?;
	dict.set_item("height", wire.height)?;
	let regions = PyList::empty(py);
	for region in &wire.regions {
		regions.append(region_to_dict(py, region)?)?;
	}
	dict.set_item("regions", regions)?;
	Ok(dict)
}

/// Serializes one display metadata entry into its wire dict.
fn display_to_dict<'py>(py: Python<'py>, display: &DesktopDisplay) -> PyResult<Bound<'py, PyDict>> {
	let dict = PyDict::new(py);
	dict.set_item("id", &display.id)?;
	dict.set_item("name", &display.name)?;
	dict.set_item("x", display.x)?;
	dict.set_item("y", display.y)?;
	dict.set_item("width", display.width)?;
	dict.set_item("height", display.height)?;
	dict.set_item("pixel_x", display.pixel_x)?;
	dict.set_item("pixel_y", display.pixel_y)?;
	dict.set_item("pixel_width", display.pixel_width)?;
	dict.set_item("pixel_height", display.pixel_height)?;
	dict.set_item("scale", display.scale)?;
	Ok(dict)
}

/// Serializes the displays list of a capture result dict.
fn displays_to_list<'py>(py: Python<'py>, displays: &[DesktopDisplay]) -> PyResult<Bound<'py, PyList>> {
	let list = PyList::empty(py);
	for display in displays {
		list.append(display_to_dict(py, display)?)?;
	}
	Ok(list)
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
		// macOS: demote this process to a background-only application before any capture
		// API call — otherwise LaunchServices promotes it to a Foreground app on first
		// use and a Python icon appears in the Dock. Runs on the main thread, before the
		// native worker (and hence any xcap call) is ever started; best-effort, never
		// fails the constructor.
		#[cfg(target_os = "macos")]
		macos::demote_to_background_only();
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
	/// `region` restricts `"desktop"` captures to a rectangle of the composite
	/// desktop — a `(x, y, width, height)` sequence (or a dict with those
	/// keys) in logical desktop coordinates relative to the composite origin.
	/// The crop happens before `caps` downscaling and is clamped to the
	/// displays it overlaps (a region that overlaps no display is an error);
	/// combining a region with a window target is an error.
	///
	/// Returns a dict: `{"png_bytes": bytes, "width": int, "height": int,
	/// "source_width": int, "source_height": int, "backend": str,
	/// "geometry": dict, "displays": [dict, ...]}`. `geometry` carries the
	/// frame's `kind`/`regions` in the wire format consumed by
	/// `map_point(x, y, geometry)`; `displays` describes the monitors visible
	/// in the frame (id/name/logical rect/pixel rect/scale).
	#[pyo3(signature = (target, caps=None, region=None))]
	fn capture(
		&self,
		py: Python<'_>,
		target: String,
		caps: Option<CaptureCaps>,
		region: Option<CaptureRegion>,
	) -> PyResult<Py<PyDict>> {
		let core = Arc::clone(&self.core);
		let target = Target::parse(&target);
		let caps = caps.unwrap_or_default();
		let result = py.detach(move || {
			match core.call(|reply| Request::Capture { target, caps, region, reply }) {
				Ok(Response::Capture(capture)) => Ok(capture),
				Ok(_) => Err(DesktopError::internal("unexpected response")),
				Err(error) => Err(error),
			}
		});
		let capture = result?;
		let geometry = geometry_to_dict(py, &capture.geometry)?;
		let displays = displays_to_list(py, &capture.displays)?;
		let dict = PyDict::new(py);
		dict.set_item("png_bytes", capture.data)?;
		dict.set_item("width", capture.width)?;
		dict.set_item("height", capture.height)?;
		dict.set_item("source_width", capture.source_width)?;
		dict.set_item("source_height", capture.source_height)?;
		dict.set_item("backend", capture.backend)?;
		dict.set_item("geometry", geometry)?;
		dict.set_item("displays", displays)?;
		Ok(dict.unbind())
	}

	/// Map capture-frame pixel coordinates to global logical desktop
	/// coordinates.
	///
	/// `geometry` is the `geometry` dict returned by `capture()` (its wire
	/// format is self-contained: kind, frame size and per-region logical +
	/// pixel rects), so this is a pure function of the captured frame — no
	/// session state is consulted. Region crops map back to global logical
	/// desktop coordinates (a crop never re-anchors to its own origin), and
	/// window frames map relative to the window position at capture time
	/// (input methods re-anchor live windows themselves).
	///
	/// Returns a dict `{"x": float, "y": float}`.
	fn map_point(
		&self,
		py: Python<'_>,
		x: f64,
		y: f64,
		geometry: GeometryWire,
	) -> PyResult<Py<PyDict>> {
		let frame = FrameGeometry::from_wire(&geometry);
		let (x, y) = frame.map_point_static(x, y).map_err(PyErr::from)?;
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

	/// Bring window `window_id` to the front: restore it if minimized, then
	/// make its application foreground.
	fn raise_window(&self, py: Python<'_>, window_id: String) -> PyResult<()> {
		let core = Arc::clone(&self.core);
		py.detach(move || {
			core.call(|reply| Request::RaiseWindow { id: window_id, reply })
				.and_then(response_unit)
		})
		.map_err(PyErr::from)
	}

	/// Snapshot the accessibility tree of `target` (`"desktop"` = the focused
	/// window, or a window id from `list_windows`) as an indented text tree
	/// whose lines carry stable `[ref=eN]` handles for the reference-based
	/// methods below. Re-snapshotting invalidates previous refs (a stale ref
	/// is reported as a `StaleRef` error).
	///
	/// `opts` may set `"max_depth"`, `"max_nodes"` and `"all"`.
	#[pyo3(signature = (target, opts=None))]
	fn ax_snapshot(
		&self,
		py: Python<'_>,
		target: String,
		opts: Option<AxSnapshotOptions>,
	) -> PyResult<Py<AxSnapshot>> {
		let core = Arc::clone(&self.core);
		let target = Target::parse(&target);
		let options = opts.unwrap_or_default();
		let result = py.detach(move || match core.call(|reply| Request::AxSnapshot { target, options, reply }) {
			Ok(Response::Snapshot(snapshot)) => Ok(snapshot),
			Ok(_) => Err(DesktopError::internal("unexpected response")),
			Err(error) => Err(error),
		});
		let snapshot = result?;
		Py::new(py, snapshot).map_err(Into::into)
	}

	/// Query the accessibility nodes of `target` matching `query` — a dict
	/// with optional `"role"` / `"title"` / `"value"` / `"limit"` keys (or
	/// `None` for no filter). Returns the matching nodes, each carrying a
	/// stable `ref` handle.
	fn ax_query(&self, py: Python<'_>, target: String, query: AxQuery) -> PyResult<Vec<Py<AxNode>>> {
		let core = Arc::clone(&self.core);
		let target = Target::parse(&target);
		let result = py.detach(move || match core.call(|reply| Request::AxQuery { target, query, reply }) {
			Ok(Response::Nodes(nodes)) => Ok(nodes),
			Ok(_) => Err(DesktopError::internal("unexpected response")),
			Err(error) => Err(error),
		});
		let nodes = result?;
		nodes.into_iter().map(|node| Py::new(py, node)).collect()
	}

	/// Accessibility hit-test at global logical desktop coordinates (not
	/// capture-frame pixels); needs no prior capture. Returns the deepest
	/// element under the point, or `None`.
	fn ax_element_at(&self, py: Python<'_>, target: String, x: f64, y: f64) -> PyResult<Option<Py<AxNode>>> {
		let core = Arc::clone(&self.core);
		let target = Target::parse(&target);
		let result = py.detach(move || match core.call(|reply| Request::AxElementAt { target, x, y, reply }) {
			Ok(Response::Node(node)) => Ok(node),
			Ok(_) => Err(DesktopError::internal("unexpected response")),
			Err(error) => Err(error),
		});
		match result? {
			Some(node) => Py::new(py, node).map(Some),
			None => Ok(None),
		}
	}

	/// The currently focused accessibility element of the desktop, if any.
	fn ax_focused(&self, py: Python<'_>) -> PyResult<Option<Py<AxNode>>> {
		let core = Arc::clone(&self.core);
		let result = py.detach(move || match core.call(|reply| Request::AxFocused { reply }) {
			Ok(Response::Node(node)) => Ok(node),
			Ok(_) => Err(DesktopError::internal("unexpected response")),
			Err(error) => Err(error),
		});
		match result? {
			Some(node) => Py::new(py, node).map(Some),
			None => Ok(None),
		}
	}

	/// Re-read one node by its `ref` handle (from `ax_snapshot` / `ax_query` /
	/// child/parent navigation). Fails with a `StaleRef` error once the ref's
	/// snapshot generation is gone.
	fn ax_node(&self, py: Python<'_>, reference: String) -> PyResult<Py<AxNode>> {
		let core = Arc::clone(&self.core);
		let result = py.detach(move || match core.call(|reply| Request::AxNode { reference, reply }) {
			Ok(Response::Node(Some(node))) => Ok(node),
			Ok(_) => Err(DesktopError::internal("unexpected response")),
			Err(error) => Err(error),
		});
		let node = result?;
		Py::new(py, node).map_err(Into::into)
	}

	/// The raw accessibility attributes of a node as `(name, value)` pairs.
	fn ax_attributes(&self, py: Python<'_>, reference: String) -> PyResult<Vec<(String, String)>> {
		let core = Arc::clone(&self.core);
		py.detach(move || match core.call(|reply| Request::AxAttributes { reference, reply }) {
			Ok(Response::Attributes(attributes)) => Ok(attributes),
			Ok(_) => Err(DesktopError::internal("unexpected response")),
			Err(error) => Err(error),
		})
		.map_err(PyErr::from)
	}

	/// The direct children of a node, each carrying its own `ref` handle.
	fn ax_children(&self, py: Python<'_>, reference: String) -> PyResult<Vec<Py<AxNode>>> {
		let core = Arc::clone(&self.core);
		let result = py.detach(move || match core.call(|reply| Request::AxChildren { reference, reply }) {
			Ok(Response::Nodes(nodes)) => Ok(nodes),
			Ok(_) => Err(DesktopError::internal("unexpected response")),
			Err(error) => Err(error),
		});
		let nodes = result?;
		nodes.into_iter().map(|node| Py::new(py, node)).collect()
	}

	/// The parent of a node, or `None` for the root.
	fn ax_parent(&self, py: Python<'_>, reference: String) -> PyResult<Option<Py<AxNode>>> {
		let core = Arc::clone(&self.core);
		let result = py.detach(move || match core.call(|reply| Request::AxParent { reference, reply }) {
			Ok(Response::Node(node)) => Ok(node),
			Ok(_) => Err(DesktopError::internal("unexpected response")),
			Err(error) => Err(error),
		});
		match result? {
			Some(node) => Py::new(py, node).map(Some),
			None => Ok(None),
		}
	}

	/// Perform a named accessibility action on a node (e.g. `"press"`); the
	/// node's `actions` list names what it supports.
	fn ax_perform(&self, py: Python<'_>, reference: String, action: String) -> PyResult<()> {
		let core = Arc::clone(&self.core);
		py.detach(move || {
			core.call(|reply| Request::AxPerform { reference, action, reply })
				.and_then(response_unit)
		})
		.map_err(PyErr::from)
	}

	/// Set the value of a value-carrying node (e.g. a text field).
	fn ax_set_value(&self, py: Python<'_>, reference: String, value: String) -> PyResult<()> {
		let core = Arc::clone(&self.core);
		py.detach(move || {
			core.call(|reply| Request::AxSetValue { reference, value, reply })
				.and_then(response_unit)
		})
		.map_err(PyErr::from)
	}

	/// Move keyboard focus to a node.
	fn ax_focus(&self, py: Python<'_>, reference: String) -> PyResult<()> {
		let core = Arc::clone(&self.core);
		py.detach(move || {
			core.call(|reply| Request::AxFocus { reference, reply })
				.and_then(response_unit)
		})
		.map_err(PyErr::from)
	}

	/// Click the center of a node's bounds. `opts` may set `button`,
	/// `count`, `modifiers` and `delivery_mode`.
	#[pyo3(signature = (reference, opts=None))]
	fn ax_click(&self, py: Python<'_>, reference: String, opts: Option<PointerOptions>) -> PyResult<()> {
		let options = ParsedPointerOptions::parse(opts)?;
		let core = Arc::clone(&self.core);
		py.detach(move || {
			core.call(|reply| Request::AxClick { reference, options, reply })
				.and_then(response_unit)
		})
		.map_err(PyErr::from)
	}
}

#[cfg(test)]
mod capture_tests {
	use image::{Rgba, RgbaImage};

	use super::*;
	use crate::desktop::{
		backend::{AxBackend, Backend},
		error::ErrorCode,
		keys::KeyName,
	};

	const WAYLAND_ID: &str = "atspi::1.31:/org/a11y/atspi/accessible/1";

	/// Backend that mints a composite AT-SPI window id, mirroring the Wayland
	/// `AtSpiAx` path, plus a plain 1x desktop display. Exists to exercise
	/// `Worker::process` without a real display.
	struct FakeWaylandBackend {
		window:         DesktopWindow,
		desktop_display: DesktopDisplay,
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
				desktop_display: DesktopDisplay {
					id:           "1".to_string(),
					name:         "Fake".to_string(),
					x:            0,
					y:            0,
					width:        400,
					height:       300,
					scale:        1.0,
					pixel_x:      0,
					pixel_y:      0,
					pixel_width:  400,
					pixel_height: 300,
					is_primary:   true,
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
			Ok(vec![self.desktop_display.clone()])
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
				Target::Desktop => {
					let image = RgbaImage::from_pixel(400, 300, Rgba([18, 52, 86, 255]));
					let geometry = FrameGeometry::for_displays(&[self.desktop_display.clone()]);
					Ok((image, geometry))
				},
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

		fn raise_window(&mut self, _: &str) -> CoreResult<()> {
			unreachable!("raise_window not exercised")
		}

		fn ax(&mut self) -> Option<&mut dyn AxBackend> {
			None
		}
	}

	fn worker_with(backend: impl Backend + 'static) -> Worker {
		Worker {
			backend:      Ok(Box::new(backend)),
			registry:     AxRegistry::default(),
			frames:       HashMap::new(),
			capabilities: Arc::new(Mutex::new(DesktopCapabilities::unavailable())),
		}
	}

	fn capture_request(target: Target) -> Request {
		let (reply, _rx) = flume::bounded(1);
		Request::Capture { target, caps: CaptureCaps::default(), region: None, reply }
	}

	fn region_request(target: Target, region: CaptureRegion) -> Request {
		let (reply, _rx) = flume::bounded(1);
		Request::Capture { target, caps: CaptureCaps::default(), region: Some(region), reply }
	}

	fn caps_region_request(
		target: Target,
		caps: CaptureCaps,
		region: Option<CaptureRegion>,
	) -> Request {
		let (reply, _rx) = flume::bounded(1);
		Request::Capture { target, caps, region, reply }
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
		// T2b wire format: window frames keep Window kind + capture-time dims.
		assert_eq!(
			capture.geometry.kind,
			GeometryKindWire::Window { captured_width: 64, captured_height: 48 }
		);
		assert_eq!(capture.geometry.width, 64);
		assert_eq!(capture.geometry.height, 48);
		assert_eq!(capture.geometry.regions.len(), 1);
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

	/// T2b: a desktop region capture crops the full composite raster *before*
	/// caps; the cropped frame keeps Desktop kind, its region's logical rect is
	/// the intersection in global logical coordinates, its pixel rect is
	/// crop-local, and display metadata is clipped to the crop.
	#[test]
	fn desktop_region_capture_crops_composite_and_geometry() {
		let mut worker = worker_with(FakeWaylandBackend::new());
		let response = worker
			.process(&region_request(
				Target::Desktop,
				CaptureRegion { x: 100.0, y: 50.0, width: 200.0, height: 150.0 },
			))
			.expect("region capture should succeed");
		let Response::Capture(capture) = response else {
			panic!("expected a capture response");
		};
		assert_eq!(capture.target, "desktop");
		assert_eq!(capture.width, 200);
		assert_eq!(capture.height, 150);
		assert_eq!(capture.source_width, 200); // no caps — crop size is pre-caps
		assert_eq!(capture.source_height, 150);
		assert_eq!(capture.geometry.kind, GeometryKindWire::Desktop);
		assert_eq!(capture.geometry.width, 200);
		assert_eq!(capture.geometry.height, 150);
		assert_eq!(capture.geometry.regions.len(), 1);
		let region = &capture.geometry.regions[0];
		assert_eq!((region.x, region.y), (100.0, 50.0));
		assert_eq!((region.width, region.height), (200.0, 150.0));
		assert_eq!((region.pixel_x, region.pixel_y), (0.0, 0.0));
		assert_eq!((region.pixel_width, region.pixel_height), (200.0, 150.0));
		assert_eq!(capture.displays.len(), 1);
		assert_eq!(capture.displays[0].id, "1");
		assert_eq!((capture.displays[0].pixel_x, capture.displays[0].pixel_y), (0, 0));
		assert_eq!((capture.displays[0].pixel_width, capture.displays[0].pixel_height), (200, 150));
		// Region crops are for viewing only: a crop alone must not store a frame,
		// because pointer coordinates belong to the most recent *full* capture.
		let Err(err) = worker.frame(&Target::Desktop) else {
			panic!("a region crop must not store a frame");
		};
		assert_eq!(err.code, ErrorCode::InvalidCoordinateFrame);
	}

	/// Frame semantics (fail-loud): a region crop invalidates the target's
	/// stored frame — the crop is a different viewport whose pixels are not
	/// valid pointer coordinates, so keeping the old full frame would let a
	/// model feed crop pixels into input and silently click the wrong place.
	/// Coordinate input after a crop fails with InvalidCoordinateFrame until
	/// a fresh full capture re-stores the frame.
	#[test]
	fn region_capture_invalidates_fullscreen_frame() {
		let mut worker = worker_with(FakeWaylandBackend::new());

		// Full desktop capture with caps: stored frame is the downsampled 200x150,
		// and coordinate input maps against it.
		worker
			.process(&caps_region_request(
				Target::Desktop,
				CaptureCaps { max_width: Some(200), max_height: None },
				None,
			))
			.expect("full desktop capture should succeed");
		let frame = worker.frame(&Target::Desktop).expect("full capture stores a frame");
		assert_eq!(frame.map_point_static(99.0, 74.0).unwrap(), (198.0, 148.0));

		// Region crop: the returned geometry describes the crop (100x75), but it
		// must NOT be usable for pointer input — the stored frame is gone.
		let response = worker
			.process(&region_request(
				Target::Desktop,
				CaptureRegion { x: 0.0, y: 0.0, width: 100.0, height: 75.0 },
			))
			.expect("region capture should succeed");
		let Response::Capture(capture) = response else {
			panic!("expected a capture response");
		};
		assert_eq!((capture.width, capture.height), (100, 75));

		// Coordinate input fails loudly: the crop invalidated the frame.
		let Err(err) = worker.map_point(&Target::Desktop, 50.0, 37.0) else {
			panic!("coordinate input after a region crop must fail");
		};
		assert_eq!(err.code, ErrorCode::InvalidCoordinateFrame);

		// A later full capture restores the frame and coordinate input again.
		worker
			.process(&capture_request(Target::Desktop))
			.expect("full desktop capture should succeed");
		let frame = worker.frame(&Target::Desktop).expect("full capture stores a frame");
		assert_eq!(frame.map_point_static(399.0, 299.0).unwrap(), (399.0, 299.0));
	}

	/// T2b: region crops happen before `apply_capture_caps`, so caps downscale
	/// the crop (not the whole composite) and `source_*` stays at crop size.
	#[test]
	fn desktop_region_capture_downscales_after_crop() {
		let mut worker = worker_with(FakeWaylandBackend::new());
		let caps = CaptureCaps { max_width: Some(200), max_height: None };
		let response = worker
			.process(&caps_region_request(
				Target::Desktop,
				caps,
				Some(CaptureRegion { x: 0.0, y: 0.0, width: 400.0, height: 300.0 }),
			))
			.expect("region capture with caps should succeed");
		let Response::Capture(capture) = response else {
			panic!("expected a capture response");
		};
		assert_eq!((capture.width, capture.height), (200, 150));
		assert_eq!((capture.source_width, capture.source_height), (400, 300));
		assert_eq!(capture.geometry.kind, GeometryKindWire::Desktop);
	}

	/// T2b: a region fully outside every display clamps to nothing and is
	/// rejected as InvalidTarget (region coordinates are logical desktop
	/// coordinates; out-of-bounds parts of valid regions are clamped away).
	#[test]
	fn region_outside_desktop_is_rejected() {
		let mut worker = worker_with(FakeWaylandBackend::new());
		let Err(err) = worker.process(&region_request(
			Target::Desktop,
			CaptureRegion { x: 1000.0, y: 1000.0, width: 50.0, height: 50.0 },
		)) else {
			panic!("a region outside the desktop should fail");
		};
		assert_eq!(err.code, ErrorCode::InvalidTarget);
	}

	/// T2b: combining a region with a window target is invalid, and the check
	/// fires before any backend capture runs.
	#[test]
	fn window_capture_rejects_region() {
		let mut worker = worker_with(FakeWaylandBackend::new());
		let Err(err) = worker.process(&region_request(
			Target::Window(WAYLAND_ID.to_string()),
			CaptureRegion { x: 0.0, y: 0.0, width: 10.0, height: 10.0 },
		)) else {
			panic!("window + region should fail");
		};
		assert_eq!(err.code, ErrorCode::InvalidTarget);
	}

	/// T2b: a plain desktop capture (no region) still reports Desktop geometry
	/// with the full display region and matching display metadata.
	#[test]
	fn desktop_full_capture_reports_full_geometry() {
		let mut worker = worker_with(FakeWaylandBackend::new());
		let response = worker
			.process(&capture_request(Target::Desktop))
			.expect("desktop capture should succeed");
		let Response::Capture(capture) = response else {
			panic!("expected a capture response");
		};
		assert_eq!((capture.width, capture.height), (400, 300));
		assert_eq!(capture.geometry.kind, GeometryKindWire::Desktop);
		assert_eq!(capture.geometry.regions.len(), 1);
		let region = &capture.geometry.regions[0];
		assert_eq!((region.x, region.y), (0.0, 0.0));
		assert_eq!((region.pixel_width, region.pixel_height), (400.0, 300.0));
		assert_eq!(capture.displays.len(), 1);
		assert_eq!(capture.displays[0].scale, 1.0);
	}
}

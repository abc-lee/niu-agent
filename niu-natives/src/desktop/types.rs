use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

/// Monitor geometry in both global logical desktop coordinates and composite
/// screenshot pixels.
#[pyclass(skip_from_py_object)]
#[derive(Debug, Clone)]
pub struct DesktopDisplay {
	#[pyo3(get)]
	pub id:           String,
	#[pyo3(get)]
	pub name:         String,
	#[pyo3(get)]
	pub x:            i32,
	#[pyo3(get)]
	pub y:            i32,
	#[pyo3(get)]
	pub width:        u32,
	#[pyo3(get)]
	pub height:       u32,
	#[pyo3(get)]
	pub scale:        f64,
	#[pyo3(get)]
	pub pixel_x:      u32,
	#[pyo3(get)]
	pub pixel_y:      u32,
	#[pyo3(get)]
	pub pixel_width:  u32,
	#[pyo3(get)]
	pub pixel_height: u32,
	#[pyo3(get)]
	pub is_primary:   bool,
}

/// One capturable top-level window in global logical desktop coordinates.
#[pyclass(skip_from_py_object)]
#[derive(Debug, Clone)]
pub struct DesktopWindow {
	/// Backend-defined opaque window id, valid as a capture target while the
	/// window lives. Numeric on X11/Win32/macOS; a composite AT-SPI string on
	/// Wayland (e.g. `atspi::1.31:/org/a11y/atspi/accessible/1`). Never parse
	/// it.
	#[pyo3(get)]
	pub id: String,
	/// Window title; may be empty for untitled windows.
	#[pyo3(get)]
	pub title: String,
	/// Owning application name.
	#[pyo3(get)]
	pub app: String,
	/// Owning process id when the platform exposes it.
	#[pyo3(get)]
	pub pid: Option<u32>,
	#[pyo3(get)]
	pub x: i32,
	#[pyo3(get)]
	pub y: i32,
	#[pyo3(get)]
	pub width: u32,
	#[pyo3(get)]
	pub height: u32,
	/// Whether the window currently holds input focus.
	#[pyo3(get)]
	pub focused: bool,
}

/// One display/region rectangle serialized over the Python wire format: the
/// logical desktop rect (x/y/width/height) plus the corresponding rect in the
/// composite screenshot's pixel space (pixel_*), both non-local when the frame
/// is a region crop of a larger composite.
#[derive(Debug, Clone, PartialEq)]
pub struct RegionWire {
	pub x:            f64,
	pub y:            f64,
	pub width:        f64,
	pub height:       f64,
	pub pixel_x:      f64,
	pub pixel_y:      f64,
	pub pixel_width:  f64,
	pub pixel_height: f64,
}

#[derive(Debug, Clone, PartialEq)]
pub enum GeometryKindWire {
	Desktop,
	Window { captured_width: u32, captured_height: u32 },
}

/// A `FrameGeometry` serialized as plain data for the capture()/map_point()
/// Python dict wire format. `width`/`height` are the frame's pixel size (the
/// size of the returned PNG). For `Window` frames the region rect also records
/// the window's logical position at capture time, so a geometry dict alone is
/// enough to map capture pixels back to global logical desktop coordinates.
#[derive(Debug, Clone, PartialEq)]
pub struct GeometryWire {
	pub kind:    GeometryKindWire,
	pub width:   u32,
	pub height:  u32,
	pub regions: Vec<RegionWire>,
}

/// Captured PNG frame plus geometry bookkeeping. Internal session payload;
/// the Python `capture()` method serializes it into a dict with the fields
/// below plus a `geometry` sub-dict (T2b wire format).
#[pyclass(skip_from_py_object)]
#[derive(Debug, Clone)]
pub struct DesktopCapture {
	#[pyo3(get)]
	pub data:           Vec<u8>,
	#[pyo3(get)]
	pub width:          u32,
	#[pyo3(get)]
	pub height:         u32,
	/// Pre-scaling capture width in native pixels; equals `width` when unscaled.
	#[pyo3(get)]
	pub source_width:   u32,
	/// Pre-scaling capture height in native pixels; equals `height` when
	/// unscaled.
	#[pyo3(get)]
	pub source_height:  u32,
	#[pyo3(get)]
	pub target:         String,
	#[pyo3(get)]
	pub displays:       Vec<DesktopDisplay>,
	#[pyo3(get)]
	pub backend:        String,
	#[pyo3(get)]
	pub display_server: Option<String>,
	/// Post-caps frame geometry (kind/regions), wire-serializable; consumed by
	/// `map_point(x, y, geometry)`.
	pub geometry: GeometryWire,
}

#[pyclass(skip_from_py_object)]
#[derive(Debug, Clone)]
pub struct DesktopCapabilities {
	#[pyo3(get)]
	pub backend: String,
	#[pyo3(get)]
	pub display_server: Option<String>,
	#[pyo3(get)]
	pub capture: bool,
	#[pyo3(get)]
	pub input: bool,
	/// Accessibility tree traversal is not exposed (ax_lite keeps only the
	/// macOS window-focus subset) — always `false`.
	#[pyo3(get)]
	pub ax: bool,
	#[pyo3(get)]
	pub background_window_input: bool,
	#[pyo3(get)]
	pub delivery_modes: Vec<String>,
	#[pyo3(get)]
	pub capture_permission: String,
	#[pyo3(get)]
	pub input_permission: String,
	/// Accessibility traversal permission is not exposed (see `ax`) — always
	/// `"unavailable"`.
	#[pyo3(get)]
	pub ax_permission: String,
	#[pyo3(get)]
	pub display_count: u32,
}

impl DesktopCapabilities {
	pub(crate) fn unavailable() -> Self {
		Self {
			backend: "unavailable".to_string(),
			display_server: None,
			capture: false,
			input: false,
			ax: false,
			background_window_input: false,
			delivery_modes: Vec::new(),
			capture_permission: "unavailable".to_string(),
			input_permission: "unavailable".to_string(),
			ax_permission: "unavailable".to_string(),
			display_count: 0,
		}
	}
}

/// Session construction options. Python input: `None` or a dict with optional
/// `"display"` key.
#[derive(Debug, Clone, Default)]
pub struct DesktopSessionOptions {
	pub display: Option<String>,
}

impl<'a, 'py> FromPyObject<'a, 'py> for DesktopSessionOptions {
	type Error = PyErr;

	fn extract(obj: Borrowed<'a, 'py, PyAny>) -> Result<Self, Self::Error> {
		if obj.is_none() {
			return Ok(Self::default());
		}
		let dict = obj.cast::<PyDict>().map_err(|_| {
			PyTypeError::new_err("DesktopSessionOptions must be None or a dict")
		})?;
		let display = optional_item(&dict, "display")?;
		Ok(Self { display })
	}
}

/// Capture caps. Python input: `None` or a dict with optional `"max_width"` /
/// `"max_height"` keys.
#[derive(Debug, Clone, Default)]
pub struct CaptureCaps {
	pub max_width:  Option<u32>,
	pub max_height: Option<u32>,
}

impl<'a, 'py> FromPyObject<'a, 'py> for CaptureCaps {
	type Error = PyErr;

	fn extract(obj: Borrowed<'a, 'py, PyAny>) -> Result<Self, Self::Error> {
		if obj.is_none() {
			return Ok(Self::default());
		}
		let dict = obj.cast::<PyDict>().map_err(|_| {
			PyTypeError::new_err("CaptureCaps must be None or a dict")
		})?;
		Ok(Self {
			max_width: optional_item(&dict, "max_width")?,
			max_height: optional_item(&dict, "max_height")?,
		})
	}
}

/// Pointer/typing options. Python input: `None` or a dict with optional
/// `"button"` / `"count"` / `"modifiers"` / `"delivery_mode"` keys.
#[derive(Debug, Clone, Default)]
pub struct PointerOptions {
	pub button:        Option<String>,
	pub count:         Option<u32>,
	pub modifiers:     Option<Vec<String>>,
	pub delivery_mode: Option<String>,
}

impl<'a, 'py> FromPyObject<'a, 'py> for PointerOptions {
	type Error = PyErr;

	fn extract(obj: Borrowed<'a, 'py, PyAny>) -> Result<Self, Self::Error> {
		if obj.is_none() {
			return Ok(Self::default());
		}
		let dict = obj.cast::<PyDict>().map_err(|_| {
			PyTypeError::new_err("PointerOptions must be None or a dict")
		})?;
		Ok(Self {
			button: optional_item(&dict, "button")?,
			count: optional_item(&dict, "count")?,
			modifiers: optional_item(&dict, "modifiers")?,
			delivery_mode: optional_item(&dict, "delivery_mode")?,
		})
	}
}

/// A point in capture-frame pixel coordinates (drag path element).
/// Python input: a dict with `"x"`/`"y"` keys, or a 2-sequence.
#[derive(Debug, Clone, Copy)]
pub struct DesktopPoint {
	pub x: f64,
	pub y: f64,
}

impl<'a, 'py> FromPyObject<'a, 'py> for DesktopPoint {
	type Error = PyErr;

	fn extract(obj: Borrowed<'a, 'py, PyAny>) -> Result<Self, Self::Error> {
		if let Ok(dict) = obj.cast::<PyDict>() {
			let x = required_item(&dict, "x")?;
			let y = required_item(&dict, "y")?;
			return Ok(Self { x, y });
		}
		let (x, y) = obj
			.extract::<(f64, f64)>()
			.map_err(|_| PyTypeError::new_err("DesktopPoint must be a dict with 'x'/'y' or a 2-sequence"))?;
		Ok(Self { x, y })
	}
}

/// Reads an optional dict item: a missing key or an explicit `None` value both
/// yield `None`; a present value must extract as `T`.
fn optional_item<'py, T>(dict: &Bound<'py, PyDict>, key: &str) -> PyResult<Option<T>>
where
	for<'a> T: FromPyObject<'a, 'py>,
{
	match dict.get_item(key)? {
		Some(value) if !value.is_none() => {
			Ok(Some(value.extract().map_err(Into::into)?))
		},
		_ => Ok(None),
	}
}

/// Reads a required dict item: a missing key or `None` value is an error.
fn required_item(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<f64> {
	match dict.get_item(key)?.filter(|value| !value.is_none()) {
		Some(value) => value.extract(),
		None => Err(PyTypeError::new_err(format!(
			"DesktopPoint dict is missing required '{key}'"
		))),
	}
}

/// Extracts a numeric dict item as `f64` (ints and floats both accepted).
fn numeric_item(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<f64> {
	match dict.get_item(key)?.filter(|value| !value.is_none()) {
		Some(value) => value.extract(),
		None => Err(PyTypeError::new_err(format!(
			"region/geometry dict is missing required '{key}'"
		))),
	}
}

/// Reads a required scalar region/geometry item (numbers or booleans/strings
/// stay typed via `T`).
fn required_typed<'py, T>(dict: &Bound<'py, PyDict>, key: &str) -> PyResult<T>
where
	for<'a> T: FromPyObject<'a, 'py, Error = PyErr>,
{
	match dict.get_item(key)?.filter(|value| !value.is_none()) {
		Some(value) => value.extract(),
		None => Err(PyTypeError::new_err(format!(
			"region/geometry dict is missing required '{key}'"
		))),
	}
}

/// A region-capture rectangle in logical desktop coordinates.
/// Python input: `(x, y, width, height)` sequence or a dict with those keys.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct CaptureRegion {
	pub x:      f64,
	pub y:      f64,
	pub width:  f64,
	pub height: f64,
}

impl<'a, 'py> FromPyObject<'a, 'py> for CaptureRegion {
	type Error = PyErr;

	fn extract(obj: Borrowed<'a, 'py, PyAny>) -> Result<Self, Self::Error> {
		if let Ok(dict) = obj.cast::<PyDict>() {
			return Ok(Self {
				x:      numeric_item(&dict, "x")?,
				y:      numeric_item(&dict, "y")?,
				width:  numeric_item(&dict, "width")?,
				height: numeric_item(&dict, "height")?,
			});
		}
		let values = obj
			.extract::<Vec<f64>>()
			.map_err(|_| PyTypeError::new_err(
				"capture region must be a (x, y, width, height) sequence or a dict with those keys",
			))?;
		if values.len() != 4 {
			return Err(PyTypeError::new_err(format!(
				"capture region must have exactly 4 elements (x, y, width, height), got {}",
				values.len()
			)));
		}
		Ok(Self {
			x:      values[0],
			y:      values[1],
			width:  values[2],
			height: values[3],
		})
	}
}

impl GeometryWire {
	/// Parses one region entry of a `geometry` dict (see `FromPyObject` for
	/// `GeometryWire`).
	fn region_from_dict(dict: &Bound<'_, PyDict>) -> PyResult<RegionWire> {
		Ok(RegionWire {
			x:            numeric_item(dict, "x")?,
			y:            numeric_item(dict, "y")?,
			width:        numeric_item(dict, "width")?,
			height:       numeric_item(dict, "height")?,
			pixel_x:      numeric_item(dict, "pixel_x")?,
			pixel_y:      numeric_item(dict, "pixel_y")?,
			pixel_width:  numeric_item(dict, "pixel_width")?,
			pixel_height: numeric_item(dict, "pixel_height")?,
		})
	}
}

/// A geometry dict as returned by `capture()`: `{kind, width, height, regions,
/// [captured_width, captured_height]}` where each region is
/// `{x, y, width, height, pixel_x, pixel_y, pixel_width, pixel_height}`.
impl<'a, 'py> FromPyObject<'a, 'py> for GeometryWire {
	type Error = PyErr;

	fn extract(obj: Borrowed<'a, 'py, PyAny>) -> Result<Self, Self::Error> {
		let dict = obj.cast::<PyDict>().map_err(|_| {
			PyTypeError::new_err("geometry must be the 'geometry' dict returned by capture()")
		})?;
		let kind = required_typed::<String>(&dict, "kind")?;
		let width = required_typed::<u32>(&dict, "width")?;
		let height = required_typed::<u32>(&dict, "height")?;
		let regions_value = dict.get_item("regions")?.ok_or_else(|| {
			PyTypeError::new_err("geometry dict is missing required 'regions'")
		})?;
		let regions = regions_value
			.try_iter()
			.map_err(|_| PyTypeError::new_err("geometry 'regions' must be a list of dicts"))?
			.map(|item| {
				let item = item?;
				let region = item
					.cast::<PyDict>()
					.map_err(|_| PyTypeError::new_err("each geometry region must be a dict"))?;
				Self::region_from_dict(&region)
			})
			.collect::<PyResult<Vec<RegionWire>>>()?;
		let kind = match kind.as_str() {
			"desktop" => GeometryKindWire::Desktop,
			"window" => GeometryKindWire::Window {
				captured_width: required_typed(&dict, "captured_width")?,
				captured_height: required_typed(&dict, "captured_height")?,
			},
			other => {
				return Err(PyTypeError::new_err(format!(
					"geometry 'kind' must be 'desktop' or 'window', got {other:?}"
				)))
			},
		};
		Ok(Self { kind, width, height, regions })
	}
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Target {
	Desktop,
	Window(String),
}

impl Target {
	pub(crate) fn parse(value: &str) -> Self {
		if value.eq_ignore_ascii_case("desktop") {
			Self::Desktop
		} else {
			Self::Window(value.to_string())
		}
	}

	pub(crate) fn key(&self) -> &str {
		match self {
			Self::Desktop => "desktop",
			Self::Window(id) => id,
		}
	}
}

#[derive(Debug, Clone)]
pub enum DisplaySelector {
	All,
	Id(String),
}

impl DisplaySelector {
	pub(crate) fn parse(display: Option<String>) -> Self {
		match display {
			Some(id) if !id.trim().is_empty() && !id.eq_ignore_ascii_case("all") => Self::Id(id),
			_ => Self::All,
		}
	}
}

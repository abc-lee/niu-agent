use std::io::Cursor;

use image::{DynamicImage, ImageFormat, RgbaImage, imageops::FilterType};

use super::{
	error::{CoreResult, DesktopError},
	types::{
		CaptureCaps, CaptureRegion, DesktopDisplay, DesktopWindow, GeometryKindWire, GeometryWire,
		RegionWire,
	},
};

pub const MAX_COMPOSITE_PIXELS: u64 = 268_435_456;

#[derive(Debug, Clone, PartialEq)]
struct FrameRegion {
	x:            f64,
	y:            f64,
	width:        f64,
	height:       f64,
	pixel_x:      f64,
	pixel_y:      f64,
	pixel_width:  f64,
	pixel_height: f64,
}

#[derive(Debug, Clone, PartialEq)]
enum FrameKind {
	Desktop,
	Window { captured_width: u32, captured_height: u32 },
	Identity,
}

#[derive(Debug, Clone, PartialEq)]
pub struct FrameGeometry {
	width:   u32,
	height:  u32,
	regions: Vec<FrameRegion>,
	kind:    FrameKind,
}

impl FrameGeometry {
	pub(crate) fn for_displays(displays: &[DesktopDisplay]) -> Self {
		let width = displays
			.iter()
			.map(|d| d.pixel_x.saturating_add(d.pixel_width))
			.max()
			.unwrap_or(0);
		let height = displays
			.iter()
			.map(|d| d.pixel_y.saturating_add(d.pixel_height))
			.max()
			.unwrap_or(0);
		let regions = displays
			.iter()
			.map(|d| FrameRegion {
				x:            f64::from(d.x),
				y:            f64::from(d.y),
				width:        f64::from(d.width),
				height:       f64::from(d.height),
				pixel_x:      f64::from(d.pixel_x),
				pixel_y:      f64::from(d.pixel_y),
				pixel_width:  f64::from(d.pixel_width),
				pixel_height: f64::from(d.pixel_height),
			})
			.collect();
		Self { width, height, regions, kind: FrameKind::Desktop }
	}

	pub(crate) fn for_window(window: &DesktopWindow, px_width: u32, px_height: u32) -> Self {
		Self {
			width:   px_width,
			height:  px_height,
			regions: vec![FrameRegion {
				x:            f64::from(window.x),
				y:            f64::from(window.y),
				width:        f64::from(window.width),
				height:       f64::from(window.height),
				pixel_x:      0.0,
				pixel_y:      0.0,
				pixel_width:  f64::from(px_width),
				pixel_height: f64::from(px_height),
			}],
			kind:    FrameKind::Window {
				captured_width:  window.width,
				captured_height: window.height,
			},
		}
	}

	pub(crate) const fn identity_global() -> Self {
		Self {
			width:   u32::MAX,
			height:  u32::MAX,
			regions: Vec::new(),
			kind:    FrameKind::Identity,
		}
	}

	pub(crate) fn map_point(
		&self,
		x: f64,
		y: f64,
		current_window: Option<&DesktopWindow>,
	) -> CoreResult<(f64, f64)> {
		if !x.is_finite()
			|| !y.is_finite()
			|| (self.kind != FrameKind::Identity
				&& (x < 0.0 || y < 0.0 || x >= f64::from(self.width) || y >= f64::from(self.height)))
		{
			return Err(DesktopError::invalid_coordinate_frame(format!(
				"coordinate ({x}, {y}) is outside the last capture frame ({}x{} px); pointer/hit-test \
				 coordinates are pixels in the most recent screenshot of this target",
				self.width, self.height
			)));
		}
		if self.kind == FrameKind::Identity {
			return Ok((x, y));
		}
		let region = self
			.regions
			.iter()
			.find(|r| {
				x >= r.pixel_x
					&& x < r.pixel_x + r.pixel_width
					&& y >= r.pixel_y
					&& y < r.pixel_y + r.pixel_height
			})
			.ok_or_else(|| {
				DesktopError::invalid_coordinate_frame(format!(
					"capture coordinate ({x}, {y}) falls between display regions; pick a point inside \
					 one display"
				))
			})?;
		let local_x = (x - region.pixel_x) * region.width / region.pixel_width;
		let local_y = (y - region.pixel_y) * region.height / region.pixel_height;
		match self.kind {
			FrameKind::Window { captured_width, captured_height } => {
				let current = current_window.ok_or_else(|| {
					DesktopError::window_not_found("target window is no longer available")
				})?;
				if current.width != captured_width || current.height != captured_height {
					return Err(DesktopError::invalid_coordinate_frame(
						"target window was resized since capture; capture it again before coordinate \
						 input",
					));
				}
				Ok((f64::from(current.x) + local_x, f64::from(current.y) + local_y))
			},
			FrameKind::Desktop => Ok((region.x + local_x, region.y + local_y)),
			FrameKind::Identity => Ok((x, y)),
		}
	}

	fn scaled(&mut self, ratio_x: f64, ratio_y: f64, width: u32, height: u32) {
		for region in &mut self.regions {
			region.pixel_x *= ratio_x;
			region.pixel_width *= ratio_x;
			region.pixel_y *= ratio_y;
			region.pixel_height *= ratio_y;
		}
		self.width = width;
		self.height = height;
	}

	pub(crate) fn display_metadata(&self, source: &[DesktopDisplay]) -> Vec<DesktopDisplay> {
		self
			.regions
			.iter()
			.map(|region| {
				// Regions are built from (and after a region crop are a subset
				// of) the source displays, in the same order. Match each region
				// to the display that contains its logical rect instead of
				// zipping, so cropped frames stay aligned with a possibly
				// longer display list.
				let display = source
					.iter()
					.find(|display| {
						let right = f64::from(display.x) + f64::from(display.width);
						let bottom = f64::from(display.y) + f64::from(display.height);
						region.x >= f64::from(display.x) - 1e-6
							&& region.y >= f64::from(display.y) - 1e-6
							&& region.x + region.width <= right + 1e-6
							&& region.y + region.height <= bottom + 1e-6
					})
					.expect("every frame region must be contained in a source display");
				DesktopDisplay {
					id:           display.id.clone(),
					name:         display.name.clone(),
					x:            display.x,
					y:            display.y,
					width:        display.width,
					height:       display.height,
					scale:        display.scale,
					pixel_x:      region.pixel_x.round() as u32,
					pixel_y:      region.pixel_y.round() as u32,
					pixel_width:  region.pixel_width.round().max(1.0) as u32,
					pixel_height: region.pixel_height.round().max(1.0) as u32,
					is_primary:   display.is_primary,
				}
			})
			.collect()
	}

	/// Serializes this frame for the Python `capture()` dict wire format.
	pub(crate) fn to_wire(&self) -> GeometryWire {
		GeometryWire {
			kind: match self.kind {
				FrameKind::Desktop => GeometryKindWire::Desktop,
				FrameKind::Window { captured_width, captured_height } => {
					GeometryKindWire::Window { captured_width, captured_height }
				},
				// Identity frames are Rust-side sentinels for AX element clicks
				// (coordinates are already global desktop coordinates); they are
				// passed straight to the backend and never cross the Python wire.
				// `GeometryKindWire` has no identity variant, so this arm is
				// unreachable by construction.
				FrameKind::Identity => unreachable!("identity frames never cross the Python wire"),
			},
			width:   self.width,
			height:  self.height,
			regions: self
				.regions
				.iter()
				.map(|region| RegionWire {
					x:            region.x,
					y:            region.y,
					width:        region.width,
					height:       region.height,
					pixel_x:      region.pixel_x,
					pixel_y:      region.pixel_y,
					pixel_width:  region.pixel_width,
					pixel_height: region.pixel_height,
				})
				.collect(),
		}
	}

	/// Rebuilds a frame from a geometry dict returned by `capture()`.
	pub(crate) fn from_wire(wire: &GeometryWire) -> Self {
		Self {
			width:  wire.width,
			height: wire.height,
			kind: match wire.kind {
				GeometryKindWire::Desktop => FrameKind::Desktop,
				GeometryKindWire::Window { captured_width, captured_height } => {
					FrameKind::Window { captured_width, captured_height }
				},
			},
			regions: wire
				.regions
				.iter()
				.map(|region| FrameRegion {
					x:            region.x,
					y:            region.y,
					width:        region.width,
					height:       region.height,
					pixel_x:      region.pixel_x,
					pixel_y:      region.pixel_y,
					pixel_width:  region.pixel_width,
					pixel_height: region.pixel_height,
				})
				.collect(),
		}
	}

	/// Maps a capture-frame pixel to global logical desktop coordinates using
	/// only the geometry recorded in this frame (no live target lookup).
	///
	/// Both frame kinds map identically here: window frames carry the window's
	/// logical rect at capture time in their region, so the result is anchored
	/// to capture time (input methods that need live re-anchoring go through
	/// [`map_point`](Self::map_point) instead).
	pub(crate) fn map_point_static(&self, x: f64, y: f64) -> CoreResult<(f64, f64)> {
		if !x.is_finite()
			|| !y.is_finite()
			|| x < 0.0
			|| y < 0.0
			|| x >= f64::from(self.width)
			|| y >= f64::from(self.height)
		{
			return Err(DesktopError::invalid_coordinate_frame(format!(
				"coordinate ({x}, {y}) is outside the capture frame ({}x{} px); pointer/hit-test \
				 coordinates are pixels in the most recent screenshot of this target",
				self.width, self.height
			)));
		}
		let region = self
			.regions
			.iter()
			.find(|r| {
				x >= r.pixel_x
					&& x < r.pixel_x + r.pixel_width
					&& y >= r.pixel_y
					&& y < r.pixel_y + r.pixel_height
			})
			.ok_or_else(|| {
				DesktopError::invalid_coordinate_frame(format!(
					"capture coordinate ({x}, {y}) falls between display regions; pick a point inside \
					 one display"
				))
			})?;
		let local_x = (x - region.pixel_x) * region.width / region.pixel_width;
		let local_y = (y - region.pixel_y) * region.height / region.pixel_height;
		Ok((region.x + local_x, region.y + local_y))
	}

	/// Desktop composite frames only. Crops this frame to `region` (logical
	/// desktop coordinates relative to the composite desktop origin, matching
	/// the logical rects of the per-display regions).
	///
	/// Each display region is intersected with the logical region rect and its
	/// share is transformed into that display's pixel space (mixed DPI: every
	/// region keeps its own `pixel_width / width` scale — there is no single
	/// global ×scale). The per-display pixel spans are then unioned into one
	/// crop rect, which is returned as `(x, y, width, height)` so the caller
	/// can crop the composite raster before applying capture caps.
	///
	/// After the call `self` describes only the cropped area: the surviving
	/// regions' `pixel_*` rects are translated into the cropped frame and
	/// clipped to the exact whole-pixel span covered, while their logical rects
	/// stay in global desktop coordinates (each is the affine inverse of its
	/// pixel span, so `map_point`/`map_point_static` remain exact). `kind`
	/// stays `Desktop`; out-of-desktop parts of the region are clamped away.
	///
	/// Errors with `InvalidTarget` when the region has zero/negative size or
	/// overlaps no display.
	pub(crate) fn crop_to_logical_region(
		&mut self,
		region: &CaptureRegion,
	) -> CoreResult<(u32, u32, u32, u32)> {
		if !matches!(self.kind, FrameKind::Desktop) {
			return Err(DesktopError::internal(
				"region cropping requires a desktop composite frame",
			));
		}
		if !region.x.is_finite()
			|| !region.y.is_finite()
			|| !region.width.is_finite()
			|| !region.height.is_finite()
		{
			return Err(DesktopError::invalid_target("capture region coordinates must be finite"));
		}
		if region.width <= 0.0 || region.height <= 0.0 {
			return Err(DesktopError::invalid_target(
				"capture region width and height must be greater than zero",
			));
		}
		let frame_width = f64::from(self.width);
		let frame_height = f64::from(self.height);
		let mut crops: Vec<FrameRegion> = Vec::new();
		for source in &self.regions {
			if source.width <= 0.0 || source.height <= 0.0 {
				continue;
			}
			// Logical intersection with the requested region.
			let lx0 = source.x.max(region.x);
			let ly0 = source.y.max(region.y);
			let lx1 = (source.x + source.width).min(region.x + region.width);
			let ly1 = (source.y + source.height).min(region.y + region.height);
			if lx1 <= lx0 || ly1 <= ly0 {
				continue;
			}
			// The display's own pixel<->logical scale.
			let scale_x = source.pixel_width / source.width;
			let scale_y = source.pixel_height / source.height;
			let px0 = source.pixel_x + (lx0 - source.x) * scale_x;
			let px1 = source.pixel_x + (lx1 - source.x) * scale_x;
			let py0 = source.pixel_y + (ly0 - source.y) * scale_y;
			let py1 = source.pixel_y + (ly1 - source.y) * scale_y;
			// Whole-pixel span covering [px0, px1), clamped into both this
			// display's pixel rect and the composite frame.
			let fx0 = px0.floor().max(source.pixel_x).max(0.0);
			let fx1 = px1
				.ceil()
				.min(source.pixel_x + source.pixel_width)
				.min(frame_width);
			let fy0 = py0.floor().max(source.pixel_y).max(0.0);
			let fy1 = py1
				.ceil()
				.min(source.pixel_y + source.pixel_height)
				.min(frame_height);
			if fx0 >= fx1 || fy0 >= fy1 {
				continue;
			}
			// The cropped span's logical rect is the affine inverse of the
			// pixel span, keeping pixel<->logical mapping exact on the crop.
			let cx0 = source.x + (fx0 - source.pixel_x) * source.width / source.pixel_width;
			let cx1 = source.x + (fx1 - source.pixel_x) * source.width / source.pixel_width;
			let cy0 = source.y + (fy0 - source.pixel_y) * source.height / source.pixel_height;
			let cy1 = source.y + (fy1 - source.pixel_y) * source.height / source.pixel_height;
			crops.push(FrameRegion {
				x:            cx0,
				y:            cy0,
				width:        cx1 - cx0,
				height:       cy1 - cy0,
				pixel_x:      fx0,
				pixel_y:      fy0,
				pixel_width:  fx1 - fx0,
				pixel_height: fy1 - fy0,
			});
		}
		if crops.is_empty() {
			return Err(DesktopError::invalid_target(format!(
				"capture region ({}, {}, {}, {}) overlaps no display; region coordinates are \
				 logical desktop coordinates relative to the composite desktop origin",
				region.x, region.y, region.width, region.height
			)));
		}
		let bx0 = crops.iter().map(|crop| crop.pixel_x).fold(f64::INFINITY, f64::min);
		let by0 = crops.iter().map(|crop| crop.pixel_y).fold(f64::INFINITY, f64::min);
		let bx1 = crops
			.iter()
			.map(|crop| crop.pixel_x + crop.pixel_width)
			.fold(0.0f64, f64::max);
		let by1 = crops
			.iter()
			.map(|crop| crop.pixel_y + crop.pixel_height)
			.fold(0.0f64, f64::max);
		let width = (bx1 - bx0) as u32;
		let height = (by1 - by0) as u32;
		for crop in &mut crops {
			crop.pixel_x -= bx0;
			crop.pixel_y -= by0;
		}
		self.regions = crops;
		self.width = width;
		self.height = height;
		Ok((bx0 as u32, by0 as u32, width, height))
	}
}

pub fn apply_capture_caps(
	mut image: RgbaImage,
	geometry: &mut FrameGeometry,
	caps: &CaptureCaps,
) -> CoreResult<RgbaImage> {
	if image.width() == 0 || image.height() == 0 {
		return Err(DesktopError::capture_failed("capture returned an empty image"));
	}
	if caps.max_width == Some(0) || caps.max_height == Some(0) {
		return Err(DesktopError::invalid_target("capture caps must be greater than zero"));
	}
	let mut ratio = 1.0f64;
	if let Some(max_width) = caps.max_width {
		ratio = ratio.min(f64::from(max_width) / f64::from(image.width()));
	}
	if let Some(max_height) = caps.max_height {
		ratio = ratio.min(f64::from(max_height) / f64::from(image.height()));
	}
	let width = (f64::from(image.width()) * ratio).round().max(1.0) as u32;
	let height = (f64::from(image.height()) * ratio).round().max(1.0) as u32;
	if u64::from(width) * u64::from(height) > MAX_COMPOSITE_PIXELS {
		return Err(DesktopError::capture_failed(format!(
			"composite {width}x{height} exceeds the native safety limit"
		)));
	}
	if width != image.width() || height != image.height() {
		let ratio_x = f64::from(width) / f64::from(image.width());
		let ratio_y = f64::from(height) / f64::from(image.height());
		image = image::imageops::resize(&image, width, height, FilterType::Triangle);
		geometry.scaled(ratio_x, ratio_y, width, height);
	}
	Ok(image)
}

pub fn encode_png(image: RgbaImage) -> CoreResult<Vec<u8>> {
	let mut png = Vec::with_capacity(image.len() / 2);
	DynamicImage::ImageRgba8(image)
		.write_to(&mut Cursor::new(&mut png), ImageFormat::Png)
		.map_err(|error| DesktopError::capture_failed(format!("PNG encoding failed: {error}")))?;
	Ok(png)
}

#[cfg(test)]
mod tests {
	use image::Rgba;

	use super::*;
	use crate::desktop::error::ErrorCode;

	fn display(scale: f64) -> DesktopDisplay {
		display_at("1", scale, 100, 50, 400, 300, 0, 0)
	}

	/// Generalized display constructor: logical rect + composite pixel origin.
	fn display_at(
		id: &str,
		scale: f64,
		x: i32,
		y: i32,
		width: u32,
		height: u32,
		pixel_x: u32,
		pixel_y: u32,
	) -> DesktopDisplay {
		DesktopDisplay {
			id:   id.to_string(),
			name: id.to_string(),
			x,
			y,
			width,
			height,
			scale,
			pixel_x,
			pixel_y,
			pixel_width: (width as f64 * scale).round() as u32,
			pixel_height: (height as f64 * scale).round() as u32,
			is_primary: true,
		}
	}
	fn window(x: i32, y: i32) -> DesktopWindow {
		DesktopWindow {
			id: "7".into(),
			title: "T".into(),
			app: "A".into(),
			pid: None,
			x,
			y,
			width: 400,
			height: 300,
			focused: false,
		}
	}

	#[test]
	fn pixel_to_logical_at_one_and_two_x() {
		for scale in [1.0, 2.0] {
			let f = FrameGeometry::for_displays(&[display(scale)]);
			assert_eq!(f.map_point(200.0 * scale, 100.0 * scale, None).unwrap(), (300.0, 150.0));
		}
	}
	#[test]
	fn moved_window_is_reanchored() {
		let f = FrameGeometry::for_window(&window(10, 20), 800, 600);
		assert_eq!(f.map_point(400.0, 300.0, Some(&window(110, 220))).unwrap(), (310.0, 370.0));
	}
	/// Identity frame: coordinates are already global logical desktop
	/// coordinates (AX element bounds), so map_point passes them through
	/// verbatim — no region lookup (works with empty regions) and negative
	/// multi-display coordinates stay valid.
	#[test]
	fn identity_global_maps_coordinates_verbatim() {
		let f = FrameGeometry::identity_global();
		assert_eq!((f.width, f.height), (u32::MAX, u32::MAX));
		assert!(f.regions.is_empty());
		assert_eq!(f.map_point(-100.0, 42.5, None).unwrap(), (-100.0, 42.5));
		assert_eq!(f.map_point(0.0, 0.0, None).unwrap(), (0.0, 0.0));
		assert_eq!(f.map_point(3_800.0, 2_160.0, None).unwrap(), (3_800.0, 2_160.0));
	}
	#[test]
	fn cap_scaling_adjusts_geometry() {
		let mut f = FrameGeometry::for_displays(&[display(2.0)]);
		let image = RgbaImage::from_pixel(800, 600, Rgba([0, 0, 0, 255]));
		let image = apply_capture_caps(image, &mut f, &CaptureCaps {
			max_width:  Some(400),
			max_height: Some(400),
		})
		.unwrap();
		assert_eq!((image.width(), image.height()), (400, 300));
		assert_eq!(f.map_point(200.0, 100.0, None).unwrap(), (300.0, 150.0));
	}

	/// 1x + 2x dual-display composite used by the T2b region tests:
	/// logical x∈[100,500) 1x (pixels 0..400) then x∈[500,900) 2x
	/// (pixels 400..1200); both logical y∈[50,350) at pixels 0..300 (1x) and
	/// 0..600 (2x). Composite raster is 1200x600.
	fn dual_mixed_dpi() -> Vec<DesktopDisplay> {
		vec![
			display_at("1x", 1.0, 100, 50, 400, 300, 0, 0),
			display_at("2x", 2.0, 500, 50, 400, 300, 400, 0),
		]
	}

	/// T2b acceptance: a cross-screen logical region over the mixed-DPI
	/// composite crops to the union of per-display pixel spans (no single
	/// ×scale), regions keep global logical rects, and map_point round-trips
	/// crop pixels back to global logical coordinates on both displays.
	#[test]
	fn region_crop_mixed_dpi_dual_display_geometry_and_map() {
		let displays = dual_mixed_dpi();
		let mut frame = FrameGeometry::for_displays(&displays);
		assert_eq!((frame.width, frame.height), (1200, 600));

		// Sanity: uncropped pixels map through each display's own scale.
		assert_eq!(frame.map_point_static(600.0, 100.0).unwrap(), (600.0, 100.0));
		assert_eq!(frame.map_point_static(600.0, 300.0).unwrap(), (600.0, 200.0));

		// Logical region [350,650) x [100,250) — spans both displays, cutting
		// through each display's left/right side.
		let crop = frame
			.crop_to_logical_region(&CaptureRegion {
				x:      350.0,
				y:      100.0,
				width:  300.0,
				height: 150.0,
			})
			.unwrap();
		// A(1x) contributes pixels [250,400)x[50,200), B(2x) [400,700)x[100,400)
		// → union bbox (250,50) 450x350.
		assert_eq!(crop, (250, 50, 450, 350));
		assert_eq!((frame.width, frame.height), (450, 350));
		assert!(matches!(frame.kind, FrameKind::Desktop));
		assert_eq!(frame.regions.len(), 2);

		// 1x display piece: logical rect is the intersection [350,500)x[100,250)
		// in global coordinates; pixels translated into the crop frame.
		let a = &frame.regions[0];
		assert_eq!((a.x, a.y, a.width, a.height), (350.0, 100.0, 150.0, 150.0));
		assert_eq!((a.pixel_x, a.pixel_y), (0.0, 0.0));
		assert_eq!((a.pixel_width, a.pixel_height), (150.0, 150.0));
		// 2x display piece: [500,650)x[100,250) logical; its own scale kept.
		let b = &frame.regions[1];
		assert_eq!((b.x, b.y, b.width, b.height), (500.0, 100.0, 150.0, 150.0));
		assert_eq!((b.pixel_x, b.pixel_y), (150.0, 50.0));
		assert_eq!((b.pixel_width, b.pixel_height), (300.0, 300.0));

		// Round-trips: crop pixels map to *global* logical desktop coords.
		assert_eq!(frame.map_point_static(75.0, 100.0).unwrap(), (425.0, 200.0));
		assert_eq!(frame.map_point_static(0.0, 0.0).unwrap(), (350.0, 100.0));
		assert_eq!(frame.map_point_static(300.0, 200.0).unwrap(), (575.0, 175.0));
		assert_eq!(frame.map_point_static(449.0, 349.0).unwrap(), (649.5, 249.5));
		// Off-frame pixels are rejected.
		assert!(frame.map_point_static(450.0, 0.0).is_err());
		assert!(frame.map_point_static(0.0, 350.0).is_err());
		// Pixels inside the crop but in the gap between display pieces fail
		// with the between-regions error rather than a wrong coordinate.
		assert!(frame.map_point_static(10.0, 300.0).is_err());
	}

	/// T2b: out-of-bounds parts of a region are clamped to the displays —
	/// negative/huge rects crop to exactly what the displays cover.
	#[test]
	fn region_crop_clamps_out_of_bounds_edges() {
		let displays = dual_mixed_dpi();
		let mut frame = FrameGeometry::for_displays(&displays);
		let crop = frame
			.crop_to_logical_region(&CaptureRegion {
				x:      300.0,
				y:      -100.0,
				width:  2000.0,
				height: 700.0,
			})
			.unwrap();
		// A covers pixels [200,400)x[0,300); B the full [400,1200)x[0,600).
		assert_eq!(crop, (200, 0, 1000, 600));
		assert_eq!(frame.regions.len(), 2);
		assert_eq!(frame.map_point_static(0.0, 0.0).unwrap(), (300.0, 50.0));
	}

	/// T2b: a region that overlaps no display (or has no area) is an
	/// InvalidTarget error, pinning the clamp's empty-overlap behavior.
	#[test]
	fn region_outside_desktop_or_degenerate_is_rejected() {
		let displays = dual_mixed_dpi();
		let mut frame = FrameGeometry::for_displays(&displays);
		for bad in [
			CaptureRegion { x: 10000.0, y: 10000.0, width: 10.0, height: 10.0 },
			CaptureRegion { x: 0.0, y: 0.0, width: 0.0, height: 10.0 },
			CaptureRegion { x: 0.0, y: 0.0, width: 10.0, height: -5.0 },
		] {
			let err = frame.crop_to_logical_region(&bad).expect_err("should be rejected");
			assert_eq!(err.code, ErrorCode::InvalidTarget, "{bad:?}");
		}
	}

	/// T2b: the geometry wire format round-trips a full desktop frame and a
	/// cropped (region) frame through `to_wire`/`from_wire` unchanged, and the
	/// rebuilt frame maps pixels identically.
	#[test]
	fn geometry_wire_roundtrip_desktop_and_region() {
		let displays = dual_mixed_dpi();
		let frame = FrameGeometry::for_displays(&displays);
		let rebuilt = FrameGeometry::from_wire(&frame.to_wire());
		assert_eq!(rebuilt, frame);
		assert_eq!(
			rebuilt.map_point_static(600.0, 300.0).unwrap(),
			frame.map_point_static(600.0, 300.0).unwrap()
		);

		let mut cropped = frame;
		cropped
			.crop_to_logical_region(&CaptureRegion {
				x:      350.0,
				y:      100.0,
				width:  300.0,
				height: 150.0,
			})
			.unwrap();
		let rebuilt = FrameGeometry::from_wire(&cropped.to_wire());
		assert_eq!(rebuilt, cropped);
		for (x, y) in [(0.0, 0.0), (75.0, 100.0), (300.0, 200.0), (449.0, 349.0)] {
			assert_eq!(
				rebuilt.map_point_static(x, y).unwrap(),
				cropped.map_point_static(x, y).unwrap(),
				"map mismatch at ({x}, {y})"
			);
		}
	}

	/// T2b: window frames round-trip the wire format (kind keeps the
	/// capture-time window dims) and map to capture-time global logical
	/// coordinates via the static path.
	#[test]
	fn geometry_wire_roundtrip_window_frame() {
		let frame = FrameGeometry::for_window(&window(10, 20), 800, 600);
		let rebuilt = FrameGeometry::from_wire(&frame.to_wire());
		assert_eq!(rebuilt, frame);
		assert_eq!(rebuilt.map_point_static(400.0, 300.0).unwrap(), (210.0, 170.0));
	}
}

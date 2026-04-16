# Knowledge Graph UI Redesign Spec

## Context

The graph visualization frontend (`ui/graph/`) currently uses a cotton-paper theme with SVG dot pattern background, handwritten fonts, dashed borders, and pencil text shadows. Nodes display text labels and use varied shapes (circle, rect, polygon, hexagon). Edges show text labels with varying widths. Filter buttons hide/show nodes by type.

The user wants a cleaner, more minimal redesign while keeping the cotton paper color feel. The key innovation is replacing simple type filters with a **perspective mode** that dynamically re-lays out the entire graph around a chosen "core" type.

## Design

### 1. Visual Style

**Background**: `#faf8f0` warm cream (keep), remove SVG dot pattern `background-image`.

**Font**: System font stack for all UI elements. Remove Google Fonts (`Ma Shan Zheng`, `Caveat`). Remove pencil `text-shadow` on all elements.

```css
font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
```

**Borders**: Thin solid lines `1px solid rgba(0,0,0,0.08)`. Remove all `2px dashed` borders.

**Shadows**: Subtle `box-shadow: 0 1px 3px rgba(0,0,0,0.06)`. Remove pencil-style offset shadows.

### 2. Nodes = Dots

All nodes render as circles with **no text labels**. Type is distinguished by color:

| Type | Color | Fallback |
|------|-------|----------|
| person | `#4A90D9` (blue) | |
| organization | `#5CB85C` (green) | |
| Document | `#E8A838` (gold) | |
| Concept | `#E06B9E` (pink) | |
| location | `#9B59B6` (purple) | |
| event | `#F39C12` (orange) | |
| other Entity | `#95A5A6` (gray) | |

**Size by connection count**:
- Core type nodes (current perspective): `3 + Math.log(connectionCount + 1) * 2` radius
- Non-core nodes: `2 + Math.log(connectionCount + 1) * 1.5` radius
- Search matches: same as core formula, no extra multiplier

**Hover**: Show tooltip with node name + type. Custom HTML tooltip positioned via `graph.graph2ScreenCoords()`.

### 3. Edges = Thin Lines

- All edges: `1px` width, `rgba(0,0,0,0.12)` color for connected, `rgba(0,0,0,0.04)` for unconnected
- No text labels on edges
- No arrow heads, straight lines (curvature=0)
- Remove `edgeTypeLabels` rendering
- Remove confidence-based width variation

### 4. Perspective Mode (Core Innovation)

**Concept**: Toolbar buttons switch the "perspective core". This changes force layout parameters so core-type nodes become visual hubs, with all other nodes orbiting around them. Switching triggers animated layout re-flow — all nodes move to new positions.

**Default state**: On first load, no perspective is active. All nodes display at medium size (12-20px) with standard force layout. The toolbar shows all perspective buttons as inactive (gray). User clicks a button to activate a perspective.

**Perspective options** (as toolbar buttons):

| Button | Core Type | Layout Effect |
|--------|-----------|---------------|
| 人物 | Entity where type=person | Person nodes large & spread, others cluster around |
| 组织 | Entity where type=organization | Organization nodes large & spread |
| 文档 | Document | Document nodes large & spread |
| 概念 | Concept | Concept nodes large & spread |

**Implementation**:
- Uses force-graph (vasturiano) with d3-force engine
- charge strength = -2, link distance = 30, link strength = 0.8
- Core nodes: larger size + full opacity (0.95)
- Non-core nodes: smaller size + dimmed opacity (0.4)
- On perspective switch: `reLayout()` rebuilds data and re-runs simulation
- Active perspective button highlighted with its type color

**Entity type grouping** (no backend changes): When "人物" perspective is active, all person entities become large hub dots. Other entities (locations, events, etc.) and documents/concepts appear as small dots connected to their related person hubs. This creates a natural "orbiting" visual without needing a Folder node type.

### 5. Search Behavior

On search input:
1. Find matching nodes by label/description
2. If matches found:
   - Matching nodes: core size + full opacity (1)
   - Non-matching nodes: small size + dimmed opacity (0.35)
   - Call `reLayout()` to rebuild and re-flow
3. On clear search: restore previous perspective layout

### 6. Detail Panel

**Trigger**: Click on a node (left-click). Right-click expands neighborhood.

**Content**:
- Node name (title)
- Type label
- Description (if any)
- Source (for Documents)
- **Media thumbnail** (new): If the node is a Document with a media URI (image/video), display a thumbnail:
  - Images: `<img>` tag with the file URI, max 280px wide, rounded corners
  - Videos: `<video>` tag with poster frame, controls, max 280px wide
  - Only show for URIs matching image/video extensions: `.jpg`, `.jpeg`, `.png`, `.gif`, `.webp`, `.mp4`, `.mov`, `.avi`, `.webm`
- Related edges list (as current)

**Style**: Right-side slide-out panel, cream background, clean typography. Remove dashed borders, use solid thin borders.

### 7. Electron Menu Bar

In `main.js`, add `autoHideMenuBar: true` to BrowserWindow options to hide the default File/Edit/View menu.

### 8. Toolbar Layout

```
[🔍 search input]  [人物] [组织] [文档] [概念]
```

Single row. Left: search box. Right: perspective buttons. Buttons are small rounded rectangles with the type's color as background when active, light gray when inactive.

### 9. Status Bar

Keep bottom status bar but simplify: show node count + edge count only. Remove per-type breakdown (types are now visible via perspective).

## Files to Modify

| File | Changes |
|------|---------|
| `ui/graph/styles.css` | Rewrite: remove pattern, dashed borders, handwritten fonts, pencil shadows. Add dot-node styles, tooltip styles, clean toolbar |
| `ui/graph/renderer.js` | Major rewrite: force-graph (vasturiano) replacing G6 v5, perspective mode, dot-only nodes, thin edges, hover tooltips, search re-layout, media thumbnails in detail panel |
| `ui/graph/index.html` | Remove Google Fonts links, update toolbar HTML for perspective buttons, add tooltip container |
| `ui/graph/main.js` | Add `autoHideMenuBar: true` |

## Verification

1. Open graph window — no menu bar visible
2. Clean cream background, no dot pattern
3. All nodes are colored dots, no text labels on nodes
4. Hover a node — tooltip appears with name + type
5. Click a node — detail panel slides in with info + media thumbnail if applicable
6. No perspective active by default — all nodes medium-sized, evenly laid out
7. Click "人物" — person nodes enlarge and spread, others shrink and dim, animated transition
8. Click "文档" — document nodes enlarge and spread, animated transition
9. Search for a term — matching nodes enlarge and full opacity, others dim
10. Clear search — layout returns to current perspective
11. Edges are all thin straight lines with no text, no arrows

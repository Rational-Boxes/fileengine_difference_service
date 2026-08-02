# Difference Service — version-comparison back end

A FileEngine microservice that generates **visual diffs between two versions of a
file**. Given a file identifier and a target version, it produces a color-coded
comparison of that version against a base version (the immediate predecessor by
default). Comparisons are computed by **format-specific plugins** behind a common
interface, precomputed on version-creation events, and stored as **renditions**
(hidden children) of the source file for the front end to fetch.

Color convention across all formats: **red = deleted, green = added,
orange = modified.**

## 1. Scope & goals

- **2D** — produce a per-page, scriptable **SVG** that lets the front end show
  *before*, *after*, and a color-coded *difference* view. Primary server-side
  target is **PDF**. Raster images are a front-end before/after flip (see §5.3);
  the plugin framework leaves extension points for other 2D formats.
- **3D** — produce a single **Xeokit XKT** whose object tree has three top-level
  groups — *old*, *new*, and *difference* — so the viewer can switch between them
  with show/hide/x-ray. Input formats for v1: **IFC, glTF/GLB, STEP**.
- **Generalized interface + per-format plugins.** Adding a format is adding a
  plugin; the service core (auth, event ingest, rendition writing, caching, API)
  is format-agnostic.

## 2. Integration with FileEngine

The service follows the same integration model as `convert_search_ai`:

- **Core access** via the reused `fileengine` Python gRPC client. Content is read
  through identity-bound `ManagedFiles` (end-user identity for on-demand requests;
  a service/agent identity for the background worker).
- **Auth** — LDAP bind → bearer token (`/auth/token`); every request surface is
  **permission-gated**: a caller must hold FileEngine **READ** on the file to
  request or retrieve its diff. Same `ldap_auth` / `core_client` pattern as
  `convert_search_ai`.
- **Renditions** — diff outputs are written back as **hidden-child renditions**
  under the source file's UID (the established rendition pattern), so the existing
  file-serving surface delivers them and ACLs are inherited from the parent file.

### 2.1 Event-driven precompute

The service is an **event consumer** on the FileEngine event stream
(`fileengine:events`, per `convert_search_ai/design_documents/EVENT_CONTRACT.md`).

- On **`file.updated`** (a new content version written), the worker computes the
  diff of the new version against its base and writes the diff rendition(s). This
  is the primary path — requests are normally cache hits.
- **Ignore events where `is_rendition: true`** so the worker never recurses on its
  own output.
- On **`file.deleted`**, cascade-remove the file's diff renditions.
- **Idempotency** — dedupe on `event_id`; collapse repeated work on the logical
  key `(file_uid, target_version)`. At-least-once delivery, so processing must be
  idempotent (re-deriving the same rendition for the same state is a no-op).
- On-demand generation for a not-yet-computed pair (e.g. a request for an older
  version, or an explicit base) is supported via the same code path, gated by the
  caller's READ permission.

## 3. Version selection

- **Default:** target version vs. its **immediate predecessor** in the version
  chain.
- **Optional explicit base:** a request may supply a base version id to compare an
  arbitrary earlier version against the target.
- The **target's newer** version is "new" (added/green); the **base** is "old"
  (deleted/red).

## 4. Plugin interface

A generalized `DiffPlugin`, dispatched by MIME type through a registry
(registration order = priority), mirroring `convert_search_ai`'s
`ConversionPlugin` / `PluginRegistry`.

```python
class DiffPlugin(ABC):
    name: str            # stable identifier, recorded on the rendition
    version: int         # bump to force regeneration of prior outputs (§6)

    def supports(self, mime: str) -> bool: ...

    # Produce the diff renditions for base vs target. Must degrade gracefully:
    # pick the highest-fidelity tier the input supports, per unit (§5.1 / §5.2),
    # and never fail the file — return a lower tier rather than raising.
    def diff(self, base: SourceRef, target: SourceRef) -> DiffResult: ...
```

`DiffResult` carries the rendition bytes plus **mode metadata** (§7): the overall
mode and the per-unit (per-page / per-element) tier map. `SourceRef` gives the
plugin the version's content bytes and MIME.

The plugin selects the best tier each **unit** supports and may mix tiers within
one result (e.g. a PDF with vector and scanned pages).

## 5. Format plugins

### 5.1 PDF (2D) — per-page degradation ladder

Each page independently uses the highest-fidelity tier its content supports:

1. **Vector object-level** *(best)* — match vector drawing/text objects between
   versions and classify each as added / deleted / modified; emit them into the
   SVG layers (§7) tagged by state.
2. **Text + raster hybrid** — semantic diff for text runs; raster overlay for the
   graphics layer where object identity is unrecoverable.
3. **Raster pixel overlay** *(fallback)* — render both pages to bitmaps and
   color-code changed regions; used for scanned / image-only pages.

Page correspondence must tolerate inserted / deleted / reordered pages (whole
inserted pages are all-added, deleted pages all-deleted).

**Full vectorization — no client font dependencies.** The output SVG is a
*complete* vector graphic: all text is converted to **glyph path outlines**, never
`<text>` elements that rely on fonts installed on the client. The diff is still
computed at the object level from the source PDF's text runs and vector objects
(so `data-diff-state` reflects semantic add/delete/modify); the *rendered* result
embeds those glyphs as `<path>` geometry so a page renders identically on any
client regardless of available fonts. (Embedded raster images on a page remain
raster; scanned pages use the raster tier.)

Output: one **per-page SVG** conforming to the §7 contract.

### 5.2 3D — per-element degradation ladder

Inputs: **IFC, glTF/GLB, STEP**. Each element (or element group) uses the best
tier available:

1. **IFC GlobalId matching** *(best)* — match elements by IFC GlobalId across
   versions; added / deleted by presence, modified by geometry hash. Consistent
   with the BCF/xeokit BIM stack.
2. **Hybrid** — GlobalId matching for identity, boolean ops only to visualize
   geometry-level deltas on *modified* elements.
3. **True mesh boolean** *(fallback)* — geometric boolean difference/intersection
   for mesh-only formats (glTF, tessellated STEP) with no stable identity.

Output: a single **Xeokit XKT** whose object tree has three top-level groups —
**old**, **new**, **difference** — selectable via show/hide/x-ray. Elements are
tagged by state so the viewer applies the red/green/orange convention.

**The existing Xeokit viewer is reused as-is** — no viewer changes. The service's
only responsibility is to **organize the model into the old / new / difference
layers**; the current viewer's standard show/hide/x-ray controls drive the three
views.

### 5.3 Raster images (2D) — front-end flip

Raster image versions do **not** get a server-side diff rendition; the front end
performs a before/after flip between the two image versions directly. The plugin
framework reserves extension points for future 2D formats (e.g. 2D CAD) that would
warrant server-side diffing.

## 6. Caching & invalidation

- A diff rendition is keyed by
  `(file_uid, base_version, target_version, plugin_name, plugin_version)`.
- The generating **plugin name + version are recorded on the rendition.** When a
  plugin's `version` is bumped (algorithm upgrade), stale renditions are
  regenerated; a matching key is served from cache.
- `file.deleted` cascades removal of the file's diff renditions (§2.1).

## 7. Front-end contract (defined here)

There is no pre-existing front-end spec; this is the contract.

### 7.1 Result shape

- **One diff rendition per version-pair.** For 2D that rendition addresses the
  set of per-page SVGs; for 3D it is the XKT.
- The rendition carries a **`mode` metadata field**:
  - `mode`: `"vector"` | `"raster"` | `"mixed"` (2D) / `"xkt"` (3D).
  - a **per-unit map** — `pages: [{ index, mode }]` (2D) — so the front end knows,
    per page, whether to drive the scriptable-vector view or the raster-overlay
    view; `elements`/group summary for 3D.
  - `base_version`, `target_version`, `plugin`, `plugin_version`.

### 7.2 2D SVG scriptable hooks

Each page SVG is authored so the front end can toggle views without re-fetching:

- **Three logical layers**, each a group with a stable id: `#diff-old`,
  `#diff-new`, `#diff-changes`.
- Every diffed element carries `data-diff-state` ∈
  `added` | `deleted` | `modified` | `unchanged`.
- **No font dependencies:** text is emitted as `<path>` glyph outlines, not
  `<text>` (§5.1), so the SVG is self-contained and renders identically on any
  client.
- State→color is applied by front-end CSS (red/green/orange); the SVG ships the
  semantic state, not hard-coded colors, so themes can restyle.
- Root `<svg>` carries `data-diff-mode` (`vector` | `raster`). For **raster**
  pages the SVG embeds the old/new/overlay bitmaps as images inside the same
  three-layer structure with the same `data-diff-state` semantics, so **one
  front-end view engine drives both modes**.
- View switching = showing/hiding the layer groups: *before* → `#diff-old`,
  *after* → `#diff-new`, *difference* → `#diff-changes` (over a base).

### 7.3 3D hooks

**Reuse the existing Xeokit viewer unchanged.** The contract is purely the model
layout: the XKT object tree exposes `old` / `new` / `difference` as the three
top-level nodes, with per-element state driving the red/green/orange convention.
The viewer's existing show/hide/x-ray controls switch between the three views — no
new viewer code.

## 8. Service surface (FastAPI)

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/healthz` | liveness |
| GET  | `/readyz` | readiness (gRPC core + LDAP) |
| POST | `/auth/token` | LDAP bind → bearer token |
| GET  | `/whoami` | resolved identity |
| GET  | `/files/{uid}/diff` | READ-gated diff for target vs base (query: `version`, optional `base`); returns/points at the rendition + `mode` metadata |
| POST | `/diff/reconcile` | trigger a reconcile sweep (recompute missing / stale-plugin renditions) |

Unauthenticated `/healthz` `/readyz` (and any `/poolz`) bind **loopback-only**,
per the monitoring convention.

## 9. Configuration

Environment-driven `Config`, following the `convert_search_ai` split:

- **`FILEENGINE_*`** — shared: gRPC core endpoint, LDAP, event stream
  (`FILEENGINE_EVENTS_STREAM`, consumer group), tenant.
- **`DIFF_*`** — service-local: worker concurrency, plugin toggles, cache/rendition
  settings, tool paths (poppler, IfcOpenShell, geometry/boolean backends,
  convert2xkt).

## 10. Deployment & testing

- Packaged as a container in the unified stack (`docker_unified`), same shape as
  the other FileEngine microservices.
- **`@live`-gated** pytest harness plus a full-stack end-to-end test: version
  written → event consumed → diff rendition produced → READ-gated retrieval →
  correct `mode` metadata, across a vector PDF, a scanned PDF (raster fallback),
  and an IFC pair (GlobalId) with a mesh fallback.

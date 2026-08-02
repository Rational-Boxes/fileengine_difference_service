# Difference Service — Development Plan

Status: **Design** (implementation not yet started). Companion:
[`SPECIFICATION.md`](./SPECIFICATION.md).

## 1. Purpose & scope

`difference_service` is a Python/FastAPI microservice in the FileEngine ecosystem
(sibling to `convert_search_ai`, `mcp`, the bridges). For two versions of a file
held in FileEngine it produces a **color-coded visual diff** — a scriptable
per-page **SVG** for 2D (PDF) and a Xeokit **XKT** with old/new/difference layers
for 3D — precomputed on version events and stored as **hidden-child renditions**,
every request gated by the caller's FileEngine **READ** permission.

**Out of scope:** the front-end viewers themselves. The existing Xeokit viewer is
reused unchanged; the 2D SVG contract (§7 of the spec) is consumed by a front-end
that is built separately.

## 2. Position in the ecosystem

| Dependency | Role | Dev endpoint |
|------------|------|--------------|
| FileEngine core (gRPC) | Version content, rendition writes, **permission checks** | `:50051` |
| LDAP / OpenLDAP | Authentication + role authority | `:1389` |
| Redis | Event consumption (dev transport) | `:6379` |
| PostgreSQL | Worker/job + cache-index state (if needed beyond rendition metadata) | `:5434` (dev) |

Conventions inherited from `convert_search_ai` (mirror exactly): environment +
working-dir `.env` `Config`; `FILEENGINE_*` shared names, `DIFF_*` local; reused
`fileengine` gRPC client with identity-bound `ManagedFiles`; LDAP→bearer auth;
`pyproject.toml` src-layout; pytest with `@live` gating; a `Containerfile`.

## 3. Milestones

Each milestone is independently shippable and ends with `@live`-gated tests.

### M0 — Scaffolding
- Package + `pyproject.toml` (src layout), `Config` (`FILEENGINE_*` / `DIFF_*`).
- Reused `fileengine` gRPC client + `core_client` (end-user vs. worker identity);
  LDAP bind → bearer (`/auth/token`, `/whoami`).
- `/healthz` / `/readyz` (loopback-bound), readiness probes core + LDAP.
- **Plugin framework**: `DiffPlugin` interface + `PluginRegistry` (dispatch by
  MIME, `name`/`version` fields), `DiffResult` / mode-metadata types — no format
  plugins yet.
- `@live` harness against a dev core + LDAP.

### M1 — Event ingest, versioning & rendition writer
- Event consumer on `fileengine:events`: act on `file.updated`, ignore
  `is_rendition`, cascade-delete diff renditions on `file.deleted`.
- Idempotency (dedupe `event_id`; collapse `(file_uid, target_version)`).
- **Version-pair resolution**: default target vs. immediate predecessor; optional
  explicit base.
- **Hidden-child rendition writer** (gRPC) with `mode` metadata + cache key
  `(file_uid, base, target, plugin_name, plugin_version)`; regenerate on plugin
  `version` bump.
- **Reconcile sweep** — backfill missing / stale-plugin renditions.
- Validated with a trivial pass-through plugin (no real diff yet).

### M2 — PDF (2D) plugin
- Per-page degradation ladder: **vector object-level → text+raster hybrid →
  raster pixel overlay**, tier chosen per page.
- Object-level matching of text runs / vector objects → `data-diff-state`.
- **Full vectorization**: glyphs emitted as `<path>` outlines (no client fonts).
- SVG three-layer contract (`#diff-old` / `#diff-new` / `#diff-changes`) +
  `data-diff-mode`; page correspondence tolerant of insert/delete/reorder.
- Raster fallback embeds bitmaps in the same layer structure.
- Finalizes the front-end SVG contract (spec §7).

### M3 — 3D plugin
- Per-element ladder: **IFC GlobalId matching → hybrid (GlobalId + boolean on
  modified) → true mesh boolean**.
- Inputs **IFC, glTF/GLB, STEP**; XKT output via the convert2xkt pipeline.
- Object tree organized into **old / new / difference** top-level groups;
  per-element state drives red/green/orange. **Existing Xeokit viewer reused
  unchanged.**
- (STEP/glTF land on the mesh-boolean tier — heaviest path; see spec §10 note. May
  be scoped as M3a IFC-first, M3b glTF/STEP.)

### M4 — Request surface & permission gating
- `GET /files/{uid}/diff` (query `version`, optional `base`) — READ-gated;
  serves the cached rendition + `mode` metadata, or triggers on-demand generation
  via the M1 path for an uncomputed pair.
- `POST /diff/reconcile`.
- Permission cache (TTL-bounded, invalidated by `acl.changed` / `role.*`).

### Hardening
- Request guards (size caps, structured error mapping), content-/secret-free
  audit log.
- Full-stack `@live` e2e: version written → event consumed → rendition produced →
  READ-gated retrieval → correct `mode`, across a **vector PDF**, a **scanned PDF**
  (raster fallback), and an **IFC pair** (GlobalId) with a **mesh fallback**.
- Container wired into the unified stack (`docker_unified`).

## 4. Dependencies & risks

- **Geometry backends** (IfcOpenShell, a boolean/mesh kernel, convert2xkt) are the
  heaviest external deps — isolate behind the plugin so failures degrade a tier
  rather than failing the file.
- **PDF vector-object identity** is the hardest 2D problem; the hybrid/raster tiers
  are the safety net when object matching is unreliable.
- **STEP/glTF have no stable IDs** → always mesh-boolean; expensive and fragile.
  Defer past v1 if IFC dominates real inputs.

# Difference Service — Development Plan

Status: **M0 + M1 complete** (2026-08-17) — scaffolding, auth, plugin framework,
manifest types, event ingest, version-pair resolution, the rendition writer and the
reconcile sweep, all validated end-to-end against a live core. A **fixture corpus**
with documented ground truth is in place for M2/M3. Next: **M2** (the PDF plugin,
starting with the object-matching spike). Companion:
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

### M0 — Scaffolding ✅ *(done 2026-08-17)*
- Package + `pyproject.toml` (src layout), `Config` (`FILEENGINE_*` / `DIFF_*`).
- Reused `fileengine` gRPC client + `core_client` (end-user vs. worker identity);
  LDAP bind → bearer (`/auth/token`, `/whoami`).
- `/healthz` / `/readyz` (loopback-bound), readiness probes core + LDAP.
- **Plugin framework**: `DiffPlugin` interface + `PluginRegistry` (dispatch by
  MIME, `name`/`version` fields), `DiffResult` / mode-metadata types — no format
  plugins yet.
- `@live` harness against a dev core + LDAP.

Notes from the build:
- **No Postgres.** §2 listed it as "if needed beyond rendition metadata" — it is
  not. The cache key and its invalidation live on the renditions (§6) and the
  manifest is the commit marker (§7.1.1); a database would be a second source of
  truth that could disagree with what is actually stored.
- The manifest types landed in M0 rather than M1, since `DiffResult` is only
  meaningful alongside the manifest it commits to.
- The core reports the **root sentinel with an empty `uid`** — assert on
  `name`/`is_dir`, not on the uid round-tripping.
- The dev admin user resolves to **`system_admin`**, a full ACL bypass in the core,
  so a permission result for an admin proves nothing about the ACL. Fail-closed
  behaviour must be tested against an unreachable core, not a bogus UID.

### M1 — Event ingest, versioning & rendition writer ✅ *(done 2026-08-17)*
- Event consumer on `fileengine:events`: act on `file.updated`, ignore
  `is_rendition`, cascade-delete diff renditions on `file.deleted`.
- Idempotency (dedupe `event_id`; collapse `(file_uid, target_version)`).
- **Version-pair resolution**: default target vs. immediate predecessor; optional
  explicit base.
- **Hidden-child rendition writer** (gRPC): content children first, **`manifest`
  written last as the atomic commit marker** (spec §7.1.1); cache key
  `(file_uid, base, target, plugin_name, plugin_version)`; regenerate on plugin
  `version` bump; a fully-failed run still writes a `status: "failed"` manifest.
- **Reconcile sweep** — backfill missing / stale-plugin renditions.
- Validated with a trivial pass-through plugin (no real diff yet).

Notes from the build:
- **The manifest is the idempotency record**, so there is still no database. A
  complete, non-stale manifest for the pair *is* the cache hit; event-id dedupe in
  the consumer is only an optimisation in front of it.
- **MIME needed its own dispatch policy.** The `MimeResolver` copied from
  `folder_actions` sniffs content only and ignores the file name — correct there,
  where MIME is a security whitelist that must fail closed against a renamed file.
  Here MIME picks a *plugin*, so `DispatchMimeResolver` adds the name fallback;
  without it every `text/plain` file resolved to `None` and reported "unsupported".
- **Version ordering was verified against a live core, not the docstring:**
  `revisions()` is newest-first and `get(back=N)` reads `revisions[N]`, so a larger
  index is an OLDER version. Getting this backwards silently reverses a diff.
- One current diff is kept per file; a new version or a plugin-version bump changes
  the key and the superseded children are pruned, rather than accumulating an
  unbounded history of pairs that nothing reads.

### Fixture corpus *(added 2026-08-17)*

`src/tests/fixtures/` generates version pairs with **documented ground truth** for
M2/M3 — programmatically, never as committed binaries, so each difference is one
readable edit and the corpus needs no toolchain to produce. 30 pairs: 14 PDF, 9
IFC, 7 glTF. Several exist specifically to *fail* a naive implementation:
`pdf.shifted_page` (whole-page translation must not read as 100% modified),
`pdf.inserted_object` (a mid-stream insert must not re-flow the trailing objects),
`ifc.property_only` (a property change has NO visual delta), `gltf.renamed_node`
(glTF names are not identity). `test_fixtures.py` asserts each pair encodes the
change it claims — a broken ruler is worse than no ruler.

### M2 — PDF (2D) plugin
- **Core research spike (do first, highest risk): PDF object-matching key** — no
  stable identity exists, so derive one: global page-alignment pass → position-
  independent per-object signature → LCS over draw-op order (spec §5.1). Low
  matcher confidence on a page must degrade it to the hybrid/raster tier rather
  than emit a misleading diff. De-risks the rest of M2 before the SVG work.
- Per-page degradation ladder: **vector object-level → text+raster hybrid →
  raster pixel overlay**, tier chosen per page.
- Object-level matching of text runs / vector objects → `data-diff-state`, with the
  per-tier **`modified` predicate** of spec §5.1.
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

# Difference Service — Development Plan

Status: **M0 – M3 complete** (2026-08-17) — scaffolding, auth, plugin framework,
manifest types, event ingest, version-pair resolution, the rendition writer and the
reconcile sweep, the **PDF (2D) plugin**, and the **3D plugin** covering IFC,
glTF/GLB and the OpenCASCADE CAD formats — all validated end-to-end against a live
core. A **fixture corpus** with documented ground truth backs both matchers. Next:
**M4** (the request surface). Companion: [`SPECIFICATION.md`](./SPECIFICATION.md).

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

### M2 — PDF (2D) plugin ✅ *(done 2026-08-17)*
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

Notes from the build:
- **The spike worked as specified.** All three stages earn their place, and the
  fixture corpus proves each: without global alignment `shifted_page` reports 100%
  modified; without the LCS `inserted_object` cascades into the trailing objects.
  Both now report exactly the documented ground truth.
- **`difflib` is not usable for the LCS.** Its autojunk heuristic discards elements
  appearing in >1% of a large sequence — on a dense page that is precisely the
  repeated rules and glyph runs holding an alignment together. A plain O(n·m) LCS
  with an explicit size cap replaced it; exceeding the cap lowers confidence, which
  degrades the page rather than hanging the worker.
- **Confidence must measure comprehension, not change.** Coverage was first divided
  by the LARGER side, so a two-object page with one honest addition scored 0.67 and
  a one-object page 0.5 — degrading good diffs to raster for the crime of having
  something added. It is now divided by the smaller side, which separates "not
  understood" from "legitimately gained content".
- **A page is never dropped.** When no tier can render one, an explicit
  `unavailable` placeholder holds its slot; omitting it yields a result that looks
  complete while missing a page, which a reviewer reads as "nothing changed here".
  When *no* page renders, the result is simply `failed` — placeholders for every
  page would add nothing over the failure detail.
- **Glyph outlines come from a metric-compatible substitute** (Liberation Sans/
  Serif/Mono via fontTools) for the non-embedded base-14 fonts. This satisfies the
  *contract* — the SVG is self-contained paths, no client font is consulted — but
  differs in typeface fidelity when a document embeds an unusual face. Extracting
  the embedded font program (FontFile/FontFile2/FontFile3) is the follow-up; until
  then a page whose text cannot be outlined degrades rather than emitting `<text>`.
- **Rasterization shells out to poppler's `pdftoppm`** rather than taking a Python
  binding: pdf2image only wraps the same binary, and PyMuPDF is a heavyweight
  extra. A missing binary degrades the tier; it never fails the service.
- The **hybrid tier (2) is not separately implemented** — it currently routes to
  raster. A half-measure with no independent backend would be a tier in name only.

### M3 — 3D plugin ✅ *(done 2026-08-17)*
- Per-element ladder: **IFC GlobalId matching → hybrid (GlobalId + boolean on
  modified) → true mesh boolean**.
- Inputs **IFC, glTF/GLB, STEP**; XKT output via the convert2xkt pipeline.
- Object tree organized into **old / new / difference** top-level groups;
  per-element state drives red/green/orange. **Existing Xeokit viewer reused
  unchanged.**
- (STEP/glTF land on the mesh-boolean tier — heaviest path; see spec §10 note. May
  be scoped as M3a IFC-first, M3b glTF/STEP.)

Notes from the build — **NOT scoped IFC-first**; every supported format landed
together, because the design that makes that cheap is the right one anyway:
- **The tier is chosen by the identity the data carries, not by file extension.**
  Every format normalizes into one `Model3D` and a single matcher compares them:
  durable ids (IFC GlobalId) take tier 1, everything else takes geometry matching.
  Adding a format is writing a *loader*; the diff, the states, the XKT and the
  manifest are unchanged by construction. An IFC that fails to tessellate falls to
  geometry matching by the same rule, with no special case.
- **Coverage**: IFC (ifcopenshell) → tier 1; glTF/GLB (native parser) → tier 3;
  STEP, IGES, BREP, OBJ, STL (OpenCASCADE `DRAWEXE` → glTF) → tier 3. One
  conversion hop bought all five CAD formats, and their behaviour is identical to
  glTF's rather than a parallel implementation that could drift.
- **The geometry hash is tessellated world-space vertices**, never the entity
  graph. A structural hash is far cheaper and far too easy to contaminate with a
  non-geometric attribute — and that failure mode (a model that looks identical
  lighting up orange) is the one that teaches reviewers to ignore the colour.
  World coordinates are non-negotiable: in local coordinates a wall moved 10m
  hashes exactly as before.
- **Mesh deflection is fixed, not adaptive.** Two versions must be tessellated
  identically or every element compares modified purely because the mesher chose
  different triangles. The `cad.unchanged` fixture exists to prove this holds.
- **Geometry pairing is globally nearest-first, not in file order.** Iterating the
  old list and letting each element claim its closest partner looks equivalent but
  isn't: with several copies of one component, whichever came first in the file
  claimed a distant partner and the copy that genuinely stayed put was reported
  deleted+added.
- Without `convert2xkt` the merged glTF is delivered instead — a complete
  three-group model beats no diff, and the manifest says which the viewer is
  getting.

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

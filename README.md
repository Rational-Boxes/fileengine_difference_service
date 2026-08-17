# difference_service

FastAPI microservice that generates **visual diffs between two versions of a file**
in the FileEngine virtual filesystem. Given a file and a target version it produces
a colour-coded comparison against a base version (the immediate predecessor by
default) — a scriptable per-page **SVG** for 2D, a Xeokit **XKT** with
old/new/difference layers for 3D. Comparisons come from format-specific plugins,
are precomputed on version events, and are stored as **hidden-child renditions** of
the source file.

Colour convention across every format: **red = deleted, green = added,
orange = modified.**

See [`SPECIFICATION.md`](SPECIFICATION.md) for the full contract and
[`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md) for the milestones.

Structurally a sibling of `convert_search_ai` / `folder_actions`: reused
`fileengine` gRPC client, LDAP→bearer auth, `FILEENGINE_*` shared config with
`DIFF_*` private knobs.

## Status

**M0 – M4 complete.** Config, auth, dual-identity core access, the plugin
framework and manifest types (M0); the event worker, version-pair resolution, the
rendition writer and the reconcile sweep (M1); the **PDF plugin** with per-page
tier degradation (M2); the **3D plugin** across IFC, glTF/GLB and CAD (M3); the
**request surface** with its READ gate and permission cache (M4) — all validated
end-to-end against a live core. Remaining: the hardening pass.

### PDF diffing (M2)

Each page independently takes the best tier its content supports:

| Tier | When | Output |
|---|---|---|
| **vector** | page parsed, matcher confident, glyph outlines available | objects tagged `added`/`deleted`/`modified`/`unchanged` |
| **raster** | scanned/image-only pages, or any page tier 1 disclaims | page bitmaps + a difference mask, same layer structure |
| **unavailable** | no tier could render the page | placeholder holding the page's slot |

A document with both kinds reports mode `mixed`, which is why the manifest carries
a per-page map rather than one document-wide mode.

Object identity is *derived*, in the three stages §5.1 prescribes: cancel the
dominant page translation, sign each object position-independently, then align by
LCS over draw order. Each defeats a specific failure: without the first, a page
that shifted reports 100% modified; without the third, one inserted object makes
every following object read as modified. Where the matcher cannot explain a page,
it degrades rather than emitting a confidently wrong diff.

Text is emitted as `<path>` glyph outlines, never `<text>`, so a diff renders
identically on any client. Outlines come from a metric-compatible substitute
(Liberation via fontTools) for non-embedded base-14 fonts; embedded-font
extraction is the pending fidelity improvement. Rasterization shells out to
poppler's `pdftoppm` — absent, the raster tier degrades rather than failing.

Processes — one package, three entry points:

| Script | Role |
|---|---|
| `difference-service` | HTTP API (**:8100**) |
| `difference-consumer` | event worker — precomputes diffs on `file.updated` |
| `difference-reconcile` | backfill sweep — missing and stale-plugin results |

## Permission model

Two identities, and the distinction is the whole security story (§2):

- **On-demand requests act as the calling user.** Every diff surface is gated on
  the caller's FileEngine **READ** for the file, checked by the core as that user.
  The gate fails **closed** — an unreachable core denies.
- **The background worker acts as a service principal** to precompute diffs. It is
  not `system_admin`; its rights come from ACL grants. Because renditions are
  hidden children of the source file they inherit its ACLs, so a precomputed diff
  is readable by exactly those who can already read the file it describes.

> Note: `ldap_auth` maps a tenant's `administrators` group to the core's
> `system_admin` role, matching the sibling services. `system_admin` is a full ACL
> bypass in the core, so a permission result for an administrator is not evidence
> that an ACL allowed it.

## Plugin framework

A `DiffPlugin` is dispatched by MIME through a registry (registration order =
priority), mirroring `convert_search_ai`'s `ConversionPlugin` / `PluginRegistry`:

```python
class DiffPlugin(ABC):
    name: str              # recorded on the rendition
    version: int           # part of the cache key — bump to force regeneration
    def supports(self, mime: str) -> bool: ...
    def diff(self, base: SourceRef, target: SourceRef) -> DiffResult: ...
```

The governing rule is **degrade, never fail**: a plugin picks the best tier each
unit (page / element) supports, may mix tiers in one result, and returns a lower
tier rather than raising. The registry enforces this — a plugin that raises anyway
becomes a `failed` result, so the worker always has something to commit.

An **unsupported MIME type is not a failure**: raster images are meant to be a
front-end before/after flip (§5.3), so "nothing to do" must never look like "tried
and failed".

## The manifest

The manifest is the **atomic commit marker** (§7.1.1) and the reason an
at-least-once, crash-prone pipeline is safe to read from:

- The worker writes all content children first, then the manifest **last**. Its
  presence *is* the "diff complete" signal.
- The read path keys off it: **no manifest ⇒ pending**, never a partial diff. The
  `expected` child list lets a reader verify the set before serving.
- A run that failed even at its last-resort tier still writes a `failed` manifest,
  so "attempted and failed" (fall back to side-by-side) stays distinguishable from
  "never attempted" (compute it).

Cache key: `(file_uid, base_version, target_version, plugin_name, plugin_version)`.
Bumping a plugin's `version` misses the old key and regenerates.

## Service surface

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/healthz` | liveness |
| GET | `/readyz` | readiness (core gRPC + LDAP) |
| GET | `/poolz` | active plugin count |
| POST | `/auth/token` | LDAP bind → bearer token |
| GET | `/whoami` | resolved identity |
| GET | `/plugins` | active plugins + versions |
| GET | `/files/{uid}/diff` | READ-gated diff for a version pair (`version`, `base`) |
| POST | `/diff/reconcile` | trigger a backfill sweep (tenant admin) |

`GET /files/{uid}/diff` answers off the manifest status, so the front end has a
branch for every outcome rather than inferring state from a missing body:

| Status | Code | What the FE does |
|---|---|---|
| `ready` | 200 | render — the response carries the manifest + fetchable child references |
| `pending` | 202 | poll; generation was queued |
| `failed` | 422 | fall back to a plain side-by-side, with the failure detail |
| `unsupported` | 200 | flip between the two versions itself (§5.3) — **not** an error |
| `none` | 200 | a first version has no predecessor; stop polling |

It does **not** block while computing: a BIM or PDF diff takes tens of seconds, so
holding the request open would tie up a worker and time out at the proxy. The child
bytes are served by the ordinary file surface — they are hidden children of the
source file and inherit its ACLs.

The unauthenticated monitoring endpoints bind **loopback-only** and honour
`FILEENGINE_MONITORING_ALLOW_IPS`.

### 3D diffing (M3)

Every supported 3D format normalizes into one internal model, and the tier is
chosen by **what identity the data carries** — never by file extension:

| Formats | Identity | Tier |
|---|---|---|
| IFC | GlobalId | **stable-id** — `modified` splits into geometry (orange) vs property-only (**no visual delta**) |
| glTF / GLB | none | **geometry** — matched by shape; a move is honestly removed + added volume |
| STEP, IGES, BREP, OBJ, STL | none | **geometry**, via OpenCASCADE tessellation to glTF |

Adding a format is writing a *loader*: the diff, the states, the output and the
manifest are unchanged by construction. An IFC that fails to tessellate falls to
geometry matching by the same rule.

Output is one XKT whose object tree has `old` / `new` / `difference` at the top,
plus a MetaModel sidecar carrying each element's state and change kind. **The
existing Xeokit viewer is reused unchanged** — its stock show/hide/x-ray drives the
three views. A property-only change appears in the MetaModel (visible on selection)
but never in the `difference` group, because painting geometry that looks identical
teaches reviewers to distrust the colour.

## Fixtures

`src/tests/fixtures/` generates version pairs with **documented ground truth** —
14 PDF, 9 IFC, 7 glTF, plus 3 toolchain-generated STEP pairs. They are generated, never committed binaries, so each
difference is one readable edit and the corpus needs no toolchain to produce.

Several exist specifically to *fail* a naive implementation, and those are the
interesting ones:

| Fixture | Why it is hard |
|---|---|
| `pdf.shifted_page` | every object translated equally — must cancel the global shift, not report 100% modified |
| `pdf.inserted_object` | a mid-stream insert must not re-flow the trailing objects into "modified" |
| `pdf.moved_object` / `relocated_object` | bracket the displacement threshold: modified vs deleted+added |
| `pdf.mixed_tier` | vector page + scanned page in one document → mode `mixed` |
| `ifc.property_only` | a property change has **no visual delta** — must not paint orange |
| `ifc.reordered_entities` | STEP entity numbers are not identity; only GlobalId is |
| `gltf.renamed_node` | glTF names are not identity — renaming must not read as delete+add |
| `cad.unchanged` | proves OCCT tessellation is deterministic; if it weren't, every CAD element would compare modified |

```python
from tests.fixtures import pdf, ifc, gltf
before, after = pdf.inserted_object_pair()
```

`test_fixtures.py` asserts every pair actually encodes the change it claims — a
broken ruler is worse than no ruler.

## Run (dev)

```bash
pip install -e ../python_interface        # the fileengine gRPC client
pip install -e '.[dev]'
cp .env.example .env                       # fill in credentials
difference-service                         # :8100
```

## Test

```bash
pytest                    # hermetic units — no core, LDAP, or redis needed
DIFF_LIVE_PASSWORD='…' pytest -m live      # against a live core + LDAP
```

The `@live` harness skips per-dependency, so a partial stack still gives a useful
signal (LDAP tests run when the core is down). Start the stack with
`scripts/start_backend_services.sh`.

---

AGPL-3.0-or-later.

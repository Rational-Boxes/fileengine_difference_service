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

**M0 (scaffolding) complete** — config, auth, dual-identity core access, the
plugin framework, the manifest/cache-key types, and the health/auth HTTP surface,
with a `@live` harness that passes against a real core + LDAP. **No format plugins
yet**: every pair currently reports "unsupported", which is the intended M0
contract. PDF is M2, 3D is M3, and the event worker + rendition writer are M1.

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

`GET /files/{uid}/diff` and `POST /diff/reconcile` arrive with the rendition writer
(M1/M4) — serving them needs stored manifests to serve from.

The unauthenticated monitoring endpoints bind **loopback-only** and honour
`FILEENGINE_MONITORING_ALLOW_IPS`.

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

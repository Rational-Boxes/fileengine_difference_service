# Copyright (C) 2026 James Hickman
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""HTTP surface for difference_service (SPECIFICATION.md §8).

  GET  /healthz          liveness
  GET  /readyz           readiness (core gRPC + LDAP)
  GET  /poolz            simple pool probe
  POST /auth/token       LDAP bind -> bearer token
  GET  /whoami           resolved identity
  GET  /plugins          active diff plugins and the MIME types they claim
  GET  /files/{uid}/diff READ-gated diff for a version pair (query: version, base)
  POST /diff/reconcile   trigger a backfill sweep (tenant admin)

The monitoring endpoints are unauthenticated and bind loopback-only (see app.py);
everything else is authenticated, and every *diff* surface is additionally gated on
the caller's FileEngine READ for the file (deps.require_read).

``/files/{uid}/diff`` answers off the manifest status (§7.1.1), giving the front
end a defined branch for every outcome — ready, computing, failed, unsupported —
rather than leaving it to infer state from a missing body.
"""
from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import __version__
from .config import Config
from .core_client import CoreClient
from .deps import identity
from .ldap_auth import Identity, authenticate
from .manifest import Manifest
from .plugins.base import DiffStatus
from .renditions import DiffRenditionStore
from .versions import NoPredecessor, VersionError, resolve_pair

log = logging.getLogger("difference_service.api")

router = APIRouter()


# --------------------------- readiness probes ------------------------------
def _check_core(config: Config) -> bool:
    """gRPC core reachable. A channel-ready probe rather than an RPC, so readiness
    does not depend on the worker principal's ACLs."""
    try:
        import grpc
        channel = grpc.insecure_channel(config.grpc_address)
        try:
            grpc.channel_ready_future(channel).result(timeout=2)
            return True
        finally:
            channel.close()
    except Exception:
        return False


def _check_ldap(config: Config) -> bool:
    """LDAP reachable AND the worker principal's credentials are valid — the
    worker cannot precompute anything without them, so a service that cannot bind
    is genuinely not ready, not merely degraded."""
    try:
        if not config.agent_user or not config.agent_password:
            return False
        return authenticate(config, config.agent_user, config.agent_password).authenticated
    except Exception:
        return False


# ------------------------------- health ------------------------------------
@router.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "service": "difference_service", "version": __version__}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    config: Config = request.app.state.config
    # The probes block (gRPC / LDAP) — run them off the event loop.
    checks = {
        "core": await run_in_threadpool(_check_core, config),
        "ldap": await run_in_threadpool(_check_ldap, config),
    }
    ok = all(checks.values())
    return JSONResponse({"status": "ok" if ok else "degraded", "checks": checks},
                        status_code=200 if ok else 503)


@router.get("/poolz")
def poolz(request: Request) -> dict:
    """Cheap liveness detail for the worker/plugin surface — no external calls."""
    registry = request.app.state.registry
    return {"status": "ok", "plugins": len(registry.plugins)}


# -------------------------------- auth -------------------------------------
@router.post("/auth/token")
def auth_token(request: Request, body: dict = Body(...)) -> JSONResponse:
    config: Config = request.app.state.config
    ident = authenticate(config, body.get("username", ""), body.get("password", ""))
    if not ident.authenticated:
        return JSONResponse(status_code=401, content={"error": "invalid credentials"})
    token = request.app.state.token_store.issue(ident)
    return JSONResponse(status_code=200,
                        content={"access_token": token, "token_type": "bearer"})


@router.get("/whoami")
def whoami(ident: Identity = Depends(identity)) -> dict:
    return {"user": ident.user, "roles": ident.roles, "tenant": ident.tenant}


# ------------------------------ capability ---------------------------------
@router.get("/plugins")
def plugins(request: Request, ident: Identity = Depends(identity)) -> dict:
    """Active diff plugins, in dispatch (priority) order.

    ``version`` is part of the cache key (§6), so exposing it lets an operator see
    at a glance which algorithm generation a deployment will produce and whether a
    rollout has actually taken effect."""
    registry = request.app.state.registry
    return {
        "plugins": [{"name": p.name, "version": p.version} for p in registry.plugins],
    }


# ------------------------------------------------------------------ the diff
def _user_core(request: Request, ident: Identity) -> CoreClient:
    """A core client bound to the CALLING USER — never the worker principal."""
    return CoreClient(request.app.state.config, ident.tenant, identity=ident)


def _require_read(request: Request, core: CoreClient, file_uid: str) -> None:
    """Enforce the caller's FileEngine READ, or 403 (§2).

    Deliberately not 404: the caller has already proven identity, and hiding the
    difference between "no such file" and "not yours" tells them nothing they
    could not learn by listing. A clear 403 also lets the front end distinguish a
    permission problem from a missing diff — very different fixes."""
    if not request.app.state.permissions.can_read(core, file_uid):
        raise HTTPException(status_code=403, detail="READ permission required")


def _child_references(store: DiffRenditionStore, file_uid: str,
                      manifest: Manifest) -> List[dict]:
    """Addressable references to the content children, in unit order.

    The bytes themselves are served by the ordinary file surface — they are hidden
    children of the source file, so they inherit its ACLs — which is why this
    returns uids to fetch rather than inlining megabytes of SVG. The per-unit mode
    travels with each reference so the front end knows which view engine to use
    for that page without consulting the manifest separately."""
    present = {e.name: e.uid for e in store.children(file_uid)}
    units = {u["index"]: u for u in manifest.units}
    out: List[dict] = []
    for index, name in enumerate(manifest.expected):
        unit = units.get(index, {})
        out.append({
            "index": index,
            "name": name,
            "uid": present.get(name, ""),
            "mode": unit.get("mode", manifest.mode),
            "kind": unit.get("kind", "page"),
        })
    return out


@router.get("/files/{file_uid}/diff")
def file_diff(file_uid: str, request: Request,
              version: str = Query("", description="target version; default newest"),
              base: str = Query("", description="explicit base version (optional)"),
              ident: Identity = Depends(identity)) -> JSONResponse:
    """The diff for a version pair, or a defined answer for why there isn't one.

    Per §8 the response is driven by the manifest's status, so the front end has a
    branch for every outcome instead of guessing:

      * **ready**       -> 200, manifest + child references.
      * **pending**     -> 202, generation queued; the client polls.
      * **failed**      -> 422 with the failure detail, so the FE falls back to a
        plain side-by-side rather than rendering a broken diff.
      * **unsupported** -> 200; §5.3 says raster images are a front-end flip, so
        "nothing to compute" must not look like an error.
      * **none**        -> 200; a first version has no predecessor and never will,
        so saying so stops the FE polling forever for something that cannot come.
    """
    core = _user_core(request, ident)
    _require_read(request, core, file_uid)

    # Resolve the pair AS THE CALLER, so a version they cannot see cannot be
    # probed through this endpoint.
    try:
        pair = resolve_pair(core, file_uid, version, base or None)
    except NoPredecessor as e:
        return JSONResponse(status_code=200, content={
            "status": "none", "reason": "no_predecessor", "detail": str(e),
            "file_uid": file_uid})
    except VersionError as e:
        raise HTTPException(status_code=404, detail=str(e))

    registry = request.app.state.registry
    store = DiffRenditionStore(core)

    mime = request.app.state.mime_resolver(core).resolve(file_uid) or ""
    plugin = registry.for_mime(mime)
    if plugin is None:
        return JSONResponse(status_code=200, content={
            "status": "unsupported", "mime": mime, "file_uid": file_uid,
            "base_version": pair.base, "target_version": pair.target})

    manifest = store.find_manifest(file_uid, pair.base, pair.target,
                                   plugin.name, plugin.version)

    if manifest is not None and manifest.status == DiffStatus.FAILED:
        return JSONResponse(status_code=422, content={
            "status": "failed", "file_uid": file_uid,
            "base_version": pair.base, "target_version": pair.target,
            "failure": manifest.failure or {},
            "plugin": manifest.plugin, "plugin_version": manifest.plugin_version})

    if manifest is not None and store.is_complete(file_uid, manifest):
        return JSONResponse(status_code=200, content={
            "status": "ready",
            "manifest": manifest.as_dict(),
            "children": _child_references(store, file_uid, manifest)})

    # No manifest, or one whose children are not all present: treated as
    # not-yet-computed (§7.1.1), NEVER as a partial diff. Queue and let the caller
    # poll.
    queued = request.app.state.jobs.submit(ident.tenant, file_uid,
                                           pair.target, pair.base)
    return JSONResponse(status_code=202, content={
        "status": "pending", "queued": queued, "file_uid": file_uid,
        "base_version": pair.base, "target_version": pair.target,
        "plugin": plugin.name, "plugin_version": plugin.version})


@router.post("/diff/reconcile")
def diff_reconcile(request: Request, body: dict = Body(default={}),
                   ident: Identity = Depends(identity)) -> JSONResponse:
    """Trigger a backfill sweep for the caller's tenant (§8).

    Tenant-admin only: a sweep walks the whole tree and can queue a great deal of
    work, so it is not something an ordinary reader should be able to set off. The
    sweep runs as the worker principal and grants nothing — it only computes diffs
    for files the worker can already read.

    Answers 202 rather than holding the connection: a sweep is minutes of work and
    the caller wants an acknowledgement, not a timeout."""
    if not ident.is_admin:
        raise HTTPException(status_code=403, detail="tenant administrator required")

    config: Config = request.app.state.config
    max_files = body.get("max_files") or getattr(config, "reconcile_max_files", 0) or None
    started = request.app.state.jobs.submit_sweep(ident.tenant, max_files=max_files,
                                                  registry=request.app.state.registry)
    return JSONResponse(status_code=202, content={
        "status": "started" if started else "busy",
        "tenant": ident.tenant, "max_files": max_files})

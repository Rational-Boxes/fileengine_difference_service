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

The monitoring endpoints are unauthenticated and bind loopback-only (see app.py);
everything else is authenticated, and every *diff* surface is additionally gated on
the caller's FileEngine READ for the file (deps.require_read).

M0 ships the framework: the diff read path (``GET /files/{uid}/diff``) and
``POST /diff/reconcile`` arrive with the rendition writer in M1/M4, since serving
them requires stored manifests to serve *from*. ``/plugins`` is included now
because it is the honest answer to "what can this service actually diff?" and it
is what the M0 @live harness asserts against.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import __version__
from .config import Config
from .deps import identity
from .ldap_auth import Identity, authenticate

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

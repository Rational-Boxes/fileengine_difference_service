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

"""Request-scoped identity + authorization dependencies (SPECIFICATION.md §2, §8).

``identity`` resolves the caller (Basic/Bearer + tenant) or 401s. ``require_read``
is the gate the spec puts on *every* diff surface: a caller must hold FileEngine
READ on the file to request or retrieve its diff.

The READ check is delegated to the core as the calling user — the service never
decides permissions itself, and never uses the worker principal to answer a user's
request. It fails closed (core_client.check_permission), so an unreachable core
denies rather than leaks.
"""
from fastapi import HTTPException, Request

from .core_client import CoreClient
from .http_auth import extract_tenant, resolve_identity
from .ldap_auth import Identity


def identity(request: Request) -> Identity:
    """Resolve the requesting user from Authorization (Basic/Bearer) + tenant, or 401."""
    config = request.app.state.config
    headers = {k.lower(): v for k, v in request.headers.items()}
    tenant = extract_tenant(headers, headers.get("host", ""), config.tenant)
    ident = resolve_identity(
        headers.get("authorization", ""), tenant, config,
        request.app.state.token_store,
        getattr(request.app.state, "bridge_verifier", None),
    )
    if ident is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return ident


def user_core(request: Request, ident: Identity) -> CoreClient:
    """A core client bound to the *calling user* — never the worker principal."""
    return CoreClient(request.app.state.config, ident.tenant, identity=ident)


def require_read(request: Request, ident: Identity, file_uid: str) -> None:
    """Enforce the caller's FileEngine READ on ``file_uid``, or 403 (§2).

    404 is deliberately NOT used for a permission failure here: the caller already
    proved identity, and distinguishing "no such file" from "not yours" leaks
    nothing they could not learn by listing. Denying with 403 keeps the failure
    legible to a front end that must choose between an error and a retry."""
    core = user_core(request, ident)
    if not core.check_permission(file_uid, "READ"):
        raise HTTPException(status_code=403, detail="READ permission required")

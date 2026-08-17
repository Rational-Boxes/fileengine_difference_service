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

"""FileEngine gRPC clients bound to an identity (trusted-upstream model, §2).

Two identities, and the distinction is the whole permission story:

  - **End user** — ``client_for(identity)``. Every *request* surface reads content
    and checks permissions as the caller, so the core's own ACLs gate the diff.
    A user who cannot READ the file cannot obtain its diff.
  - **Worker principal** — ``CoreClient``, acting as the difference_service agent
    account, used by the event-driven precompute path. It is **not**
    ``system_admin``; its rights come from ACL grants. It reads version content
    and writes diff renditions back under the source file.

Renditions inherit the source file's ACLs (they are hidden children of it), so the
worker never widens access: a precomputed diff is readable exactly by those who can
already read the file it describes.

``fileengine`` is imported lazily so config/auth/health import without the gRPC
stack present.
"""
from __future__ import annotations

import contextvars
import logging
from typing import List, Optional

from .ldap_auth import Identity, authenticate

log = logging.getLogger("difference_service.core_client")

# Request-scoped client IP (set by the HTTP middleware), forwarded for core audit.
request_source_addr: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "request_source_addr", default="")


def client_for(identity: Identity, config):
    """A gRPC client that acts as ``identity`` (the end user)."""
    from ._client import ManagedFiles
    return ManagedFiles(
        server_address=config.grpc_address,
        user_name=identity.user,
        user_roles=identity.roles,
        tenant=identity.tenant or config.tenant,
        source_addr=request_source_addr.get(),
    )


def agent_identity(config) -> Identity:
    """Authenticate the service's own worker principal against LDAP."""
    return authenticate(config, config.agent_user, config.agent_password)


def agent_client(config):
    """A gRPC client acting as the difference_service worker principal (§2).
    No ACL bypass — its rights come from ACL grants on the files it diffs."""
    return client_for(agent_identity(config), config)


def service_actor(config) -> str:
    """Actor name the worker writes as — lets the consumer recognise (and ignore)
    its own rendition writes, alongside the ``is_rendition`` flag (§2.1)."""
    return config.agent_user or "svc:difference_service"


class CoreClient:
    """Thin wrapper over a ``ManagedFiles`` bound to one tenant.

    Exposes only what the diff pipeline needs. Constructed per-tenant because the
    service consumes a shared multi-tenant event stream, so every core operation
    must run in the *event's* tenant, not one fixed at startup."""

    def __init__(self, config, tenant: Optional[str] = None, *, identity: Optional[Identity] = None):
        self.config = config
        self.tenant = tenant or config.tenant
        # ``identity`` set => act as that end user; otherwise the worker principal.
        self._identity = identity
        self.actor = identity.user if identity else service_actor(config)
        self._mf = None

    def _client(self):
        if self._mf is None:
            if self._identity is not None:
                self._mf = client_for(self._identity, self.config)
            else:
                self._mf = agent_client(self.config)
        return self._mf

    # -- reads --
    def stat(self, uid: str):
        return self._client().stat(uid, tenant=self.tenant)

    def parent_of(self, uid: str) -> str:
        return self.stat(uid).parent_uid

    def revisions(self, uid: str) -> List:
        """The file's stored versions, newest-last as the core returns them.
        The version-pair resolver (§3) walks this to find a target's predecessor."""
        return list(self._client().revisions(uid, tenant=self.tenant) or [])

    def read_version(self, uid: str, back: int = 0) -> bytes:
        """Full content of a version. ``back=0`` is the latest; ``back=N`` walks
        N revisions back, matching the core's ``get`` semantics."""
        buf = self._client().get(uid, back=back, tenant=self.tenant)
        try:
            return buf.read()
        finally:
            try:
                buf.close()
            except Exception:
                pass

    def read_prefix(self, uid: str, n: int = 8192) -> bytes:
        """First ``n`` bytes of the latest version — for content MIME sniffing."""
        buf = self._client().get(uid, tenant=self.tenant)
        try:
            return buf.read(n)
        finally:
            try:
                buf.close()
            except Exception:
                pass

    def metadata(self, uid: str) -> dict:
        try:
            return self._client().get_metadata_values(uid, tenant=self.tenant)
        except Exception:
            return {}

    def list_renditions(self, uid: str) -> List:
        """The file's hidden-child renditions — where diff output is stored (§2)."""
        try:
            return list(self._client().list_renditions(uid, tenant=self.tenant) or [])
        except Exception:
            return []

    def check_permission(self, uid: str, permission: str = "READ") -> bool:
        """Does this client's identity hold ``permission`` on ``uid``? The READ gate
        for every request surface (§2). Fails CLOSED — an unreachable core denies."""
        try:
            return bool(self._client().check_permission(uid, permission, tenant=self.tenant))
        except Exception:
            log.warning("permission check failed for %s (%s) — denying",
                        uid, permission, exc_info=True)
            return False

    # -- writes (rendition output) --
    def set_metadata(self, uid: str, key: str, value: str) -> bool:
        return self._client().set_metadata_value(uid, key, str(value), tenant=self.tenant)

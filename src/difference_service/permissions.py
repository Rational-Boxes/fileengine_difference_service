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

"""READ-permission cache for the request surface (SPECIFICATION.md §2, M4).

Every diff surface is gated on the caller's FileEngine READ, checked by the core as
that user. Asking the core on every request would put a gRPC round trip in front of
each poll while a diff computes, so decisions are cached — under three rules that
matter more than the caching itself:

  * **Fail closed.** An unreachable or erroring core caches nothing and denies. A
    permission cache that guesses "allow" on error is a vulnerability, not an
    optimisation.
  * **Only positive-or-negative decisions the core actually made** are stored, so a
    denial cannot be manufactured by an outage.
  * **Short TTL plus event invalidation.** The TTL (≤5 min) bounds staleness in the
    worst case; ``acl.changed`` / ``role.*`` events evict precisely and immediately,
    so a revoked grant stops working in about the time it takes the event to arrive
    rather than at the end of the window.

Mirrors ``convert_search_ai``'s ``PermissionGate`` so the two services age the same
way and an operator reasons about one model, not two.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, List, Tuple

log = logging.getLogger("difference_service.permissions")


class PermissionGate:
    """TTL-bounded cache of per-(tenant, user, file) READ decisions."""

    def __init__(self, ttl_seconds: int = 300):
        self.ttl = max(0, int(ttl_seconds))
        self._lock = threading.Lock()
        #: (tenant, user, file_uid) -> (allowed, expires_at)
        self._cache: Dict[Tuple[str, str, str], Tuple[bool, float]] = {}

    # ------------------------------------------------------------- decisions
    def can_read(self, core, file_uid: str) -> bool:
        """Does ``core``'s identity hold READ on ``file_uid``?

        ``core`` must be a CoreClient bound to the CALLING USER — passing the
        worker principal here would answer a different question entirely and hand
        every caller the worker's rights."""
        tenant = getattr(core, "tenant", "") or ""
        user = getattr(core, "actor", "") or ""
        key = (tenant, user, file_uid)

        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and hit[1] > now:
                return hit[0]

        allowed = self._check(core, file_uid)
        if allowed is None:
            return False                    # error -> deny, and cache nothing
        if self.ttl:
            with self._lock:
                self._cache[key] = (allowed, time.time() + self.ttl)
        return allowed

    def _check(self, core, file_uid: str):
        """``True``/``False`` from the core, or ``None`` when it could not answer.

        The ``None`` case is why this is separate: an exception must not be cached
        as a denial either, or one blip would lock a user out for the whole TTL."""
        try:
            return bool(core.check_permission(file_uid, "READ"))
        except Exception:
            log.warning("permission check errored for %s; denying", file_uid,
                        exc_info=True)
            return None

    # ---------------------------------------------------------- invalidation
    def invalidate_resource(self, tenant: str, file_uid: str) -> int:
        """Drop decisions for one resource (``acl.changed``)."""
        return self._drop(lambda k: k[0] == tenant and k[2] == file_uid)

    def invalidate_member(self, tenant: str, user: str) -> int:
        """Drop a user's decisions across the tenant (``role.assigned`` /
        ``role.member_removed``) — a role change fans out to every resource that
        role could reach, so nothing narrower is safe."""
        return self._drop(lambda k: k[0] == tenant and k[1] == user)

    def invalidate_tenant(self, tenant: str) -> int:
        """Drop the tenant's decisions (``role.deleted`` — members unknown)."""
        return self._drop(lambda k: k[0] == tenant)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    def _drop(self, predicate) -> int:
        with self._lock:
            doomed = [k for k in self._cache if predicate(k)]
            for k in doomed:
                del self._cache[k]
        return len(doomed)

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._cache)

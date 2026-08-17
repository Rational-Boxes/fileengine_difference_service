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

"""Permission cache (SPECIFICATION.md §2, M4).

A permission cache is a security control, so these test the *failure* directions
hardest: an error must never become a cached allow, and an eviction must actually
reach the core again.
"""
import time

from difference_service.permissions import PermissionGate


class FakeCore:
    def __init__(self, tenant="default", user="alice", allow=True, raises=False):
        self.tenant = tenant
        self.actor = user
        self.allow = allow
        self.raises = raises
        self.calls = 0

    def check_permission(self, uid, permission="READ"):
        self.calls += 1
        if self.raises:
            raise RuntimeError("core down")
        return self.allow


def test_an_allow_is_cached():
    core, gate = FakeCore(), PermissionGate(ttl_seconds=60)
    assert gate.can_read(core, "F") is True
    assert gate.can_read(core, "F") is True
    assert core.calls == 1


def test_a_denial_is_cached_too():
    # Caching only allows would let a denied user hammer the core on every poll.
    core, gate = FakeCore(allow=False), PermissionGate(ttl_seconds=60)
    assert gate.can_read(core, "F") is False
    assert gate.can_read(core, "F") is False
    assert core.calls == 1


def test_an_error_denies_and_is_not_cached():
    # THE rule: a cache that guesses "allow" on error is a vulnerability, and one
    # that caches the denial locks a user out for the whole TTL over a blip.
    core, gate = FakeCore(raises=True), PermissionGate(ttl_seconds=60)
    assert gate.can_read(core, "F") is False
    assert gate.can_read(core, "F") is False
    assert core.calls == 2                     # asked again, not cached
    assert gate.size == 0


def test_decisions_are_scoped_per_user():
    gate = PermissionGate(ttl_seconds=60)
    alice = FakeCore(user="alice", allow=True)
    bob = FakeCore(user="bob", allow=False)
    assert gate.can_read(alice, "F") is True
    assert gate.can_read(bob, "F") is False     # must not read alice's entry


def test_decisions_are_scoped_per_tenant():
    gate = PermissionGate(ttl_seconds=60)
    a = FakeCore(tenant="t1", allow=True)
    b = FakeCore(tenant="t2", allow=False)
    assert gate.can_read(a, "F") is True
    assert gate.can_read(b, "F") is False


def test_entries_expire():
    core, gate = FakeCore(), PermissionGate(ttl_seconds=1)
    gate.can_read(core, "F")
    time.sleep(1.1)
    gate.can_read(core, "F")
    assert core.calls == 2


def test_a_zero_ttl_disables_caching():
    core, gate = FakeCore(), PermissionGate(ttl_seconds=0)
    gate.can_read(core, "F")
    gate.can_read(core, "F")
    assert core.calls == 2


# --------------------------------------------------------------- eviction
def test_resource_invalidation_forces_a_recheck():
    core, gate = FakeCore(), PermissionGate(ttl_seconds=60)
    gate.can_read(core, "F")
    assert gate.invalidate_resource("default", "F") == 1
    gate.can_read(core, "F")
    assert core.calls == 2


def test_resource_invalidation_leaves_other_files_alone():
    core, gate = FakeCore(), PermissionGate(ttl_seconds=60)
    gate.can_read(core, "F")
    gate.can_read(core, "G")
    gate.invalidate_resource("default", "F")
    gate.can_read(core, "G")
    assert core.calls == 2                     # G still cached


def test_member_invalidation_drops_that_users_decisions_across_the_tenant():
    # A role change fans out to every resource the role could reach.
    gate = PermissionGate(ttl_seconds=60)
    alice, bob = FakeCore(user="alice"), FakeCore(user="bob")
    gate.can_read(alice, "F")
    gate.can_read(alice, "G")
    gate.can_read(bob, "F")
    assert gate.invalidate_member("default", "alice") == 2
    assert gate.size == 1                      # bob's decision survives


def test_tenant_invalidation_drops_everything_for_that_tenant():
    gate = PermissionGate(ttl_seconds=60)
    gate.can_read(FakeCore(tenant="t1", user="a"), "F")
    gate.can_read(FakeCore(tenant="t1", user="b"), "G")
    gate.can_read(FakeCore(tenant="t2", user="c"), "H")
    assert gate.invalidate_tenant("t1") == 2
    assert gate.size == 1


def test_a_revoked_grant_stops_working_after_invalidation():
    # The point of the whole mechanism, end to end.
    core, gate = FakeCore(allow=True), PermissionGate(ttl_seconds=300)
    assert gate.can_read(core, "F") is True
    core.allow = False                          # access revoked in the core
    assert gate.can_read(core, "F") is True     # ...still cached, as designed
    gate.invalidate_resource("default", "F")    # acl.changed arrives
    assert gate.can_read(core, "F") is False


def test_clear_empties_the_cache():
    core, gate = FakeCore(), PermissionGate(ttl_seconds=60)
    gate.can_read(core, "F")
    gate.clear()
    assert gate.size == 0

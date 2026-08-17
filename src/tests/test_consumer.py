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

"""Event-worker tests (SPECIFICATION.md §2.1)."""
from difference_service.consumer import EventConsumer
from difference_service.pipeline import Outcome, RunReport


class FakePipeline:
    def __init__(self, outcome=Outcome.COMPUTED):
        self.runs = []
        self.deletes = []
        self.outcome = outcome
        self.raise_on_run = None

    def run(self, file_uid, target="", base=None, force=False):
        if self.raise_on_run:
            raise self.raise_on_run
        self.runs.append((file_uid, target))
        return RunReport(self.outcome, file_uid=file_uid, target=target)

    def cascade_delete(self, file_uid):
        self.deletes.append(file_uid)
        return 1


class Config:
    tenant = "default"
    enabled_plugins = set()


def _consumer(pipeline=None):
    pipeline = pipeline or FakePipeline()
    c = EventConsumer(Config(), registry=object(),
                      pipeline_factory=lambda tenant: pipeline)
    c._fake = pipeline
    return c


def _event(**kw):
    e = {"event_id": "e1", "type": "file.updated", "tenant": "default",
         "file_uid": "F", "version": "v2", "is_rendition": False, "is_folder": False}
    e.update(kw)
    return e


# ------------------------------------------------------------------ routing
def test_file_updated_runs_the_pipeline_for_that_version():
    c = _consumer()
    assert c.handle(_event()) is True
    assert c._fake.runs == [("F", "v2")]


def test_file_deleted_cascades():
    c = _consumer()
    assert c.handle(_event(type="file.deleted", version="")) is True
    assert c._fake.deletes == ["F"]


def test_unrelated_event_types_are_acked_and_ignored():
    c = _consumer()
    for t in ("file.created", "file.moved", "file.renamed", "acl.changed", "role.assigned"):
        assert c.handle(_event(type=t, event_id=t)) is True
    assert c._fake.runs == [] and c._fake.deletes == []


def test_folder_events_are_ignored():
    c = _consumer()
    assert c.handle(_event(is_folder=True)) is True
    assert c._fake.runs == []


def test_event_without_a_file_uid_is_ignored():
    c = _consumer()
    assert c.handle(_event(file_uid="")) is True
    assert c._fake.runs == []


# ------------------------------------------------------- the recursion guard
def test_rendition_events_are_ignored():
    # THE guard: our own diff children are hidden children of the source file, and
    # every write emits an event. Reacting would diff our own diffs forever.
    c = _consumer()
    assert c.handle(_event(is_rendition=True)) is True
    assert c._fake.runs == []


def test_rendition_delete_events_are_ignored_too():
    c = _consumer()
    assert c.handle(_event(type="file.deleted", is_rendition=True)) is True
    assert c._fake.deletes == []


# ----------------------------------------------------------------- dedupe
def test_a_repeated_event_id_is_only_handled_once():
    c = _consumer()
    c.handle(_event())
    c.handle(_event())
    assert len(c._fake.runs) == 1


def test_distinct_event_ids_both_run():
    c = _consumer()
    c.handle(_event(event_id="a"))
    c.handle(_event(event_id="b"))
    assert len(c._fake.runs) == 2


def test_the_seen_window_is_bounded():
    c = _consumer()
    for i in range(5000):
        c.handle(_event(event_id=f"e{i}"))
    assert len(c._seen) <= 4096


# ------------------------------------------------------------- ack semantics
def test_a_transient_error_is_left_unacked_for_redelivery():
    c = _consumer(FakePipeline(outcome=Outcome.ERROR))
    assert c.handle(_event()) is False


def test_a_content_failure_is_terminal_and_acked():
    # It already wrote a failed manifest; redelivering would only repeat it.
    c = _consumer(FakePipeline(outcome=Outcome.FAILED))
    assert c.handle(_event()) is True


def test_an_unsupported_type_is_acked():
    c = _consumer(FakePipeline(outcome=Outcome.UNSUPPORTED))
    assert c.handle(_event()) is True


def test_a_raising_pipeline_is_left_unacked():
    p = FakePipeline()
    p.raise_on_run = RuntimeError("core down")
    c = _consumer(p)
    assert c.handle(_event()) is False


def test_tenant_is_taken_from_the_event():
    seen = []

    def factory(tenant):
        seen.append(tenant)
        return FakePipeline()

    c = EventConsumer(Config(), registry=object(), pipeline_factory=factory)
    c.handle(_event(tenant="acme"))
    assert seen == ["acme"]


# ------------------------------------------------- permission invalidation
class FakeGate:
    def __init__(self):
        self.calls = []
        self.cleared = 0
        self.raise_on = None

    def invalidate_resource(self, tenant, file_uid):
        if self.raise_on == "resource":
            raise RuntimeError("boom")
        self.calls.append(("resource", tenant, file_uid))

    def invalidate_member(self, tenant, user):
        self.calls.append(("member", tenant, user))

    def invalidate_tenant(self, tenant):
        self.calls.append(("tenant", tenant))

    def clear(self):
        self.cleared += 1


def _gated():
    gate = FakeGate()
    pipeline = FakePipeline()
    c = EventConsumer(Config(), registry=object(),
                      pipeline_factory=lambda tenant: pipeline, permissions=gate)
    c._fake, c._gate = pipeline, gate
    return c


def test_acl_changed_invalidates_that_resource():
    c = _gated()
    assert c.handle(_event(type="acl.changed", file_uid="X")) is True
    assert c._gate.calls == [("resource", "default", "X")]
    assert c._fake.runs == []          # governance events do not trigger a diff


def test_role_membership_change_invalidates_the_member():
    # A role change fans out to every resource the role could reach, so nothing
    # narrower than the member is safe.
    c = _gated()
    c.handle(_event(type="role.assigned", event_id="r1", member="bob"))
    c.handle(_event(type="role.member_removed", event_id="r2", member="carol"))
    assert c._gate.calls == [("member", "default", "bob"), ("member", "default", "carol")]


def test_role_deleted_invalidates_the_whole_tenant():
    # Its members are unknown by the time the event arrives.
    c = _gated()
    c.handle(_event(type="role.deleted", member=""))
    assert c._gate.calls == [("tenant", "default")]


def test_a_role_event_without_a_member_falls_back_to_the_tenant():
    c = _gated()
    c.handle(_event(type="role.assigned", member=""))
    assert c._gate.calls == [("tenant", "default")]


def test_a_failed_invalidation_clears_the_whole_cache():
    # A cache that cannot be evicted precisely is a correctness risk; dropping
    # everything is the safe direction.
    c = _gated()
    c._gate.raise_on = "resource"
    assert c.handle(_event(type="acl.changed", file_uid="X")) is True
    assert c._gate.cleared == 1


def test_governance_events_are_ignored_without_a_cache():
    # The worker often runs alone, with no API-side cache to evict.
    c = _consumer()
    assert c.handle(_event(type="acl.changed", file_uid="X")) is True

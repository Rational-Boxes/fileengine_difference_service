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


class FakeCore:
    def __init__(self, pending=None, fail=False):
        self._pending = pending or {}
        self.acks = []
        self.fail = fail

    def list_pending_erasures(self, participant, limit=0, tenant=None, all_tenants=True):
        assert all_tenants, "the sweep must ask for all tenants"
        return [{**it, "tenant": t} for t, items in self._pending.items() for it in items]

    def acknowledge_erasure(self, erasure_id, participant, complied=True, detail="",
                            tenant=None):
        if self.fail:
            raise RuntimeError("core unreachable")
        self.acks.append({"erasure_id": erasure_id, "participant": participant,
                          "complied": complied, "detail": detail})
        return "complete"


def _consumer(pipeline=None, core=None):
    pipeline = pipeline or FakePipeline()
    c = EventConsumer(Config(), registry=object(),
                      pipeline_factory=lambda tenant: pipeline, core=core)
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


# ── Erasure (PROPOSAL_accountability_record.md §5.4) ────────────────────────

def test_erasure_removes_diff_children_and_acknowledges():
    core = FakeCore()
    c = _consumer(core=core)
    assert c.handle(_event(type="file.erased", version="", erasure_id="e1")) is True
    assert c._fake.deletes == ["F"]
    assert len(core.acks) == 1
    assert core.acks[0]["participant"] == "difference"
    assert core.acks[0]["complied"] is True
    # The detail says what happened AND that there is no local store — an
    # auditor reading the record should not be left wondering what else this
    # service might still be holding.
    assert "no local store" in core.acks[0]["detail"]


def test_a_soft_delete_is_not_acknowledged_as_an_erasure():
    # file.deleted already cascades. It must not touch the erasure record.
    core = FakeCore()
    c = _consumer(core=core)
    assert c.handle(_event(type="file.deleted", version="")) is True
    assert core.acks == []


def test_a_failed_cascade_is_reported_and_retried():
    core = FakeCore()
    pipe = FakePipeline()
    def boom(file_uid):
        raise RuntimeError("core unreachable")
    pipe.cascade_delete = boom
    c = _consumer(pipe, core=core)

    # False = redeliver. A core that was briefly unreachable is exactly the case
    # redelivery fixes, and the erasure stays outstanding meanwhile.
    assert c.handle(_event(type="file.erased", version="", erasure_id="e1")) is False
    assert core.acks and core.acks[0]["complied"] is False


def test_a_lost_acknowledgement_still_acks_the_event():
    # The removal happened; re-processing it would just repeat a no-op, and the
    # sweep re-offers the erasure until an acknowledgement lands.
    core = FakeCore(fail=True)
    c = _consumer(core=core)
    assert c.handle(_event(type="file.erased", version="", erasure_id="e1")) is True
    assert c._fake.deletes == ["F"]


def test_the_sweep_catches_what_the_event_bus_dropped():
    core = FakeCore(pending={"default": [{"erasure_id": "e9", "uid": "U9",
                                          "tenant": "default", "initiated_at": 1}]})
    c = _consumer(core=core)
    assert c.sweep_erasures([]) == 1
    assert c._fake.deletes == ["U9"]
    assert core.acks[0]["erasure_id"] == "e9"


def test_without_a_core_client_the_children_are_still_removed():
    c = _consumer(core=None)
    assert c.handle(_event(type="file.erased", version="", erasure_id="e1")) is True
    assert c._fake.deletes == ["F"]


def test_the_erasure_sweeper_thread_actually_runs():
    """The sweeper runs in a daemon thread, so nothing else here executes its body.

    That is how a missing `import time` shipped: the module parsed, every test
    passed, and the thread would have died on its first tick with a NameError
    nobody would see until an erasure went unacknowledged. This drives one
    iteration directly.
    """
    import threading
    core = FakeCore(pending={"default": [{"erasure_id": "e1", "uid": "U1",
                                          "tenant": "default", "initiated_at": 1}]})
    c = _consumer(core=core)

    started = []
    real = threading.Thread

    class RunOnce(real):
        def start(self):                      # run the body inline, once
            started.append(self.name)
            try:
                # The loop is infinite; stop it after the first sleep.
                import difference_service.consumer as mod
                orig = mod.time.sleep
                def stop(_s):
                    mod.time.sleep = orig
                    raise KeyboardInterrupt
                mod.time.sleep = stop
                try:
                    self._target()
                except KeyboardInterrupt:
                    pass
            finally:
                mod = None

    threading.Thread = RunOnce
    try:
        c._start_erasure_sweeper()
    finally:
        threading.Thread = real

    assert started, "the sweeper thread was started"
    assert core.acks and core.acks[0]["erasure_id"] == "e1", \
        "one sweep iteration ran and acknowledged"

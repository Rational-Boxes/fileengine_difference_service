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

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

"""Pipeline + rendition-writer tests (§2.1, §6, §7.1.1) against a fake core.

The fake models the parts of the core the pipeline actually depends on — version
ordering, hidden children, content round-trips — so the atomicity and cache rules
can be asserted without a live stack.
"""
import pytest

from difference_service.manifest import Manifest, manifest_name
from difference_service.pipeline import DiffPipeline, Outcome
from difference_service.plugins import (
    DiffChild, DiffMode, DiffPlugin, DiffResult, DiffStatus, PluginRegistry,
)


class Entry:
    def __init__(self, uid, name):
        self.uid, self.name = uid, name
        self.is_container = False


class FakeMF:
    """Hidden children of one file, with content."""
    def __init__(self):
        self.children = {}     # uid -> (parent, name)
        self.content = {}      # uid -> bytes
        self._n = 0
        self.write_order = []  # names in the order they were PUT

    def dir(self, uid, tenant=None):
        return [Entry(u, n) for u, (p, n) in self.children.items() if p == uid]

    def touch(self, parent, name, tenant=None):
        self._n += 1
        uid = f"child-{self._n}"
        self.children[uid] = (parent, name)
        return uid

    def put(self, uid, data, tenant=None):
        self.content[uid] = data
        self.write_order.append(self.children[uid][1])
        return True

    def get(self, uid, back=0, tenant=None):
        import io
        return io.BytesIO(self.content.get(uid, b""))

    def remove(self, uid, tenant=None):
        self.children.pop(uid, None)
        return True


class Rev:
    def __init__(self, v):
        self.version = v


class Info:
    name = "doc.txt"


class FakeCore:
    def __init__(self, versions=("v2", "v1"), payloads=None):
        self.tenant = "default"
        self._versions = list(versions)
        self._payloads = payloads or {"v2": b"new", "v1": b"old"}
        self.mf = FakeMF()

    def _client(self):
        return self.mf

    def revisions(self, uid):
        return [Rev(v) for v in self._versions]

    def read_version(self, uid, back=0):
        return self._payloads[self._versions[back]]

    def stat(self, uid):
        return Info()

    def read_prefix(self, uid, n=8192):
        return self._payloads[self._versions[0]]


class Recorder(DiffPlugin):
    name = "rec"
    version = 1

    def __init__(self, children=1, status=DiffStatus.READY):
        self.calls = 0
        self._children = children
        self._status = status
        self.seen = []

    def supports(self, mime):
        return True

    def diff(self, base, target):
        self.calls += 1
        self.seen.append((base.version, target.version, base.data, target.data))
        if self._status == DiffStatus.FAILED:
            return DiffResult.failed("render", "no backend")
        return DiffResult(children=[
            DiffChild(kind="page", index=i, data=f"<svg id={i}/>".encode(),
                      mime="image/svg+xml", ext="svg", mode=DiffMode.VECTOR)
            for i in range(self._children)])


class Config:
    max_source_bytes = 0
    enabled_plugins = set()


def _pipe(core, plugin):
    p = DiffPipeline(Config(), PluginRegistry([plugin]), core)
    p.mime.resolve = lambda uid: "text/plain"     # events carry no mime (§2)
    return p


# --------------------------------------------------------------- happy path
def test_computes_and_commits_a_result():
    core, plugin = FakeCore(), Recorder(children=2)
    report = _pipe(core, plugin).run("F")
    assert report.outcome == Outcome.COMPUTED
    assert report.manifest.status == DiffStatus.READY
    assert len(report.manifest.expected) == 2


def test_the_plugin_receives_base_as_old_and_target_as_new():
    # §3: target is "new". Swapping these silently inverts every diff.
    core, plugin = FakeCore(), Recorder()
    _pipe(core, plugin).run("F")
    base_v, target_v, base_data, target_data = plugin.seen[0]
    assert (base_v, target_v) == ("v1", "v2")
    assert (base_data, target_data) == (b"old", b"new")


def test_manifest_is_written_last():
    # §7.1.1: the manifest's presence IS the completion signal, so it must land
    # after every content child or a reader can serve a half-formed result.
    core, plugin = FakeCore(), Recorder(children=3)
    report = _pipe(core, plugin).run("F")
    order = core.mf.write_order
    assert order[-1] == manifest_name(report.manifest.key)
    assert len(order) == 4


def test_a_failed_run_still_commits_a_manifest():
    core, plugin = FakeCore(), Recorder(status=DiffStatus.FAILED)
    report = _pipe(core, plugin).run("F")
    assert report.outcome == Outcome.FAILED
    assert report.manifest.status == DiffStatus.FAILED
    assert report.manifest.failure["stage"] == "render"
    # It is stored, so "attempted and failed" is distinguishable from "never
    # attempted" (§7.1.1).
    assert manifest_name(report.manifest.key) in core.mf.write_order


# -------------------------------------------------------------------- cache
def test_second_run_is_a_cache_hit_and_does_no_work():
    core, plugin = FakeCore(), Recorder()
    pipe = _pipe(core, plugin)
    pipe.run("F")
    report = pipe.run("F")
    assert report.outcome == Outcome.CACHED
    assert plugin.calls == 1


def test_force_bypasses_the_cache():
    core, plugin = FakeCore(), Recorder()
    pipe = _pipe(core, plugin)
    pipe.run("F")
    assert pipe.run("F", force=True).outcome == Outcome.COMPUTED
    assert plugin.calls == 2


def test_a_missing_content_child_is_not_served_from_cache():
    # The manifest landed but a child did not — must recompute, not serve partial.
    core, plugin = FakeCore(), Recorder(children=2)
    pipe = _pipe(core, plugin)
    report = pipe.run("F")
    victim = next(u for u, (_p, n) in core.mf.children.items()
                  if n == report.manifest.expected[0])
    core.mf.remove(victim)
    assert pipe.run("F").outcome == Outcome.COMPUTED


def test_a_plugin_version_bump_regenerates():
    # §6: bumping version changes the cache key, so the old result stops matching.
    core, plugin = FakeCore(), Recorder()
    _pipe(core, plugin).run("F")
    plugin.version = 2
    assert _pipe(core, plugin).run("F").outcome == Outcome.COMPUTED
    assert plugin.calls == 2


def test_an_older_plugin_generation_of_the_same_pair_is_pruned():
    # A newer differ answering the identical question makes the old rendering dead
    # weight — that, and only that, is what "superseded" means.
    core, plugin = FakeCore(), Recorder()
    _pipe(core, plugin).run("F")
    plugin.version = 2
    report = _pipe(core, plugin).run("F")
    names = {n for _u, (_p, n) in core.mf.children.items()}
    assert names == set(report.manifest.expected) | {manifest_name(report.manifest.key)}


def test_a_different_pair_survives_a_newer_comparison():
    # Versions are immutable, so a computed pair is correct forever: a new upload
    # must not discard it. Comments can be anchored to a pair, and pruning here
    # would silently turn those into dead ends — plus recomputing can only ever
    # reproduce the same bytes, at the cost of a tens-of-seconds job.
    core = FakeCore(versions=("v2", "v1"))
    first = _pipe(core, Recorder()).run("F")

    # A third version lands; the newest pair is now v3 -> v2.
    core._versions = ["v3", "v2", "v1"]
    core._payloads["v3"] = b"newest"
    second = _pipe(core, Recorder()).run("F")
    assert second.manifest.key != first.manifest.key

    names = {n for _u, (_p, n) in core.mf.children.items()}
    for m in (first.manifest, second.manifest):
        assert set(m.expected) <= names, "a still-valid comparison was pruned"
        assert manifest_name(m.key) in names


def test_the_surviving_older_pair_is_still_served_from_cache():
    # The point of keeping it: asking for that pair again does no work.
    core = FakeCore(versions=("v2", "v1"))
    _pipe(core, Recorder()).run("F")
    core._versions = ["v3", "v2", "v1"]
    core._payloads["v3"] = b"newest"
    _pipe(core, Recorder()).run("F")

    plugin = Recorder()
    again = _pipe(core, plugin).run("F", base="v1", target="v2")
    assert again.outcome == Outcome.CACHED
    assert plugin.calls == 0


# ------------------------------------------------------------- non-outcomes
def test_unsupported_type_writes_nothing():
    class NoOne(Recorder):
        def supports(self, mime):
            return False

    core = FakeCore()
    report = _pipe(core, NoOne()).run("F")
    assert report.outcome == Outcome.UNSUPPORTED
    assert report.manifest is None
    assert core.mf.write_order == []      # §5.3: nothing to do is not a failure


def test_first_version_is_not_an_error_and_writes_nothing():
    core = FakeCore(versions=("v1",), payloads={"v1": b"only"})
    report = _pipe(core, Recorder()).run("F")
    assert report.outcome == Outcome.NO_PREDECESSOR
    assert core.mf.write_order == []


def test_oversized_side_degrades_instead_of_running():
    class Cap(Config):
        max_source_bytes = 2

    core, plugin = FakeCore(), Recorder()
    pipe = DiffPipeline(Cap(), PluginRegistry([plugin]), core)
    pipe.mime.resolve = lambda uid: "text/plain"
    assert pipe.run("F").outcome == Outcome.TOO_LARGE
    assert plugin.calls == 0


def test_unknown_target_version_is_an_error_not_a_failed_manifest():
    core = FakeCore()
    report = _pipe(core, Recorder()).run("F", target="nope")
    assert report.outcome == Outcome.ERROR
    assert core.mf.write_order == []


# ------------------------------------------------------------------ delete
def test_cascade_delete_removes_only_our_children():
    core, plugin = FakeCore(), Recorder()
    pipe = _pipe(core, plugin)
    pipe.run("F")
    core.mf.touch("F", "20260101-thumbnail.png")     # another service's rendition
    assert pipe.cascade_delete("F") >= 1
    names = {n for _u, (_p, n) in core.mf.children.items()}
    assert names == {"20260101-thumbnail.png"}


def test_cascade_delete_is_idempotent():
    core = FakeCore()
    pipe = _pipe(core, Recorder())
    pipe.run("F")
    pipe.cascade_delete("F")
    assert pipe.cascade_delete("F") == 0

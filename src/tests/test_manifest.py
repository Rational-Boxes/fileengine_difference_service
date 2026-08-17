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

"""Manifest / cache-key unit tests (SPECIFICATION.md §6, §7.1, §7.1.1)."""
from difference_service.manifest import (
    Manifest, child_name, manifest_name, pair_key,
)
from difference_service.plugins import DiffChild, DiffMode, DiffResult, DiffStatus


def _child(index=0, mode=DiffMode.VECTOR):
    return DiffChild(kind="page", index=index, data=b"<svg/>", mime="image/svg+xml",
                     ext="svg", mode=mode)


def _manifest(**kw):
    base = dict(file_uid="F", base_version="v1", target_version="v2",
                plugin="pdf", plugin_version=1)
    base.update(kw)
    return Manifest(**base)


# ------------------------------------------------------------------ the key
def test_key_is_stable_for_the_same_inputs():
    assert pair_key("F", "v1", "v2", "pdf", 1) == pair_key("F", "v1", "v2", "pdf", 1)


def test_key_changes_with_every_component():
    k = pair_key("F", "v1", "v2", "pdf", 1)
    assert k != pair_key("G", "v1", "v2", "pdf", 1)      # file
    assert k != pair_key("F", "v0", "v2", "pdf", 1)      # base
    assert k != pair_key("F", "v1", "v3", "pdf", 1)      # target
    assert k != pair_key("F", "v1", "v2", "3d", 1)       # plugin
    assert k != pair_key("F", "v1", "v2", "pdf", 2)      # plugin version


def test_key_is_direction_sensitive():
    # base->target is not the same comparison as target->base (§3: target is "new").
    assert pair_key("F", "v1", "v2", "pdf", 1) != pair_key("F", "v2", "v1", "pdf", 1)


def test_child_names_are_index_padded_so_they_sort():
    key = "k"
    names = [child_name(key, "page", i, "svg") for i in (2, 10, 1)]
    assert sorted(names) == [child_name(key, "page", i, "svg") for i in (1, 2, 10)]


def test_manifest_name_is_distinct_from_content_children():
    key = pair_key("F", "v1", "v2", "pdf", 1)
    assert manifest_name(key) not in {child_name(key, "page", 0, "svg")}


# ------------------------------------------------------------- from_result
def test_built_from_a_result_carries_mode_units_and_expected_set():
    result = DiffResult(children=[_child(0, DiffMode.VECTOR), _child(1, DiffMode.RASTER)])
    m = Manifest.from_result(result, file_uid="F", base_version="v1",
                             target_version="v2", plugin="pdf", plugin_version=1)
    assert m.status == DiffStatus.READY
    assert m.mode == DiffMode.MIXED
    assert [u["index"] for u in m.units] == [0, 1]
    assert len(m.expected) == 2
    assert all(m.key in name for name in m.expected)


def test_a_failed_run_still_produces_a_manifest_with_the_failure():
    # §7.1.1: "attempted and failed" must stay distinguishable from "never
    # attempted" (no manifest at all) — otherwise the FE cannot choose between
    # falling back to side-by-side and waiting for a result.
    result = DiffResult.failed("render", "no raster backend", tiers=["vector", "raster"])
    m = Manifest.from_result(result, file_uid="F", base_version="v1",
                             target_version="v2", plugin="pdf", plugin_version=1)
    assert m.status == DiffStatus.FAILED
    assert m.expected == []
    assert m.failure["stage"] == "render"
    assert m.failure["tiers_attempted"] == ["vector", "raster"]


# ------------------------------------------------------------ serialization
def test_round_trips_through_bytes():
    result = DiffResult(children=[_child(0), _child(1, DiffMode.RASTER)])
    m = Manifest.from_result(result, file_uid="F", base_version="v1",
                             target_version="v2", plugin="pdf", plugin_version=3)
    back = Manifest.from_bytes(m.to_bytes())
    assert back.as_dict() == m.as_dict()
    assert back.key == m.key


def test_serialization_is_byte_stable():
    # Regenerating an unchanged result must reproduce the identical manifest, or
    # "idempotent" would only mean "equivalent" and every re-run would rewrite.
    a = _manifest(units=[{"index": 0, "mode": "vector", "kind": "page"}])
    b = _manifest(units=[{"index": 0, "mode": "vector", "kind": "page"}])
    assert a.to_bytes() == b.to_bytes()


def test_failure_is_absent_not_null_when_successful():
    assert "failure" not in _manifest().as_dict()


# -------------------------------------------------------------- read path
def test_completeness_check_catches_a_missing_child():
    result = DiffResult(children=[_child(0), _child(1)])
    m = Manifest.from_result(result, file_uid="F", base_version="v1",
                             target_version="v2", plugin="pdf", plugin_version=1)
    assert m.is_complete(m.expected)
    assert not m.is_complete(m.expected[:1])
    assert m.is_complete(m.expected + ["some.other.child"])   # extras are fine


def test_staleness_is_detected_per_plugin_and_version():
    m = _manifest(plugin="pdf", plugin_version=1)
    assert not m.is_stale("pdf", 1)
    assert m.is_stale("pdf", 2)      # algorithm upgrade -> regenerate (§6)
    assert m.is_stale("3d", 1)       # different plugin entirely

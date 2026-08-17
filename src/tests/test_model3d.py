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

"""3D matching scored against the fixture ground truth (SPECIFICATION.md §5.2).

Covers both identity strategies and, above all, the `modified` predicate: a
geometry change is visible, a property-only change is NOT. Getting that backwards
repaints a model that looks identical, after which reviewers stop trusting orange.
"""
import pytest

from difference_service.plugins.base import DiffState
from difference_service.plugins.gltf_objects import parse_gltf
from difference_service.plugins.model3d import (
    Change, Element3D, Model3D, Tier, match_models,
)
from tests.fixtures import gltf as G, ifc as I

ifcopenshell = pytest.importorskip(
    "ifcopenshell", reason="ifcopenshell not installed (pip install '.[ifc]')")

from difference_service.plugins.ifc_objects import parse_ifc  # noqa: E402


def _ifc(pair):
    before, after = pair
    return match_models(parse_ifc(before), parse_ifc(after))


def _gltf(pair):
    before, after = pair
    return match_models(parse_gltf(before), parse_gltf(after))


def _counts(delta):
    return {s: delta.count(s) for s in
            (DiffState.ADDED, DiffState.DELETED, DiffState.MODIFIED, DiffState.UNCHANGED)}


# ------------------------------------------------------------ tier selection
def test_ifc_uses_stable_ids():
    assert _ifc(I.unchanged_pair()).tier == Tier.STABLE_ID


def test_gltf_falls_to_geometry_matching():
    # glTF has no durable identity, so claiming tier 1 would be a false promise.
    assert _gltf(G.unchanged_pair()).tier == Tier.GEOMETRY


def test_a_model_with_few_ids_does_not_claim_tier_one():
    # A handful of ids among many elements would report the whole remainder as
    # added+deleted; a majority is required.
    mixed = Model3D(elements=[
        Element3D(stable_id="a", verts=(0, 0, 0), faces=(0,)),
        Element3D(local_id="n1", verts=(1, 1, 1), faces=(0,)),
        Element3D(local_id="n2", verts=(2, 2, 2), faces=(0,)),
    ])
    assert not mixed.has_stable_ids


# ------------------------------------------------------------ IFC (tier 1)
def test_ifc_unchanged():
    c = _counts(_ifc(I.unchanged_pair()))
    assert c[DiffState.UNCHANGED] == 2 and c[DiffState.MODIFIED] == 0


def test_ifc_added_and_deleted_elements():
    assert _counts(_ifc(I.added_element_pair()))[DiffState.ADDED] == 1
    assert _counts(_ifc(I.deleted_element_pair()))[DiffState.DELETED] == 1


def test_ifc_moved_element_keeps_identity_and_is_a_geometry_change():
    delta = _ifc(I.moved_element_pair())
    assert _counts(delta)[DiffState.MODIFIED] == 1
    assert delta.count_change(Change.GEOMETRY) == 1
    # Identity survived: a move must NOT read as delete + add.
    assert _counts(delta)[DiffState.ADDED] == 0
    assert _counts(delta)[DiffState.DELETED] == 0


def test_ifc_resized_element_is_a_geometry_change():
    # The geometry hash must cover the profile, not just the placement.
    delta = _ifc(I.resized_element_pair())
    assert delta.count_change(Change.GEOMETRY) == 1


def test_ifc_property_only_change_has_no_visual_delta():
    # THE §5.2 rule. Recorded, surfaced on selection, never painted.
    delta = _ifc(I.property_only_pair())
    assert _counts(delta)[DiffState.MODIFIED] == 1
    assert delta.count_change(Change.PROPERTY) == 1
    assert delta.count_change(Change.GEOMETRY) == 0
    assert delta.visual_changes == []

    changed = [d for d in delta.deltas if d.state == DiffState.MODIFIED][0]
    assert changed.changed_properties        # the reviewer can still see WHAT changed
    assert not changed.has_visual_delta


def test_ifc_rename_is_not_a_change():
    delta = _ifc(I.renamed_element_pair())
    assert _counts(delta)[DiffState.UNCHANGED] == 2
    assert delta.visual_changes == []


def test_ifc_entity_order_is_not_identity():
    # STEP entity numbers are file-local; only GlobalId is identity.
    delta = _ifc(I.reordered_entities_pair())
    assert _counts(delta)[DiffState.UNCHANGED] == 3
    assert delta.visual_changes == []


def test_ifc_combined_changes_are_all_detected():
    delta = _ifc(I.combined_pair())
    c = _counts(delta)
    assert c[DiffState.ADDED] == 1 and c[DiffState.DELETED] == 1
    assert delta.count_change(Change.GEOMETRY) == 1


def test_a_geometry_change_outranks_a_simultaneous_property_edit():
    # Reporting "property only" for an element that also MOVED would hide the move.
    old = Element3D(stable_id="x", verts=(0, 0, 0, 1, 0, 0, 0, 1, 0), faces=(0, 1, 2),
                    properties={"P.a": "1"})
    new = Element3D(stable_id="x", verts=(5, 0, 0, 6, 0, 0, 5, 1, 0), faces=(0, 1, 2),
                    properties={"P.a": "2"})
    delta = match_models(Model3D(elements=[old]), Model3D(elements=[new]))
    changed = delta.deltas[0]
    assert changed.change == Change.GEOMETRY
    assert changed.has_visual_delta
    assert changed.changed_properties == ["P.a"]      # still reported


# ----------------------------------------------------------- glTF (tier 3)
def test_gltf_unchanged():
    assert _counts(_gltf(G.unchanged_pair()))[DiffState.UNCHANGED] == 1


def test_gltf_added_and_deleted_meshes():
    assert _counts(_gltf(G.added_mesh_pair()))[DiffState.ADDED] == 1
    deleted = _counts(_gltf(G.deleted_mesh_pair()))
    assert deleted[DiffState.DELETED] == 1
    assert deleted[DiffState.UNCHANGED] == 1


def test_gltf_move_reads_as_removed_plus_added_volume():
    # Without identity there is no basis for claiming "the same thing moved".
    c = _counts(_gltf(G.moved_mesh_pair()))
    assert c[DiffState.ADDED] == 1 and c[DiffState.DELETED] == 1
    assert c[DiffState.MODIFIED] == 0


def test_gltf_node_names_are_not_identity():
    # The trap: exporters rewrite names freely, so matching on them invents changes.
    delta = _gltf(G.renamed_node_pair())
    assert _counts(delta)[DiffState.UNCHANGED] == 1
    assert delta.visual_changes == []


def test_gltf_node_order_is_not_identity():
    delta = _gltf(G.reordered_nodes_pair())
    assert _counts(delta)[DiffState.UNCHANGED] == 2
    assert delta.visual_changes == []


def test_geometry_pairing_is_independent_of_file_order():
    # Regression: pairing in list order let a distant copy claim the match first,
    # so the copy that actually stayed put was reported deleted+added.
    a = Element3D(local_id="a", verts=(0, 0, 0, 1, 0, 0, 0, 1, 0), faces=(0, 1, 2))
    b = Element3D(local_id="b", verts=(50, 0, 0, 51, 0, 0, 50, 1, 0), faces=(0, 1, 2))
    old = Model3D(elements=[b, a])          # distant copy FIRST
    new = Model3D(elements=[a])
    delta = match_models(old, new)
    assert delta.count(DiffState.UNCHANGED) == 1
    assert delta.count(DiffState.DELETED) == 1
    assert delta.count(DiffState.ADDED) == 0


# ----------------------------------------------------------------- geometry
def test_geometry_hash_ignores_names_and_properties():
    # If anything non-geometric leaks into the hash, property edits repaint the
    # model — the failure §5.2 is most concerned to prevent.
    a = Element3D(stable_id="x", name="Wall", verts=(0, 0, 0), faces=(0,),
                  properties={"P.a": "1"})
    b = Element3D(stable_id="x", name="Wall renamed", verts=(0, 0, 0), faces=(0,),
                  properties={"P.a": "999"})
    assert a.geometry_hash == b.geometry_hash


def test_geometry_hash_covers_faces_not_only_vertices():
    a = Element3D(verts=(0, 0, 0, 1, 0, 0, 0, 1, 0), faces=(0, 1, 2))
    b = Element3D(verts=(0, 0, 0, 1, 0, 0, 0, 1, 0), faces=(2, 1, 0))
    assert a.geometry_hash != b.geometry_hash


def test_shape_signature_is_position_independent():
    a = Element3D(verts=(0, 0, 0, 1, 0, 0, 0, 1, 0), faces=(0, 1, 2))
    b = Element3D(verts=(10, 0, 0, 11, 0, 0, 10, 1, 0), faces=(0, 1, 2))
    assert a.shape_signature == b.shape_signature
    assert a.geometry_hash != b.geometry_hash


# --------------------------------------------------------------- confidence
def test_a_legitimate_addition_does_not_depress_confidence():
    assert _ifc(I.added_element_pair()).confidence == 1.0
    assert _ifc(I.deleted_element_pair()).confidence == 1.0

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

"""3D plugin: dispatch across formats and the XKT layer contract (§5.2, §7.3)."""
import json

import pytest

from difference_service.plugins.base import DiffMode, DiffStatus, SourceRef
from difference_service.plugins.three_d import ThreeDDiffPlugin
from difference_service.plugins.xkt import (
    GROUP_DIFFERENCE, GROUP_NEW, GROUP_OLD, build_merged_gltf, build_metamodel,
)
from tests.fixtures import cad as CAD, gltf as G, ifc as I

pytest.importorskip("ifcopenshell", reason="ifcopenshell not installed")


def _src(data, mime, name, version="v1"):
    return SourceRef(uid="F", version=version, data=data, mime=mime, name=name)


def _diff(pair, mime, name, plugin=None):
    before, after = pair
    plugin = plugin or ThreeDDiffPlugin()
    return plugin.diff(_src(before, mime, name, "v1"), _src(after, mime, name, "v2"))


def _metamodel(result):
    child = [c for c in result.children if c.kind == "metamodel"][0]
    return json.loads(child.data)


def _model_child(result):
    return [c for c in result.children if c.kind == "model"][0]


# ---------------------------------------------------------------- dispatch
@pytest.mark.parametrize("mime", [
    "application/x-ifc", "model/gltf-binary", "model/gltf+json",
    "application/step", "model/iges", "model/stl",
])
def test_claims_every_supported_3d_mime(mime):
    assert ThreeDDiffPlugin().supports(mime)


@pytest.mark.parametrize("name,expected", [
    ("model.ifc", "ifc"), ("model.glb", "gltf"), ("model.gltf", "gltf"),
    ("part.step", "step"), ("part.stp", "step"), ("part.iges", "iges"),
    ("part.brep", "brep"), ("mesh.obj", "obj"), ("mesh.stl", "stl"),
])
def test_falls_back_to_the_file_name(name, expected):
    # 3D MIME types are poorly standardised and often arrive as
    # application/octet-stream, so the name is frequently the only signal.
    assert ThreeDDiffPlugin()._format_for("application/octet-stream", name) == expected


def test_does_not_claim_unrelated_types():
    p = ThreeDDiffPlugin()
    assert not p.supports("application/pdf")
    assert not p.supports("image/png")
    assert not p.supports("text/plain")


# ------------------------------------------------------------------- IFC
def test_ifc_pair_produces_a_model_and_a_metamodel():
    result = _diff(I.combined_pair(), "application/x-ifc", "m.ifc")
    assert result.status == DiffStatus.READY
    assert result.mode == DiffMode.XKT
    assert {c.kind for c in result.children} == {"model", "metamodel"}


def test_the_object_tree_has_exactly_the_three_groups():
    # §7.3: the stock Xeokit viewer drives these with show/hide/x-ray, so the
    # names and the fact there are exactly three are the contract.
    meta = _metamodel(_diff(I.combined_pair(), "application/x-ifc", "m.ifc"))
    layers = [o["id"] for o in meta["metaObjects"] if o["type"] == "Layer"]
    assert layers == [GROUP_OLD, GROUP_NEW, GROUP_DIFFERENCE]


def test_element_states_are_recorded_for_the_viewer():
    meta = _metamodel(_diff(I.combined_pair(), "application/x-ifc", "m.ifc"))
    states = {o.get("state") for o in meta["metaObjects"] if o.get("state")}
    assert {"added", "deleted", "modified"} <= states


def test_a_property_only_change_is_recorded_but_not_in_the_difference_group():
    # The §5.2 rule, end to end: visible on selection, invisible on the model.
    result = _diff(I.property_only_pair(), "application/x-ifc", "m.ifc")
    meta = _metamodel(result)

    in_difference = [o for o in meta["metaObjects"]
                     if o.get("parent") == GROUP_DIFFERENCE]
    assert in_difference == [], "a property-only change must not be painted"

    modified = [o for o in meta["metaObjects"] if o.get("state") == "modified"]
    assert modified, "the change must still be recorded"
    assert modified[0]["change"] == "property"
    assert modified[0]["changedProperties"]


def test_a_geometry_change_does_appear_in_the_difference_group():
    result = _diff(I.moved_element_pair(), "application/x-ifc", "m.ifc")
    meta = _metamodel(result)
    in_difference = [o for o in meta["metaObjects"]
                     if o.get("parent") == GROUP_DIFFERENCE]
    assert len(in_difference) == 1
    assert in_difference[0]["change"] == "geometry"


def test_the_summary_reports_the_tier_used():
    meta = _metamodel(_diff(I.unchanged_pair(), "application/x-ifc", "m.ifc"))
    assert meta["diffSummary"]["tier"] == "stable-id"


# ------------------------------------------------------------------ glTF
def test_gltf_pair_uses_geometry_matching():
    meta = _metamodel(_diff(G.added_mesh_pair(), "model/gltf-binary", "m.glb"))
    assert meta["diffSummary"]["tier"] == "geometry"


def test_gltf_rename_produces_no_difference_content():
    meta = _metamodel(_diff(G.renamed_node_pair(), "model/gltf-binary", "m.glb"))
    assert [o for o in meta["metaObjects"] if o.get("parent") == GROUP_DIFFERENCE] == []


# ------------------------------------------------------------------- CAD
@pytest.mark.skipif(not CAD.available(), reason="DRAWEXE (OpenCASCADE) not installed")
def test_step_is_diffed_through_the_same_path():
    pair = CAD.resized_solid_pair()
    if pair is None:
        pytest.skip("could not generate the STEP fixture")
    result = _diff(pair, "application/step", "part.step")
    assert result.status == DiffStatus.READY
    meta = _metamodel(result)
    # No durable ids in STEP, so a resized solid is honestly removed + added
    # volume rather than a claimed "same thing, changed" (§5.2 tier 3).
    assert meta["diffSummary"]["tier"] == "geometry"
    assert meta["diffSummary"]["added"] >= 1
    assert meta["diffSummary"]["deleted"] >= 1


@pytest.mark.skipif(not CAD.available(), reason="DRAWEXE (OpenCASCADE) not installed")
def test_step_tessellation_is_deterministic():
    # If OCCT meshed the same solid differently between two runs, EVERY element
    # would compare modified and no CAD diff would ever be usable.
    pair = CAD.unchanged_pair()
    if pair is None:
        pytest.skip("could not generate the STEP fixture")
    meta = _metamodel(_diff(pair, "application/step", "part.step"))
    assert meta["diffSummary"]["unchanged"] >= 1
    assert meta["diffSummary"]["added"] == 0
    assert meta["diffSummary"]["deleted"] == 0


# ------------------------------------------------------------- degradation
def test_an_unreadable_pair_fails_cleanly():
    result = ThreeDDiffPlugin().diff(
        _src(b"not a model", "application/x-ifc", "m.ifc"),
        _src(b"still not", "application/x-ifc", "m.ifc"))
    assert result.status == DiffStatus.FAILED
    assert result.failure.stage == "parse"


def test_an_unrecognised_format_fails_at_dispatch():
    result = ThreeDDiffPlugin().diff(
        _src(b"x", "application/octet-stream", "mystery.bin"),
        _src(b"y", "application/octet-stream", "mystery.bin"))
    assert result.status == DiffStatus.FAILED
    assert result.failure.stage == "dispatch"


def test_without_convert2xkt_the_merged_gltf_is_still_delivered():
    # A complete three-group model beats no diff at all; the manifest's mode says
    # what the viewer is getting.
    plugin = ThreeDDiffPlugin(convert2xkt="definitely-not-installed")
    result = _diff(I.added_element_pair(), "application/x-ifc", "m.ifc", plugin)
    assert result.status == DiffStatus.READY
    child = _model_child(result)
    assert child.ext == "glb"
    assert child.data.startswith(b"glTF")


# ------------------------------------------------------------ merged model
def test_the_merged_gltf_groups_every_element():
    from difference_service.plugins.ifc_objects import parse_ifc
    from difference_service.plugins.model3d import match_models

    before, after = I.combined_pair()
    delta = match_models(parse_ifc(before), parse_ifc(after))
    glb = build_merged_gltf(delta)
    assert glb is not None and glb.startswith(b"glTF")

    import struct
    length = struct.unpack("<I", glb[12:16])[0]
    doc = json.loads(glb[20:20 + length].decode("utf-8"))
    groups = [n for n in doc["nodes"] if "children" in n]
    assert [g["name"] for g in groups] == [GROUP_OLD, GROUP_NEW, GROUP_DIFFERENCE]
    # The old and new layers must each be a COMPLETE model, or a "before" view
    # would show only what was deleted.
    by_name = {g["name"]: g for g in groups}
    assert by_name[GROUP_OLD]["children"] and by_name[GROUP_NEW]["children"]


def test_an_empty_delta_builds_no_model():
    from difference_service.plugins.model3d import ModelDelta
    assert build_merged_gltf(ModelDelta()) is None


def test_metamodel_is_json_serialisable_and_stable():
    from difference_service.plugins.ifc_objects import parse_ifc
    from difference_service.plugins.model3d import match_models

    before, after = I.combined_pair()
    delta = match_models(parse_ifc(before), parse_ifc(after))
    a = json.dumps(build_metamodel(delta), sort_keys=True)
    b = json.dumps(build_metamodel(delta), sort_keys=True)
    assert a == b

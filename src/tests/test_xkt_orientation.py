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

"""The comparison must face the same way as the model it describes.

Reported anomaly: the difference rendition of an IFC model appeared rotated 90°
from the model the viewer shows for the same file.

The cause is a convention mismatch, not a maths error. glTF DEFINES +Y as up.
The normal rendition hands the ORIGINAL IFC to convert2xkt, which knows IFC is
Z-up and converts. The comparison instead writes extracted vertices into a glTF —
which declares them Y-up — so nothing rotated them and the two renditions
disagreed by exactly 90° about X.

Only IFC is affected: glTF sources are Y-up by definition, and CAD is tessellated
through glTF before it is parsed, so it arrives Y-up too. These tests pin both
halves of that, because "fix the rotation" applied indiscriminately would break
the sources that were already right.
"""
import json
import struct

from difference_service.plugins.model3d import (
    DiffState, Element3D, ElementDelta, Model3D, ModelDelta,
)
from difference_service.plugins.xkt import build_merged_gltf

# A quarter turn about X, as a quaternion [x, y, z, w].
QUARTER_TURN_X = -0.7071067811865476


def _gltf_of(glb: bytes) -> dict:
    """Pull the JSON chunk back out of a GLB."""
    assert glb[:4] == b"glTF"
    json_len = struct.unpack("<I", glb[12:16])[0]
    return json.loads(glb[20:20 + json_len].decode("utf-8"))


def _delta(up_axis: str) -> ModelDelta:
    cube = Element3D(
        stable_id="1", name="wall", type_name="IfcWall",
        verts=[0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0],
        faces=[0, 1, 2],
    )
    moved = Element3D(
        stable_id="1", name="wall", type_name="IfcWall",
        verts=[0.0, 0.0, 1.0, 1.0, 0.0, 1.0, 0.0, 1.0, 1.0],
        faces=[0, 1, 2],
    )
    d = ModelDelta(up_axis=up_axis)
    d.deltas.append(ElementDelta(DiffState.MODIFIED, old=cube, new=moved))
    return d


def _scene_roots(gltf: dict) -> list:
    return [gltf["nodes"][i] for i in gltf["scenes"][0]["nodes"]]


def test_z_up_geometry_is_turned_to_face_the_way_gltf_expects():
    gltf = _gltf_of(build_merged_gltf(_delta("z")))
    roots = _scene_roots(gltf)
    assert len(roots) == 1, "Z-up output should hang under a single orienting root"
    root = roots[0]
    assert "rotation" in root, "the root must carry the Z-up to Y-up rotation"
    x, y, z, w = root["rotation"]
    assert abs(x - QUARTER_TURN_X) < 1e-9 and y == 0.0 and z == 0.0
    assert abs(w - abs(QUARTER_TURN_X)) < 1e-9
    # The three comparison layers must still be reachable, now beneath the root.
    names = {gltf["nodes"][i]["name"] for i in root["children"]}
    assert names == {"old", "new", "difference"}


def test_y_up_geometry_is_left_exactly_as_it_is():
    # glTF and CAD sources are already Y-up. Rotating them would introduce the
    # very fault this fixes, in the sources that never had it.
    gltf = _gltf_of(build_merged_gltf(_delta("y")))
    roots = _scene_roots(gltf)
    assert {r["name"] for r in roots} == {"old", "new", "difference"}
    assert not any("rotation" in r for r in roots)


def test_orienting_does_not_move_a_single_vertex():
    # The correction is a node transform, so the coordinates the matcher compared
    # are byte-identical in both outputs. Anything else risks the geometry tier
    # pairing differently as a side effect of how the file is written out.
    z_up = build_merged_gltf(_delta("z"))
    y_up = build_merged_gltf(_delta("y"))
    json_len_z = struct.unpack("<I", z_up[12:16])[0]
    json_len_y = struct.unpack("<I", y_up[12:16])[0]
    bin_z = z_up[20 + json_len_z:]
    bin_y = y_up[20 + json_len_y:]
    assert bin_z == bin_y, "vertex data must be identical; only the scene graph differs"


def test_the_ifc_loader_declares_z_up_and_the_others_do_not():
    # Pins the source of truth for the above: if a loader's convention changes,
    # this is where it should be noticed.
    assert Model3D(source_format="gltf").up_axis == "y"
    from difference_service.plugins import ifc_objects
    model = ifc_objects.parse_ifc(b"not really an ifc file")
    assert model.up_axis == "z", "IFC is Z-up even when the parse yields nothing"

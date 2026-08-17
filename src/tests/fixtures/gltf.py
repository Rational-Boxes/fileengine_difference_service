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

"""Minimal glTF/GLB fixtures — the *no stable identity* case (M3, spec §5.2).

These exist to exercise the hardest 3D tier. Unlike IFC there is no GlobalId, so
per §5.2 glTF always lands on tier 3, the **true mesh boolean**: correspondence has
to be inferred from geometry, and the plan flags this as expensive and fragile
(DEVELOPMENT_PLAN §4: "STEP/glTF have no stable IDs → always mesh-boolean; defer
past v1 if IFC dominates").

Node *names* are included deliberately as a trap. They look like identity and an
implementation is tempted to match on them, but glTF names are optional,
non-unique, and freely rewritten by exporters — ``renamed_node_pair`` is the
fixture that punishes relying on them.

Self-contained GLB (binary glTF) is produced so a fixture is a single byte string
with its buffer embedded, rather than a JSON file plus sidecars.
"""
from __future__ import annotations

import json
import struct
from typing import List, Tuple

#: A unit triangle, repeated/offset to build the fixtures.
_TRI = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)]


def _mesh_bytes(vertices: List[Tuple[float, float, float]]) -> bytes:
    out = bytearray()
    for x, y, z in vertices:
        out += struct.pack("<fff", x, y, z)
    return bytes(out)


def _glb(nodes: List[dict], meshes: List[List[Tuple[float, float, float]]]) -> bytes:
    """Assemble a valid GLB: JSON chunk + BIN chunk, 4-byte aligned.

    Written by hand for the same reasons as the PDF fixtures — exact control and
    no dependency — but kept spec-correct so a real loader will read it."""
    buffer = bytearray()
    accessors, buffer_views, gltf_meshes = [], [], []

    for verts in meshes:
        data = _mesh_bytes(verts)
        offset = len(buffer)
        buffer += data
        while len(buffer) % 4:                     # accessor alignment
            buffer += b"\x00"
        buffer_views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(data)})
        xs = [v[0] for v in verts]
        ys = [v[1] for v in verts]
        zs = [v[2] for v in verts]
        accessors.append({
            "bufferView": len(buffer_views) - 1, "componentType": 5126,
            "count": len(verts), "type": "VEC3",
            "min": [min(xs), min(ys), min(zs)], "max": [max(xs), max(ys), max(zs)],
        })
        gltf_meshes.append({"primitives": [{"attributes": {"POSITION": len(accessors) - 1}}]})

    gltf = {
        "asset": {"version": "2.0", "generator": "difference_service fixtures"},
        "scene": 0,
        "scenes": [{"nodes": list(range(len(nodes)))}],
        "nodes": nodes,
        "meshes": gltf_meshes,
        "accessors": accessors,
        "bufferViews": buffer_views,
        "buffers": [{"byteLength": len(buffer)}],
    }

    json_chunk = json.dumps(gltf, separators=(",", ":"), sort_keys=True).encode("utf-8")
    while len(json_chunk) % 4:
        json_chunk += b" "
    bin_chunk = bytes(buffer)
    while len(bin_chunk) % 4:
        bin_chunk += b"\x00"

    total = 12 + 8 + len(json_chunk) + 8 + len(bin_chunk)
    out = bytearray(b"glTF")
    out += struct.pack("<II", 2, total)
    out += struct.pack("<I", len(json_chunk)) + b"JSON" + json_chunk
    out += struct.pack("<I", len(bin_chunk)) + b"BIN\x00" + bin_chunk
    return bytes(out)


def _offset(verts, dx=0.0, dy=0.0, dz=0.0):
    return [(x + dx, y + dy, z + dz) for x, y, z in verts]


def _scaled(verts, k):
    return [(x * k, y * k, z * k) for x, y, z in verts]


# --------------------------------------------------------------------------

def unchanged_pair() -> Tuple[bytes, bytes]:
    """Identical models. Ground truth: nothing changed — the boolean difference
    volume is empty."""
    nodes = [{"name": "tri", "mesh": 0}]
    return _glb(nodes, [_TRI]), _glb(nodes, [_TRI])


def added_mesh_pair() -> Tuple[bytes, bytes]:
    """After has one extra, spatially separate mesh.

    Ground truth: the new mesh is entirely added. With no ids this must be derived
    geometrically — the added volume is exactly the new triangle."""
    before = _glb([{"name": "tri", "mesh": 0}], [_TRI])
    after = _glb([{"name": "tri", "mesh": 0}, {"name": "tri2", "mesh": 1}],
                 [_TRI, _offset(_TRI, dx=10.0)])
    return before, after


def deleted_mesh_pair() -> Tuple[bytes, bytes]:
    """The reverse of ``added_mesh_pair``. Ground truth: that mesh is deleted."""
    after, before = added_mesh_pair()
    return before, after


def moved_mesh_pair() -> Tuple[bytes, bytes]:
    """One mesh translated by a small amount.

    Ground truth: a geometry delta. Without identity this necessarily reads as a
    deleted volume at the old position plus an added volume at the new one — the
    honest tier-3 answer, and worth pinning so nobody "fixes" it into a move by
    matching on node name."""
    nodes = [{"name": "tri", "mesh": 0}]
    return _glb(nodes, [_TRI]), _glb(nodes, [_offset(_TRI, dx=0.5)])


def scaled_mesh_pair() -> Tuple[bytes, bytes]:
    """One mesh scaled up in place.

    Ground truth: a partial overlap — the difference volume is the shell between
    the two sizes, not the whole object. The case that distinguishes a real
    boolean from a naive "any vertex changed ⇒ all changed"."""
    nodes = [{"name": "tri", "mesh": 0}]
    return _glb(nodes, [_TRI]), _glb(nodes, [_scaled(_TRI, 2.0)])


def renamed_node_pair() -> Tuple[bytes, bytes]:
    """Identical geometry; the node NAME changed.

    Ground truth: nothing changed. glTF names are optional, non-unique and rewritten
    freely by exporters, so they are not identity — a matcher keying on them
    reports a phantom delete+add here. The trap fixture."""
    return (_glb([{"name": "wall_01", "mesh": 0}], [_TRI]),
            _glb([{"name": "Wall.001", "mesh": 0}], [_TRI]))


def reordered_nodes_pair() -> Tuple[bytes, bytes]:
    """Same two meshes, emitted in the opposite order.

    Ground truth: nothing changed. Node index is not identity either; a positional
    matcher reports both nodes modified."""
    a, b = _TRI, _offset(_TRI, dx=10.0)
    return (_glb([{"name": "one", "mesh": 0}, {"name": "two", "mesh": 1}], [a, b]),
            _glb([{"name": "two", "mesh": 0}, {"name": "one", "mesh": 1}], [b, a]))


#: Every pair, keyed by name, with its ground truth.
PAIRS = {
    "unchanged": (unchanged_pair, "nothing changed; empty difference volume"),
    "added_mesh": (added_mesh_pair, "one mesh added"),
    "deleted_mesh": (deleted_mesh_pair, "one mesh deleted"),
    "moved_mesh": (moved_mesh_pair, "translated => deleted volume + added volume"),
    "scaled_mesh": (scaled_mesh_pair, "scaled => partial overlap, shell is the delta"),
    "renamed_node": (renamed_node_pair, "name changed only; nothing changed"),
    "reordered_nodes": (reordered_nodes_pair, "node order differs; nothing changed"),
}

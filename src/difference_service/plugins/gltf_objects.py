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

"""glTF / GLB loader (SPECIFICATION.md §5.2, tier 3).

glTF carries **no durable element identity**, which is the whole reason it lands on
the geometry-matching tier. Two things in the format look like identity and are
not, and both are load-bearing mistakes:

  * **Node names** are optional, non-unique, and rewritten freely by exporters
    (``wall_01`` becomes ``Wall.001`` on a round-trip). Matching on them invents a
    delete plus an add on every re-export.
  * **Node indices** are file-local ordering, so a reordered export would read as
    a wholly rewritten model.

So names are carried for display only, never into ``stable_id``, and geometry is
baked to **world space** by composing each node's transform down the scene graph —
without that, a model whose objects were repositioned purely by node transforms
would compare byte-identical.

Parsed with the standard library alone (GLB is a small container and glTF is JSON),
so no additional dependency is needed for the common case.
"""
from __future__ import annotations

import base64
import json
import logging
import struct
from typing import Dict, List, Optional, Sequence, Tuple

from .model3d import Element3D, Model3D

log = logging.getLogger("difference_service.plugins.gltf_objects")

#: glTF componentType -> (struct format, byte size)
_COMPONENT = {
    5120: ("b", 1), 5121: ("B", 1), 5122: ("h", 2),
    5123: ("H", 2), 5125: ("I", 4), 5126: ("f", 4),
}

#: glTF accessor type -> component count
_TYPE_COUNT = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4,
               "MAT2": 4, "MAT3": 9, "MAT4": 16}

Matrix4 = Tuple[float, ...]

_IDENTITY4: Matrix4 = (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1)


def _mat_mul(a: Matrix4, b: Matrix4) -> Matrix4:
    """Column-major 4x4 multiply, matching glTF's convention."""
    out = [0.0] * 16
    for col in range(4):
        for row in range(4):
            out[col * 4 + row] = sum(a[k * 4 + row] * b[col * 4 + k] for k in range(4))
    return tuple(out)


def _trs_matrix(node: dict) -> Matrix4:
    """A node's local transform: an explicit matrix, or composed T·R·S."""
    if "matrix" in node:
        return tuple(float(v) for v in node["matrix"])

    tx, ty, tz = node.get("translation", (0.0, 0.0, 0.0))
    qx, qy, qz, qw = node.get("rotation", (0.0, 0.0, 0.0, 1.0))
    sx, sy, sz = node.get("scale", (1.0, 1.0, 1.0))

    # Quaternion -> rotation matrix (column-major).
    r = (
        1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy + qz * qw), 2 * (qx * qz - qy * qw), 0,
        2 * (qx * qy - qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz + qx * qw), 0,
        2 * (qx * qz + qy * qw), 2 * (qy * qz - qx * qw), 1 - 2 * (qx * qx + qy * qy), 0,
        0, 0, 0, 1,
    )
    s = (sx, 0, 0, 0, 0, sy, 0, 0, 0, 0, sz, 0, 0, 0, 0, 1)
    t = (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, tx, ty, tz, 1)
    return _mat_mul(t, _mat_mul(r, s))


def _apply(m: Matrix4, x: float, y: float, z: float) -> Tuple[float, float, float]:
    return (m[0] * x + m[4] * y + m[8] * z + m[12],
            m[1] * x + m[5] * y + m[9] * z + m[13],
            m[2] * x + m[6] * y + m[10] * z + m[14])


# ------------------------------------------------------------------ container

def _split_glb(data: bytes) -> Tuple[Optional[dict], bytes]:
    """``(json, binary_chunk)`` from a GLB, or ``(None, b"")`` if not a GLB."""
    if len(data) < 12 or data[:4] != b"glTF":
        return None, b""
    _version, total = struct.unpack("<II", data[4:12])
    doc: Optional[dict] = None
    binary = b""
    offset = 12
    while offset + 8 <= min(total, len(data)):
        length, kind = struct.unpack("<I", data[offset:offset + 4])[0], data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + length]
        if kind == b"JSON":
            try:
                doc = json.loads(payload.decode("utf-8"))
            except Exception:
                return None, b""
        elif kind.startswith(b"BIN"):
            binary = payload
        offset += 8 + length
    return doc, binary


def _load_document(data: bytes) -> Tuple[Optional[dict], bytes]:
    doc, binary = _split_glb(data)
    if doc is not None:
        return doc, binary
    try:                                   # plain .gltf JSON
        return json.loads(data.decode("utf-8")), b""
    except Exception:
        return None, b""


def _buffer_bytes(doc: dict, binary: bytes, index: int) -> bytes:
    """Resolve buffer ``index``. Only embedded buffers are supported — an external
    ``.bin`` URI cannot be fetched from inside a version blob, and guessing would
    silently produce an empty model."""
    buffers = doc.get("buffers", [])
    if index >= len(buffers):
        return b""
    uri = buffers[index].get("uri")
    if uri is None:
        return binary                      # the GLB BIN chunk
    if uri.startswith("data:"):
        _head, _sep, b64 = uri.partition(",")
        try:
            return base64.b64decode(b64)
        except Exception:
            return b""
    return b""


def _read_accessor(doc: dict, binary: bytes, index: int) -> List[float]:
    """Flat values of an accessor, honouring byteStride."""
    try:
        accessor = doc["accessors"][index]
    except (KeyError, IndexError, TypeError):
        return []
    count = int(accessor.get("count", 0))
    fmt, size = _COMPONENT.get(int(accessor.get("componentType", 0)), (None, 0))
    n_comp = _TYPE_COUNT.get(accessor.get("type", ""), 0)
    if not fmt or not n_comp or not count:
        return []

    view_index = accessor.get("bufferView")
    if view_index is None:
        return [0.0] * (count * n_comp)          # sparse/zero-filled
    view = doc.get("bufferViews", [])[view_index]
    buf = _buffer_bytes(doc, binary, int(view.get("buffer", 0)))
    base = int(view.get("byteOffset", 0)) + int(accessor.get("byteOffset", 0))
    stride = int(view.get("byteStride", 0)) or size * n_comp

    out: List[float] = []
    for i in range(count):
        start = base + i * stride
        chunk = buf[start:start + size * n_comp]
        if len(chunk) < size * n_comp:
            break
        out.extend(struct.unpack("<" + fmt * n_comp, chunk))
    return out


# --------------------------------------------------------------------- loader

def parse_gltf(data: bytes, source_format: str = "gltf") -> Model3D:
    """Parse glTF/GLB into the shared model. Never raises."""
    model = Model3D(source_format=source_format)
    doc, binary = _load_document(data)
    if not doc:
        model.problems.append("unreadable-gltf")
        return model

    nodes = doc.get("nodes", []) or []
    meshes = doc.get("meshes", []) or []

    # Walk the scene graph so each node's world transform is the composition of
    # its ancestors'. Objects repositioned purely by transforms would otherwise
    # compare identical.
    scene_index = int(doc.get("scene", 0) or 0)
    scenes = doc.get("scenes", []) or [{}]
    roots = scenes[scene_index].get("nodes", list(range(len(nodes)))) \
        if scene_index < len(scenes) else list(range(len(nodes)))

    stack: List[Tuple[int, Matrix4]] = [(int(i), _IDENTITY4) for i in roots]
    seen = set()
    while stack:
        node_index, parent = stack.pop()
        if node_index in seen or node_index >= len(nodes):
            continue
        seen.add(node_index)
        node = nodes[node_index] or {}
        world = _mat_mul(parent, _trs_matrix(node))

        for child in node.get("children", []) or []:
            stack.append((int(child), world))

        mesh_index = node.get("mesh")
        if mesh_index is None or mesh_index >= len(meshes):
            continue

        verts: List[float] = []
        faces: List[int] = []
        for primitive in (meshes[mesh_index].get("primitives") or []):
            position = (primitive.get("attributes") or {}).get("POSITION")
            if position is None:
                continue
            flat = _read_accessor(doc, binary, int(position))
            offset = len(verts) // 3
            for i in range(0, len(flat) - 2, 3):
                x, y, z = _apply(world, flat[i], flat[i + 1], flat[i + 2])
                verts.extend((x, y, z))
            indices = primitive.get("indices")
            if indices is not None:
                faces.extend(int(v) + offset for v in _read_accessor(doc, binary, int(indices)))
            else:
                faces.extend(range(offset, offset + len(flat) // 3))

        if not verts:
            continue

        model.elements.append(Element3D(
            # stable_id stays EMPTY on purpose: glTF has no durable identity, and
            # the node name only looks like one. Leaving it empty is what routes
            # this model to geometry matching instead of a false tier 1.
            stable_id="",
            local_id=f"node-{node_index}",
            name=str(node.get("name", "") or ""),
            type_name="mesh",
            verts=tuple(verts), faces=tuple(faces),
        ))

    if not model.elements:
        model.problems.append("no-geometry")
    return model

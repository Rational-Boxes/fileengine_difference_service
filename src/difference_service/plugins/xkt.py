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

"""Build the diff XKT: old / new / difference layers (SPECIFICATION.md §5.2, §7.3).

The spec is emphatic that **the existing Xeokit viewer is reused unchanged** — the
service's only job is to organize the comparison into three top-level groups the
viewer's stock show/hide/x-ray controls can already drive. So the output is
deliberately boring: one XKT whose object tree has exactly ``old``, ``new`` and
``difference`` at the top, plus a MetaModel describing that tree and each element's
state.

How it is assembled:

1. A **merged glTF** is generated holding both versions' geometry, with every
   element parented under one of the three groups. Element geometry is already in
   world coordinates by the time it reaches here (the loaders' responsibility), so
   the two versions coexist in one space without re-transforming anything.
2. ``convert2xkt`` turns that into the XKT.
3. A **MetaModel JSON** carries the tree and per-element ``state`` /
   ``change`` so the viewer can colour by state and a properties panel can show
   *why* something is orange — including the property-only changes that carry no
   visual delta at all (§5.2).

The ``difference`` group holds only elements with a **visual** delta. A
property-only change deliberately does not appear there: it is recorded in the
MetaModel and surfaced on selection, because painting geometry that looks
identical teaches reviewers to distrust the colour.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import struct
import subprocess
import tempfile
from typing import Dict, List, Optional, Sequence, Tuple

from .base import DiffState
from .model3d import Change, ElementDelta, ModelDelta

log = logging.getLogger("difference_service.plugins.xkt")

#: The three top-level groups §7.3 requires.
GROUP_OLD = "old"
GROUP_NEW = "new"
GROUP_DIFFERENCE = "difference"


def convert2xkt_available(path: str = "convert2xkt") -> bool:
    return shutil.which(path) is not None


# ------------------------------------------------------------- merged glTF

def _mesh_arrays(verts: Sequence[float], faces: Sequence[int]) -> Tuple[bytes, bytes, int, int]:
    """``(position_bytes, index_bytes, vertex_count, index_count)``."""
    pos = b"".join(struct.pack("<f", float(v)) for v in verts)
    idx = b"".join(struct.pack("<I", int(i)) for i in faces)
    return pos, idx, len(verts) // 3, len(faces)


def build_merged_gltf(delta: ModelDelta) -> Optional[bytes]:
    """One GLB holding both versions, grouped old / new / difference.

    Returns ``None`` when there is no geometry to place — an empty model would
    convert to an empty XKT that looks like "no differences" rather than
    "nothing could be built"."""
    buffer = bytearray()
    accessors: List[dict] = []
    views: List[dict] = []
    meshes: List[dict] = []
    nodes: List[dict] = []

    group_children: Dict[str, List[int]] = {
        GROUP_OLD: [], GROUP_NEW: [], GROUP_DIFFERENCE: []}

    def add_mesh(element, node_name: str) -> Optional[int]:
        if not element or not element.verts:
            return None
        pos, idx, v_count, i_count = _mesh_arrays(element.verts, element.faces)
        if not v_count:
            return None

        # POSITION
        offset = len(buffer)
        buffer.extend(pos)
        while len(buffer) % 4:
            buffer.append(0)
        views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(pos)})
        xs = element.verts[0::3]; ys = element.verts[1::3]; zs = element.verts[2::3]
        accessors.append({
            "bufferView": len(views) - 1, "componentType": 5126, "count": v_count,
            "type": "VEC3",
            "min": [min(xs), min(ys), min(zs)], "max": [max(xs), max(ys), max(zs)],
        })
        position_accessor = len(accessors) - 1

        primitive = {"attributes": {"POSITION": position_accessor}}
        if i_count:
            offset = len(buffer)
            buffer.extend(idx)
            while len(buffer) % 4:
                buffer.append(0)
            views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(idx)})
            accessors.append({"bufferView": len(views) - 1, "componentType": 5125,
                              "count": i_count, "type": "SCALAR"})
            primitive["indices"] = len(accessors) - 1

        meshes.append({"primitives": [primitive]})
        nodes.append({"name": node_name, "mesh": len(meshes) - 1})
        return len(nodes) - 1

    # Every element appears in the layer(s) it belongs to. An unchanged element is
    # in both old and new so either view is a complete model — a "before" view
    # showing only deletions would be useless.
    for i, d in enumerate(delta.deltas):
        eid = _element_id(d, i)
        if d.old is not None:
            node = add_mesh(d.old, f"{eid}|old")
            if node is not None:
                group_children[GROUP_OLD].append(node)
        if d.new is not None:
            node = add_mesh(d.new, f"{eid}|new")
            if node is not None:
                group_children[GROUP_NEW].append(node)
        if d.has_visual_delta:
            source = d.new if d.new is not None else d.old
            node = add_mesh(source, f"{eid}|diff")
            if node is not None:
                group_children[GROUP_DIFFERENCE].append(node)

    if not nodes:
        return None

    group_nodes = []
    for group in (GROUP_OLD, GROUP_NEW, GROUP_DIFFERENCE):
        nodes.append({"name": group, "children": group_children[group]})
        group_nodes.append(len(nodes) - 1)

    gltf = {
        "asset": {"version": "2.0", "generator": "difference_service"},
        "scene": 0,
        "scenes": [{"nodes": group_nodes}],
        "nodes": nodes,
        "meshes": meshes,
        "accessors": accessors,
        "bufferViews": views,
        "buffers": [{"byteLength": len(buffer)}],
    }
    return _pack_glb(gltf, bytes(buffer))


def _pack_glb(gltf: dict, buffer: bytes) -> bytes:
    json_chunk = json.dumps(gltf, separators=(",", ":"), sort_keys=True).encode("utf-8")
    while len(json_chunk) % 4:
        json_chunk += b" "
    bin_chunk = buffer
    while len(bin_chunk) % 4:
        bin_chunk += b"\x00"
    total = 12 + 8 + len(json_chunk) + 8 + len(bin_chunk)
    out = bytearray(b"glTF")
    out += struct.pack("<II", 2, total)
    out += struct.pack("<I", len(json_chunk)) + b"JSON" + json_chunk
    out += struct.pack("<I", len(bin_chunk)) + b"BIN\x00" + bin_chunk
    return bytes(out)


def _element_id(d: ElementDelta, index: int) -> str:
    element = d.element
    if element is None:
        return f"e{index}"
    return element.stable_id or element.local_id or f"e{index}"


# ------------------------------------------------------------- MetaModel

def build_metamodel(delta: ModelDelta, *, project_id: str = "diff") -> dict:
    """A xeokit MetaModel describing the three groups and per-element state.

    This is what lets the stock viewer colour by state and show *why* an element
    changed. Property-only changes appear here with ``change: "property"`` and no
    presence in the difference group — visible on selection, invisible on the
    model, exactly as §5.2 requires."""
    objects: List[dict] = [
        {"id": project_id, "name": "Difference", "type": "Project", "parent": None},
    ]
    for group in (GROUP_OLD, GROUP_NEW, GROUP_DIFFERENCE):
        objects.append({"id": group, "name": group, "type": "Layer", "parent": project_id})

    for i, d in enumerate(delta.deltas):
        eid = _element_id(d, i)
        element = d.element
        base = {
            "name": (element.name if element else "") or eid,
            "type": (element.type_name if element else "") or "Mesh",
            "state": d.state,
            "change": d.change or "",
            "changedProperties": list(d.changed_properties),
        }
        if d.old is not None:
            objects.append({**base, "id": f"{eid}|old", "parent": GROUP_OLD})
        if d.new is not None:
            objects.append({**base, "id": f"{eid}|new", "parent": GROUP_NEW})
        if d.has_visual_delta:
            objects.append({**base, "id": f"{eid}|diff", "parent": GROUP_DIFFERENCE})

    return {
        "id": project_id,
        "projectId": project_id,
        "author": "difference_service",
        "metaObjects": objects,
        # Not part of the xeokit schema, but harmless to carry and far easier than
        # recomputing totals in the viewer.
        "diffSummary": {
            "tier": delta.tier,
            "confidence": delta.confidence,
            "added": delta.count(DiffState.ADDED),
            "deleted": delta.count(DiffState.DELETED),
            "modified": delta.count(DiffState.MODIFIED),
            "unchanged": delta.count(DiffState.UNCHANGED),
            "geometryChanges": delta.count_change(Change.GEOMETRY),
            "propertyChanges": delta.count_change(Change.PROPERTY),
        },
    }


# ------------------------------------------------------------- conversion

def gltf_to_xkt(glb: bytes, metamodel: Optional[dict] = None, *,
                binary: str = "convert2xkt", timeout_s: int = 300) -> Optional[bytes]:
    """Run ``convert2xkt`` over a merged GLB. ``None`` if unavailable or it fails."""
    if not convert2xkt_available(binary) or not glb:
        return None

    with tempfile.TemporaryDirectory(prefix="diffsvc-xkt-") as tmp:
        src = os.path.join(tmp, "merged.glb")
        out = os.path.join(tmp, "merged.xkt")
        with open(src, "wb") as fh:
            fh.write(glb)
        cmd = [binary, "-s", src, "-o", out, "-f", "gltf"]
        if metamodel is not None:
            meta_path = os.path.join(tmp, "metamodel.json")
            with open(meta_path, "w") as fh:
                json.dump(metamodel, fh)
            cmd += ["-m", meta_path]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout_s, cwd=tmp)
        except (subprocess.TimeoutExpired, OSError):
            log.warning("convert2xkt failed", exc_info=True)
            return None
        if not os.path.isfile(out):
            log.info("convert2xkt produced no output: %s",
                     (proc.stderr or proc.stdout or b"")[:300])
            return None
        with open(out, "rb") as fh:
            return fh.read()

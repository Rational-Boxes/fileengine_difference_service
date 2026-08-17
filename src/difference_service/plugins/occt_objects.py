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

"""CAD loader via OpenCASCADE (SPECIFICATION.md §5.2, tier 3).

Brings the boundary-representation CAD formats — STEP, IGES, BREP, OBJ, STL — into
the same comparison as everything else by tessellating them to glTF with
OpenCASCADE's ``DRAWEXE`` and then reusing the glTF loader. One conversion hop buys
every one of these formats at once, and their diff behaviour is identical to
glTF's by construction rather than by a parallel implementation that could drift.

These formats carry **no durable element identity** (STEP entity numbers are
file-local, as the ``ifc.reordered_entities`` fixture demonstrates for the same
Part-21 syntax), so they land on geometry matching. The plan is explicit that this
is the heaviest, most fragile path — hence the strict fail-soft posture here: a
missing binary, an unreadable file or a timeout yields an empty model with the
reason recorded, never an exception.

The mesh deflection is the accuracy/size trade-off. It is deliberately fixed
rather than adaptive: the geometry hash compares tessellated vertices, so two
versions must be tessellated *identically* or every element would appear modified
purely because the mesher chose different triangles.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from typing import Dict, Optional

from .gltf_objects import parse_gltf
from .model3d import Model3D

log = logging.getLogger("difference_service.plugins.occt_objects")

#: Linear deflection for tessellation. MUST be constant across versions — an
#: adaptive value would re-triangulate identical geometry differently between two
#: versions and report the whole model as modified.
MESH_DEFLECTION = 0.1

#: Canonical format tag -> the DRAWEXE reader command.
READERS: Dict[str, str] = {
    "step": "stepread",
    "iges": "igesread",
    "brep": "readbrep",
    "obj": "readobj",
    "stl": "readstl",
}

#: Extension aliases, normalised to the canonical tag. Worth doing explicitly:
#: the tag reaches the manifest and the logs, so ``.stp`` and ``.step`` must not
#: describe the same file two different ways.
ALIASES: Dict[str, str] = {
    "stp": "step", "step": "step",
    "igs": "iges", "iges": "iges",
    "brep": "brep", "obj": "obj", "stl": "stl",
}

MIME_FORMATS: Dict[str, str] = {
    "application/step": "step",
    "application/x-step": "step",
    "model/step": "step",
    "application/iges": "iges",
    "model/iges": "iges",
    "application/x-brep": "brep",
    "model/obj": "obj",
    "text/x-obj": "obj",
    "model/stl": "stl",
    "application/sla": "stl",
}


def format_for(mime: str, name: str = "") -> str:
    """Resolve a source format tag from MIME or file name, or ``""``."""
    key = (mime or "").split(";")[0].strip().lower()
    if key in MIME_FORMATS:
        return MIME_FORMATS[key]
    ext = (name or "").rsplit(".", 1)[-1].lower() if "." in (name or "") else ""
    return ALIASES.get(ext, "")


def available() -> bool:
    return shutil.which("DRAWEXE") is not None


def _script(reader: str, src: str, out: str) -> str:
    """A DRAWEXE script: read → tessellate → write self-contained GLB.

    ``.glb`` rather than ``.gltf`` on purpose — the JSON flavour emits a separate
    ``.bin`` sidecar, and a loader reading a version blob has nowhere to fetch it
    from, so the model would silently come back empty."""
    if reader == "stepread":
        load = f'stepread "{src}" s *\nrenamevar s_1 shape'
    elif reader == "igesread":
        load = f'igesread "{src}" s *\nrenamevar s_1 shape'
    elif reader == "readbrep":
        load = f'readbrep "{src}" shape'
    elif reader == "readobj":
        load = f'readobj shape "{src}"'
    elif reader == "readstl":
        load = f'readstl shape "{src}"'
    else:
        load = f'{reader} "{src}" shape'
    return (f"pload ALL\n{load}\n"
            f"incmesh shape {MESH_DEFLECTION}\n"
            f'writegltf shape "{out}"\n')


def to_glb(data: bytes, source_format: str, *, timeout_s: int = 120) -> Optional[bytes]:
    """Tessellate a CAD file to self-contained GLB, or ``None``."""
    reader = READERS.get(source_format)
    if reader is None or not available() or not data:
        return None

    with tempfile.TemporaryDirectory(prefix="diffsvc-occt-") as tmp:
        src = os.path.join(tmp, f"in.{source_format}")
        out = os.path.join(tmp, "out.glb")
        script = os.path.join(tmp, "run.tcl")
        with open(src, "wb") as fh:
            fh.write(data)
        with open(script, "w") as fh:
            fh.write(_script(reader, src, out))
        try:
            proc = subprocess.run(["DRAWEXE", "-f", script],
                                  capture_output=True, timeout=timeout_s, cwd=tmp)
        except (subprocess.TimeoutExpired, OSError):
            log.warning("DRAWEXE failed for %s", source_format, exc_info=True)
            return None
        if not os.path.isfile(out):
            log.info("DRAWEXE produced no output for %s: %s",
                     source_format, (proc.stderr or proc.stdout or b"")[:200])
            return None
        with open(out, "rb") as fh:
            return fh.read()


def parse_cad(data: bytes, source_format: str) -> Model3D:
    """Load a CAD file into the shared model via tessellation. Never raises."""
    model = Model3D(source_format=source_format)
    if not available():
        model.problems.append("drawexe-missing")
        return model
    glb = to_glb(data, source_format)
    if not glb:
        model.problems.append(f"tessellation-failed:{source_format}")
        return model
    out = parse_gltf(glb, source_format=source_format)
    # Keep the ORIGINAL format on the model: the diff behaviour is glTF's, but the
    # manifest and logs should say what the user actually uploaded.
    out.source_format = source_format
    return out

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

"""B-rep CAD fixtures — STEP / IGES / BREP pairs (M3 tier 3).

**Unlike the other fixture modules these need a toolchain**: STEP solids are
boundary representations with hundreds of interdependent entities, so hand-writing
them the way ``ifc.py`` and ``pdf.py`` do is not practical. They are generated with
OpenCASCADE's ``DRAWEXE`` instead, and every builder returns ``None`` when it is
absent so the suite skips rather than fails.

The trade-off is deliberate and worth stating: these fixtures are less reviewable
than the hand-written ones and their exact bytes depend on the OCCT version. They
therefore assert *structural* ground truth — a solid added, a solid resized — not
byte-level expectations.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Optional, Tuple


def available() -> bool:
    return shutil.which("DRAWEXE") is not None


def _build(script: str, outputs) -> Optional[Tuple[bytes, ...]]:
    """Run a DRAWEXE script in a temp dir and read back ``outputs``."""
    if not available():
        return None
    with tempfile.TemporaryDirectory(prefix="diffsvc-fixture-") as tmp:
        path = os.path.join(tmp, "make.tcl")
        with open(path, "w") as fh:
            fh.write("pload ALL\n" + script)
        try:
            subprocess.run(["DRAWEXE", "-f", path], capture_output=True,
                           timeout=120, cwd=tmp)
        except (subprocess.TimeoutExpired, OSError):
            return None
        out = []
        for name in outputs:
            full = os.path.join(tmp, name)
            if not os.path.isfile(full):
                return None
            with open(full, "rb") as fh:
                out.append(fh.read())
        return tuple(out)


def _step_pair(before: str, after: str) -> Optional[Tuple[bytes, bytes]]:
    """Build each side in its OWN DRAWEXE run.

    Not a stylistic choice: ``stepwrite`` accumulates into the session's model, so
    writing both sides in one script silently puts the first shape into the second
    file too ("Model not empty before transferring", and an entity count that
    quietly doubles). The "unchanged" pair would then differ, and every fixture
    built this way would be measuring something other than what it claims."""
    first = _build(f"{before}\nstepwrite a shp out.step\n", ("out.step",))
    if first is None:
        return None
    second = _build(f"{after}\nstepwrite a shp2 out.step\n", ("out.step",))
    if second is None:
        return None
    return (first[0], second[0])


# --------------------------------------------------------------------------

def unchanged_pair() -> Optional[Tuple[bytes, bytes]]:
    """The same solid written twice. Ground truth: nothing changed.

    Sharper than it looks — it proves the tessellation is *deterministic*. If OCCT
    meshed the same solid differently on two runs, every element would compare
    modified and no CAD diff would ever be usable."""
    return _step_pair("box shp 10 20 30", "box shp2 10 20 30")


# NOTE: there is deliberately no multi-solid ("added solid") STEP fixture.
# Every way of building one in DRAWEXE's script mode — `compound`, `bfuse` — exits
# without writing a file on this OCCT build, and shipping a fixture that silently
# fails to generate is worse than not having one. Nothing is actually lost in
# coverage: a CAD file reaches tier 3 by being tessellated to glTF, so
# `gltf.added_mesh_pair` already exercises the identical matching path. What these
# STEP fixtures uniquely prove is that the OCCT *conversion* is faithful and
# deterministic, which the remaining three do.


def resized_solid_pair() -> Optional[Tuple[bytes, bytes]]:
    """The solid's dimensions change. Ground truth: a geometry delta.

    With no stable ids this reads as removed + added volume, which is the honest
    tier-3 answer (§5.2) — there is nothing tying the two boxes together."""
    return _step_pair("box shp 10 20 30", "box shp2 10 20 60")


def moved_solid_pair() -> Optional[Tuple[bytes, bytes]]:
    """The same solid translated. Ground truth: removed + added volume."""
    return _step_pair("box shp 10 20 30", "box shp2 100 0 0 10 20 30")


#: Builders return ``None`` when DRAWEXE is unavailable — callers skip.
PAIRS = {
    "unchanged": (unchanged_pair, "same solid; tessellation must be deterministic"),
    "resized_solid": (resized_solid_pair, "dimensions changed => volume delta"),
    "moved_solid": (moved_solid_pair, "translated => removed + added volume"),
}

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

"""The 3D diff plugin (SPECIFICATION.md §5.2, §7.3).

Handles **every supported 3D format** through one path: a per-format loader
normalizes to the shared ``Model3D``, one matcher compares them, and one builder
emits the XKT. Adding a format is adding a loader — the diff, the states, the
output and the manifest are unchanged by construction.

    IFC          -> ifcopenshell        -> stable ids  -> tier 1
    glTF / GLB   -> native parser       -> geometry    -> tier 3
    STEP, IGES,  -> OpenCASCADE ->glTF  -> geometry    -> tier 3
    BREP, OBJ, STL

The tier is chosen by **what identity the data carries**, never by extension: an
IFC that fails to tessellate falls to geometry matching just as glTF does, and a
future format that can recover durable ids gets tier 1 for free.

Output is a single XKT whose object tree has ``old`` / ``new`` / ``difference`` at
the top, plus a MetaModel sidecar carrying each element's state and change kind.
The existing Xeokit viewer is reused unchanged — its stock show/hide/x-ray drives
the three views (§7.3).
"""
from __future__ import annotations

import json
import logging
from typing import List, Optional

from .base import (
    DiffChild, DiffMode, DiffPlugin, DiffResult, SourceRef,
)
from .model3d import Model3D, ModelDelta, match_models
from .xkt import build_merged_gltf, build_metamodel, gltf_to_xkt, gltf_to_xkt_file

log = logging.getLogger("difference_service.plugins.three_d")

#: MIME types handled, mapped to the loader that understands them.
IFC_MIMES = ("application/x-ifc", "application/ifc", "model/ifc")
GLTF_MIMES = ("model/gltf+json", "model/gltf-binary", "application/gltf",
              "model/gltf")


class ThreeDDiffPlugin(DiffPlugin):
    """Version comparison for BIM/CAD/mesh models."""

    name = "3d"
    #: Part of the cache key (§6) — bump when output would change.
    version = 1

    def __init__(self, *, config=None, convert2xkt: str = ""):
        self.config = config
        self.convert2xkt = (convert2xkt
                            or getattr(config, "convert2xkt_path", "")
                            or "convert2xkt")

    # ------------------------------------------------------------- dispatch
    def supports(self, mime: str) -> bool:
        return self._format_for(mime, "") != ""

    def _format_for(self, mime: str, name: str) -> str:
        from .occt_objects import format_for as cad_format

        key = (mime or "").split(";")[0].strip().lower()
        if key in IFC_MIMES:
            return "ifc"
        if key in GLTF_MIMES:
            return "gltf"
        cad = cad_format(key, name)
        if cad:
            return cad
        # Extension fallback: 3D MIME types are poorly standardised and frequently
        # arrive as application/octet-stream, so the name is often the only signal.
        ext = (name or "").rsplit(".", 1)[-1].lower() if "." in (name or "") else ""
        if ext in ("ifc", "ifczip"):
            return "ifc"
        if ext in ("gltf", "glb"):
            return "gltf"
        return cad_format("", name)

    # ----------------------------------------------------------------- load
    def _load(self, ref: SourceRef, source_format: str) -> Model3D:
        """Load one side into the shared model. Never raises."""
        try:
            if source_format == "ifc":
                from .ifc_objects import parse_ifc
                return parse_ifc(ref.data)
            if source_format == "gltf":
                from .gltf_objects import parse_gltf
                return parse_gltf(ref.data)
            from .occt_objects import parse_cad
            return parse_cad(ref.data, source_format)
        except Exception as e:
            log.warning("3d: loader for %s raised", source_format, exc_info=True)
            model = Model3D(source_format=source_format)
            model.problems.append(f"loader-error:{type(e).__name__}")
            return model

    # ------------------------------------------------------------------ diff
    def diff(self, base: SourceRef, target: SourceRef) -> DiffResult:
        source_format = (self._format_for(target.mime, target.name)
                         or self._format_for(base.mime, base.name))
        if not source_format:
            return DiffResult.failed("dispatch", "unrecognised 3D format",
                                     tiers=[DiffMode.XKT])

        old = self._load(base, source_format)
        new = self._load(target, source_format)
        if not old.elements and not new.elements:
            return DiffResult.failed(
                "parse",
                f"no geometry could be read from either version ({source_format}): "
                + ",".join(sorted(set(old.problems) | set(new.problems))[:4]),
                tiers=[DiffMode.XKT])

        delta = match_models(old, new)
        log.info("3d: %s tier=%s +%d -%d ~%d =%d (confidence %.2f)",
                 source_format, delta.tier,
                 delta.count("added"), delta.count("deleted"),
                 delta.count("modified"), delta.count("unchanged"),
                 delta.confidence)

        merged = build_merged_gltf(delta)
        if merged is None:
            return DiffResult.failed("build", "no geometry to place in the model",
                                     tiers=[DiffMode.XKT])
        metamodel = build_metamodel(delta)

        children: List[DiffChild] = []
        # File-backed: the XKT is the largest thing produced here, and the
        # writer streams it (plugins.base.DiffChild).
        kept = gltf_to_xkt_file(merged, metamodel, binary=self.convert2xkt)
        if kept:
            xkt_path, cleanup = kept
            children.append(DiffChild.from_path("model", 0, xkt_path,
                                                "application/octet-stream", "xkt",
                                                mode=DiffMode.XKT, cleanup=cleanup))
        else:
            # convert2xkt is a Node tool and may simply not be installed. The
            # merged glTF is still a complete, viewable three-group model, so
            # shipping it beats failing — the viewer needs a different loader, and
            # the manifest's mode says which.
            log.info("3d: convert2xkt unavailable; delivering the merged glTF")
            children.append(DiffChild(kind="model", index=0, data=merged,
                                      mime="model/gltf-binary", ext="glb",
                                      mode=DiffMode.XKT))

        # The MetaModel travels WITH the model: without it the viewer has geometry
        # but no states, and every element would render the same colour.
        children.append(DiffChild(
            kind="metamodel", index=1,
            data=json.dumps(metamodel, separators=(",", ":"), sort_keys=True).encode("utf-8"),
            mime="application/json", ext="json", mode=DiffMode.XKT))

        return DiffResult(children=children)

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

"""Extract elements from an IFC model (SPECIFICATION.md §5.2, tier 1).

Unlike PDF, IFC *has* stable identity: the **GlobalId**. Tier-1 matching is
therefore a lookup rather than an inference, and the interesting work is defining
what "the same element changed" means — which §5.2 splits in two:

  * **geometry** changed → a visible delta → orange.
  * **properties** changed, geometry identical → **no visual delta**. Recorded as
    ``change=property`` and surfaced on selection, never painted.

That split is the whole reason the geometry hash is computed from **tessellated
world-coordinate vertices** rather than from the entity graph. A structural hash
over the IFC representation is tempting and much cheaper, but it is far too easy
for it to pick up an attribute that has nothing to do with shape — and the failure
mode is a model that looks identical lighting up orange, after which reviewers
learn to ignore the colour entirely. Tessellating asks the only question that
matters: does this element occupy different space than it did?

World coordinates are essential: a moved element must hash differently, and local
coordinates would make a wall moved 10m across the site hash exactly as before.
"""
from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from typing import Dict, List, Tuple

from .model3d import Element3D, Model3D

log = logging.getLogger("difference_service.plugins.ifc_objects")

#: Spatial containers and annotations are excluded: they carry no geometry a
#: reviewer compares, and including them would report every storey as "modified"
#: whenever anything inside it moved.
_EXCLUDED_TYPES = {
    "IfcSite", "IfcBuilding", "IfcBuildingStorey", "IfcSpace", "IfcAnnotation",
    "IfcGrid", "IfcOpeningElement", "IfcProject",
}


def _properties_of(product) -> Dict[str, str]:
    """Flatten an element's property sets to ``{"Pset.Prop": "value"}``.

    Deliberately separate from geometry: this is the channel that must NOT
    influence the visual diff (§5.2)."""
    out: Dict[str, str] = {}
    try:
        for rel in getattr(product, "IsDefinedBy", None) or ():
            if not rel.is_a("IfcRelDefinesByProperties"):
                continue
            pset = getattr(rel, "RelatingPropertyDefinition", None)
            if pset is None or not pset.is_a("IfcPropertySet"):
                continue
            pset_name = getattr(pset, "Name", "") or "Pset"
            for prop in getattr(pset, "HasProperties", None) or ():
                if not prop.is_a("IfcPropertySingleValue"):
                    continue
                value = getattr(prop, "NominalValue", None)
                text = getattr(value, "wrappedValue", value)
                out[f"{pset_name}.{prop.Name}"] = "" if text is None else str(text)
    except Exception:
        log.debug("property extraction failed", exc_info=True)
    return out


def parse_ifc(data: bytes) -> Model3D:
    """Parse IFC bytes into the shared model. Never raises."""
    model = Model3D(source_format="ifc")
    try:
        import ifcopenshell
    except Exception:
        model.problems.append("ifcopenshell-missing")
        return model

    path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".ifc", delete=False) as fh:
            fh.write(data)
            path = fh.name

        try:
            ifc = ifcopenshell.open(path)
        except Exception as e:
            model.problems.append(f"open-failed:{type(e).__name__}")
            return model
        model.schema = getattr(ifc, "schema", "") or ""

        shapes = _tessellate_all(ifc, model)

        for product in ifc.by_type("IfcProduct"):
            gid = getattr(product, "GlobalId", None)
            if not gid or product.is_a() in _EXCLUDED_TYPES:
                continue
            if not getattr(product, "Representation", None):
                continue                      # no shape: not a comparable element

            verts, faces = shapes.get(gid, ((), ()))
            if not verts:
                # No tessellation means no comparable shape. Recorded rather than
                # emitted as a geometry-less element, which would otherwise match
                # every other geometry-less element by an empty hash.
                model.problems.append(f"no-geometry:{gid}")
                continue
            model.elements.append(Element3D(
                stable_id=gid,                       # IFC GlobalId -> tier 1
                local_id=gid,
                name=getattr(product, "Name", "") or "",
                type_name=product.is_a(),
                verts=verts, faces=faces,
                properties=_properties_of(product),
            ))
        return model
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def _tessellate_all(ifc, model: Model3D) -> Dict[str, Tuple[Tuple[float, ...], Tuple[int, ...]]]:
    """World-space triangles per GlobalId.

    Uses the iterator when available (it is far faster on real models than
    per-element ``create_shape``), falling back to per-element so a model that
    upsets the iterator still yields what it can — a partially tessellated model
    is better than none, and the missing elements are recorded rather than
    silently treated as geometry-less."""
    out: Dict[str, Tuple[Tuple[float, ...], Tuple[int, ...]]] = {}
    try:
        import ifcopenshell.geom as geom
    except Exception:
        model.problems.append("ifcopenshell.geom-missing")
        return out

    settings = geom.settings()
    _enable_world_coords(settings, model)

    try:
        iterator = geom.iterator(settings, ifc)
        if iterator.initialize():
            while True:
                shape = iterator.get()
                gid = getattr(shape, "guid", "") or ""
                if gid:
                    out[gid] = (tuple(shape.geometry.verts), tuple(shape.geometry.faces))
                if not iterator.next():
                    break
            return out
    except Exception:
        log.debug("geometry iterator failed; falling back", exc_info=True)
        model.problems.append("iterator-failed")

    for product in ifc.by_type("IfcProduct"):
        gid = getattr(product, "GlobalId", None)
        if not gid or not getattr(product, "Representation", None):
            continue
        try:
            shape = geom.create_shape(settings, product)
            out[gid] = (tuple(shape.geometry.verts), tuple(shape.geometry.faces))
        except Exception:
            model.problems.append(f"shape-failed:{gid}")
    return out


def _enable_world_coords(settings, model: Model3D) -> None:
    """Turn on world coordinates across ifcopenshell's differing settings APIs.

    Non-negotiable for correctness: in local coordinates a wall moved ten metres
    hashes exactly as it did before, so every move would be reported unchanged."""
    for attempt in (
        lambda: settings.set(settings.USE_WORLD_COORDS, True),
        lambda: settings.set("use-world-coords", True),
        lambda: settings.set("USE_WORLD_COORDS", True),
    ):
        try:
            attempt()
            return
        except Exception:
            continue
    model.problems.append("world-coords-unavailable")
    log.warning("ifcopenshell: could not enable world coordinates; moved elements "
                "may hash as unchanged")

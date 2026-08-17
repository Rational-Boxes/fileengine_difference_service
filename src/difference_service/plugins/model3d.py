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

"""The format-agnostic 3D model and matcher (SPECIFICATION.md §5.2).

Every supported 3D format normalizes into ``Model3D``/``Element3D`` here, and the
diff is computed on *that* — never per format. The payoff is that the tier ladder
is driven by **what identity the data actually carries**, not by file extension:

  * **Tier 1 — stable id.** Both sides carry durable element ids (IFC GlobalId),
    so matching is a lookup. ``modified`` splits into a geometry change (visible,
    orange) and a property-only change (**no visual delta**, §5.2).
  * **Tier 3 — geometry.** No stable ids (glTF, STEP, and any tessellated format),
    so correspondence must be inferred from shape. A moved or scaled mesh honestly
    reads as a removed volume plus an added one, because without identity there is
    no basis for claiming it is "the same thing, moved".

Adding a format is therefore writing a loader, not a differ. A loader that can
recover ids gets tier 1 for free; one that can only tessellate gets tier 3 — and
either way the output, the states and the manifest are identical.

Two traps this file exists to avoid, both encoded in the fixtures:

  * **Names are not identity.** glTF node names and IFC ``Name`` attributes are
    optional, non-unique and rewritten freely by exporters. Matching on them
    invents changes when a file is re-exported.
  * **File order is not identity.** STEP entity numbers and glTF node indices are
    file-local; a reordered export must read as unchanged.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .base import DiffState

#: Vertex rounding (model units) for geometry hashing — absorbs tessellation noise.
VERTEX_GRID = 1e-4

#: How far two unidentified meshes may sit apart and still be considered the same
#: object for tier-3 pairing, as a fraction of the model's bounding-box diagonal.
GEOMETRY_PROXIMITY = 0.02

#: Below this share of matched elements, tier-1 results are not trustworthy.
MIN_CONFIDENCE = 0.6


class Tier:
    """Which identity strategy produced a comparison."""
    STABLE_ID = "stable-id"      # §5.2 tier 1 — IFC GlobalId
    GEOMETRY = "geometry"        # §5.2 tier 3 — mesh correspondence
    NONE = "none"


class Change:
    """What kind of change a *modified* element underwent (§5.2)."""
    GEOMETRY = "geometry"        # occupies different space -> visible, orange
    PROPERTY = "property"        # metadata only -> NO visual delta


@dataclass
class Element3D:
    """One comparable element, whatever format it came from."""
    #: Durable identity where the format provides one (IFC GlobalId). Empty means
    #: the format carries none — which selects tier 3, not a failure.
    stable_id: str = ""
    #: Loader-local key, unique within its model. Never used for matching.
    local_id: str = ""
    name: str = ""
    type_name: str = ""
    verts: Tuple[float, ...] = ()
    faces: Tuple[int, ...] = ()
    properties: Dict[str, str] = field(default_factory=dict)

    _hash: str = ""

    @property
    def key(self) -> str:
        return self.stable_id or self.local_id

    @property
    def has_geometry(self) -> bool:
        return bool(self.verts)

    @property
    def geometry_hash(self) -> str:
        """Hash of the shape ALONE — never name, id or properties.

        That exclusion is the whole point: if anything non-geometric leaks in, a
        property-only edit repaints a model that looks identical, and reviewers
        learn to distrust the colour."""
        if not self._hash:
            self._hash = geometry_hash(self.verts, self.faces)
        return self._hash

    @property
    def centroid(self) -> Tuple[float, float, float]:
        if not self.verts:
            return (0.0, 0.0, 0.0)
        n = len(self.verts) // 3
        return (sum(self.verts[0::3]) / n,
                sum(self.verts[1::3]) / n,
                sum(self.verts[2::3]) / n)

    @property
    def shape_signature(self) -> str:
        """Position-INDEPENDENT shape key, for tier-3 correspondence.

        Vertices are re-expressed relative to the centroid, so the same mesh in two
        places shares a signature and a pure translation is detectable as such."""
        if not self.verts:
            return ""
        cx, cy, cz = self.centroid
        rel: List[float] = []
        for i in range(0, len(self.verts), 3):
            rel += [self.verts[i] - cx, self.verts[i + 1] - cy, self.verts[i + 2] - cz]
        return geometry_hash(tuple(rel), self.faces)


@dataclass
class Model3D:
    """A parsed model, normalized away from its source format."""
    elements: List[Element3D] = field(default_factory=list)
    source_format: str = ""
    #: Reasons the load was incomplete; lowers confidence rather than failing.
    problems: List[str] = field(default_factory=list)

    @property
    def has_stable_ids(self) -> bool:
        """Do enough elements carry durable ids to attempt tier 1?

        A *majority* is required rather than "any": a model where a handful of
        elements happen to have ids would otherwise take tier 1 and report the
        entire remainder as added and deleted."""
        if not self.elements:
            return False
        with_ids = sum(1 for e in self.elements if e.stable_id)
        return with_ids * 2 > len(self.elements)

    def by_stable_id(self) -> Dict[str, Element3D]:
        return {e.stable_id: e for e in self.elements if e.stable_id}

    @property
    def bbox_diagonal(self) -> float:
        xs, ys, zs = [], [], []
        for e in self.elements:
            for i in range(0, len(e.verts), 3):
                xs.append(e.verts[i]); ys.append(e.verts[i + 1]); zs.append(e.verts[i + 2])
        if not xs:
            return 0.0
        return math.dist((min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs)))


def geometry_hash(verts: Sequence[float], faces: Sequence[int]) -> str:
    """Stable hash of a tessellated shape.

    Faces are hashed as well as vertices: two solids can share a vertex cloud and
    be triangulated differently, which is a genuine shape difference."""
    h = hashlib.sha256()
    for v in verts:
        h.update(f"{round(float(v) / VERTEX_GRID)},".encode())
    h.update(b"|")
    for f in faces:
        h.update(f"{int(f)},".encode())
    return h.hexdigest()[:32]


# ----------------------------------------------------------------- the delta

@dataclass
class ElementDelta:
    """One element's verdict."""
    state: str
    old: Optional[Element3D] = None
    new: Optional[Element3D] = None
    #: For a modified element: ``Change.GEOMETRY`` or ``Change.PROPERTY``.
    change: str = ""
    #: Property keys whose values differ (surfaced on selection, §5.2).
    changed_properties: List[str] = field(default_factory=list)

    @property
    def element(self) -> Element3D:
        return self.new or self.old

    @property
    def has_visual_delta(self) -> bool:
        """Should this element be painted in the difference layer?

        A property-only change must NOT be — the geometry is identical, so
        colouring it means repainting something that looks unchanged (§5.2)."""
        if self.state == DiffState.UNCHANGED:
            return False
        if self.state == DiffState.MODIFIED:
            return self.change == Change.GEOMETRY
        return True


@dataclass
class ModelDelta:
    """The full comparison of two models."""
    deltas: List[ElementDelta] = field(default_factory=list)
    tier: str = Tier.NONE
    matched: int = 0
    confidence: float = 1.0

    def count(self, state: str) -> int:
        return sum(1 for d in self.deltas if d.state == state)

    def count_change(self, change: str) -> int:
        return sum(1 for d in self.deltas if d.change == change)

    @property
    def visual_changes(self) -> List[ElementDelta]:
        return [d for d in self.deltas if d.has_visual_delta]

    @property
    def trustworthy(self) -> bool:
        return self.confidence >= MIN_CONFIDENCE


# ------------------------------------------------------------------ matching

def match_models(old: Model3D, new: Model3D) -> ModelDelta:
    """Compare two models with the best strategy their identity supports."""
    if old.has_stable_ids and new.has_stable_ids:
        delta = _match_by_stable_id(old, new)
    else:
        delta = _match_by_geometry(old, new)
    delta.confidence = _confidence(old, new, delta)
    return delta


def _match_by_stable_id(old: Model3D, new: Model3D) -> ModelDelta:
    """Tier 1 — match on durable ids, then split modified into geometry/property."""
    delta = ModelDelta(tier=Tier.STABLE_ID)
    old_by_id, new_by_id = old.by_stable_id(), new.by_stable_id()

    for gid, o in old_by_id.items():
        n = new_by_id.get(gid)
        if n is None:
            delta.deltas.append(ElementDelta(DiffState.DELETED, old=o))
            continue
        delta.matched += 1
        delta.deltas.append(_classify_matched(o, n))

    for gid, n in new_by_id.items():
        if gid not in old_by_id:
            delta.deltas.append(ElementDelta(DiffState.ADDED, new=n))
    return delta


def _classify_matched(o: Element3D, n: Element3D) -> ElementDelta:
    """The §5.2 ``modified`` predicate for an identity-matched pair."""
    changed_props = sorted(
        k for k in set(o.properties) | set(n.properties)
        if o.properties.get(k) != n.properties.get(k))

    if o.geometry_hash != n.geometry_hash:
        # Geometry wins: an element that moved AND had a property edited is a
        # visible change, and reporting it as property-only would hide that.
        return ElementDelta(DiffState.MODIFIED, old=o, new=n,
                            change=Change.GEOMETRY, changed_properties=changed_props)
    if changed_props:
        return ElementDelta(DiffState.MODIFIED, old=o, new=n,
                            change=Change.PROPERTY, changed_properties=changed_props)
    # Name changes land here deliberately: a rename is not a change to the model,
    # and painting it would be indistinguishable from a real edit.
    return ElementDelta(DiffState.UNCHANGED, old=o, new=n)


def _match_by_geometry(old: Model3D, new: Model3D) -> ModelDelta:
    """Tier 3 — no durable ids, so correspondence is inferred from shape.

    Pairing is **globally nearest-first**, not in file order. Iterating the old
    list and letting each element claim its closest counterpart looks equivalent
    but is not: in a model holding several copies of one component, whichever copy
    happens to come first in the file claims a distant partner, and the copy that
    genuinely stayed put is then left over and reported as deleted+added. Sorting
    every candidate pair by distance and accepting greedily makes the result
    independent of file order — which it must be, since order is not identity."""
    delta = ModelDelta(tier=Tier.GEOMETRY)
    scale = max(old.bbox_diagonal, new.bbox_diagonal) or 1.0
    tolerance = scale * GEOMETRY_PROXIMITY

    candidates: List[Tuple[float, int, int]] = []
    for i, o in enumerate(old.elements):
        for j, n in enumerate(new.elements):
            if n.shape_signature == o.shape_signature:
                candidates.append((math.dist(o.centroid, n.centroid), i, j))
    candidates.sort()

    paired_old: Dict[int, int] = {}
    used_new = set()
    for _distance, i, j in candidates:
        if i in paired_old or j in used_new:
            continue
        paired_old[i] = j
        used_new.add(j)

    for i, o in enumerate(old.elements):
        j = paired_old.get(i)
        if j is None:
            delta.deltas.append(ElementDelta(DiffState.DELETED, old=o))
            continue
        n = new.elements[j]
        delta.matched += 1
        if math.dist(o.centroid, n.centroid) <= tolerance:
            delta.deltas.append(ElementDelta(DiffState.UNCHANGED, old=o, new=n))
        else:
            # Same shape, different place, and no identity to justify calling it a
            # move: emitted as removed + added volume (§5.2 mesh-boolean tier).
            delta.deltas.append(ElementDelta(DiffState.DELETED, old=o))
            delta.deltas.append(ElementDelta(DiffState.ADDED, new=n))

    for j, n in enumerate(new.elements):
        if j not in used_new:
            delta.deltas.append(ElementDelta(DiffState.ADDED, new=n))
    return delta


def _confidence(old: Model3D, new: Model3D, delta: ModelDelta) -> float:
    """How much to trust this comparison.

    Coverage is measured against the SMALLER model for the same reason as the PDF
    matcher: dividing by the larger conflates "not understood" with "legitimately
    gained elements", and would degrade a good diff for the crime of having
    something added to it."""
    total = min(len(old.elements), len(new.elements))
    if total == 0:
        return 1.0
    coverage = delta.matched / total
    problems = len(set(old.problems) | set(new.problems))
    return round(min(coverage, 1.0 / (1.0 + problems)), 4)

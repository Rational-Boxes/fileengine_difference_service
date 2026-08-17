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

"""Minimal IFC fixtures with known element-level differences (M3, spec §5.2).

IFC (ISO-10303-21 "STEP physical file") is text, so these are written directly —
no IfcOpenShell needed to *generate* the corpus, which means the fixtures exist
before the heavy geometry dependency is installed.

Each wall is an ``IfcWallStandardCase`` with an explicit **GlobalId**, the stable
identity §5.2 tier 1 matches on, and a swept-solid body whose profile dimensions
and placement are set per element so geometry changes are exact and intentional.

The distinction these fixtures exist to pin down is §5.2's `modified` predicate:

  * same GlobalId + different geometry  → *modified*, orange, `change=geometry`
  * same GlobalId + different property  → **no visual delta**; recorded as
    `change=property` and surfaced on selection, NOT painted orange

Getting that wrong repaints a model that looks identical, which is worse than
useless — a reviewer learns to ignore orange.
"""
from __future__ import annotations

from typing import List, Tuple

#: Stable GlobalIds (IFC base-64 GUIDs, 22 chars) so a pair's identity is fixed.
WALL_A = "1aBcDeFgHiJkLmNoPqRsT0"
WALL_B = "2aBcDeFgHiJkLmNoPqRsT1"
WALL_C = "3aBcDeFgHiJkLmNoPqRsT2"

_HEADER = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('ViewDefinition [CoordinationView]'),'2;1');
FILE_NAME('fixture.ifc','2026-01-01T00:00:00',(''),(''),'difference_service','fixtures','');
FILE_SCHEMA(('IFC4'));
ENDSEC;
DATA;
"""

_FOOTER = """ENDSEC;
END-ISO-10303-21;
"""


class _Builder:
    """Accumulates numbered STEP entities."""

    def __init__(self):
        self.lines: List[str] = []
        self._n = 0

    def add(self, body: str) -> int:
        self._n += 1
        self.lines.append(f"#{self._n}={body};")
        return self._n

    def render(self) -> bytes:
        return (_HEADER + "\n".join(self.lines) + "\n" + _FOOTER).encode("utf-8")


def _preamble(b: _Builder) -> int:
    """Units, context, project, site — the scaffolding every file needs."""
    person = b.add("IFCPERSON($,'fixture',$,$,$,$,$,$)")
    org = b.add("IFCORGANIZATION($,'RationalBoxes',$,$,$)")
    p_and_o = b.add(f"IFCPERSONANDORGANIZATION(#{person},#{org},$)")
    app = b.add(f"IFCAPPLICATION(#{org},'1.0','difference_service fixtures','DSF')")
    b.add(f"IFCOWNERHISTORY(#{p_and_o},#{app},$,.ADDED.,$,$,$,0)")

    axis = b.add("IFCDIRECTION((0.,0.,1.))")
    refd = b.add("IFCDIRECTION((1.,0.,0.))")
    origin = b.add("IFCCARTESIANPOINT((0.,0.,0.))")
    place = b.add(f"IFCAXIS2PLACEMENT3D(#{origin},#{axis},#{refd})")
    length = b.add("IFCSIUNIT(*,.LENGTHUNIT.,$,.METRE.)")
    units = b.add(f"IFCUNITASSIGNMENT((#{length}))")
    ctx = b.add(f"IFCGEOMETRICREPRESENTATIONCONTEXT($,'Model',3,1.E-05,#{place},$)")
    b.add(f"IFCPROJECT('0projectGUID0000000000',$,'Fixture Project',$,$,$,$,(#{ctx}),#{units})")
    return ctx


def _wall(b: _Builder, ctx: int, guid: str, name: str, *,
          x: float, y: float, length: float, width: float, height: float) -> int:
    """One wall with an explicit placement and a swept-solid body."""
    origin = b.add(f"IFCCARTESIANPOINT(({x:.3f},{y:.3f},0.))")
    place3d = b.add(f"IFCAXIS2PLACEMENT3D(#{origin},$,$)")
    lp = b.add(f"IFCLOCALPLACEMENT($,#{place3d})")

    prof_org = b.add("IFCCARTESIANPOINT((0.,0.))")
    prof_place = b.add(f"IFCAXIS2PLACEMENT2D(#{prof_org},$)")
    profile = b.add(
        f"IFCRECTANGLEPROFILEDEF(.AREA.,'wall',#{prof_place},{length:.3f},{width:.3f})")
    extrude_dir = b.add("IFCDIRECTION((0.,0.,1.))")
    body_org = b.add("IFCCARTESIANPOINT((0.,0.,0.))")
    body_place = b.add(f"IFCAXIS2PLACEMENT3D(#{body_org},$,$)")
    solid = b.add(
        f"IFCEXTRUDEDAREASOLID(#{profile},#{body_place},#{extrude_dir},{height:.3f})")
    shape = b.add(f"IFCSHAPEREPRESENTATION(#{ctx},'Body','SweptSolid',(#{solid}))")
    prod = b.add(f"IFCPRODUCTDEFINITIONSHAPE($,$,(#{shape}))")
    return b.add(
        f"IFCWALLSTANDARDCASE('{guid}',$,'{name}',$,$,#{lp},#{prod},$,.NOTDEFINED.)")


def _property_set(b: _Builder, wall_id: int, guid: str, fire_rating: str) -> None:
    """A property set on a wall — the channel that must NOT repaint geometry."""
    prop = b.add(
        f"IFCPROPERTYSINGLEVALUE('FireRating',$,IFCLABEL('{fire_rating}'),$)")
    pset = b.add(f"IFCPROPERTYSET('{guid}',$,'Pset_WallCommon',$,(#{prop}))")
    b.add(f"IFCRELDEFINESBYPROPERTIES('{guid[:20]}rp',$,$,$,(#{wall_id}),#{pset})")


def _model(walls, properties=None) -> bytes:
    """Build a file from ``walls`` = [(guid, name, x, y, length, width, height)]."""
    b = _Builder()
    ctx = _preamble(b)
    ids = {}
    for guid, name, x, y, length, width, height in walls:
        ids[guid] = _wall(b, ctx, guid, name, x=x, y=y,
                          length=length, width=width, height=height)
    for guid, rating in (properties or {}).items():
        if guid in ids:
            _property_set(b, ids[guid], guid, rating)
    return b.render()


# --- baseline geometry -----------------------------------------------------
_A = (WALL_A, "Wall A", 0.0, 0.0, 5.0, 0.2, 3.0)
_B = (WALL_B, "Wall B", 0.0, 5.0, 5.0, 0.2, 3.0)
_C = (WALL_C, "Wall C", 5.0, 0.0, 4.0, 0.2, 3.0)


# --------------------------------------------------------------------------
# Pairs — each returns (before, after) with the ground truth documented.
# --------------------------------------------------------------------------

def unchanged_pair() -> Tuple[bytes, bytes]:
    """Identical models. Ground truth: every element unchanged; nothing orange."""
    return _model([_A, _B]), _model([_A, _B])


def added_element_pair() -> Tuple[bytes, bytes]:
    """A wall present only in the after model.

    Ground truth: WALL_C added (green); A and B unchanged. Presence-based, so this
    is the unambiguous case tier 1 must get right by GlobalId alone."""
    return _model([_A, _B]), _model([_A, _B, _C])


def deleted_element_pair() -> Tuple[bytes, bytes]:
    """A wall present only in the before model.

    Ground truth: WALL_C deleted (red); A and B unchanged."""
    return _model([_A, _B, _C]), _model([_A, _B])


def moved_element_pair() -> Tuple[bytes, bytes]:
    """Same GlobalId, different placement.

    Ground truth: WALL_B *modified* with ``change=geometry`` — orange. Identity is
    preserved, so it must NOT read as delete+add: a wall that moved is the same
    wall, and reporting it as two events loses that."""
    moved_b = (WALL_B, "Wall B", 0.0, 7.5, 5.0, 0.2, 3.0)
    return _model([_A, _B]), _model([_A, moved_b])


def resized_element_pair() -> Tuple[bytes, bytes]:
    """Same GlobalId, same placement, different profile dimensions.

    Ground truth: WALL_A *modified*, ``change=geometry``. The geometry hash must
    cover the profile, not just the placement — a matcher comparing only transforms
    reports this unchanged, which is a silent false negative."""
    thicker_a = (WALL_A, "Wall A", 0.0, 0.0, 5.0, 0.4, 3.0)
    return _model([_A, _B]), _model([thicker_a, _B])


def property_only_pair() -> Tuple[bytes, bytes]:
    """Identical geometry; one property value changed.

    Ground truth: **no visual delta**. §5.2 is explicit that a property-only change
    is recorded as ``change=property`` and surfaced on selection, NOT painted
    orange. This is the fixture that catches a geometry-hash implementation which
    accidentally folds property data in — the failure mode where a model that looks
    identical lights up orange and reviewers learn to distrust the colour."""
    return (_model([_A, _B], {WALL_A: "REI 60"}),
            _model([_A, _B], {WALL_A: "REI 120"}))


def renamed_element_pair() -> Tuple[bytes, bytes]:
    """Same GlobalId and geometry; the human-readable Name changed.

    Ground truth: like ``property_only`` — an attribute change, no geometry delta,
    so no orange. Included separately because Name sits on the element itself
    rather than in a property set, and an implementation may treat the two
    differently without meaning to."""
    renamed_a = (WALL_A, "Wall A (revised)", 0.0, 0.0, 5.0, 0.2, 3.0)
    return _model([_A, _B]), _model([renamed_a, _B])


def reordered_entities_pair() -> Tuple[bytes, bytes]:
    """Same elements, emitted in a different order (so all entity #ids shift).

    Ground truth: nothing changed. STEP entity numbers are file-local and carry no
    identity — only GlobalId does. A matcher keying on entity number or file order
    reports the entire model changed here."""
    return _model([_A, _B, _C]), _model([_C, _B, _A])


def combined_pair() -> Tuple[bytes, bytes]:
    """One of each: added, deleted, moved, and property-only.

    Ground truth: WALL_C added, WALL_A deleted, WALL_B modified (geometry). The
    realistic case — a matcher that handles each in isolation can still mis-handle
    them together."""
    moved_b = (WALL_B, "Wall B", 1.0, 5.0, 5.0, 0.2, 3.0)
    return (_model([_A, _B], {WALL_B: "REI 60"}),
            _model([moved_b, _C], {WALL_B: "REI 120"}))


#: Every pair, keyed by name, with its ground truth.
PAIRS = {
    "unchanged": (unchanged_pair, "nothing changed"),
    "added_element": (added_element_pair, "WALL_C added"),
    "deleted_element": (deleted_element_pair, "WALL_C deleted"),
    "moved_element": (moved_element_pair, "WALL_B modified (geometry: placement)"),
    "resized_element": (resized_element_pair, "WALL_A modified (geometry: profile)"),
    "property_only": (property_only_pair, "WALL_A property changed => NO visual delta"),
    "renamed_element": (renamed_element_pair, "WALL_A name changed => NO visual delta"),
    "reordered_entities": (reordered_entities_pair, "entity order differs; nothing changed"),
    "combined": (combined_pair, "C added, A deleted, B moved"),
}

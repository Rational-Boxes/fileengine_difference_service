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

"""Real producer output, not synthesised fixtures.

The rest of the PDF suite builds its own documents, which is what makes those
tests precise — and what let this class of failure through. A hand-built fixture
uses whatever the fixture author wrote; real producers embed font SUBSETS, stamp
each with a fresh six-letter tag per export, and route text through resources
nobody would think to synthesise.

So this module asserts the properties that must hold for a document the service
did not create: the tier it lands on, and that the tier is not chosen for a
reason the document does not have. A vector drawing must diff as vector, and a
revised building must correspond with the building it was revised from.

Both corpora are registered as ``REAL_DOCUMENTS`` in their fixture modules, so
they flow into ``samples/`` and the manual-verification pass like everything
else — a real document is a fixture here, not a special case.
"""
import re
from collections import Counter

import pytest

from difference_service.plugins.base import DiffMode
from difference_service.plugins.pdf import PdfDiffPlugin
from difference_service.plugins.pdf_match import MIN_CONFIDENCE, match_page
from difference_service.plugins.pdf_objects import parse_document
from tests.fixtures import gltf as G
from tests.fixtures import pdf as F

pytest.importorskip("pypdf", reason="pypdf not installed (pip install '.[pdf]')")


@pytest.fixture(scope="module")
def blueprint():
    before, after = F.blueprint_pair()
    return parse_document(before)[0], parse_document(after)[0]


def test_the_blueprint_is_vector_content_on_both_sides(blueprint):
    """The premise. If this ever fails the rest of the module proves nothing —
    the page really would be raster and the raster tier would be right."""
    old, new = blueprint
    for page in (old, new):
        assert not page.has_raster
        assert not page.is_image_only
        assert page.text_objects and [o for o in page.objects if o.kind == "path"]


def test_the_two_exports_carry_different_subset_tags(blueprint):
    """The hazard itself, stated as a fact about the documents rather than an
    assumption in a comment: same face, different tag, per export."""
    old, new = blueprint
    old_fonts = {o.font for o in old.text_objects}
    new_fonts = {o.font for o in new.text_objects}
    assert old_fonts and new_fonts
    assert old_fonts.isdisjoint(new_fonts)                    # tags differ
    assert {f.split("+")[-1] for f in old_fonts} == {f.split("+")[-1] for f in new_fonts}


def test_a_revised_drawing_still_recognises_its_own_text(blueprint):
    """The regression, at the identity layer where it happened. Text carried the
    font's subset tag in its signature, so NOT ONE of the 245 runs shared identity
    with its counterpart — the whole text layer read as deleted and re-added on a
    drawing that was merely revised. Measured on signatures rather than verdicts
    because a run may legitimately end up relocated or edited; what must not
    happen is the same word in the same face failing to be the same object."""
    old, new = blueprint
    old_sigs = Counter(o.signature for o in old.text_objects)
    new_sigs = Counter(o.signature for o in new.text_objects)
    shared = sum((old_sigs & new_sigs).values())
    smaller = min(sum(old_sigs.values()), sum(new_sigs.values()))
    assert shared > smaller * 0.9, (
        f"only {shared} of {smaller} text runs share identity across the two "
        f"exports (was 0 when the subset tag was part of the signature)")


def test_the_blueprint_clears_the_confidence_gate(blueprint):
    old, new = blueprint
    delta = match_page(old, new)
    assert delta.confidence >= MIN_CONFIDENCE, (
        f"confidence {delta.confidence} < {MIN_CONFIDENCE}: the page would degrade "
        f"to a raster diff")
    assert delta.trustworthy


def test_the_plugin_renders_the_blueprint_as_a_vector_diff():
    """End to end, through the real plugin: what the reviewer actually receives."""
    before, after = F.blueprint_pair()
    result = PdfDiffPlugin().diff(
        _ref(before, "old"), _ref(after, "new"))
    assert result.failure is None
    assert [c.mode for c in result.children] == [DiffMode.VECTOR]


def _ref(data: bytes, version: str, mime: str = "application/pdf",
         name: str = "house-blueprint.pdf"):
    from difference_service.plugins.base import SourceRef
    return SourceRef(uid=name, version=version, data=data, mime=mime, name=name)


def test_the_blueprint_svg_keeps_its_curves_and_subpaths():
    """Rendered geometry, on the document that exposed the bug.

    The drawing carries 138 cubic Bézier operators and 41 multi-subpath objects,
    so the SVG must contain curves and must break subpaths. Asserted on counts
    because a conversion that flattened both still produced a valid, plausible
    page — nothing about the output said it was wrong."""
    before, after = F.blueprint_pair()
    result = PdfDiffPlugin().diff(_ref(before, "old"), _ref(after, "new"))
    svg = result.children[0].data.decode()
    geometry = " ".join(re.findall(r' d="([^"]*)"', svg))
    assert "C" in geometry, "no curve survived the conversion"
    # Every drawn subpath opens with M; the single-polyline conversion emitted one
    # per object, so this count sits far above the object count when subpaths hold.
    assert geometry.count("M") > len(re.findall(r"<path", svg))


def test_the_blueprint_keeps_its_word_gaps():
    """The words on this sheet are separated by TJ displacements, not by space
    glyphs — so dropping those ran them together everywhere text is used."""
    page = parse_document(F.blueprint_pair()[1])[0]
    words = {o.text for o in page.text_objects}
    assert "Covered porch" in words and "Living room" in words
    assert not any(w in words for w in ("Coveredporch", "Livingroom"))


# ------------------------------------------------------------------------ 3D
# The Blender export pair — tests.fixtures.gltf.building_pair.


@pytest.fixture(scope="module")
def building():
    from difference_service.plugins.gltf_objects import parse_gltf
    before, after = G.building_pair()
    return parse_gltf(before, "glb"), parse_gltf(after, "glb")


def test_the_real_building_parses_into_elements(building):
    """glTF has no stable ids (§5.2), so everything downstream rests on the
    geometry actually being read: nodes flattened, transforms applied, and meshes
    split by material the way an exporter really emits them — none of which the
    synthesised boxes exercise."""
    old, new = building
    assert len(old.elements) > 100 and len(new.elements) > 100
    for model in (old, new):
        assert all(e.has_geometry for e in model.elements)
        assert all(e.key for e in model.elements)           # loader-local ids exist
        assert not any(e.stable_id for e in model.elements)  # glTF carries none


def test_the_revision_adds_to_the_model_rather_than_replacing_it(building):
    old, new = building
    assert len(new.elements) > len(old.elements)


def test_two_exports_share_almost_no_exact_geometry_hashes(building):
    """Recorded because it is the trap this corpus exists to expose, and it is NOT
    a bug: 13 of 319 element hashes survive a re-export, because vertex order and
    float precision are the exporter's business and it does not promise either.

    Tier 3 is specified to infer correspondence from geometry for exactly this
    reason, and the slow test below shows it does. Anything that ever starts
    matching 3D elements on hash equality will pass every synthesised fixture in
    this repo and then correspond nothing at all on real exports — the same shape
    of failure the PDF subset tag caused above."""
    old, new = building
    old_h = {e.geometry_hash for e in old.elements}
    shared = len(old_h & {e.geometry_hash for e in new.elements})
    assert shared < len(old_h) * 0.1


@pytest.mark.slow
def test_a_revised_building_still_corresponds_with_its_original(building):
    """~10s. The 3D counterpart of the PDF regression: if correspondence collapsed,
    every element would read as deleted plus added and an edited model would light
    up entirely. Inference from geometry recovers what the hashes cannot."""
    from difference_service.plugins.model3d import Tier, match_models
    old, new = building
    delta = match_models(old, new)
    assert delta.tier == Tier.GEOMETRY
    assert delta.matched > len(old.elements) * 0.5, (
        f"only {delta.matched} of {len(old.elements)} elements corresponded")
    assert delta.count("unchanged") > 0
    assert delta.confidence >= 0.6


@pytest.mark.slow
def test_the_plugin_renders_the_real_building_end_to_end():
    """~11s: a real mesh diff on a real model. Marked slow rather than skipped —
    it is the only test that drives the whole 3D path on something an exporter
    actually produced. Run it with `pytest -m slow`."""
    from difference_service.plugins.three_d import ThreeDDiffPlugin
    before, after = G.building_pair()
    result = ThreeDDiffPlugin().diff(
        _ref(before, "old", "model/gltf-binary", "building.glb"),
        _ref(after, "new", "model/gltf-binary", "building.glb"))
    assert result.failure is None
    assert {c.kind for c in result.children} >= {"model"}

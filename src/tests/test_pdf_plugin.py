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

"""PDF plugin: tier ladder + SVG contract (SPECIFICATION.md §5.1, §7.2)."""
import re

import pytest

from difference_service.plugins.base import DiffMode, DiffStatus, SourceRef
from difference_service.plugins.pdf import PdfDiffPlugin
from difference_service.plugins.pdf_glyphs import GlyphProvider
from tests.fixtures import pdf as F

pytest.importorskip("pypdf", reason="pypdf not installed (pip install '.[pdf]')")


def _src(data, version="v1"):
    return SourceRef(uid="F", version=version, data=data, mime="application/pdf",
                     name="doc.pdf")


def _diff(pair, plugin=None):
    before, after = pair
    plugin = plugin or PdfDiffPlugin()
    return plugin.diff(_src(before, "v1"), _src(after, "v2"))


def _svg(result, index=0):
    return result.children[index].data.decode("utf-8")


class NoGlyphs(GlyphProvider):
    """A provider with no fonts at all — forces the no-outlines path."""
    def available(self, font_hint=""):
        return False

    def outline_run(self, *a, **k):
        return None


# --------------------------------------------------------------- dispatch
def test_claims_pdf_mime_types():
    p = PdfDiffPlugin()
    assert p.supports("application/pdf")
    assert p.supports("application/pdf; charset=binary")
    assert not p.supports("image/png") and not p.supports("text/plain")


def test_plugin_identity_is_stable():
    # name + version form part of the cache key (§6).
    assert PdfDiffPlugin().name == "pdf"
    assert isinstance(PdfDiffPlugin().version, int)


# ------------------------------------------------------------------ tiers
def test_a_vector_pair_reaches_tier_one():
    result = _diff(F.added_object_pair())
    assert result.status == DiffStatus.READY
    assert result.mode == DiffMode.VECTOR
    assert all(c.mode == DiffMode.VECTOR for c in result.children)


def test_a_scanned_pair_degrades_to_raster():
    # Image-only pages have no object identity, so tier 1 must disclaim them.
    result = _diff(F.scanned_pair())
    assert result.status == DiffStatus.READY
    assert result.mode == DiffMode.RASTER


def test_a_mixed_document_reports_mixed_with_a_per_page_map():
    # THE case §7.1's per-unit map exists for: one document, two tiers, so the
    # front end cannot assume a single view engine.
    result = _diff(F.mixed_tier_pair())
    assert result.mode == DiffMode.MIXED
    modes = [u["mode"] for u in result.units()]
    assert modes == [DiffMode.VECTOR, DiffMode.RASTER]


def test_no_outlines_degrades_rather_than_emitting_text():
    # The no-client-fonts rule is absolute: without outlines the page must drop a
    # tier, never fall back to a <text> element that renders differently per client.
    result = _diff(F.added_object_pair(), PdfDiffPlugin(glyphs=NoGlyphs()))
    assert all(c.mode != DiffMode.VECTOR for c in result.children)
    for c in result.children:
        assert b"<text" not in c.data


def test_a_document_with_no_renderable_page_fails_outright():
    # When NOTHING rendered, `failed` already says everything a reader needs; a
    # per-page "unavailable" placeholder for every page would add no information
    # and write useless children to storage. The manifest's failure detail is the
    # signal, and §7.1.1 gives a failed manifest an empty expected-set.
    plugin = PdfDiffPlugin(glyphs=NoGlyphs(), rasterizer=None)
    result = _diff(F.mixed_tier_pair(), plugin)
    assert result.status == DiffStatus.FAILED
    assert result.children == []
    assert result.failure.stage == "render"


def test_a_partially_unavailable_document_is_still_ready():
    # One page rendered is a usable diff; the other is explicitly marked.
    plugin = PdfDiffPlugin(rasterizer=None)              # vector ok, raster absent
    result = _diff(F.mixed_tier_pair(), plugin)
    assert result.status == DiffStatus.READY
    modes = [c.mode for c in result.children]
    assert DiffMode.VECTOR in modes and DiffMode.UNAVAILABLE in modes


def test_an_unopenable_pair_fails_cleanly():
    result = PdfDiffPlugin().diff(_src(b"not a pdf"), _src(b"also not a pdf"))
    assert result.status == DiffStatus.FAILED
    assert result.failure.stage == "parse"
    assert result.failure.tiers_attempted


# -------------------------------------------------------------- page count
def test_page_children_track_the_document():
    assert len(_diff(F.unchanged_pair()).children) == 1
    assert len(_diff(F.inserted_page_pair()).children) == 3   # 2 kept + 1 added
    assert len(_diff(F.deleted_page_pair()).children) == 3    # 2 kept + 1 deleted


def test_child_indices_are_contiguous_from_zero():
    result = _diff(F.inserted_page_pair())
    assert [c.index for c in result.children] == [0, 1, 2]


# ------------------------------------------------------- the §7.2 contract
def test_svg_carries_the_three_stable_layers():
    svg = _svg(_diff(F.added_object_pair()))
    assert re.findall(r'id="(diff-[a-z]+)"', svg) == ["diff-old", "diff-new", "diff-changes"]


def test_root_declares_the_mode():
    svg = _svg(_diff(F.added_object_pair()))
    assert 'data-diff-mode="vector"' in svg


def test_every_drawn_element_carries_a_state():
    svg = _svg(_diff(F.added_object_pair()))
    kinds = len(re.findall(r'data-diff-kind="', svg))
    states = len(re.findall(r'data-diff-state="', svg))
    assert kinds > 0 and states >= kinds


def test_text_is_glyph_outlines_not_text_elements():
    # §5.1 "full vectorization": the SVG must render identically on any client.
    svg = _svg(_diff(F.added_object_pair()))
    assert "<text" not in svg
    assert "<path d=" in svg


def test_no_colours_are_baked_into_the_document():
    # State -> colour is the front end's job, so a theme change must not require
    # regenerating stored renditions.
    svg = _svg(_diff(F.restyled_object_pair()))
    assert not re.findall(r'(?:fill|stroke)="(?!none)[^"]+"', svg)


def test_the_changes_layer_holds_only_what_differs():
    svg = _svg(_diff(F.added_object_pair()))
    changes = svg.split('id="diff-changes"')[1]
    assert 'data-diff-state="added"' in changes
    assert 'data-diff-state="unchanged"' not in changes


def test_an_unchanged_pair_has_an_empty_changes_layer():
    svg = _svg(_diff(F.unchanged_pair()))
    changes = svg.split('id="diff-changes"')[1]
    assert "data-diff-state" not in changes


def test_the_page_is_flipped_into_svg_coordinates():
    # PDF is y-up from bottom-left; SVG is y-down from top-left.
    svg = _svg(_diff(F.added_object_pair()))
    assert "scale(1,-1)" in svg


def test_raster_pages_use_the_same_layer_structure():
    # One front-end view engine must drive both modes (§7.2).
    result = _diff(F.scanned_pair())
    svg = _svg(result)
    assert re.findall(r'id="(diff-[a-z]+)"', svg) == ["diff-old", "diff-new", "diff-changes"]
    assert 'data-diff-mode="raster"' in svg
    assert "data:image/png;base64," in svg


def test_an_unavailable_page_still_mounts_in_the_view_engine():
    # A page that could not be rendered, in a document where others could, still
    # occupies its slot and carries the layer skeleton — so the front end shows an
    # honest "unavailable" for that page instead of an empty, unchanged-looking one.
    plugin = PdfDiffPlugin(rasterizer=None)          # vector ok, no raster backend
    result = _diff(F.mixed_tier_pair(), plugin)
    unavailable = [c for c in result.children if c.mode == DiffMode.UNAVAILABLE]
    assert len(unavailable) == 1
    svg = unavailable[0].data.decode()
    assert 'data-diff-mode="unavailable"' in svg
    assert re.findall(r'id="(diff-[a-z]+)"', svg) == ["diff-old", "diff-new", "diff-changes"]


# ------------------------------------------------------------------ states
def test_added_object_appears_as_added_in_the_new_layer():
    svg = _svg(_diff(F.added_object_pair()))
    new_layer = svg.split('id="diff-new"')[1].split('id="diff-changes"')[0]
    assert 'data-diff-state="added"' in new_layer


def test_deleted_object_appears_as_deleted_in_the_old_layer():
    svg = _svg(_diff(F.deleted_object_pair()))
    old_layer = svg.split('id="diff-old"')[1].split('id="diff-new"')[0]
    assert 'data-diff-state="deleted"' in old_layer


def test_a_whole_page_shift_produces_no_change_markers():
    svg = _svg(_diff(F.shifted_page_pair(dy=24)))
    changes = svg.split('id="diff-changes"')[1]
    assert "data-diff-state" not in changes


def test_an_inserted_page_is_wholly_added():
    result = _diff(F.inserted_page_pair())
    inserted = [c for c in result.children
                if b'data-diff-state="added"' in c.data
                and b'data-diff-state="unchanged"' not in c.data]
    assert inserted, "the inserted page should be entirely added"


# ------------------------------------------------------------------ output
def test_children_are_svg_renditions():
    for child in _diff(F.added_object_pair()).children:
        assert child.kind == "page"
        assert child.mime == "image/svg+xml" and child.ext == "svg"
        assert child.data.startswith(b"<svg")


def test_output_is_deterministic():
    # Byte-identical regeneration is what makes re-running the pipeline idempotent
    # rather than merely equivalent (§7.1.1).
    a = _diff(F.inserted_object_pair())
    b = _diff(F.inserted_object_pair())
    assert [c.data for c in a.children] == [c.data for c in b.children]

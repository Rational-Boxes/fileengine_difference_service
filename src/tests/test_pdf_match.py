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

"""The M2 object-matching spike, scored against the fixture ground truth (§5.1).

Every fixture pair in ``tests.fixtures.pdf`` declares what changed; these assert
the matcher agrees. The negative fixtures matter most — a matcher that passes
"one object added" while failing "whole page shifted" is the exact naive
implementation the spec warns about.
"""
import pytest

from difference_service.plugins.base import DiffState
from difference_service.plugins.pdf_match import (
    MIN_CONFIDENCE, dominant_offset, match_page, pair_pages,
)
from difference_service.plugins.pdf_objects import parse_document
from tests.fixtures import pdf as F

pytest.importorskip("pypdf", reason="pypdf not installed (pip install '.[pdf]')")


def _pages(data):
    return parse_document(data)


def _match(pair, page=0):
    before, after = pair
    return match_page(_pages(before)[page], _pages(after)[page])


def _states(delta):
    return {
        DiffState.ADDED: delta.count(DiffState.ADDED),
        DiffState.DELETED: delta.count(DiffState.DELETED),
        DiffState.MODIFIED: delta.count(DiffState.MODIFIED),
        DiffState.UNCHANGED: delta.count(DiffState.UNCHANGED),
    }


# ------------------------------------------------------------- extraction
def test_objects_are_extracted_in_draw_order():
    pages = _pages(F.unchanged_pair()[0])
    kinds = [o.kind for o in pages[0].objects]
    assert kinds == ["text", "path", "path"]     # heading, rect, line


def test_signatures_ignore_absolute_position():
    # The property everything else rests on: the same rectangle at two different
    # places must share one identity, or a move becomes a delete plus an add.
    before, after = F.moved_object_pair(dx=40)
    b = [o for o in _pages(before)[0].objects if o.kind == "path"][0]
    a = [o for o in _pages(after)[0].objects if o.kind == "path"][0]
    assert b.signature == a.signature
    assert (b.x, b.y) != (a.x, a.y)


def test_a_scanned_page_yields_only_image_objects():
    page = _pages(F.scanned_pair()[0])[0]
    assert page.has_raster
    assert page.is_image_only


def test_a_vector_page_is_not_flagged_raster():
    page = _pages(F.unchanged_pair()[0])[0]
    assert not page.has_raster and not page.is_image_only


# ----------------------------------------------------------------- control
def test_identical_pages_report_no_change():
    delta = _match(F.unchanged_pair())
    assert delta.changed == 0
    assert _states(delta)[DiffState.UNCHANGED] == 3
    assert delta.confidence == 1.0


# ------------------------------------------------------- presence changes
def test_added_object_is_exactly_one_added():
    s = _states(_match(F.added_object_pair()))
    assert s[DiffState.ADDED] == 1
    assert s[DiffState.DELETED] == 0 and s[DiffState.MODIFIED] == 0


def test_deleted_object_is_exactly_one_deleted():
    s = _states(_match(F.deleted_object_pair()))
    assert s[DiffState.DELETED] == 1
    assert s[DiffState.ADDED] == 0 and s[DiffState.MODIFIED] == 0


def test_mid_stream_insertion_does_not_cascade():
    # THE §5.1 trap. A matcher pairing object N with N+1 reports every trailing
    # object modified; LCS must align around the insertion instead.
    s = _states(_match(F.inserted_object_pair()))
    assert s[DiffState.ADDED] == 1
    assert s[DiffState.MODIFIED] == 0
    assert s[DiffState.DELETED] == 0
    assert s[DiffState.UNCHANGED] == 4      # heading + rect + line + "Notes"


# ------------------------------------------------------- position & style
def test_small_move_is_modified_not_delete_plus_add():
    delta = _match(F.moved_object_pair(dx=12))
    s = _states(delta)
    assert s[DiffState.MODIFIED] == 1
    assert s[DiffState.ADDED] == 0 and s[DiffState.DELETED] == 0
    moved = [d for d in delta.deltas if d.state == DiffState.MODIFIED][0]
    assert "moved" in moved.reason


def test_far_relocation_becomes_delete_plus_add():
    # Beyond the displacement threshold the objects no longer read as "the same
    # thing moved" (§5.1), so claiming a modification would be a false identity.
    s = _states(_match(F.relocated_object_pair()))
    assert s[DiffState.ADDED] == 1 and s[DiffState.DELETED] == 1
    assert s[DiffState.MODIFIED] == 0


def test_style_change_is_modified():
    delta = _match(F.restyled_object_pair())
    s = _states(delta)
    assert s[DiffState.MODIFIED] == 1
    assert "style" in [d.reason for d in delta.deltas if d.state == DiffState.MODIFIED][0]


def test_edited_text_is_reported_as_a_change_in_place():
    # The design choice §5.1 leaves open: signing on the string means an edit reads
    # as delete+add. Pinned here so a future change to signatures is deliberate.
    s = _states(_match(F.edited_text_pair()))
    assert s[DiffState.ADDED] == 1 and s[DiffState.DELETED] == 1
    assert s[DiffState.UNCHANGED] == 2


# ------------------------------------------------------- global alignment
def test_dominant_offset_finds_a_whole_page_shift():
    before, after = F.shifted_page_pair(dy=24)
    dx, dy = dominant_offset(_pages(before)[0].objects, _pages(after)[0].objects)
    assert (dx, dy) == (0.0, -24.0)


def test_a_whole_page_shift_reports_nothing_changed():
    # The single most important negative case: without step 1 this page reports
    # 100% modified, which is technically true and completely useless.
    delta = _match(F.shifted_page_pair(dy=24))
    assert delta.offset == (0.0, -24.0)
    assert delta.changed == 0, [d.reason for d in delta.deltas if d.state != DiffState.UNCHANGED]


def test_a_shift_plus_a_real_edit_still_isolates_the_edit():
    # Alignment must not swallow genuine changes along with the shift.
    before = F._pdf([F.text(72, 760, "Title") + F.rect(72, 600, 200, 100)
                     + F.line(72, 560, 500, 560)])
    after = F._pdf([F.text(72, 736, "Title") + F.rect(72, 576, 200, 100)
                    + F.line(72, 536, 500, 536) + F.rect(300, 400, 50, 50)])
    delta = match_page(_pages(before)[0], _pages(after)[0])
    assert delta.offset == (0.0, -24.0)
    s = _states(delta)
    assert s[DiffState.ADDED] == 1
    assert s[DiffState.MODIFIED] == 0 and s[DiffState.UNCHANGED] == 3


def test_no_dominant_shift_declines_to_align():
    # Two competing offsets must leave the page unaligned rather than half-aligned.
    before = F._pdf([F.rect(10, 10, 20, 20) + F.rect(100, 100, 30, 30)])
    after = F._pdf([F.rect(50, 10, 20, 20) + F.rect(100, 300, 30, 30)])
    assert dominant_offset(_pages(before)[0].objects, _pages(after)[0].objects) == (0.0, 0.0)


# ----------------------------------------------------------- page pairing
def test_inserted_page_leaves_its_neighbours_paired():
    before, after = F.inserted_page_pair()
    pairs = pair_pages(_pages(before), _pages(after))
    assert (0, 0) in pairs                      # page one still page one
    assert (None, 1) in pairs                   # the inserted page is added
    assert (1, 2) in pairs                      # page two moved but is unchanged


def test_deleted_page_is_reported_as_deleted():
    before, after = F.deleted_page_pair()
    pairs = pair_pages(_pages(before), _pages(after))
    assert (1, None) in pairs                   # the middle page is gone
    assert (0, 0) in pairs and (2, 1) in pairs


def test_reordered_pages_follow_their_content():
    before, after = F.reordered_page_pair()
    pairs = pair_pages(_pages(before), _pages(after))
    assert set(pairs) <= {(0, 1), (1, 0), (0, 0), (1, 1)}
    # Whatever the pairing, no page may be reported as added or deleted.
    assert all(i is not None and j is not None for i, j in pairs)


def test_pages_of_the_unchanged_pair_all_pair_up():
    before, after = F.unchanged_pair()
    assert pair_pages(_pages(before), _pages(after)) == [(0, 0)]


# ------------------------------------------------------------- confidence
def test_a_clean_vector_page_is_trustworthy():
    assert _match(F.added_object_pair()).trustworthy


def test_a_scanned_page_pair_is_not_object_matchable():
    # Image-only pages have no object identity at all, so tier 1 must not claim
    # them — this is what routes the page to the raster tier.
    page_old, page_new = _pages(F.scanned_pair()[0])[0], _pages(F.scanned_pair()[1])[0]
    assert page_old.is_image_only and page_new.is_image_only


def test_confidence_drops_when_objects_cannot_be_matched():
    # A page rewritten wholesale: low coverage, so tier 1 should not be trusted.
    before = F._pdf([b"".join(F.rect(10 * i, 10 * i, 5, 5) for i in range(1, 9))])
    after = F._pdf([b"".join(F.text(10 * i, 10 * i, f"row {i}") for i in range(1, 9))])
    delta = match_page(_pages(before)[0], _pages(after)[0])
    assert delta.confidence < MIN_CONFIDENCE
    assert not delta.trustworthy


def test_an_all_added_page_stays_trustworthy():
    # Everything genuinely new is a real answer, not a parse failure — it must not
    # be punished as low coverage or every new page would degrade to raster.
    before = F._pdf([b""])
    after = F._pdf([F.text(72, 700, "brand new") + F.rect(72, 600, 10, 10)])
    delta = match_page(_pages(before)[0], _pages(after)[0])
    assert delta.trustworthy
    assert _states(delta)[DiffState.ADDED] == 2


# --------------------------------------------------- regressions from the sweep
def test_inline_image_pixels_are_actually_read():
    # pypdf hands inline images over as a MAPPING ({"settings":…, "data":…}), not a
    # positional operand list. Iterating it as a sequence yields the KEYS, so the
    # pixel data was never read and every scanned page hashed identically —
    # two visibly different scans compared "unchanged".
    before, after = F.scanned_pair()
    b = _pages(before)[0].objects[0]
    a = _pages(after)[0].objects[0]
    assert b.kind == a.kind == "image"
    assert b.signature != a.signature, "differing scans must not share a signature"
    assert b.style == "2x2"          # settings were parsed, not just the data


def test_differing_scans_are_reported_as_changed():
    s = _states(_match(F.scanned_pair()))
    assert s[DiffState.UNCHANGED] == 0
    assert s[DiffState.ADDED] == 1 and s[DiffState.DELETED] == 1


def test_a_legitimate_addition_does_not_depress_confidence():
    # Coverage is measured against the SMALLER side. Dividing by the larger
    # conflated "not understood" with "gained content": a one-object page with one
    # addition scored 0.5 and degraded to raster for having something added to it.
    for pair in (F.added_object_pair(), F.deleted_object_pair(), F.inserted_object_pair()):
        delta = _match(pair)
        assert delta.confidence == 1.0, delta.confidence
        assert delta.trustworthy


def test_a_single_object_page_gaining_one_object_stays_tier1():
    before = F._pdf([F.rect(72, 600, 10, 10)])
    after = F._pdf([F.rect(72, 600, 10, 10) + F.rect(200, 600, 10, 10)])
    assert match_page(_pages(before)[0], _pages(after)[0]).trustworthy


def test_image_only_pages_score_zero_confidence():
    # There is no object identity to match on, so tier 1 must disclaim the page —
    # this is the signal that routes it to the raster tier.
    delta = _match(F.scanned_pair())
    assert delta.confidence == 0.0
    assert not delta.trustworthy

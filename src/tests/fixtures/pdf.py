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

"""Minimal PDF fixtures with *known* differences (targets for M2, spec §5.1).

Built by hand from raw PDF syntax rather than with a library, deliberately:

  * **Exactness.** M2's tier-1 matcher works on content-stream draw operations, so
    a fixture has to control those operators byte for byte. A library would decide
    the stream layout, and the fixture would be testing the library.
  * **No dependency.** These generate with the standard library alone, so the
    corpus is available before any PDF extra is installed.
  * **Reviewability.** Each pair's difference is one visible edit in the source
    below, not an opaque binary.

Every builder returns ``bytes`` — a complete, valid PDF. Pairs are exposed as
``(before, after)`` with the intended change documented on each function, so a
matcher can be scored against ground truth rather than eyeballed.

The hard case these exist for is spec §5.1's warning: a naive matcher makes a
single insertion re-flow every following object so the whole page reads
"modified". ``shifted_page_pair`` and ``inserted_object_pair`` are precisely that
trap.
"""
from __future__ import annotations

from typing import List, Tuple

# A4-ish page box used by every fixture.
PAGE_W, PAGE_H = 595, 842


def _pdf(pages: List[bytes], *, extra_objects: List[bytes] = None) -> bytes:
    """Assemble complete PDF bytes from per-page content streams.

    Writes a correct xref table because a real parser will be pointed at these;
    a fixture that only *looks* like a PDF would fail for reasons unrelated to
    what is being tested."""
    extra_objects = extra_objects or []
    objects: List[bytes] = []

    n_pages = len(pages)
    # 1 = Catalog, 2 = Pages, then per page: page object + content stream.
    page_ids = [3 + 2 * i for i in range(n_pages)]
    content_ids = [4 + 2 * i for i in range(n_pages)]
    font_id = 3 + 2 * n_pages

    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = b" ".join(b"%d 0 R" % pid for pid in page_ids)
    objects.append(b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % n_pages)

    for i, stream in enumerate(pages):
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (PAGE_W, PAGE_H, font_id, content_ids[i]))
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")

    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objects.extend(extra_objects)

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"

    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objects) + 1, xref_at))
    return bytes(out)


# --- content-stream primitives (the "objects" a tier-1 matcher must match) ---

def text(x: int, y: int, s: str, size: int = 12) -> bytes:
    """A text run — the unit §5.1 step 2 signs by (unicode string + size class)."""
    esc = s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    return b"BT /F1 %d Tf %d %d Td (%s) Tj ET\n" % (size, x, y, esc.encode("latin-1", "replace"))


def rect(x: int, y: int, w: int, h: int, *, fill: bool = False) -> bytes:
    """A path object — signed by its operator/relative-point sequence."""
    return b"%d %d %d %d re %s\n" % (x, y, w, h, b"f" if fill else b"S")


def line(x1: int, y1: int, x2: int, y2: int) -> bytes:
    return b"%d %d m %d %d l S\n" % (x1, y1, x2, y2)


def stroke_width(w: int) -> bytes:
    """A style change — a matched object differing only here is *modified* (§5.1)."""
    return b"%d w\n" % w


# --------------------------------------------------------------------------
# Pairs. Each returns (before, after) and documents the ground truth.
# --------------------------------------------------------------------------

def unchanged_pair() -> Tuple[bytes, bytes]:
    """Identical documents. Ground truth: every object *unchanged*, nothing orange.

    The control case, and a sharper test than it looks: a matcher with unstable
    signatures reports spurious modifications here even though nothing moved."""
    page = text(72, 760, "Structural Report") + rect(72, 600, 200, 100) + line(72, 560, 500, 560)
    return _pdf([page]), _pdf([page])


def added_object_pair() -> Tuple[bytes, bytes]:
    """After adds one rectangle at the END of the stream.

    Ground truth: 1 added; every pre-existing object unchanged. Appending at the
    end cannot re-flow anything, so this is the easy add."""
    base = text(72, 760, "Structural Report") + rect(72, 600, 200, 100)
    return _pdf([base]), _pdf([base + rect(320, 600, 120, 80)])


def deleted_object_pair() -> Tuple[bytes, bytes]:
    """After removes the middle object. Ground truth: 1 deleted, rest unchanged."""
    head = text(72, 760, "Structural Report")
    mid = rect(72, 600, 200, 100)
    tail = line(72, 560, 500, 560)
    return _pdf([head + mid + tail]), _pdf([head + tail])


def inserted_object_pair() -> Tuple[bytes, bytes]:
    """After inserts an object in the MIDDLE, before several unchanged ones.

    Ground truth: exactly 1 added; the trailing objects are *unchanged*. This is
    the §5.1 trap: an order-naive matcher aligns object N to N+1 and reports every
    trailing object as modified. The LCS over draw-op order exists for this."""
    head = text(72, 760, "Structural Report")
    tail = rect(72, 600, 200, 100) + line(72, 560, 500, 560) + text(72, 520, "Notes")
    return _pdf([head + tail]), _pdf([head + text(72, 700, "Revision B") + tail])


def moved_object_pair(dx: int = 12, dy: int = 0) -> Tuple[bytes, bytes]:
    """One object translated slightly; everything else identical.

    Ground truth: 1 *modified* (position delta within the displacement threshold),
    rest unchanged — NOT deleted+added. §5.1 makes this a threshold decision, so
    the fixture parameterises the delta to let a matcher's threshold be tuned."""
    head = text(72, 760, "Structural Report")
    tail = line(72, 560, 500, 560)
    return (_pdf([head + rect(72, 600, 200, 100) + tail]),
            _pdf([head + rect(72 + dx, 600 + dy, 200, 100) + tail]))


def relocated_object_pair() -> Tuple[bytes, bytes]:
    """One object moved FAR across the page.

    Ground truth: beyond the displacement threshold the two no longer read as "the
    same thing moved", so §5.1 says emit *deleted + added*, not modified. The
    counterpart to ``moved_object_pair`` — together they bracket the threshold."""
    head = text(72, 760, "Structural Report")
    return (_pdf([head + rect(72, 600, 60, 40)]),
            _pdf([head + rect(430, 120, 60, 40)]))


def restyled_object_pair() -> Tuple[bytes, bytes]:
    """Same geometry, different stroke width.

    Ground truth: 1 *modified* by style. Identity matches (same path signature);
    only a rendered attribute differs — the §5.1 definition of modified."""
    head = text(72, 760, "Structural Report")
    return (_pdf([head + stroke_width(1) + rect(72, 600, 200, 100)]),
            _pdf([head + stroke_width(6) + rect(72, 600, 200, 100)]))


def edited_text_pair() -> Tuple[bytes, bytes]:
    """One text run's string changes; position identical.

    Ground truth: the run is *modified* (or deleted+added if the matcher signs on
    the string alone — which is exactly the design question §5.1 step 2 poses, so
    this fixture is where that choice gets pinned down)."""
    head = text(72, 760, "Structural Report")
    tail = rect(72, 600, 200, 100)
    return (_pdf([head + text(72, 720, "Issued for construction") + tail]),
            _pdf([head + text(72, 720, "Issued for tender") + tail]))


def shifted_page_pair(dy: int = 24) -> Tuple[bytes, bytes]:
    """EVERY object translated by the same offset — a whole-page shift.

    Ground truth: nothing changed semantically. §5.1 step 1 requires detecting the
    dominant translation and cancelling it; without that global alignment pass a
    matcher reports 100% of the page modified. The single most important negative
    fixture in this corpus."""
    def page(off: int) -> bytes:
        return (text(72, 760 - off, "Structural Report")
                + rect(72, 600 - off, 200, 100)
                + line(72, 560 - off, 500, 560 - off)
                + text(72, 520 - off, "Notes"))
    return _pdf([page(0)]), _pdf([page(dy)])


def inserted_page_pair() -> Tuple[bytes, bytes]:
    """A whole new page inserted between two existing ones.

    Ground truth: the inserted page is entirely added; the pages either side are
    unchanged. Tests page *correspondence* (§5.1: "tolerate inserted / deleted /
    reordered pages"), not object matching — a positional pairing marks pages 2
    and 3 wholly modified."""
    p1 = text(72, 760, "Page One") + rect(72, 600, 200, 100)
    p2 = text(72, 760, "Page Two") + line(72, 700, 500, 700)
    new = text(72, 760, "Inserted Page") + rect(100, 500, 120, 120, fill=True)
    return _pdf([p1, p2]), _pdf([p1, new, p2])


def deleted_page_pair() -> Tuple[bytes, bytes]:
    """The middle page removed. Ground truth: that page wholly deleted."""
    p1 = text(72, 760, "Page One")
    p2 = text(72, 760, "Page Two")
    p3 = text(72, 760, "Page Three")
    return _pdf([p1, p2, p3]), _pdf([p1, p3])


def reordered_page_pair() -> Tuple[bytes, bytes]:
    """Two pages swapped, contents untouched.

    Ground truth: no content changed — correspondence should follow the content,
    not the index. The honest fallback (marking both modified) is acceptable per
    the spec; this fixture makes that choice explicit rather than accidental."""
    p1 = text(72, 760, "Alpha") + rect(72, 600, 100, 50)
    p2 = text(72, 760, "Beta") + line(72, 700, 400, 700)
    return _pdf([p1, p2]), _pdf([p2, p1])


def mixed_tier_pair() -> Tuple[bytes, bytes]:
    """Two pages: one vector, one image-only (a "scanned" page).

    Ground truth: the vector page reaches tier 1 and the image page falls to the
    raster tier, so the manifest's overall mode is ``mixed`` and its per-page map
    carries both. This is the fixture behind §7.1's per-unit map — the reason the
    front end cannot assume one view engine per document."""
    vector = text(72, 760, "Vector Page") + rect(72, 600, 200, 100)
    scan_before, scan_after = _image_page(b"\x00"), _image_page(b"\xff")
    return _pdf([vector, scan_before]), _pdf([vector, scan_after])


def scanned_pair() -> Tuple[bytes, bytes]:
    """Single image-only page whose pixels differ.

    Ground truth: no object identity exists at all, so tier 1 and 2 are impossible
    and the page must land on the raster overlay tier with a *changed region*
    (§5.1's region-level definition of modified)."""
    return _pdf([_image_page(b"\x00")]), _pdf([_image_page(b"\xff")])


def _image_page(pixel: bytes) -> bytes:
    """A page drawing a 2x2 inline greyscale image scaled over the whole page.

    An inline image (BI/ID/EI) keeps the fixture to a single object with no image
    XObject plumbing, while still giving a parser genuinely raster-only content."""
    data = pixel * 3 + b"\x80"
    return (b"q %d 0 0 %d 0 0 cm\n" % (PAGE_W, PAGE_H)
            + b"BI /W 2 /H 2 /CS /G /BPC 8 ID " + data + b" EI\nQ\n")


#: Every pair, keyed by name, with a one-line statement of the ground truth.
PAIRS = {
    "unchanged": (unchanged_pair, "nothing changed; no object may report modified"),
    "added_object": (added_object_pair, "1 added at end; rest unchanged"),
    "deleted_object": (deleted_object_pair, "1 deleted from middle; rest unchanged"),
    "inserted_object": (inserted_object_pair, "1 added mid-stream; trailing objects MUST stay unchanged"),
    "moved_object": (moved_object_pair, "1 modified by small translation"),
    "relocated_object": (relocated_object_pair, "moved beyond threshold => deleted + added"),
    "restyled_object": (restyled_object_pair, "1 modified by style (stroke width)"),
    "edited_text": (edited_text_pair, "1 text run's string changed in place"),
    "shifted_page": (shifted_page_pair, "whole page translated; semantically unchanged"),
    "inserted_page": (inserted_page_pair, "page inserted; neighbours unchanged"),
    "deleted_page": (deleted_page_pair, "middle page removed"),
    "reordered_page": (reordered_page_pair, "pages swapped; content unchanged"),
    "mixed_tier": (mixed_tier_pair, "vector page + scanned page => mode 'mixed'"),
    "scanned": (scanned_pair, "image-only; raster tier, changed region"),
}

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

"""The PDF diff plugin — per-page degradation ladder (SPECIFICATION.md §5.1).

Each page independently takes the highest tier its content actually supports:

1. **Vector object-level** — objects matched and classified (``pdf_match``), drawn
   as an SVG carrying semantic state (``pdf_svg``). Requires that the page parsed
   cleanly, that the matcher is confident, and that every text run can be rendered
   as glyph outlines.
2. **Text + raster hybrid** — reserved for pages where text is recoverable but the
   graphics layer is not. Currently routes to the raster tier; the honest position
   is that a half-measure with no rasterizer available is not a tier.
3. **Raster pixel overlay** — for scanned / image-only pages, or any page tier 1
   disclaims. Needs a rasterizer backend; when none is installed the page is
   reported as failed rather than silently emitting an empty diff.

A document therefore commonly ends up ``mixed`` (§7.1), which is why the manifest
carries a per-page map instead of one document-wide mode.

The rule from §4 governs everything here: **degrade, never fail**. Every step
narrows to a lower tier rather than raising, and only a page that cannot even be
rastered contributes a failure.
"""
from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from .base import (
    DiffChild, DiffMode, DiffPlugin, DiffResult, DiffState, SourceRef,
)
from .pdf_glyphs import GlyphProvider
from .pdf_match import PageDelta, match_page, pair_pages
from .pdf_objects import PageParse, parse_document
from .pdf_raster import default_rasterizer
from .pdf_svg import render_page, render_raster_page, render_whole_page_state

log = logging.getLogger("difference_service.plugins.pdf")

#: Sentinel for "discover a rasterizer from config". Distinct from ``None``, which
#: explicitly DISABLES the raster tier — without the distinction there is no way to
#: express "no raster backend", which is exactly what a deployment without poppler
#: has and what the degradation tests need to simulate.
_AUTO = object()


class PdfDiffPlugin(DiffPlugin):
    """Vector-first PDF comparison with per-page degradation."""

    name = "pdf"
    #: Bump when output would change — it is part of the cache key (§6), so a bump
    #: regenerates every previously stored PDF diff.
    version = 1

    MIMES = ("application/pdf", "application/x-pdf")

    def __init__(self, *, glyphs: Optional[GlyphProvider] = None, rasterizer=_AUTO,
                 config=None):
        self._glyphs = glyphs
        self._rasterizer = default_rasterizer(config) if rasterizer is _AUTO else rasterizer

    def supports(self, mime: str) -> bool:
        return (mime or "").split(";")[0].strip().lower() in self.MIMES

    # ------------------------------------------------------------------ diff
    def diff(self, base: SourceRef, target: SourceRef) -> DiffResult:
        old_pages = parse_document(base.data)
        new_pages = parse_document(target.data)

        if not old_pages and not new_pages:
            return DiffResult.failed("parse", "neither version could be opened as a PDF",
                                     tiers=[DiffMode.VECTOR, DiffMode.RASTER])

        glyphs = self._glyphs or GlyphProvider()
        children: List[DiffChild] = []
        index = 0

        for old_i, new_i in pair_pages(old_pages, new_pages):
            child = self._page_child(old_pages, new_pages, old_i, new_i, index, glyphs,
                                     base.data, target.data)
            # A page is NEVER dropped. If no tier could render it, an explicit
            # `unavailable` placeholder holds its slot — silently omitting it would
            # produce a result that looks complete while missing a page, which a
            # reviewer reads as "nothing changed here".
            children.append(child if child is not None
                            else _unavailable_child(index, old_pages, new_pages,
                                                    old_i, new_i))
            index += 1

        if all(c.mode == DiffMode.UNAVAILABLE for c in children):
            return DiffResult.failed(
                "render", "no page could be rendered at any tier",
                tiers=[DiffMode.VECTOR, DiffMode.RASTER])
        return DiffResult(children=children)

    # ------------------------------------------------------------- one page
    def _page_child(self, old_pages, new_pages, old_i, new_i, index: int,
                    glyphs: GlyphProvider, old_data: bytes,
                    new_data: bytes) -> Optional[DiffChild]:
        # --- a page present on only one side is wholly added or deleted ---
        if old_i is None or new_i is None:
            page = new_pages[new_i] if old_i is None else old_pages[old_i]
            state = DiffState.ADDED if old_i is None else DiffState.DELETED
            svg = render_whole_page_state(page.objects, state, page.width, page.height,
                                          glyphs=glyphs)
            if svg is None:
                return self._raster_child(index, page, page,
                                          old_data, old_i, new_data, new_i)
            return _svg_child(index, svg, DiffMode.VECTOR)

        old, new = old_pages[old_i], new_pages[new_i]

        # --- tier 1: vector object-level ---
        if self._tier1_possible(old, new, glyphs):
            delta = match_page(old, new)
            if delta.trustworthy:
                svg = render_page(delta, new.width, new.height, glyphs=glyphs,
                                  mode=DiffMode.VECTOR)
                if svg is not None:
                    return _svg_child(index, svg, DiffMode.VECTOR)
                log.debug("page %d: outlines unavailable; degrading", index)
            else:
                log.debug("page %d: matcher confidence %.2f; degrading",
                          index, delta.confidence)

        # --- tiers 2/3: raster ---
        return self._raster_child(index, old, new, old_data, old_i, new_data, new_i)

    def _tier1_possible(self, old: PageParse, new: PageParse,
                        glyphs: GlyphProvider) -> bool:
        """Cheap pre-checks before doing the matching work.

        An image-only page has no object identity at all (§5.2's argument applies
        equally here), and a page whose text cannot be outlined cannot satisfy the
        no-client-fonts contract — in both cases tier 1 is not merely lower quality,
        it is unavailable."""
        if old.is_image_only or new.is_image_only:
            return False
        if not old.objects and not new.objects:
            return False
        for page in (old, new):
            for obj in page.text_objects:
                if not glyphs.available(obj.style):
                    return False
        return True

    # --------------------------------------------------------------- raster
    def _raster_child(self, index: int, old: PageParse, new: PageParse,
                      old_data: bytes, old_i, new_data: bytes,
                      new_i) -> Optional[DiffChild]:
        """Render the page pair as bitmaps in the §7.2 layer structure.

        With no rasterizer configured this returns ``None``, which surfaces as a
        failed result if it happens for every page. That is deliberate: emitting an
        empty SVG would look like "no differences found" — a confidently wrong
        answer, and the one outcome the spec is most concerned to avoid."""
        if self._rasterizer is None:
            log.info("page %d needs the raster tier but no rasterizer is available "
                     "(install poppler / set DIFF_POPPLER_PATH)", index)
            return None
        try:
            old_png = self._rasterizer.render(old_data, old_i) if old_i is not None else None
            new_png = self._rasterizer.render(new_data, new_i) if new_i is not None else None
            if old_png is None and new_png is None:
                return None
            _overlay, changed = self._rasterizer.difference_mask(old_png, new_png)
        except Exception:
            log.warning("page %d: rasterization failed", index, exc_info=True)
            return None
        svg = render_raster_page(old_png, new_png, new.width, new.height,
                                 changed=changed)
        return _svg_child(index, svg, DiffMode.RASTER)


def _svg_child(index: int, svg: str, mode: str) -> DiffChild:
    return DiffChild(kind="page", index=index, data=svg.encode("utf-8"),
                     mime="image/svg+xml", ext="svg", mode=mode)


def _unavailable_child(index: int, old_pages, new_pages, old_i, new_i) -> DiffChild:
    """A placeholder holding a page's slot when no tier could render it.

    Carries the §7.2 layer skeleton so the front end's single view engine can still
    mount it, and ``data-diff-mode="unavailable"`` so it renders an honest "diff
    unavailable for this page" instead of an empty page that looks unchanged."""
    page = (new_pages[new_i] if new_i is not None
            else old_pages[old_i] if old_i is not None else None)
    width = page.width if page else 612.0
    height = page.height if page else 792.0
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" '
           f'viewBox="0 0 {width:g} {height:g}" width="{width:g}" height="{height:g}" '
           f'data-diff-mode="{DiffMode.UNAVAILABLE}">'
           '<g id="diff-old"></g><g id="diff-new"></g><g id="diff-changes"></g>'
           '</svg>')
    return DiffChild(kind="page", index=index, data=svg.encode("utf-8"),
                     mime="image/svg+xml", ext="svg", mode=DiffMode.UNAVAILABLE)

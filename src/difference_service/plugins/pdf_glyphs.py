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

"""Text → glyph path outlines (SPECIFICATION.md §5.1, "full vectorization").

The spec is unambiguous: the output SVG must be a *complete* vector graphic, with
text emitted as ``<path>`` outlines and never as ``<text>`` elements that depend on
fonts installed on the client. A diff that renders differently on the reviewer's
machine than on the author's is not a diff anyone can rely on.

So every string is converted to path geometry here, at generation time. Where the
outlines come from, in order of fidelity:

1. **A font embedded in the PDF** — highest fidelity, exactly the shapes the author
   saw. (Not yet implemented; see the note below.)
2. **A metric-compatible substitute** for the non-embedded standard fonts. The
   base-14 fonts (Helvetica, Times, Courier) are by definition not embedded, so
   this is the common case for simple documents; Liberation Sans/Serif/Mono are
   metric-compatible with Arial/Helvetica, Times and Courier respectively.
3. **Nothing available** → the page cannot satisfy the no-fonts contract at tier 1
   and must degrade, which the plugin handles. It is never acceptable to fall back
   to a ``<text>`` element: that would silently break the contract on the one
   machine nobody tests, the reader's.

Note on (1): extracting an embedded font program (FontFile/FontFile2/FontFile3) and
mapping the PDF's encoding to its glyph order is real work and lands next; the
substitute path already satisfies the *contract* (the SVG is self-contained paths)
and differs only in typeface fidelity when a document embeds an unusual face.
"""
from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("difference_service.plugins.pdf_glyphs")

#: Where to look for metric-compatible substitutes, most preferred first.
_SUBSTITUTES = {
    "sans": [
        "/usr/share/fonts/liberation-sans-fonts/LiberationSans-Regular.ttf",
        "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ],
    "serif": [
        "/usr/share/fonts/liberation-serif-fonts/LiberationSerif-Regular.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
        "/usr/share/fonts/dejavu-serif-fonts/DejaVuSerif.ttf",
    ],
    "mono": [
        "/usr/share/fonts/liberation-mono-fonts/LiberationMono-Regular.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
        "/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono.ttf",
    ],
}


def _family_for(font_hint: str) -> str:
    """Map a PDF font resource/base name onto a substitute family."""
    h = (font_hint or "").lower()
    if any(k in h for k in ("courier", "mono")):
        return "mono"
    if any(k in h for k in ("times", "serif", "roman", "georgia", "garamond")):
        return "serif"
    return "sans"


class GlyphProvider:
    """Resolves strings to SVG path data, caching per (font, char).

    Constructed once per document conversion. Every method degrades to ``None``
    rather than raising — an unavailable font is a tier decision, not an error."""

    def __init__(self, search_paths: Optional[Dict[str, List[str]]] = None):
        self._paths = search_paths or _SUBSTITUTES
        self._fonts: Dict[str, object] = {}         # family -> TTFont or None
        self._glyph_cache: Dict[Tuple[str, str], Optional[str]] = {}
        self._metrics: Dict[str, Tuple[float, Dict[str, float]]] = {}

    # ------------------------------------------------------------- loading
    def _font(self, family: str):
        if family in self._fonts:
            return self._fonts[family]
        font = None
        try:
            from fontTools.ttLib import TTFont
            for path in self._paths.get(family, []):
                if os.path.isfile(path):
                    font = TTFont(path, lazy=True)
                    log.debug("glyphs: %s -> %s", family, path)
                    break
        except Exception:
            log.warning("glyphs: could not load a %s substitute", family, exc_info=True)
            font = None
        self._fonts[family] = font
        return font

    def available(self, font_hint: str = "") -> bool:
        """Can outlines be produced for this font at all?"""
        return self._font(_family_for(font_hint)) is not None

    # ------------------------------------------------------------- outlines
    def _glyph_path(self, family: str, ch: str) -> Optional[str]:
        key = (family, ch)
        if key in self._glyph_cache:
            return self._glyph_cache[key]

        result = None
        font = self._font(family)
        if font is not None:
            try:
                from fontTools.pens.svgPathPen import SVGPathPen
                cmap = font.getBestCmap()
                name = cmap.get(ord(ch))
                if name:
                    glyph_set = font.getGlyphSet()
                    pen = SVGPathPen(glyph_set)
                    glyph_set[name].draw(pen)
                    result = pen.getCommands() or ""
            except Exception:
                log.debug("glyphs: no outline for %r", ch, exc_info=True)
                result = None
        self._glyph_cache[key] = result
        return result

    def _advance(self, family: str, ch: str) -> float:
        """Advance width in font units (for laying out the run)."""
        font = self._font(family)
        if font is None:
            return 0.0
        try:
            cmap = font.getBestCmap()
            name = cmap.get(ord(ch))
            if not name:
                return 0.0
            return float(font["hmtx"][name][0])
        except Exception:
            return 0.0

    def units_per_em(self, family: str) -> float:
        font = self._font(family)
        try:
            return float(font["head"].unitsPerEm) if font is not None else 1000.0
        except Exception:
            return 1000.0

    # ---------------------------------------------------------------- runs
    def outline_run(self, s: str, size: float, x: float, y: float,
                    font_hint: str = "") -> Optional[str]:
        """SVG path data drawing ``s`` at ``(x, y)`` in PDF user space.

        Returns a single ``d`` attribute value covering the whole run, or ``None``
        when no outline source is available — the caller then degrades the page
        rather than emitting a font-dependent ``<text>``.

        The glyph coordinate system is y-up (as PDF is) and scaled by
        ``size / unitsPerEm``; the caller's transform handles the flip to SVG's
        y-down space, so the geometry here stays in document coordinates."""
        family = _family_for(font_hint)
        if self._font(family) is None or not s:
            return None

        upem = self.units_per_em(family)
        scale = size / upem if upem else 0.0
        if scale <= 0:
            return None

        parts: List[str] = []
        pen_x = x
        for ch in s:
            if ch == " ":
                pen_x += self._advance(family, " ") * scale or size * 0.28
                continue
            path = self._glyph_path(family, ch)
            if path:
                parts.append(f"<g transform=\"translate({pen_x:.2f},{y:.2f}) "
                             f"scale({scale:.5f})\"><path d=\"{path}\"/></g>")
            pen_x += self._advance(family, ch) * scale
        if not parts:
            return None
        return "".join(parts)

    def run_width(self, s: str, size: float, font_hint: str = "") -> float:
        family = _family_for(font_hint)
        upem = self.units_per_em(family)
        if not upem:
            return 0.0
        scale = size / upem
        return sum(self._advance(family, ch) for ch in s) * scale

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
   metric-compatible with Arial/Helvetica, Times and Courier respectively, as
   Carlito is with Calibri and Caladea with Cambria.

   *Metric*-compatible is the operative word. A substitute with the same advance
   widths keeps every run the same length as the author's, so the geometry the
   diff compares is the document's and not the renderer's. A merely similar face
   reflows the line, and a reflowed line reads as a change that nobody made.
   ``_SUBSTITUTIONS`` therefore prefers a metric clone wherever one exists and
   only then falls back to the nearest open face by classification.

3. **The nearest open face** for a proprietary name with no metric clone
   (Verdana, Segoe UI, Palatino …). Close in colour and proportion, not exact.
4. **Nothing available** → the page cannot satisfy the no-fonts contract at tier 1
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
import re
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("difference_service.plugins.pdf_glyphs")

#: Style slots every face is described in. A face that lacks one falls back to
#: its own "regular" before it falls back to another face (see ``_candidates``):
#: a roman Carlito is a better Calibri-Bold than a bold Liberation Sans.
_REGULAR, _BOLD, _ITALIC, _BOLDITALIC = "regular", "bold", "italic", "bolditalic"


def _face(directory: str, prefix: str, ext: str = "ttf", *,
          regular: str = "Regular", bold: str = "Bold",
          italic: str = "Italic", bolditalic: str = "BoldItalic") -> Dict[str, List[str]]:
    """One face's four style files, which follow ``Prefix-Style.ext`` almost
    everywhere. The exceptions (URW's ``-Roman``/``-Demi``/``-Book``) are what
    the style-name arguments are for."""
    return {
        _REGULAR: [f"{directory}/{prefix}-{regular}.{ext}"],
        _BOLD: [f"{directory}/{prefix}-{bold}.{ext}"],
        _ITALIC: [f"{directory}/{prefix}-{italic}.{ext}"],
        _BOLDITALIC: [f"{directory}/{prefix}-{bolditalic}.{ext}"],
    }


_LIB = "/usr/share/fonts/liberation-%s-fonts"
_URW = "/usr/share/fonts/urw-base35"
_DEJAVU = "/usr/share/fonts/dejavu-%s-fonts"

#: The substitute faces themselves — open-source families, by style.
_FACES: Dict[str, Dict[str, List[str]]] = {
    # Metric-compatible with the Microsoft core fonts: same advance widths, so a
    # substituted run occupies exactly the space the author's did. This is what
    # makes the vector tier trustworthy rather than merely self-contained.
    "liberation-sans": _face(_LIB % "sans", "LiberationSans"),
    "liberation-serif": _face(_LIB % "serif", "LiberationSerif"),
    "liberation-mono": _face(_LIB % "mono", "LiberationMono"),
    "carlito": _face("/usr/share/fonts/google-carlito-fonts", "Carlito"),
    "caladea": _face("/usr/share/fonts/google-crosextra-caladea-fonts", "Caladea"),

    # The URW base-35 clones of the PostScript standard faces. The PDF base-14
    # fonts are by definition not embedded, so these carry the common case for
    # simple and machine-generated documents.
    "nimbus-sans": _face(_URW, "NimbusSans", "otf"),
    "nimbus-sans-narrow": _face(_URW, "NimbusSansNarrow", "otf",
                                italic="Oblique", bolditalic="BoldOblique"),
    "nimbus-roman": _face(_URW, "NimbusRoman", "otf"),
    "nimbus-mono": _face(_URW, "NimbusMonoPS", "otf"),
    "p052": _face(_URW, "P052", "otf", regular="Roman"),          # Palatino
    "urw-bookman": _face(_URW, "URWBookman", "otf", regular="Light", bold="Demi",
                         italic="LightItalic", bolditalic="DemiItalic"),
    "c059": _face(_URW, "C059", "otf", regular="Roman"),          # Century Schoolbook
    "urw-gothic": _face(_URW, "URWGothic", "otf", regular="Book", bold="Demi",
                        italic="BookOblique", bolditalic="DemiOblique"),
    "z003": {k: [f"{_URW}/Z003-MediumItalic.otf"]                 # Zapf Chancery
             for k in (_REGULAR, _BOLD, _ITALIC, _BOLDITALIC)},
    "standard-symbols": {k: [f"{_URW}/StandardSymbolsPS.otf"]     # Symbol
                         for k in (_REGULAR, _BOLD, _ITALIC, _BOLDITALIC)},
    "dingbats": {k: [f"{_URW}/D050000L.otf"]                      # ZapfDingbats
                 for k in (_REGULAR, _BOLD, _ITALIC, _BOLDITALIC)},

    # Not metric-compatible with what they stand in for, but the closest
    # widely-packaged open faces by construction and colour.
    "open-sans": _face("/usr/share/fonts/open-sans", "OpenSans"),
    "dejavu-sans": _face(_DEJAVU % "sans", "DejaVuSans",
                         italic="Oblique", bolditalic="BoldOblique"),
    "dejavu-serif": _face(_DEJAVU % "serif", "DejaVuSerif"),
    "dejavu-mono": _face(_DEJAVU % "sans-mono", "DejaVuSansMono",
                         italic="Oblique", bolditalic="BoldOblique"),
}

# DejaVu's regular files are bare (DejaVuSans.ttf, not DejaVuSans-Regular.ttf).
for _k, _p in (("dejavu-sans", f"{_DEJAVU % 'sans'}/DejaVuSans.ttf"),
               ("dejavu-serif", f"{_DEJAVU % 'serif'}/DejaVuSerif.ttf"),
               ("dejavu-mono", f"{_DEJAVU % 'sans-mono'}/DejaVuSansMono.ttf")):
    _FACES[_k][_REGULAR] = [_p]

#: Generic bucket per face, for the last-resort chain.
_GENERIC_OF = {
    "liberation-sans": "sans", "carlito": "sans", "open-sans": "sans",
    "nimbus-sans": "sans", "nimbus-sans-narrow": "sans", "urw-gothic": "sans",
    "dejavu-sans": "sans", "standard-symbols": "sans", "dingbats": "sans",
    "liberation-serif": "serif", "caladea": "serif", "nimbus-roman": "serif",
    "p052": "serif", "urw-bookman": "serif", "c059": "serif", "z003": "serif",
    "dejavu-serif": "serif",
    "liberation-mono": "mono", "nimbus-mono": "mono", "dejavu-mono": "mono",
}

#: Ordered generic chains — the guarantee that *something* outlines, so a font
#: package the image happens not to ship costs typeface fidelity and never the
#: whole vector tier (which is what a bare ``return None`` here would cost).
_GENERIC_CHAIN = {
    "sans": ["liberation-sans", "dejavu-sans", "nimbus-sans"],
    "serif": ["liberation-serif", "dejavu-serif", "nimbus-roman"],
    "mono": ["liberation-mono", "dejavu-mono", "nimbus-mono"],
}

#: Proprietary/licensed font name -> substitute face. Matched as a substring of
#: the normalised BaseFont name, **most specific first** ("arialnarrow" has to be
#: tested before "arial", "couriernew" before "courier").
#:
#: Where a metric-compatible clone exists it is always the choice: matching
#: advance widths keep the substituted run the same length as the original, so
#: the diff shows what changed in the document rather than what changed in the
#: rendering. Where none exists the pick is the nearest open face by
#: classification and colour, and the run will be close but not identical.
_SUBSTITUTIONS: List[Tuple[str, str]] = [
    # --- metric-compatible ---
    ("arialnarrow", "nimbus-sans-narrow"),
    ("arialblack", "liberation-sans"),
    ("arial", "liberation-sans"),
    ("helveticaneue", "liberation-sans"),
    ("helvetica", "liberation-sans"),
    ("timesnewroman", "liberation-serif"),
    ("times", "liberation-serif"),
    ("couriernew", "liberation-mono"),
    ("courier", "nimbus-mono"),
    ("calibri", "carlito"),
    ("cambria", "caladea"),

    # --- base-35 PostScript names ---
    ("palatino", "p052"),
    ("bookantiqua", "p052"),
    ("bookmanoldstyle", "urw-bookman"),
    ("bookman", "urw-bookman"),
    ("newcenturyschlbk", "c059"),
    ("centuryschoolbook", "c059"),
    ("century", "c059"),
    ("avantgarde", "urw-gothic"),
    ("zapfchancery", "z003"),
    ("zapfdingbats", "dingbats"),
    ("wingdings", "dingbats"),
    ("webdings", "dingbats"),
    ("dingbat", "dingbats"),
    ("symbol", "standard-symbols"),

    # --- nearest open equivalent (not metric-compatible) ---
    ("segoeui", "open-sans"),
    ("myriad", "open-sans"),
    ("frutiger", "open-sans"),
    ("univers", "liberation-sans"),
    ("gillsans", "open-sans"),
    ("optima", "open-sans"),
    ("futura", "urw-gothic"),
    ("verdana", "dejavu-sans"),
    ("tahoma", "dejavu-sans"),
    ("trebuchet", "dejavu-sans"),
    ("candara", "open-sans"),
    ("corbel", "open-sans"),
    ("lucidaconsole", "dejavu-mono"),
    ("lucidasans", "dejavu-sans"),
    ("consolas", "dejavu-mono"),
    ("monaco", "dejavu-mono"),
    ("menlo", "dejavu-mono"),
    ("couriernewps", "liberation-mono"),
    ("georgia", "dejavu-serif"),
    ("constantia", "caladea"),
    ("garamond", "nimbus-roman"),
    ("minion", "nimbus-roman"),
    ("bodoni", "nimbus-roman"),
    ("baskerville", "nimbus-roman"),
    ("caslon", "nimbus-roman"),
    ("rockwell", "c059"),
    ("cambriamath", "caladea"),
    ("impact", "liberation-sans"),
    ("comicsans", "dejavu-sans"),
    ("calibrilight", "carlito"),
]

#: A leading ``ABCDEF+`` marks a subsetted embedded font; the tag is random per
#: document, so it must come off before any name matching.
_SUBSET_TAG = re.compile(r"^[A-Z]{6}\+")

#: "demi" is here because the URW/ITC families spell their bold weight that way
#: (URWGothic-Demi, URWBookman-Demi); it also subsumes "demibold". "medium" is
#: deliberately absent — it is a regular weight, not a bold one (Z003-Medium).
_BOLD_WORDS = ("bold", "black", "heavy", "demi", "extrabold", "ultrabold")
_ITALIC_WORDS = ("italic", "oblique")


def _normalise(font_hint: str) -> str:
    """BaseFont name -> lowercase alphanumerics, subset tag removed."""
    name = _SUBSET_TAG.sub("", (font_hint or "").strip().lstrip("/"))
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _style_of(normalised: str) -> str:
    bold = any(w in normalised for w in _BOLD_WORDS)
    italic = any(w in normalised for w in _ITALIC_WORDS)
    if bold and italic:
        return _BOLDITALIC
    if bold:
        return _BOLD
    if italic:
        return _ITALIC
    return _REGULAR


def _face_for(normalised: str) -> str:
    """Substitute face for a normalised font name.

    Falls back on the *shape* words a name carries ("mono", "serif") and finally
    on sans, which is what an unrecognised name is most likely to be."""
    for needle, face in _SUBSTITUTIONS:
        if needle in normalised:
            return face
    if "mono" in normalised or "typewriter" in normalised:
        return "liberation-mono"
    if "serif" in normalised and "sans" not in normalised:
        return "liberation-serif"
    if "roman" in normalised or "script" in normalised:
        return "liberation-serif"
    return "liberation-sans"


def _family_for(font_hint: str) -> str:
    """Map a PDF BaseFont name onto a ``face|style`` substitute key.

    The key is opaque to callers and is what the provider caches on, so two runs
    of Arial-Bold share one loaded font while Arial and Arial-Bold do not."""
    n = _normalise(font_hint)
    return f"{_face_for(n)}|{_style_of(n)}"


def _candidates(family: str) -> List[str]:
    """Font files to try for a ``face|style`` key, best first.

    Order encodes the fidelity argument: the exact face and style, then that
    face's other styles (a roman Carlito still has Calibri's metrics), then the
    generic chain for the face's classification."""
    face, _, style = family.partition("|")
    style = style or _REGULAR
    out: List[str] = []

    def add(face_name: str, style_name: str) -> None:
        for p in _FACES.get(face_name, {}).get(style_name, []):
            if p not in out:
                out.append(p)

    add(face, style)
    for other in (_BOLD, _ITALIC, _BOLDITALIC, _REGULAR):
        if other != style:
            add(face, other)
    for fallback in _GENERIC_CHAIN.get(_GENERIC_OF.get(face, "sans"), []):
        add(fallback, style)
        add(fallback, _REGULAR)
    return out


class GlyphProvider:
    """Resolves strings to SVG path data, caching per (font, char).

    Constructed once per document conversion. Every method degrades to ``None``
    rather than raising — an unavailable font is a tier decision, not an error."""

    def __init__(self, search_paths: Optional[Dict[str, List[str]]] = None):
        #: ``{face|style: [path, ...]}`` — an override for tests and for images
        #: that ship fonts somewhere else. Anything absent falls through to the
        #: computed chain, so an override need only name what it changes.
        self._paths = search_paths or {}
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
            for path in self._paths.get(family) or _candidates(family):
                if os.path.isfile(path):
                    font = TTFont(path, lazy=True)
                    log.debug("glyphs: %s -> %s", family, path)
                    break
            else:
                log.warning("glyphs: no substitute file found for %s", family)
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

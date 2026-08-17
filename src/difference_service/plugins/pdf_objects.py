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

"""Extract drawable objects from a PDF page (SPECIFICATION.md §5.1, step 2).

A PDF has **no object identity** — it is a stream of drawing instructions, not a
document model. Tier-1 matching therefore has to *derive* identity, and this module
produces the units that identity is derived for: text runs, path objects, and
inline images, each with the two things the matcher needs kept strictly apart:

  * a **signature** — position-INDEPENDENT: what the thing *is*. Absolute page
    coordinates are deliberately excluded (§5.1 step 2), because including them is
    what makes a one-line insertion re-flow every following object into "modified".
  * a **position and style** — what a matched object is then compared *on*, to
    decide unchanged vs modified.

Graphics state modelled: the CTM (``cm`` within ``q``/``Q``), line width, and the
text matrix family (``Tm``/``Td``/``TD``/``T*``/``TL``). That is enough for the
positioning real documents use. Anything not modelled degrades the page's
confidence rather than producing a confidently wrong object — see ``PageParse``.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

#: Rounding for coordinates when building signatures/keys, in PDF units (1/72").
#: Coarse enough to absorb float noise, fine enough not to merge distinct objects.
_GRID = 0.5

#: Font sizes are bucketed so a 11.999 vs 12.0 difference is not a "style change".
_SIZE_BUCKET = 0.5

Matrix = Tuple[float, float, float, float, float, float]

IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def mat_mul(m: Matrix, n: Matrix) -> Matrix:
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (a * a2 + b * c2, a * b2 + b * d2,
            c * a2 + d * c2, c * b2 + d * d2,
            e * a2 + f * c2 + e2, e * b2 + f * d2 + f2)


def apply(m: Matrix, x: float, y: float) -> Tuple[float, float]:
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def _q(v: float, grid: float = _GRID) -> float:
    return round(v / grid) * grid


def _num(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


@dataclass
class PageObject:
    """One drawable unit on a page."""
    kind: str                     # "text" | "path" | "image"
    signature: str                # position-independent identity key (§5.1 step 2)
    x: float = 0.0                # anchor position on the page...
    y: float = 0.0                # ...in PDF user space
    style: str = ""               # rendered attributes compared for "modified"
    text: str = ""                # the string, for text runs (diagnostics/SVG)
    size: float = 0.0             # font size, for text runs
    points: List[Tuple[float, float]] = field(default_factory=list)  # path geometry
    ops: List[str] = field(default_factory=list)                     # raw operators
    index: int = 0                # position in draw order

    @property
    def pos(self) -> Tuple[float, float]:
        return (self.x, self.y)


@dataclass
class PageParse:
    """Everything extracted from one page, plus how well it was understood."""
    objects: List[PageObject] = field(default_factory=list)
    width: float = 612.0
    height: float = 792.0
    #: Operators encountered that this extractor does not model. Their presence
    #: lowers tier-1 confidence rather than being silently ignored — an object we
    #: could not place is worse than a page we honestly downgrade (§5.1).
    unknown_ops: List[str] = field(default_factory=list)
    #: True when the page draws raster content (an inline image or an image
    #: XObject) — a signal toward the hybrid/raster tier.
    has_raster: bool = False
    #: True when the page has NO vector/text objects at all (a scan).
    @property
    def is_image_only(self) -> bool:
        return self.has_raster and not any(o.kind != "image" for o in self.objects)

    @property
    def text_objects(self) -> List[PageObject]:
        return [o for o in self.objects if o.kind == "text"]


# --------------------------------------------------------------- signatures

def text_signature(s: str, size: float, font: str) -> str:
    """Identity of a text run: the string + a size bucket + the font resource.

    Position is excluded on purpose. The *string* is the strongest identity signal
    a PDF offers for text, so an edited string yields a different signature and
    reads as delete+add rather than modify — which §5.1 leaves as a design choice.
    Keeping the string in the key is the safer half of that trade: it never claims
    two different sentences are "the same text, modified"."""
    return f"T|{font}|{_q(size, _SIZE_BUCKET)}|{s}"


def path_signature(points: Sequence[Tuple[float, float]], ops: Sequence[str]) -> str:
    """Identity of a path: its operator sequence + points made RELATIVE to the first.

    Translating to the first point is what makes the signature position-independent,
    so the same rectangle drawn anywhere on the page shares one identity and a move
    is detectable as a move instead of a delete plus an add."""
    if not points:
        return "P|" + ",".join(ops)
    ox, oy = points[0]
    rel = ";".join(f"{_q(x - ox)},{_q(y - oy)}" for x, y in points)
    return "P|" + ",".join(ops) + "|" + rel


def image_signature(data: bytes, w: int, h: int) -> str:
    digest = hashlib.sha256(data or b"").hexdigest()[:16]
    return f"I|{w}x{h}|{digest}"


# ------------------------------------------------------------------ parsing

_PATH_PAINT = {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "n"}
_TEXT_SHOW = {"Tj", "TJ", "'", '"'}

#: Operators we knowingly ignore because they do not affect object identity or
#: placement (colour is captured via style where it matters).
_BENIGN = {
    "BT", "ET", "q", "Q", "cm", "w", "Tf", "Td", "TD", "Tm", "T*", "TL", "TD",
    "g", "G", "rg", "RG", "k", "K", "cs", "CS", "sc", "SC", "scn", "SCN",
    "gs", "J", "j", "M", "d", "ri", "i", "W", "W*", "Tc", "Tw", "Tz", "Ts", "Tr",
    "BI", "ID", "EI", "INLINE IMAGE", "Do", "BMC", "BDC", "EMC", "MP", "DP", "sh",
}


class _State:
    """Graphics + text state tracked while walking the content stream."""

    def __init__(self):
        self.ctm: Matrix = IDENTITY
        self.line_width: float = 1.0
        self.fill: str = ""
        self.stroke: str = ""
        self.font: str = ""
        self.size: float = 0.0
        self.tm: Matrix = IDENTITY      # text matrix
        self.tlm: Matrix = IDENTITY     # text line matrix
        self.leading: float = 0.0

    def clone(self) -> "_State":
        s = _State()
        s.__dict__.update(self.__dict__)
        return s


def parse_page(page, reader=None) -> PageParse:
    """Extract the drawable objects of one pypdf page.

    Never raises for content reasons: a page that cannot be tokenized returns an
    empty parse with its failure recorded in ``unknown_ops``, which the tier logic
    reads as "do not attempt tier 1 here"."""
    from pypdf.generic import ContentStream

    out = PageParse()
    try:
        box = page.mediabox
        out.width, out.height = float(box.width), float(box.height)
    except Exception:
        pass

    try:
        content = ContentStream(page.get_contents(), reader)
        operations = content.operations
    except Exception as e:                       # unreadable stream -> no tier 1
        out.unknown_ops.append(f"<parse-error:{type(e).__name__}>")
        return out

    state = _State()
    stack: List[_State] = []
    current: List[Tuple[float, float]] = []      # in-progress path points
    current_ops: List[str] = []
    index = 0

    for operands, raw_op in operations:
        op = raw_op.decode("latin-1") if isinstance(raw_op, (bytes, bytearray)) else str(raw_op)

        # --- graphics state ---
        if op == "q":
            stack.append(state.clone())
        elif op == "Q":
            state = stack.pop() if stack else _State()
        elif op == "cm" and len(operands) >= 6:
            state.ctm = mat_mul(tuple(_num(v) for v in operands[:6]), state.ctm)
        elif op == "w" and operands:
            state.line_width = _num(operands[0], 1.0)
        elif op in ("g", "rg", "k"):
            state.fill = ",".join(f"{_num(v):.3f}" for v in operands)
        elif op in ("G", "RG", "K"):
            state.stroke = ",".join(f"{_num(v):.3f}" for v in operands)

        # --- text state ---
        elif op == "BT":
            state.tm = state.tlm = IDENTITY
        elif op == "Tf" and len(operands) >= 2:
            state.font = str(operands[0])
            state.size = _num(operands[1])
        elif op == "TL" and operands:
            state.leading = _num(operands[0])
        elif op in ("Td", "TD") and len(operands) >= 2:
            tx, ty = _num(operands[0]), _num(operands[1])
            if op == "TD":
                state.leading = -ty
            state.tlm = mat_mul((1, 0, 0, 1, tx, ty), state.tlm)
            state.tm = state.tlm
        elif op == "Tm" and len(operands) >= 6:
            state.tm = state.tlm = tuple(_num(v) for v in operands[:6])
        elif op == "T*":
            state.tlm = mat_mul((1, 0, 0, 1, 0, -state.leading), state.tlm)
            state.tm = state.tlm

        # --- text showing ---
        elif op in _TEXT_SHOW:
            s = _show_text(operands, op)
            if s:
                x, y = apply(mat_mul(state.tm, state.ctm), 0.0, 0.0)
                scale = math.hypot(state.tm[0], state.tm[1]) or 1.0
                size = state.size * scale
                out.objects.append(PageObject(
                    kind="text",
                    signature=text_signature(s, size, state.font),
                    x=_q(x), y=_q(y),
                    style=f"fill={state.fill};size={_q(size, _SIZE_BUCKET)}",
                    text=s, size=size, ops=[op], index=index))
                index += 1
            if op in ("'", '"'):
                state.tlm = mat_mul((1, 0, 0, 1, 0, -state.leading), state.tlm)
                state.tm = state.tlm

        # --- path construction ---
        elif op == "re" and len(operands) >= 4:
            x, y, w, h = (_num(v) for v in operands[:4])
            pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
            current.extend(apply(state.ctm, px, py) for px, py in pts)
            current_ops.append("re")
        elif op == "m" and len(operands) >= 2:
            current.append(apply(state.ctm, _num(operands[0]), _num(operands[1])))
            current_ops.append("m")
        elif op == "l" and len(operands) >= 2:
            current.append(apply(state.ctm, _num(operands[0]), _num(operands[1])))
            current_ops.append("l")
        elif op in ("c", "v", "y"):
            for i in range(0, len(operands) - 1, 2):
                current.append(apply(state.ctm, _num(operands[i]), _num(operands[i + 1])))
            current_ops.append(op)
        elif op == "h":
            current_ops.append("h")

        # --- path painting: this is where a path becomes an object ---
        elif op in _PATH_PAINT:
            if current:
                anchor = current[0]
                out.objects.append(PageObject(
                    kind="path",
                    signature=path_signature(current, current_ops + [op]),
                    x=_q(anchor[0]), y=_q(anchor[1]),
                    style=(f"paint={op};w={_q(state.line_width, 0.1)};"
                           f"fill={state.fill};stroke={state.stroke}"),
                    points=[(_q(px), _q(py)) for px, py in current],
                    ops=current_ops + [op], index=index))
                index += 1
            current, current_ops = [], []

        # --- raster ---
        elif op in ("INLINE IMAGE", "BI"):
            out.has_raster = True
            data, w, h = _inline_image(operands)
            out.objects.append(PageObject(
                kind="image", signature=image_signature(data, w, h),
                x=0.0, y=0.0, style=f"{w}x{h}", ops=["BI"], index=index))
            index += 1
        elif op == "Do":
            # An XObject may be an image or a form; treat it as raster-ish for the
            # tier decision but keep it as an object so it can still be matched.
            name = str(operands[0]) if operands else ""
            out.has_raster = True
            out.objects.append(PageObject(
                kind="image", signature=f"X|{name}", x=0.0, y=0.0,
                style=name, ops=["Do"], index=index))
            index += 1

        elif op not in _BENIGN:
            out.unknown_ops.append(op)

    return out


def _show_text(operands, op) -> str:
    """The string a text-showing operator draws."""
    def dec(v) -> str:
        if isinstance(v, (bytes, bytearray)):
            return v.decode("latin-1", "replace")
        return str(v)

    if op == "TJ" and operands:
        parts = []
        for el in operands[0]:
            if isinstance(el, (int, float)):
                continue                      # kerning adjustment, not content
            parts.append(dec(el))
        return "".join(parts)
    if op == '"' and len(operands) >= 3:
        return dec(operands[2])
    if operands:
        return dec(operands[-1] if op == "'" else operands[0])
    return ""


def _inline_image(operands) -> Tuple[bytes, int, int]:
    """``(data, width, height)`` from an inline-image operation.

    pypdf hands these over as a MAPPING — ``{"settings": {...}, "data": b"..."}`` —
    not as a positional operand list. Iterating it as a sequence yields the keys,
    which silently produced empty pixel data and made every scanned page hash
    identically: two visibly different scans compared "unchanged". Both shapes are
    handled here so the sequence form of other producers still works."""
    data, w, h = b"", 0, 0

    def read_settings(settings) -> None:
        nonlocal w, h
        if not isinstance(settings, dict):
            return
        for key in ("/W", "/Width"):
            if key in settings:
                w = int(_num(settings[key]))
        for key in ("/H", "/Height"):
            if key in settings:
                h = int(_num(settings[key]))

    if isinstance(operands, dict):
        raw = operands.get("data")
        if isinstance(raw, (bytes, bytearray)):
            data = bytes(raw)
        read_settings(operands.get("settings"))
        return data, w, h

    for operand in operands or ():
        if isinstance(operand, (bytes, bytearray)):
            data = bytes(operand)
        elif isinstance(operand, dict):
            read_settings(operand)
            raw = operand.get("__streamdata__") or operand.get("data")
            if isinstance(raw, (bytes, bytearray)):
                data = bytes(raw)
    return data, w, h


def parse_document(data: bytes) -> List[PageParse]:
    """Every page of a PDF, or ``[]`` if the document cannot be opened."""
    import io

    import pypdf

    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        return [parse_page(page, reader) for page in reader.pages]
    except Exception:
        return []

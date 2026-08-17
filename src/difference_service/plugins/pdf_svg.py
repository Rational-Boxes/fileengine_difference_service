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

"""Render a page delta as a scriptable SVG (SPECIFICATION.md §7.2).

The contract the front end is built against, and every clause of it is load-bearing:

  * **Three layer groups** with stable ids — ``#diff-old``, ``#diff-new``,
    ``#diff-changes``. View switching is showing/hiding these, so no re-fetch is
    needed to flip between before, after and difference.
  * **``data-diff-state``** on every diffed element (``added`` / ``deleted`` /
    ``modified`` / ``unchanged``).
  * **No colours in the document.** The SVG ships semantic state; the front end's
    CSS maps state to red/green/orange, so themes can restyle without regenerating
    anything. Emitting fills here would bake one theme into stored artifacts.
  * **No font dependencies.** Text is ``<path>`` glyph outlines (``pdf_glyphs``),
    never ``<text>``.
  * **``data-diff-mode``** on the root, so one front-end view engine can drive both
    the vector and raster forms.

Coordinates: PDF user space is y-up with the origin bottom-left; SVG is y-down from
top-left. Rather than transforming every point, the whole drawing is wrapped in one
flip transform — which also keeps the emitted geometry readable against the source
PDF when debugging.
"""
from __future__ import annotations

import base64
from typing import Iterable, List, Optional
from xml.sax.saxutils import escape, quoteattr

from .base import DiffMode, DiffState
from .pdf_match import ObjectDelta, PageDelta
from .pdf_objects import PageObject

#: Layer ids from §7.2. Order matters: changes paint over the base layers.
LAYER_OLD = "diff-old"
LAYER_NEW = "diff-new"
LAYER_CHANGES = "diff-changes"


def _fmt(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".") or "0"


def _path_d(obj: PageObject) -> str:
    """Path data for a vector object, from its captured points."""
    if not obj.points:
        return ""
    d = [f"M{_fmt(obj.points[0][0])},{_fmt(obj.points[0][1])}"]
    for x, y in obj.points[1:]:
        d.append(f"L{_fmt(x)},{_fmt(y)}")
    if "re" in obj.ops or "h" in obj.ops:
        d.append("Z")
    return " ".join(d)


def _is_filled(obj: PageObject) -> bool:
    return any(op in ("f", "F", "f*", "B", "B*", "b", "b*") for op in obj.ops)


def _stroke_width(obj: PageObject) -> float:
    for part in (obj.style or "").split(";"):
        if part.startswith("w="):
            try:
                return float(part[2:])
            except ValueError:
                return 1.0
    return 1.0


def render_object(obj: PageObject, state: str, glyphs=None) -> Optional[str]:
    """One object as SVG, tagged with its diff state.

    Returns ``None`` for a text object when no glyph outlines are available — the
    caller degrades the page rather than emitting a font-dependent element."""
    attrs = f'data-diff-state="{state}"'

    if obj.kind == "text":
        if glyphs is None:
            return None
        outline = glyphs.outline_run(obj.text, obj.size or 12.0, obj.x, obj.y,
                                     font_hint=obj.style)
        if outline is None:
            return None
        # `fill` is intentionally absent: state -> colour is the front end's job.
        return f'<g {attrs} data-diff-kind="text">{outline}</g>'

    if obj.kind == "image":
        # Placed by the raster path; in a vector page an image object is a marker.
        return f'<g {attrs} data-diff-kind="image"></g>'

    d = _path_d(obj)
    if not d:
        return None
    if _is_filled(obj):
        return f'<path {attrs} data-diff-kind="path" d="{d}"/>'
    return (f'<path {attrs} data-diff-kind="path" d="{d}" '
            f'stroke-width="{_fmt(_stroke_width(obj))}" fill="none"/>')


def _layer(layer_id: str, body: Iterable[str]) -> str:
    return f'<g id="{layer_id}">' + "".join(body) + "</g>"


def render_page(delta: PageDelta, width: float, height: float,
                *, glyphs=None, mode: str = DiffMode.VECTOR) -> Optional[str]:
    """A full page SVG conforming to §7.2, or ``None`` if it cannot be rendered.

    ``None`` means some object could not be drawn without a font dependency, which
    is a tier decision for the caller — never a reason to emit a broken page."""
    old_body: List[str] = []
    new_body: List[str] = []
    changes_body: List[str] = []

    for d in delta.deltas:
        # The old layer shows the base document: everything that was there before.
        if d.old is not None:
            el = render_object(d.old, _old_state(d), glyphs)
            if el is None:
                return None
            old_body.append(el)
        # The new layer shows the target document.
        if d.new is not None:
            el = render_object(d.new, _new_state(d), glyphs)
            if el is None:
                return None
            new_body.append(el)
        # The changes layer shows only what differs, over the unchanged base.
        if d.state != DiffState.UNCHANGED:
            source = d.new if d.new is not None else d.old
            el = render_object(source, d.state, glyphs)
            if el is None:
                return None
            changes_body.append(el)

    # PDF is y-up from the bottom-left; SVG is y-down from the top-left.
    flip = f'<g transform="translate(0,{_fmt(height)}) scale(1,-1)">'
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {_fmt(width)} {_fmt(height)}" '
        f'width="{_fmt(width)}" height="{_fmt(height)}" '
        f'data-diff-mode="{mode}">'
        + flip
        + _layer(LAYER_OLD, old_body)
        + _layer(LAYER_NEW, new_body)
        + _layer(LAYER_CHANGES, changes_body)
        + "</g></svg>"
    )


def _old_state(d: ObjectDelta) -> str:
    """State to tag an object with inside the OLD layer."""
    if d.state == DiffState.DELETED:
        return DiffState.DELETED
    if d.state == DiffState.MODIFIED:
        return DiffState.MODIFIED
    return DiffState.UNCHANGED


def _new_state(d: ObjectDelta) -> str:
    if d.state == DiffState.ADDED:
        return DiffState.ADDED
    if d.state == DiffState.MODIFIED:
        return DiffState.MODIFIED
    return DiffState.UNCHANGED


def render_raster_page(old_png: Optional[bytes], new_png: Optional[bytes],
                       width: float, height: float,
                       *, changed: bool = True) -> str:
    """A raster-tier page in the SAME three-layer structure (§7.2).

    Embedding the bitmaps inside the identical layer/state scaffolding is what lets
    **one** front-end view engine drive both modes — the alternative, a separate
    image-comparison component, would double the front-end surface and let the two
    drift apart. The overlay carries a region-level ``modified`` state, which is the
    raster tier's definition of modified (§5.1)."""
    def image(data: Optional[bytes], state: str) -> str:
        if not data:
            return ""
        b64 = base64.b64encode(data).decode("ascii")
        return (f'<image data-diff-state="{state}" data-diff-kind="raster" '
                f'x="0" y="0" width="{_fmt(width)}" height="{_fmt(height)}" '
                f'href="data:image/png;base64,{b64}"/>')

    overlay = image(new_png, DiffState.MODIFIED) if changed else ""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {_fmt(width)} {_fmt(height)}" '
        f'width="{_fmt(width)}" height="{_fmt(height)}" '
        f'data-diff-mode="{DiffMode.RASTER}">'
        + _layer(LAYER_OLD, [image(old_png, DiffState.DELETED)])
        + _layer(LAYER_NEW, [image(new_png, DiffState.ADDED)])
        + _layer(LAYER_CHANGES, [overlay])
        + "</svg>"
    )


def render_whole_page_state(objects: List[PageObject], state: str,
                            width: float, height: float, *, glyphs=None) -> Optional[str]:
    """A page that is entirely added or entirely deleted (an inserted/removed page).

    Every object carries the same state, and the changes layer holds the lot — so
    an inserted page reads as wholly green rather than as a page of unrelated
    additions."""
    body: List[str] = []
    for obj in objects:
        el = render_object(obj, state, glyphs)
        if el is None:
            return None
        body.append(el)

    layer_old = body if state == DiffState.DELETED else []
    layer_new = body if state == DiffState.ADDED else []
    flip = f'<g transform="translate(0,{_fmt(height)}) scale(1,-1)">'
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {_fmt(width)} {_fmt(height)}" '
        f'width="{_fmt(width)}" height="{_fmt(height)}" '
        f'data-diff-mode="{DiffMode.VECTOR}">'
        + flip
        + _layer(LAYER_OLD, layer_old)
        + _layer(LAYER_NEW, layer_new)
        + _layer(LAYER_CHANGES, body)
        + "</g></svg>"
    )

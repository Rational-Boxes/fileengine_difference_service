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

"""A trivial line-diff plugin for plain text (DEVELOPMENT_PLAN M1).

Its purpose is to **validate the M1 machinery** — version-pair resolution, the
rendition writer's manifest-last commit, cache hits, pruning — with a real plugin
producing real children, before the genuinely hard format work of M2/M3.

It also pins down the §7.2 SVG contract in miniature: three layer groups with the
stable ids ``#diff-old`` / ``#diff-new`` / ``#diff-changes``, every diffed element
tagged with ``data-diff-state``, no colours in the document (the front end styles
state), and no font dependencies at the *contract* level. It is intentionally NOT
a fidelity target: it lays out text as ``<text>`` elements, whereas the real 2D
plugin must emit glyph outlines as ``<path>`` (§5.1). Treat the layer/state
structure as the reference here, not the glyph handling.

**Never enabled by default.** ``default_registry`` includes it only when a
deployment names it in ``DIFF_ENABLED_PLUGINS``, so production cannot pick it up
by accident just because a file happens to be text/plain.
"""
from __future__ import annotations

import difflib
from xml.sax.saxutils import escape

from .base import DiffChild, DiffMode, DiffPlugin, DiffResult, DiffState, SourceRef

_LINE_H = 14
_PAD = 8
_MAX_LINES = 2000        # a guard, not a fidelity choice: this is a validation aid


def _lines(data: bytes) -> list:
    return data.decode("utf-8", "replace").splitlines()[:_MAX_LINES]


def _row(text: str, y: int, state: str) -> str:
    return (f'<text x="{_PAD}" y="{y}" data-diff-state="{state}" '
            f'xml:space="preserve">{escape(text)}</text>')


class PassthroughTextPlugin(DiffPlugin):
    name = "passthrough-text"
    version = 1

    def supports(self, mime: str) -> bool:
        return (mime or "").startswith("text/plain")

    def diff(self, base: SourceRef, target: SourceRef) -> DiffResult:
        old, new = _lines(base.data), _lines(target.data)
        sm = difflib.SequenceMatcher(a=old, b=new, autojunk=False)

        old_rows, new_rows, change_rows = [], [], []
        y_old = y_new = y_chg = _LINE_H + _PAD

        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                for line in old[i1:i2]:
                    old_rows.append(_row(line, y_old, DiffState.UNCHANGED)); y_old += _LINE_H
                    new_rows.append(_row(line, y_new, DiffState.UNCHANGED)); y_new += _LINE_H
                continue
            # §3: the base is "old" (deleted/red), the target is "new" (added/green);
            # a replace is both, which is how the changes layer shows a modification.
            for line in old[i1:i2]:
                old_rows.append(_row(line, y_old, DiffState.DELETED)); y_old += _LINE_H
                state = DiffState.MODIFIED if tag == "replace" else DiffState.DELETED
                change_rows.append(_row(line, y_chg, state)); y_chg += _LINE_H
            for line in new[j1:j2]:
                new_rows.append(_row(line, y_new, DiffState.ADDED)); y_new += _LINE_H
                state = DiffState.MODIFIED if tag == "replace" else DiffState.ADDED
                change_rows.append(_row(line, y_chg, state)); y_chg += _LINE_H

        height = max(y_old, y_new, y_chg, _LINE_H) + _PAD
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 800 {height}" data-diff-mode="{DiffMode.VECTOR}">'
            f'<g id="diff-old">{"".join(old_rows)}</g>'
            f'<g id="diff-new">{"".join(new_rows)}</g>'
            f'<g id="diff-changes">{"".join(change_rows)}</g>'
            '</svg>'
        ).encode("utf-8")

        return DiffResult(children=[DiffChild(
            kind="page", index=0, data=svg, mime="image/svg+xml", ext="svg",
            mode=DiffMode.VECTOR)])

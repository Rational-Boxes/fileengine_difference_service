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

"""PDF object matching — the M2 research spike (SPECIFICATION.md §5.1).

A PDF carries no stable object identity, so this derives one, in the three stages
the spec prescribes. Each exists to defeat a specific failure mode:

1. **Global page alignment.** Find the dominant translation between the two pages
   (the strongest cluster of per-object offset votes) and cancel it. Without this,
   a document whose content shifted down by a few points reports *every* object
   modified — technically true, useless to a reviewer.

2. **Position-independent signatures.** Identity is what an object *is* (string +
   size + font, or operator sequence + relative geometry), never where it sits.
   Built in ``pdf_objects``; consumed here.

3. **Order-stable matching via LCS over draw order.** A longest-common-subsequence
   over the signature sequence — a "text diff" over draw operations. This is what
   stops a single mid-stream insertion from cascading: LCS aligns around the
   insertion instead of pairing object N with N+1 forever after. Residual unmatched
   objects then fall to greedy nearest-neighbour within a spatial tolerance.

Finally, **confidence**: when the matcher cannot explain a page well, the page must
degrade to the hybrid/raster tier rather than emit a confidently wrong diff. A
misleading object-level diff is worse than an honest raster one, because a reviewer
cannot tell it is wrong.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .base import DiffState
from .pdf_objects import PageObject, PageParse

#: Vote-clustering granularity for the global alignment pass, in PDF units.
_VOTE_GRID = 1.0

#: A matched object whose position differs by more than this (after alignment) is
#: "modified by position"; beyond ``RELOCATE_THRESHOLD`` the two objects no longer
#: read as the same thing moved and become delete + add (§5.1).
MOVE_TOLERANCE = 0.75
RELOCATE_THRESHOLD = 72.0        # one inch

#: Below this fraction of objects matched, tier 1 is not trustworthy for the page.
MIN_CONFIDENCE = 0.6


@dataclass
class ObjectDelta:
    """One object's verdict."""
    state: str                                  # DiffState.*
    old: Optional[PageObject] = None
    new: Optional[PageObject] = None
    reason: str = ""                            # why "modified" — position/style

    @property
    def obj(self) -> PageObject:
        """The object to draw for this delta (the new side when there is one)."""
        return self.new or self.old


@dataclass
class PageDelta:
    """The full comparison of one page pair."""
    deltas: List[ObjectDelta] = field(default_factory=list)
    offset: Tuple[float, float] = (0.0, 0.0)    # cancelled global translation
    matched: int = 0
    total: int = 0
    confidence: float = 1.0

    def count(self, state: str) -> int:
        return sum(1 for d in self.deltas if d.state == state)

    @property
    def changed(self) -> int:
        return sum(1 for d in self.deltas if d.state != DiffState.UNCHANGED)

    @property
    def trustworthy(self) -> bool:
        """May this page be rendered as an object-level (tier 1) diff?"""
        return self.confidence >= MIN_CONFIDENCE


# ---------------------------------------------------------------- alignment

def dominant_offset(old: Sequence[PageObject], new: Sequence[PageObject]) -> Tuple[float, float]:
    """The dominant translation from ``old`` to ``new`` (§5.1 step 1).

    Votes come only from objects whose signatures match *uniquely* on both sides:
    an ambiguous signature (a repeated rule line, say) would contribute noise votes
    in every direction. A tie or no clear cluster yields ``(0, 0)`` — declining to
    align is always safe, whereas aligning on a bad guess corrupts every position
    comparison downstream."""
    old_by_sig: Dict[str, List[PageObject]] = {}
    new_by_sig: Dict[str, List[PageObject]] = {}
    for o in old:
        old_by_sig.setdefault(o.signature, []).append(o)
    for n in new:
        new_by_sig.setdefault(n.signature, []).append(n)

    votes: Counter = Counter()
    for sig, olds in old_by_sig.items():
        news = new_by_sig.get(sig)
        if not news or len(olds) != 1 or len(news) != 1:
            continue
        dx = news[0].x - olds[0].x
        dy = news[0].y - olds[0].y
        votes[(round(dx / _VOTE_GRID) * _VOTE_GRID,
               round(dy / _VOTE_GRID) * _VOTE_GRID)] += 1

    if not votes:
        return (0.0, 0.0)
    (dx, dy), top = votes.most_common(1)[0]
    # A cluster that is not a clear majority of the voters is not "dominant".
    # Requiring a real majority keeps a page with two competing shifts unaligned
    # rather than half-aligned.
    if top * 2 <= sum(votes.values()):
        return (0.0, 0.0)
    return (dx, dy)


# ------------------------------------------------------------------ matching

def _lcs_pairs(a: Sequence[str], b: Sequence[str]) -> List[Tuple[int, int]]:
    """Index pairs of a longest common subsequence of two signature sequences.

    ``difflib`` is deliberately not used: its autojunk heuristic discards elements
    appearing in more than 1% of a large sequence, which on a dense page silently
    drops exactly the repeated rules and glyph runs that hold an alignment
    together. This is a plain O(n·m) LCS with an explicit cap instead."""
    n, m = len(a), len(b)
    if n == 0 or m == 0:
        return []
    # Guard the quadratic table on pathological pages; the caller treats a refusal
    # as low confidence, which degrades the page rather than hanging the worker.
    if n * m > 4_000_000:
        return []

    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        row, nxt = dp[i], dp[i + 1]
        ai = a[i]
        for j in range(m - 1, -1, -1):
            row[j] = nxt[j + 1] + 1 if ai == b[j] else max(nxt[j], row[j + 1])

    pairs: List[Tuple[int, int]] = []
    i = j = 0
    while i < n and j < m:
        if a[i] == b[j]:
            pairs.append((i, j))
            i += 1
            j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    return pairs


def _greedy_residual(old: List[PageObject], new: List[PageObject],
                     offset: Tuple[float, float],
                     tolerance: float = RELOCATE_THRESHOLD) -> List[Tuple[int, int]]:
    """Pair leftover objects by nearest neighbour within ``tolerance``.

    Only same-kind objects with equal signatures are considered — this pass exists
    to recover objects the LCS skipped for *ordering* reasons, not to invent
    matches between things that are not the same object."""
    dx, dy = offset
    pairs: List[Tuple[int, int]] = []
    used_new = set()
    for i, o in enumerate(old):
        best, best_d = -1, tolerance
        for j, n in enumerate(new):
            if j in used_new or n.signature != o.signature:
                continue
            d = math.hypot(n.x - (o.x + dx), n.y - (o.y + dy))
            if d < best_d:
                best, best_d = j, d
        if best >= 0:
            pairs.append((i, best))
            used_new.add(best)
    return pairs


def _classify(old: PageObject, new: PageObject,
              offset: Tuple[float, float]) -> Tuple[str, str]:
    """The `modified` predicate for tier 1 (§5.1).

    A matched object that renders identically is *unchanged*. One differing in a
    rendered attribute — position beyond tolerance, or style — is *modified*."""
    dx, dy = offset
    moved = math.hypot(new.x - (old.x + dx), new.y - (old.y + dy))
    if moved > RELOCATE_THRESHOLD:
        return DiffState.MODIFIED, "relocated"          # caller may split this
    reasons = []
    if moved > MOVE_TOLERANCE:
        reasons.append(f"moved {moved:.1f}")
    if old.style != new.style:
        reasons.append("style")
    if reasons:
        return DiffState.MODIFIED, "+".join(reasons)
    return DiffState.UNCHANGED, ""


def match_page(old_parse: PageParse, new_parse: PageParse) -> PageDelta:
    """Compare one page pair and classify every object."""
    old, new = list(old_parse.objects), list(new_parse.objects)
    delta = PageDelta(total=max(len(old), len(new)))

    if not old and not new:
        return delta

    # 1) Cancel any whole-page translation before comparing positions.
    delta.offset = dominant_offset(old, new)

    # 2) Order-stable alignment over draw order.
    pairs = _lcs_pairs([o.signature for o in old], [n.signature for n in new])
    matched_old = {i for i, _ in pairs}
    matched_new = {j for _, j in pairs}

    # 3) Recover leftovers the LCS skipped for ordering reasons.
    residual_old = [i for i in range(len(old)) if i not in matched_old]
    residual_new = [j for j in range(len(new)) if j not in matched_new]
    if residual_old and residual_new:
        sub = _greedy_residual([old[i] for i in residual_old],
                               [new[j] for j in residual_new], delta.offset)
        for si, sj in sub:
            i, j = residual_old[si], residual_new[sj]
            pairs.append((i, j))
            matched_old.add(i)
            matched_new.add(j)

    # 4) Verdicts.
    for i, j in sorted(pairs):
        state, reason = _classify(old[i], new[j], delta.offset)
        if reason == "relocated":
            # Beyond the displacement threshold these no longer read as the same
            # thing moved, so they are emitted as a delete plus an add (§5.1).
            delta.deltas.append(ObjectDelta(DiffState.DELETED, old=old[i], reason="relocated"))
            delta.deltas.append(ObjectDelta(DiffState.ADDED, new=new[j], reason="relocated"))
            continue
        delta.deltas.append(ObjectDelta(state, old=old[i], new=new[j], reason=reason))

    for i in range(len(old)):
        if i not in matched_old:
            delta.deltas.append(ObjectDelta(DiffState.DELETED, old=old[i]))
    for j in range(len(new)):
        if j not in matched_new:
            delta.deltas.append(ObjectDelta(DiffState.ADDED, new=new[j]))

    delta.matched = len(matched_old)
    delta.confidence = _confidence(old_parse, new_parse, delta)
    return delta


def _confidence(old_parse: PageParse, new_parse: PageParse, delta: PageDelta) -> float:
    """How much to trust this page's object-level result (§5.1).

    Two independent penalties. **Coverage**: the share of objects that matched —
    a page where most objects are unmatched has probably been mis-parsed rather
    than wholly rewritten. **Comprehension**: operators the extractor does not
    model, which mean objects may be misplaced. Both are needed; a page can parse
    every operator and still match nothing, or match everything it saw while
    having failed to see half the page.

    Coverage is measured against the SMALLER side, not the larger. Matches can
    never exceed ``min(len(old), len(new))``, so dividing by the larger conflates
    "this page is not understood" with "this page legitimately gained content" — a
    two-object page with one honest addition would score 0.67 and a one-object page
    with one addition 0.5, degrading perfectly good diffs to raster for the crime
    of having something added to them."""
    total = min(len(old_parse.objects), len(new_parse.objects))
    if total == 0:
        return 1.0
    coverage = delta.matched / total

    unknown = len(set(old_parse.unknown_ops) | set(new_parse.unknown_ops))
    comprehension = 1.0 / (1.0 + unknown)

    # An honestly empty page (everything genuinely added or deleted) should not be
    # punished for low coverage — that is a real answer, not a parse failure. It is
    # recognisable because one side is empty.
    if not old_parse.objects or not new_parse.objects:
        coverage = 1.0

    return round(min(coverage, comprehension), 4)


# ------------------------------------------------------------ page pairing

def pair_pages(old_pages: Sequence[PageParse],
               new_pages: Sequence[PageParse]) -> List[Tuple[Optional[int], Optional[int]]]:
    """Correspond pages across the two documents (§5.1).

    Positional pairing is wrong the moment a page is inserted or deleted: every
    later page shifts and the whole tail reads as rewritten. So pages are aligned
    by an LCS over a per-page content fingerprint, which tolerates insertion,
    deletion and reordering; ``(i, None)`` is a deleted page and ``(None, j)`` an
    added one."""
    def fingerprint(p: PageParse) -> str:
        return "|".join(sorted(o.signature for o in p.objects))

    old_fp = [fingerprint(p) for p in old_pages]
    new_fp = [fingerprint(p) for p in new_pages]

    pairs = _lcs_pairs(old_fp, new_fp)
    matched_old = {i for i, _ in pairs}
    matched_new = {j for _, j in pairs}

    # Unmatched pages pair up positionally among themselves — a page whose content
    # changed is still that page, and a purely positional fallback here is right
    # because both sides' leftovers are in document order.
    left = [i for i in range(len(old_pages)) if i not in matched_old]
    right = [j for j in range(len(new_pages)) if j not in matched_new]
    for i, j in zip(left, right):
        pairs.append((i, j))
        matched_old.add(i)
        matched_new.add(j)

    out: List[Tuple[Optional[int], Optional[int]]] = [(i, j) for i, j in pairs]
    out += [(i, None) for i in range(len(old_pages)) if i not in matched_old]
    out += [(None, j) for j in range(len(new_pages)) if j not in matched_new]
    out.sort(key=lambda p: (p[1] if p[1] is not None else 10 ** 6,
                            p[0] if p[0] is not None else 10 ** 6))
    return out

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

"""Diff plugin interface + result types (SPECIFICATION.md §4, §7).

A plugin turns a *pair of versions* into a set of stored children plus the mode
metadata describing what it managed to produce. The governing rule (§4) is
**degrade, never fail**: a plugin picks the highest-fidelity tier each unit — page,
element — actually supports, may mix tiers within one result, and returns a lower
tier rather than raising. A plugin that raises anyway is caught by the registry and
turned into a ``failed`` result, so one bad input can never take down the worker.

Two levels of vocabulary, kept deliberately separate:

  * ``DiffResult`` is **plugin-facing** — what this algorithm produced for this
    pair. The plugin knows its own tiers and children; it knows nothing about
    version ids or storage.
  * ``Manifest`` (manifest.py) is **service-facing** — the stored commit marker
    that adds the pair key, plugin identity, and expected child set.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Iterator, List, Optional


# --- vocabulary -------------------------------------------------------------

class DiffState:
    """Per-element state carried into the output as ``data-diff-state`` (§7.2).
    The colour convention is applied by the front end: red=deleted, green=added,
    orange=modified (§0)."""
    ADDED = "added"
    DELETED = "deleted"
    MODIFIED = "modified"
    UNCHANGED = "unchanged"

    ALL = (ADDED, DELETED, MODIFIED, UNCHANGED)


class DiffMode:
    """Fidelity tier of a produced unit, and of the result overall (§7.1).

    2D tiers run vector → hybrid → raster (§5.1); 3D produces ``xkt`` (§5.2).
    ``MIXED`` is only ever an *overall* mode — it means the per-unit map carries
    more than one tier, which is expected and normal (a PDF with vector and
    scanned pages)."""
    VECTOR = "vector"
    HYBRID = "hybrid"
    RASTER = "raster"
    XKT = "xkt"
    MIXED = "mixed"
    #: A unit that could not be produced at ANY tier. It still occupies its slot in
    #: the per-unit map so page indices stay aligned with the document — dropping
    #: it instead would yield a result that looks complete while silently missing a
    #: page, which reads to a reviewer as "no changes here".
    UNAVAILABLE = "unavailable"

    #: Per-unit tiers, best first. Used to summarize an overall mode.
    TIERS_2D = (VECTOR, HYBRID, RASTER)


class DiffStatus:
    """Manifest status (§7.1.1). ``PENDING`` is never *stored* — the absence of a
    manifest is what "pending" means on the read path; it exists so the API can
    report a uniform status to the front end."""
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


# --- inputs -----------------------------------------------------------------

@dataclass
class SourceRef:
    """One side of a comparison: a version's content and how to interpret it."""
    uid: str                 # the source file's UID (same for both sides)
    version: str             # the version identifier this content came from
    data: bytes              # full version content
    mime: str = ""           # resolved MIME type
    name: str = ""           # file name, for logging / tool hints

    @property
    def size(self) -> int:
        return len(self.data or b"")


# --- outputs ----------------------------------------------------------------

#: Bytes read from a file-backed child at a time.
CHILD_CHUNK = 4 * 1024 * 1024


@dataclass
class DiffChild:
    """One stored child of the result — a per-page SVG (2D) or the XKT (3D).

    ``index`` orders the children within a result and is what the manifest's
    per-unit map keys on; for a single-child 3D result it is 0.

    The payload lives **either** in memory (``data``) or on disk (``path``). A
    per-page SVG is small and belongs in memory; an XKT carrying old/new/
    difference layers for a large model does not — the plugin already wrote it
    as a file, and reading it back just to hand it to a streaming writer holds
    the whole model for no reason.

    **Ownership.** A file-backed child owns its file: the producing plugin's
    temp dir is gone by the time anyone reads it, so the file is moved somewhere
    the child controls and ``cleanup`` releases it. Whoever consumes a
    :class:`DiffResult` must ``close()`` it, or the files leak."""
    kind: str                # "page" (2D) | "model" (3D)
    index: int
    data: Optional[bytes] = None
    mime: str = ""
    ext: str = ""
    mode: str = DiffMode.VECTOR   # the tier THIS unit achieved
    path: Optional[str] = None
    cleanup: Optional[Callable[[], None]] = None

    @classmethod
    def from_path(cls, kind: str, index: int, path: str, mime: str, ext: str,
                  mode: str = DiffMode.VECTOR,
                  cleanup: Optional[Callable[[], None]] = None) -> "DiffChild":
        return cls(kind=kind, index=index, data=None, mime=mime, ext=ext,
                   mode=mode, path=path, cleanup=cleanup)

    @property
    def size(self) -> int:
        if self.path is not None:
            try:
                return os.path.getsize(self.path)
            except OSError:
                return 0
        return len(self.data or b"")

    def chunks(self, size: int = CHILD_CHUNK) -> Iterator[bytes]:
        """Yield the payload in bounded pieces, from wherever it lives."""
        if self.path is not None:
            with open(self.path, "rb") as f:
                while True:
                    piece = f.read(size)
                    if not piece:
                        return
                    yield piece
        elif self.data:
            for start in range(0, len(self.data), size):
                yield self.data[start:start + size]

    def read(self) -> bytes:
        """The whole payload, for callers that genuinely need one buffer."""
        return b"".join(self.chunks())

    def release(self) -> None:
        if self.cleanup is not None:
            try:
                self.cleanup()
            finally:
                self.cleanup = None
                self.path = None


@dataclass
class DiffFailure:
    """Why a run produced nothing usable (§7.1). Recorded on a ``failed`` manifest
    so "attempted and failed" stays distinguishable from "never attempted"."""
    stage: str                                    # e.g. "render", "match", "encode"
    reason: str = ""
    tiers_attempted: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"stage": self.stage, "reason": self.reason,
                "tiers_attempted": list(self.tiers_attempted)}


@dataclass
class DiffResult:
    """What a plugin produced for one version pair.

    ``children`` may legitimately be empty only when ``status`` is ``failed`` — a
    ready result with no children would present the front end with a diff that has
    nothing to show."""
    children: List[DiffChild] = field(default_factory=list)
    status: str = DiffStatus.READY
    failure: Optional[DiffFailure] = None
    #: Set to override the derived overall mode; normally left to ``mode``.
    overall_mode: Optional[str] = None

    def close(self) -> None:
        """Release every file-backed child. Safe to call twice."""
        for c in self.children:
            c.release()

    def __enter__(self) -> "DiffResult":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @classmethod
    def failed(cls, stage: str, reason: str = "", tiers: Optional[List[str]] = None) -> "DiffResult":
        return cls(children=[], status=DiffStatus.FAILED,
                   failure=DiffFailure(stage=stage, reason=reason,
                                       tiers_attempted=list(tiers or [])))

    @property
    def mode(self) -> str:
        """Overall mode summarizing the per-unit tiers (§7.1).

        One distinct tier across all units reports that tier; more than one reports
        ``mixed``, which is the signal that the front end must consult the per-unit
        map rather than assume a single view engine for the whole document."""
        if self.overall_mode:
            return self.overall_mode
        tiers = {c.mode for c in self.children if c.mode}
        if not tiers:
            return DiffMode.RASTER if self.status == DiffStatus.FAILED else DiffMode.VECTOR
        if len(tiers) == 1:
            return next(iter(tiers))
        return DiffMode.MIXED

    @property
    def is_ready(self) -> bool:
        return self.status == DiffStatus.READY and bool(self.children)

    def units(self) -> List[dict]:
        """The per-unit map for the manifest: ``[{index, mode, kind}]`` (§7.1)."""
        return [{"index": c.index, "mode": c.mode, "kind": c.kind}
                for c in sorted(self.children, key=lambda c: c.index)]


# --- the plugin contract ----------------------------------------------------

class DiffPlugin(ABC):
    """A differ for a family of MIME types (§4).

    ``name`` is recorded on every rendition the plugin produces and ``version`` is
    part of the cache key — bumping ``version`` is how an algorithm upgrade forces
    regeneration of everything the previous algorithm produced (§6). Treat it as a
    released number: bump it when output would differ, never reuse it."""

    name: str = "plugin"
    version: int = 1

    @abstractmethod
    def supports(self, mime: str) -> bool:
        """Does this plugin handle ``mime``? Called in registration order."""

    @abstractmethod
    def diff(self, base: SourceRef, target: SourceRef) -> DiffResult:
        """Produce the diff children for ``base`` (old) → ``target`` (new).

        Per §3 the target is "new" (added/green) and the base is "old"
        (deleted/red). Implementations must degrade rather than raise: pick the
        best tier each unit supports and fall back tier-by-tier, returning a
        ``failed`` result only when even the last-resort tier is impossible."""

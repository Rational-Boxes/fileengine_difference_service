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

"""Version-pair resolution (SPECIFICATION.md §3).

Which two versions get compared, and how to fetch each one's bytes.

  * **Default** — the target versus its **immediate predecessor** in the chain.
  * **Explicit base** — a caller may name any earlier version instead.
  * The **target is "new"** (added / green); the **base is "old"** (deleted / red).

Two facts about the core's version API, both verified against a live core rather
than taken from docstrings, because getting either backwards silently produces a
*reversed or wrong* diff — the kind of bug that looks like a plausible result:

  1. ``revisions()`` returns versions **newest first**.
  2. ``get(back=N)`` reads ``revisions()[N]`` — so ``back=0`` is the newest, and
     an index into the revisions list is directly usable as ``back``.

Version ids are ``YYYYMMDD_HHMMSS.mmm`` strings, which sort lexicographically in
chronological order; ordering here is taken from the core's list order rather than
from string comparison, so a future id format cannot silently break it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


class VersionError(Exception):
    """The requested pair cannot be formed."""


class NoPredecessor(VersionError):
    """The target is the file's first version, so there is nothing to compare to.

    Not an error condition in the pipeline's sense — a first version legitimately
    has no diff. Callers treat it like an unsupported type: no work, no failed
    manifest, nothing to retry."""


@dataclass
class VersionPair:
    """A resolved comparison: ``base`` (old) → ``target`` (new)."""
    file_uid: str
    base: str            # version id of the old side
    target: str          # version id of the new side
    base_back: int       # ``get(back=)`` index for the base
    target_back: int     # ``get(back=)`` index for the target

    @property
    def is_identity(self) -> bool:
        return self.base == self.target


def version_list(core, file_uid: str) -> List[str]:
    """The file's version ids, newest first (the core's own order)."""
    return [r.version for r in core.revisions(file_uid)]


def resolve_pair(core, file_uid: str, target: str = "",
                 base: Optional[str] = None) -> VersionPair:
    """Resolve the pair to compare for ``file_uid``.

    ``target`` empty means the newest version. ``base`` empty/None means the
    target's immediate predecessor.

    Raises ``NoPredecessor`` when the target is the oldest version and no explicit
    base was given, and ``VersionError`` when a named version does not exist or the
    base is not older than the target."""
    versions = version_list(core, file_uid)
    if not versions:
        raise VersionError(f"{file_uid} has no versions")

    # --- target ---
    if not target:
        target_idx = 0                      # newest
        target = versions[0]
    else:
        try:
            target_idx = versions.index(target)
        except ValueError:
            raise VersionError(f"target version {target!r} not found on {file_uid}")

    # --- base ---
    if base:
        try:
            base_idx = versions.index(base)
        except ValueError:
            raise VersionError(f"base version {base!r} not found on {file_uid}")
        # Newest-first ordering means a LARGER index is an OLDER version. An
        # explicit base must be strictly older than the target, or "old vs new"
        # is inverted and every added line would render as deleted.
        if base_idx <= target_idx:
            raise VersionError(
                f"base {base!r} is not older than target {target!r} on {file_uid}")
    else:
        base_idx = target_idx + 1
        if base_idx >= len(versions):
            raise NoPredecessor(
                f"{target!r} is the first version of {file_uid}; nothing to diff against")
        base = versions[base_idx]

    return VersionPair(file_uid=file_uid, base=base, target=target,
                       base_back=base_idx, target_back=target_idx)

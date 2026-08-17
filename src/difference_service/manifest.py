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

"""The diff manifest — result descriptor and atomic commit marker (§7.1, §7.1.1).

The manifest is the single most load-bearing object in the design, because it is
what makes an at-least-once, crash-prone pipeline safe to read from:

  * The worker writes **all content children first**, then the manifest **last**.
    The manifest's presence *is* the "diff complete" signal.
  * The read path keys off it: **no manifest ⇒ pending**, never a partial diff.
    A reader may additionally check the children against ``expected`` before
    serving, which catches a half-written set whose manifest somehow landed.
  * A run that failed even at its last-resort tier still writes a manifest with
    ``status: "failed"`` — so "attempted and failed" (fall back to side-by-side)
    stays distinguishable from "never attempted" (compute it).

The cache key (§6) is ``(file_uid, base_version, target_version, plugin_name,
plugin_version)``. It is rendered into a stable rendition name so a stored result
can be located without an index, and so bumping a plugin's ``version`` naturally
misses the old key and regenerates.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import List, Optional

from .plugins.base import DiffResult, DiffStatus

#: Rendition format tag marking a child as belonging to a diff result.
RENDITION_FORMAT = "diff"

#: Schema version of the manifest document itself. Distinct from a plugin's
#: ``version``: this changes when the manifest's SHAPE changes, which readers key
#: off, whereas a plugin version changes when its OUTPUT changes.
MANIFEST_SCHEMA = 1


def pair_key(file_uid: str, base_version: str, target_version: str,
             plugin_name: str, plugin_version: int) -> str:
    """The §6 cache key, as a short stable string.

    Hashed because version identifiers are timestamps and plugin names are free
    text — concatenating them raw would produce rendition names that are long,
    and fragile against any character the storage layer treats specially."""
    raw = "|".join([file_uid, base_version or "", target_version or "",
                    plugin_name or "", str(plugin_version)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def child_name(key: str, kind: str, index: int, ext: str) -> str:
    """Stored name of one content child, e.g. ``diff.<key>.page.003.svg``."""
    return f"{RENDITION_FORMAT}.{key}.{kind}.{index:03d}.{ext}"


def manifest_name(key: str) -> str:
    """Stored name of the manifest child, e.g. ``diff.<key>.manifest.json``."""
    return f"{RENDITION_FORMAT}.{key}.manifest.json"


@dataclass
class Manifest:
    """The stored description of one version-pair result."""
    file_uid: str
    base_version: str
    target_version: str
    plugin: str
    plugin_version: int
    status: str = DiffStatus.READY
    mode: str = ""
    #: Per-unit map — ``[{index, mode, kind}]``. For 2D this is the page map the
    #: front end uses to pick a view engine per page (§7.1).
    units: List[dict] = field(default_factory=list)
    #: Names of the content children this manifest commits, so a reader can verify
    #: the set is complete rather than trusting the manifest's mere presence.
    expected: List[str] = field(default_factory=list)
    failure: Optional[dict] = None
    schema: int = MANIFEST_SCHEMA

    @property
    def key(self) -> str:
        return pair_key(self.file_uid, self.base_version, self.target_version,
                        self.plugin, self.plugin_version)

    # --- construction ---
    @classmethod
    def from_result(cls, result: DiffResult, *, file_uid: str, base_version: str,
                    target_version: str, plugin: str, plugin_version: int) -> "Manifest":
        key = pair_key(file_uid, base_version, target_version, plugin, plugin_version)
        expected = [child_name(key, c.kind, c.index, c.ext) for c in
                    sorted(result.children, key=lambda c: c.index)]
        return cls(
            file_uid=file_uid,
            base_version=base_version,
            target_version=target_version,
            plugin=plugin,
            plugin_version=plugin_version,
            status=result.status,
            mode=result.mode,
            units=result.units(),
            expected=expected,
            failure=result.failure.as_dict() if result.failure else None,
        )

    # --- serialization ---
    def as_dict(self) -> dict:
        d = {
            "schema": self.schema,
            "status": self.status,
            "mode": self.mode,
            "file_uid": self.file_uid,
            "base_version": self.base_version,
            "target_version": self.target_version,
            "plugin": self.plugin,
            "plugin_version": self.plugin_version,
            "key": self.key,
            "units": list(self.units),
            "expected": list(self.expected),
        }
        if self.failure is not None:
            d["failure"] = self.failure
        return d

    def to_bytes(self) -> bytes:
        """Canonical JSON — sorted keys and no incidental whitespace, so an
        unchanged result re-serializes byte-identically. That is what makes
        regeneration of the same inputs genuinely idempotent (§7.1.1) instead of
        merely equivalent."""
        return json.dumps(self.as_dict(), sort_keys=True,
                          separators=(",", ":")).encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> "Manifest":
        d = json.loads(raw.decode("utf-8"))
        return cls(
            file_uid=d.get("file_uid", ""),
            base_version=d.get("base_version", ""),
            target_version=d.get("target_version", ""),
            plugin=d.get("plugin", ""),
            plugin_version=int(d.get("plugin_version", 0)),
            status=d.get("status", DiffStatus.READY),
            mode=d.get("mode", ""),
            units=list(d.get("units", [])),
            expected=list(d.get("expected", [])),
            failure=d.get("failure"),
            schema=int(d.get("schema", MANIFEST_SCHEMA)),
        )

    # --- read-path checks ---
    def is_complete(self, present_names) -> bool:
        """Are all committed children actually present? Guards the case where the
        manifest landed but a content child did not (§7.1.1)."""
        have = set(present_names or ())
        return all(name in have for name in self.expected)

    def is_stale(self, plugin: str, plugin_version: int) -> bool:
        """Was this produced by a different plugin, or an older version of it?
        A stale manifest is regenerated rather than served (§6)."""
        return self.plugin != plugin or int(self.plugin_version) != int(plugin_version)

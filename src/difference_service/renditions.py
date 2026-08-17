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

"""Diff renditions — reading and writing results as hidden children (§2, §7.1.1).

Per ``file_engine_core/design_documents/file_renditions.md`` a rendition is simply
a child file whose parent is the source file's UID, created with ordinary calls.
Diff results live there, which is what makes them inherit the source file's ACLs
for free: a stored diff is readable by exactly those who can read the file it
describes.

**Write order is the contract, not an implementation detail.** ``write_result``
puts every content child down first and the manifest *last*, because the manifest's
presence is the sole "this diff is complete" signal (§7.1.1). At-least-once
delivery plus a worker that can die mid-write means any other order can leave a
half-formed result that a reader would happily serve.

Naming is content-addressed on the §6 cache key, so a stored result is locatable
with a single directory listing and no index: ``diff.<key>.page.000.svg``,
``diff.<key>.manifest.json``. A plugin-version bump changes the key, so old
children simply stop matching and are pruned rather than overwritten in place.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from .manifest import RENDITION_FORMAT, Manifest, child_name, manifest_name, pair_key

log = logging.getLogger("difference_service.renditions")

#: Prefix identifying any child this service owns. Used when pruning so a child
#: belonging to another service (a CSAI preview, a markup) is never removed.
_PREFIX = RENDITION_FORMAT + "."


def is_diff_child(name: str) -> bool:
    """Is ``name`` a child this service wrote? Deliberately strict — pruning acts
    on this, and a false positive deletes somebody else's rendition."""
    return bool(name) and name.startswith(_PREFIX)


def key_of(name: str) -> str:
    """The cache key embedded in one of our child names, or ``""``."""
    if not is_diff_child(name):
        return ""
    parts = name.split(".")
    return parts[1] if len(parts) > 2 else ""


class DiffRenditionStore:
    """Reads and writes diff results under a source file.

    ``core`` is a ``CoreClient``; the identity it carries decides whose rights are
    used — the worker principal for precompute, the calling user for on-demand."""

    def __init__(self, core):
        self.core = core
        self.mf = core._client()
        self.tenant = core.tenant

    # ------------------------------------------------------------------ read
    def children(self, file_uid: str) -> List:
        """Every hidden child of the file (ours and other services')."""
        try:
            return list(self.mf.dir(file_uid, tenant=self.tenant) or [])
        except Exception:
            log.debug("could not list children of %s", file_uid, exc_info=True)
            return []

    def child_names(self, file_uid: str) -> set:
        return {e.name for e in self.children(file_uid)}

    def read_manifest(self, file_uid: str, key: str) -> Optional[Manifest]:
        """The stored manifest for ``key``, or ``None`` if absent/unreadable.

        ``None`` means **pending** — never "partial" and never "failed" (§7.1.1).
        A manifest that fails to parse is treated as absent so a corrupt marker
        causes a regeneration rather than a permanent broken result."""
        name = manifest_name(key)
        for e in self.children(file_uid):
            if e.name != name:
                continue
            try:
                buf = self.mf.get(e.uid, tenant=self.tenant)
                try:
                    return Manifest.from_bytes(buf.read())
                finally:
                    try:
                        buf.close()
                    except Exception:
                        pass
            except Exception:
                log.warning("unreadable manifest %s on %s — treating as absent",
                            name, file_uid, exc_info=True)
                return None
        return None

    def find_manifest(self, file_uid: str, base: str, target: str,
                      plugin: str, plugin_version: int) -> Optional[Manifest]:
        """The manifest for a specific pair + plugin generation, if stored."""
        return self.read_manifest(
            file_uid, pair_key(file_uid, base, target, plugin, plugin_version))

    def is_complete(self, file_uid: str, m: Manifest) -> bool:
        """Manifest present AND every content child it commits is there too."""
        return m.is_complete(self.child_names(file_uid))

    def child_uid(self, file_uid: str, name: str) -> str:
        for e in self.children(file_uid):
            if e.name == name:
                return e.uid
        return ""

    # ----------------------------------------------------------------- write
    def write_result(self, file_uid: str, result, *, base: str, target: str,
                     plugin: str, plugin_version: int) -> Manifest:
        """Store a result: content children first, then the manifest (§7.1.1).

        Returns the manifest that was committed. Raises if a *content* child
        cannot be written — the caller retries rather than committing a manifest
        that promises children which are not there. A failed-run manifest (no
        children) still commits, so the failure is recorded."""
        m = Manifest.from_result(result, file_uid=file_uid, base_version=base,
                                 target_version=target, plugin=plugin,
                                 plugin_version=plugin_version)
        key = m.key
        existing = self.child_names(file_uid)

        # 1) Content children. Re-writing an identical key is a no-op, which is
        #    what makes redelivery cheap rather than merely safe.
        for child in sorted(result.children, key=lambda c: c.index):
            name = child_name(key, child.kind, child.index, child.ext)
            if name in existing:
                continue
            uid = self.mf.touch(file_uid, name, tenant=self.tenant)
            uid = getattr(uid, "uid", uid)
            self.mf.put(uid, child.data, tenant=self.tenant)

        # 2) The manifest, last — this is the commit.
        mname = manifest_name(key)
        muid = self.child_uid(file_uid, mname)
        if not muid:
            muid = self.mf.touch(file_uid, mname, tenant=self.tenant)
            muid = getattr(muid, "uid", muid)
        self.mf.put(muid, m.to_bytes(), tenant=self.tenant)
        log.info("committed diff %s for %s (%s -> %s, %s v%d, %d children)",
                 key, file_uid, base, target, plugin, plugin_version, len(m.expected))
        return m

    # ----------------------------------------------------------------- prune
    def prune(self, file_uid: str, keep_keys) -> List[str]:
        """Remove our children whose key is not in ``keep_keys``.

        Best-effort: a failed delete is logged, not raised, so cleanup never fails
        a diff that already succeeded. Only children matching our own prefix are
        even considered."""
        keep = set(keep_keys or ())
        removed: List[str] = []
        for e in self.children(file_uid):
            if not is_diff_child(e.name):
                continue
            if key_of(e.name) in keep:
                continue
            try:
                self.mf.remove(e.uid, tenant=self.tenant)
                removed.append(e.name)
            except Exception:
                log.warning("could not prune stale diff child %s (%s) of %s",
                            e.name, e.uid, file_uid, exc_info=True)
        return removed

    def prune_keys(self, file_uid: str, drop_keys) -> List[str]:
        """Remove our children whose key IS in ``drop_keys`` — the inverse of
        :meth:`prune`, and the one the pipeline wants.

        Naming what to drop rather than what to keep is the safer direction here:
        a keep-list deletes anything it forgot to mention, which is how every
        still-valid comparison of an older pair came to be discarded. A drop-list
        can only remove what it can name."""
        drop = set(drop_keys or ())
        if not drop:
            return []
        removed: List[str] = []
        for e in self.children(file_uid):
            if not is_diff_child(e.name) or key_of(e.name) not in drop:
                continue
            try:
                self.mf.remove(e.uid, tenant=self.tenant)
                removed.append(e.name)
            except Exception:
                log.warning("could not prune superseded diff child %s (%s) of %s",
                            e.name, e.uid, file_uid, exc_info=True)
        return removed

    def remove_all(self, file_uid: str) -> List[str]:
        """Cascade-remove every diff child of a file (§2.1, on ``file.deleted``).

        Note the core already cascades a *hard* delete of the parent; this exists
        for the soft-delete case and for reconcile-driven cleanup, and is
        idempotent — a file whose children are already gone yields ``[]``."""
        return self.prune(file_uid, keep_keys=())

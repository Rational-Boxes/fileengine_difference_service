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

"""The diff pipeline: version pair → content → plugin → stored renditions.

One entry point, ``DiffPipeline.run``, shared by every path that produces a diff —
the event worker, the reconcile sweep, and (M4) on-demand requests. Sharing it is
deliberate: the §7.1.1 atomicity rules and the §6 cache semantics are then
implemented once, and an on-demand request cannot accidentally produce a result
shaped differently from a precomputed one.

**Idempotency without a database.** The stored manifest *is* the idempotency
record. Before doing any work the pipeline looks for a complete, non-stale manifest
for the pair; finding one is a cache hit and the run stops there. That collapses
repeated work on the logical key ``(file_uid, target_version)`` (§2.1) with no
second source of truth to drift — which is exactly why this service has no
Postgres. Event-id dedupe sits *in front* of this, in the consumer, and is only an
optimisation: the manifest check is what makes redelivery correct.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .manifest import Manifest
from .mime import DispatchMimeResolver
from .plugins.base import DiffStatus, SourceRef
from .renditions import DiffRenditionStore
from .versions import NoPredecessor, VersionError, VersionPair, resolve_pair

log = logging.getLogger("difference_service.pipeline")


class Outcome:
    """Why a run ended. Only ``COMPUTED`` and ``FAILED`` write a manifest."""
    COMPUTED = "computed"          # ran a plugin and committed a result
    CACHED = "cached"              # a complete, current manifest already existed
    UNSUPPORTED = "unsupported"    # no plugin claims the type (§5.3) — not a failure
    NO_PREDECESSOR = "no_predecessor"   # first version; nothing to compare to
    TOO_LARGE = "too_large"        # a side exceeds DIFF_MAX_SOURCE_BYTES
    FAILED = "failed"              # attempted, exhausted its tiers, manifest written
    ERROR = "error"                # could not even attempt (e.g. version missing)


@dataclass
class RunReport:
    outcome: str
    file_uid: str = ""
    base: str = ""
    target: str = ""
    plugin: str = ""
    plugin_version: int = 0
    manifest: Optional[Manifest] = None
    detail: str = ""

    @property
    def wrote_manifest(self) -> bool:
        return self.manifest is not None


class DiffPipeline:
    """Produces (or serves) the diff for one version pair."""

    def __init__(self, config, registry, core):
        self.config = config
        self.registry = registry
        self.core = core
        self.store = DiffRenditionStore(core)
        self.mime = DispatchMimeResolver(core)

    # ------------------------------------------------------------------ run
    def run(self, file_uid: str, target: str = "", base: Optional[str] = None,
            *, force: bool = False) -> RunReport:
        """Produce the diff for ``file_uid``'s ``target`` version against ``base``.

        ``force`` bypasses the cache hit (used by an explicit regenerate); it does
        not bypass the write-order rules."""
        # 1) Which two versions?
        try:
            pair = resolve_pair(self.core, file_uid, target, base)
        except NoPredecessor as e:
            log.debug("%s: %s", file_uid, e)
            return RunReport(Outcome.NO_PREDECESSOR, file_uid=file_uid,
                             target=target, detail=str(e))
        except VersionError as e:
            log.info("%s: %s", file_uid, e)
            return RunReport(Outcome.ERROR, file_uid=file_uid, target=target,
                             detail=str(e))

        # 2) Which plugin? Dispatch on the type, before reading any content — a
        #    format nobody handles should cost one stat, not two full downloads.
        mime = self.mime.resolve(file_uid) or ""
        plugin = self.registry.for_mime(mime)
        if plugin is None:
            log.debug("%s: no diff plugin for %r", file_uid, mime)
            return RunReport(Outcome.UNSUPPORTED, file_uid=file_uid, base=pair.base,
                             target=pair.target, detail=mime)

        # 3) Cache: a complete, current manifest means the work is already done.
        if not force:
            existing = self.store.find_manifest(file_uid, pair.base, pair.target,
                                                plugin.name, plugin.version)
            if existing is not None and self.store.is_complete(file_uid, existing):
                return RunReport(Outcome.CACHED, file_uid=file_uid, base=pair.base,
                                 target=pair.target, plugin=plugin.name,
                                 plugin_version=plugin.version, manifest=existing)

        # 4) Content. Read both sides, guarding the size ceiling so one huge pair
        #    cannot exhaust a worker that other files are queued behind.
        try:
            refs = self._read_pair(file_uid, pair, mime)
        except _TooLarge as e:
            log.info("%s: %s", file_uid, e)
            return RunReport(Outcome.TOO_LARGE, file_uid=file_uid, base=pair.base,
                             target=pair.target, detail=str(e))
        except Exception as e:
            log.warning("%s: could not read version pair", file_uid, exc_info=True)
            return RunReport(Outcome.ERROR, file_uid=file_uid, base=pair.base,
                             target=pair.target, detail=f"{type(e).__name__}: {e}")
        base_ref, target_ref = refs

        # 5) Diff. The registry guarantees a DiffResult even from a misbehaving
        #    plugin, so from here on there is always something to commit.
        plugin, result = self.registry.diff(base_ref, target_ref)

        # 6) Commit, then drop superseded results for this file.
        manifest = self.store.write_result(
            file_uid, result, base=pair.base, target=pair.target,
            plugin=plugin.name, plugin_version=plugin.version)
        self._prune_superseded(file_uid, keep=manifest.key)

        outcome = Outcome.FAILED if result.status == DiffStatus.FAILED else Outcome.COMPUTED
        return RunReport(outcome, file_uid=file_uid, base=pair.base, target=pair.target,
                         plugin=plugin.name, plugin_version=plugin.version,
                         manifest=manifest)

    # -------------------------------------------------------------- helpers
    def _read_pair(self, file_uid: str, pair: VersionPair, mime: str):
        """Both sides' bytes, target first so an oversized target fails fast."""
        cap = int(getattr(self.config, "max_source_bytes", 0) or 0)

        target_data = self.core.read_version(file_uid, back=pair.target_back)
        if cap and len(target_data) > cap:
            raise _TooLarge(f"target version {pair.target} is {len(target_data)} bytes (cap {cap})")
        base_data = self.core.read_version(file_uid, back=pair.base_back)
        if cap and len(base_data) > cap:
            raise _TooLarge(f"base version {pair.base} is {len(base_data)} bytes (cap {cap})")

        name = ""
        try:
            name = getattr(self.core.stat(file_uid), "name", "") or ""
        except Exception:
            pass

        return (
            SourceRef(uid=file_uid, version=pair.base, data=base_data, mime=mime, name=name),
            SourceRef(uid=file_uid, version=pair.target, data=target_data, mime=mime, name=name),
        )

    def _prune_superseded(self, file_uid: str, keep: str) -> None:
        """Drop diff children for other keys of this file.

        A new version supersedes the previous pair, and a plugin-version bump
        supersedes the old generation (§6); both show up as a different key. Kept
        deliberately simple — one current diff per file — rather than retaining a
        history of pairs that nothing reads and that would grow without bound."""
        try:
            removed = self.store.prune(file_uid, keep_keys={keep})
            if removed:
                log.info("pruned %d superseded diff child(ren) of %s", len(removed), file_uid)
        except Exception:
            log.warning("prune failed for %s (result kept)", file_uid, exc_info=True)

    # --------------------------------------------------------------- delete
    def cascade_delete(self, file_uid: str) -> int:
        """Remove every diff child of a deleted file (§2.1). Idempotent."""
        removed = self.store.remove_all(file_uid)
        if removed:
            log.info("cascade-removed %d diff child(ren) of %s", len(removed), file_uid)
        return len(removed)


class _TooLarge(Exception):
    pass

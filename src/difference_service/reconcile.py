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

"""Reconcile sweep — backfill missing and stale diff renditions (M1).

Events are go-forward only and the stream is fail-open (drops-oldest under
backpressure, capped retention), so this sweep is what covers the initial corpus,
files that changed during an outage, and — importantly — **plugin upgrades**: a
bumped plugin ``version`` changes the cache key, so every previously-computed diff
becomes stale and needs regenerating without anyone touching the files.

The sweep does not re-implement any of that logic. It walks the tree and calls the
same ``DiffPipeline.run`` the worker uses, which already answers "is there a
complete, current manifest for this pair?" — so a file that is up to date costs one
MIME resolve plus one manifest read, and everything else is genuinely missing work.
That also means the sweep can never produce a differently-shaped result from the
event path.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, Optional

from .config import Config, load_dotenv
from .core_client import CoreClient
from .pipeline import DiffPipeline, Outcome
from .plugins.registry import default_registry

log = logging.getLogger("difference_service.reconcile")

#: The core's root sentinel.
ROOT_UID = "00000000-0000-0000-0000-000000000000"

#: Depth bound for the tree walk — a guard against a pathological/cyclic tree,
#: not a policy limit.
_MAX_DEPTH = 64


def reconcile_tenant(pipeline: DiffPipeline, tenant: str, *,
                     max_files: Optional[int] = None,
                     root: str = ROOT_UID) -> Dict[str, int]:
    """Depth-first walk of one tenant's tree, diffing anything not up to date.

    Returns a count per outcome. A directory that vanishes or is unreadable
    mid-walk is skipped rather than aborting the sweep — it is a live filesystem,
    and a sweep that dies on the first concurrent delete is useless."""
    counts: Dict[str, int] = {"files": 0}
    mf = pipeline.core._client()
    stack = [(root, 0)]
    seen = set()

    while stack:
        uid, depth = stack.pop()
        if uid in seen or depth > _MAX_DEPTH:
            continue
        seen.add(uid)

        try:
            entries = mf.dir(uid, tenant=tenant)
        except Exception:
            log.debug("reconcile: could not list %s; skipping", uid, exc_info=True)
            continue
        if not entries:
            continue

        for e in entries:
            if getattr(e, "is_container", False):
                stack.append((e.uid, depth + 1))
                continue
            # Skip our own output: diff children are hidden children of a file, so
            # a walk that recursed into them would try to diff diffs.
            from .renditions import is_diff_child
            if is_diff_child(getattr(e, "name", "")):
                continue

            counts["files"] += 1
            try:
                report = pipeline.run(e.uid)
            except Exception:
                counts[Outcome.ERROR] = counts.get(Outcome.ERROR, 0) + 1
                log.exception("reconcile: diff failed for %s", e.uid)
                continue
            counts[report.outcome] = counts.get(report.outcome, 0) + 1

            if max_files and counts["files"] >= max_files:
                log.info("reconcile: hit max_files=%s — sweep truncated, NOT complete",
                         max_files)
                return counts
    return counts


def reconcile(config: Config, tenant: Optional[str] = None, *,
              registry=None, max_files: Optional[int] = None) -> Dict[str, int]:
    """Build a worker-identity pipeline and sweep one tenant (default: config's)."""
    tenant = tenant or config.tenant
    registry = registry if registry is not None else default_registry(config)
    pipeline = DiffPipeline(config, registry, CoreClient(config, tenant))
    result = reconcile_tenant(pipeline, tenant, max_files=max_files)
    log.info("reconcile(%s): %s", tenant, result)
    return result


def main() -> None:
    """Periodic sweep entry point (``difference-reconcile``)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv()
    config = Config()
    interval = int(getattr(config, "reconcile_interval_s", 0) or 0)
    log.info("difference_service reconcile started (tenant=%s interval=%ss)",
             config.tenant, interval or "one-shot")
    try:
        while True:
            try:
                reconcile(config)
            except Exception:
                log.exception("reconcile sweep failed")
            if not interval:
                return
            time.sleep(interval)
    except KeyboardInterrupt:  # pragma: no cover - operator stop
        log.info("difference_service reconcile stopping")


if __name__ == "__main__":  # pragma: no cover
    main()

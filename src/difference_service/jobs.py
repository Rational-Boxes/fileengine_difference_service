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

"""On-demand diff generation queue (SPECIFICATION.md §8, M4).

``GET /files/{uid}/diff`` for an uncomputed pair answers **202 computing** and
queues the work — it does not block. That is a deliberate contract: a PDF or a BIM
model can take tens of seconds, and holding an HTTP request open for that long ties
up a worker, times out at whatever proxy sits in front, and gives the front end no
way to show progress. A 202 plus polling gives the FE a defined state machine.

The queue is small and boring on purpose:

  * **Coalesced by pair key**, so a front end polling every second cannot enqueue
    the same job forty times, and two users asking for the same diff wait on one
    computation.
  * **Bounded**, so a burst of requests for uncomputed diffs cannot grow the queue
    without limit; a rejected submission simply means the caller keeps polling and
    the reconcile sweep will catch the pair anyway.
  * **Runs as the WORKER principal**, never the caller. The request is authorized
    as the user before it is queued; the generation itself writes renditions and
    must not depend on that user still being around when it runs.

Correctness does not rest on this queue. Everything it does, the event worker and
the reconcile sweep also do, and the pipeline's manifest check makes a duplicate
run a cache hit. Losing the queue on restart costs latency, not results.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, Optional, Set, Tuple

log = logging.getLogger("difference_service.jobs")

#: Maximum jobs in flight or waiting before submissions are refused.
DEFAULT_MAX_PENDING = 64


class JobQueue:
    """Coalescing background runner for on-demand diff generation."""

    def __init__(self, pipeline_factory: Callable[[str], object], *,
                 workers: int = 2, max_pending: int = DEFAULT_MAX_PENDING,
                 config=None):
        self._pipeline_factory = pipeline_factory
        self._config = config
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, workers), thread_name_prefix="diff-job")
        self._lock = threading.Lock()
        self._pending: Set[Tuple[str, str, str, str]] = set()
        self._max_pending = max_pending
        #: Purely for observability — what the last run of each key decided.
        self._last: Dict[Tuple[str, str, str, str], str] = {}

    # ---------------------------------------------------------- observability
    def stats(self) -> Dict[str, int]:
        """Queue depth and capacity, for the metrics endpoint.

        `pending` climbing toward `max_pending` is the saturation signal: once it
        is full, submissions are refused rather than queued, so a scraper sees
        the pressure before work starts being dropped.
        """
        with self._lock:
            return {"pending": len(self._pending), "max_pending": self._max_pending}

    # ------------------------------------------------------------ submission
    def submit(self, tenant: str, file_uid: str, target: str = "",
               base: str = "") -> bool:
        """Queue a generation. ``False`` if already queued or the queue is full.

        A ``False`` return is not an error the caller needs to surface: the pair
        is either already being computed or will be picked up by the sweep, and
        either way the client's next poll tells it the truth."""
        key = (tenant, file_uid, target or "", base or "")
        with self._lock:
            if key in self._pending:
                return False
            if len(self._pending) >= self._max_pending:
                log.info("job queue full (%d); refusing %s", self._max_pending, file_uid)
                return False
            self._pending.add(key)

        try:
            self._executor.submit(self._run, key)
            return True
        except RuntimeError:                    # executor already shut down
            with self._lock:
                self._pending.discard(key)
            return False

    # --------------------------------------------------------------- running
    def _run(self, key: Tuple[str, str, str, str]) -> None:
        tenant, file_uid, target, base = key
        try:
            pipeline = self._pipeline_factory(tenant)
            report = pipeline.run(file_uid, target=target, base=base or None)
            with self._lock:
                self._last[key] = report.outcome
            log.info("on-demand diff %s (%s): %s", file_uid, tenant, report.outcome)
        except Exception:
            with self._lock:
                self._last[key] = "error"
            log.exception("on-demand diff failed for %s", file_uid)
        finally:
            with self._lock:
                self._pending.discard(key)

    # ----------------------------------------------------------- inspection
    def is_pending(self, tenant: str, file_uid: str, target: str = "",
                   base: str = "") -> bool:
        with self._lock:
            return (tenant, file_uid, target or "", base or "") in self._pending

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._pending)

    def last_outcome(self, tenant: str, file_uid: str, target: str = "",
                     base: str = "") -> Optional[str]:
        with self._lock:
            return self._last.get((tenant, file_uid, target or "", base or ""))

    # ------------------------------------------------------------ sweeps
    def submit_sweep(self, tenant: str, *, max_files=None, registry=None,
                     config=None) -> bool:
        """Queue a reconcile sweep for a tenant, coalesced like any other job.

        Coalescing matters more here than for a single pair: a sweep is minutes of
        work, so an admin clicking twice (or a health check retrying) must not
        start a second walk of the same tree alongside the first."""
        key = ("sweep", tenant, "", "")
        with self._lock:
            if key in self._pending:
                return False
            self._pending.add(key)

        def _run():
            try:
                from .reconcile import reconcile
                cfg = config or self._config
                counts = reconcile(cfg, tenant, registry=registry, max_files=max_files)
                with self._lock:
                    self._last[key] = "swept"
                log.info("reconcile sweep (%s): %s", tenant, counts)
            except Exception:
                with self._lock:
                    self._last[key] = "error"
                log.exception("reconcile sweep failed for %s", tenant)
            finally:
                with self._lock:
                    self._pending.discard(key)

        try:
            self._executor.submit(_run)
            return True
        except RuntimeError:
            with self._lock:
                self._pending.discard(key)
            return False

    def shutdown(self, wait: bool = False) -> None:
        self._executor.shutdown(wait=wait)

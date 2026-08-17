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

"""The event worker — precompute diffs on version events (SPECIFICATION.md §2.1).

Consumes the shared ``fileengine:events`` stream with a private consumer group and
acts on two types:

  * **file.updated** — a new content version was written: diff it against its
    predecessor so a later request is a cache hit.
  * **file.deleted** — cascade-remove the file's diff renditions.

Everything else is acked and ignored. Critically, **events where
``is_rendition`` is true are skipped**: the worker's own output lands as hidden
children of the source file, and every one of those writes emits its own event. A
worker that reacted to them would diff its own diffs, forever.

Runs as the worker principal (a core client per event tenant, since the stream is
shared across tenants). Ack happens only after a terminal outcome, so a crash
mid-run redelivers rather than losing the work; redelivery is safe because the
pipeline's manifest check collapses it to a cache hit.
"""
from __future__ import annotations

import logging
from collections import OrderedDict

from .config import Config, load_dotenv
from .core_client import CoreClient
from .events import RedisEventSource
from .pipeline import DiffPipeline, Outcome
from .plugins.registry import default_registry

log = logging.getLogger("difference_service.consumer")

#: Event types the worker acts on. Everything else is acked untouched.
HANDLED = ("file.updated", "file.deleted")

#: How many recent event ids to remember for in-process dedupe. This is only an
#: optimisation — the durable guard is the stored manifest — so a bounded window
#: is enough and a restart losing it costs one cache-hit run, not correctness.
_SEEN_MAX = 4096


class EventConsumer:
    """Dispatches recognized events to the diff pipeline."""

    def __init__(self, config: Config, registry=None, *, pipeline_factory=None):
        self.config = config
        self.registry = registry if registry is not None else default_registry(config)
        # A pipeline per tenant: the shared stream is multi-tenant, and every core
        # operation must run in the event's tenant, not one fixed at startup.
        self._pipelines: dict = {}
        self._pipeline_factory = pipeline_factory or self._build_pipeline
        self._seen: "OrderedDict[str, bool]" = OrderedDict()

    def _build_pipeline(self, tenant: str) -> DiffPipeline:
        return DiffPipeline(self.config, self.registry, CoreClient(self.config, tenant))

    def pipeline(self, tenant: str) -> DiffPipeline:
        if tenant not in self._pipelines:
            self._pipelines[tenant] = self._pipeline_factory(tenant)
        return self._pipelines[tenant]

    # ---------------------------------------------------------------- dedupe
    def _already_seen(self, event_id: str) -> bool:
        if not event_id:
            return False
        if event_id in self._seen:
            return True
        self._seen[event_id] = True
        if len(self._seen) > _SEEN_MAX:
            self._seen.popitem(last=False)
        return False

    # --------------------------------------------------------------- dispatch
    def handle(self, event: dict) -> bool:
        """Process one event. Returns ``True`` if the entry may be acked.

        ``False`` means "retry this" and is reserved for outcomes a redelivery
        could plausibly fix — a core that was unreachable, say. A genuine content
        failure is terminal: it has already been recorded as a failed manifest, so
        redelivering it would just repeat the failure."""
        etype = event.get("type") or ""
        event_id = event.get("event_id") or ""
        file_uid = event.get("file_uid") or ""

        if etype not in HANDLED:
            return True

        # The worker's own rendition writes emit events too — reacting to them
        # would diff our own output recursively (§2.1).
        if event.get("is_rendition"):
            log.debug("ignoring rendition event %s (%s)", event_id, etype)
            return True

        if event.get("is_folder") or not file_uid:
            return True

        if self._already_seen(event_id):
            log.debug("event %s already handled in-process", event_id)
            return True

        tenant = event.get("tenant") or "default"
        try:
            pipe = self.pipeline(tenant)
        except Exception:
            log.exception("could not build a pipeline for tenant %s", tenant)
            return False

        if etype == "file.deleted":
            try:
                pipe.cascade_delete(file_uid)
            except Exception:
                log.exception("cascade delete failed for %s", file_uid)
                return False
            return True

        # file.updated
        target = event.get("version") or ""
        try:
            report = pipe.run(file_uid, target=target)
        except Exception:
            log.exception("diff run raised for %s (%s)", file_uid, target)
            return False

        if report.outcome == Outcome.ERROR:
            # Could not attempt — a transient core/version problem. Leave un-acked
            # so redelivery retries; the manifest check makes that cheap if the
            # work in fact completed.
            log.info("event %s: %s (%s) — leaving un-acked", event_id,
                     report.outcome, report.detail)
            return False

        log.info("event %s: %s %s (%s -> %s)", event_id, report.outcome,
                 file_uid, report.base, report.target)
        return True

    # ------------------------------------------------------------------- loop
    def run_forever(self, source: RedisEventSource) -> None:
        source.ensure_group()
        log.info("difference_service consumer started (stream=%s group=%s consumer=%s)",
                 source.stream, source.group, source.consumer)
        try:
            while True:
                for msg_id, event in source.read(count=16, block_ms=5000):
                    try:
                        ack = self.handle(event)
                    except Exception:
                        # Poison entry: log, count, ack — a message we cannot even
                        # parse will never succeed on redelivery.
                        log.exception("unhandled error processing entry %s", msg_id)
                        ack = True
                    if ack:
                        source.ack([msg_id])
        except KeyboardInterrupt:  # pragma: no cover - operator stop
            log.info("difference_service consumer stopping")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv()
    config = Config()
    consumer = EventConsumer(config)
    source = RedisEventSource(config, config.consumer_name)
    consumer.run_forever(source)


if __name__ == "__main__":  # pragma: no cover
    main()

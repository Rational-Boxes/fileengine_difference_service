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
import time
from collections import OrderedDict

from .config import Config, load_dotenv
from .core_client import CoreClient
from .events import RedisEventSource
from .pipeline import DiffPipeline, Outcome
from .plugins.registry import default_registry

log = logging.getLogger("difference_service.consumer")

#: Event types the worker acts on. Everything else is acked untouched.
HANDLED = ("file.updated", "file.deleted", "file.erased")

# The name this service acknowledges erasures under; must match the core's
# FILEENGINE_ERASURE_PARTICIPANTS entry or the core waits forever for an
# acknowledgement filed under a name it is not looking for.
ERASURE_PARTICIPANT = "difference"

#: Governance events that invalidate cached READ decisions (§2, M4). They change
#: *effective* access, so a cached "allow" outlives the grant that justified it
#: until one of these arrives — the TTL alone would leave a revoked permission
#: working for its full window.
GOVERNANCE = ("acl.changed", "role.assigned", "role.member_removed", "role.deleted")

#: How many recent event ids to remember for in-process dedupe. This is only an
#: optimisation — the durable guard is the stored manifest — so a bounded window
#: is enough and a restart losing it costs one cache-hit run, not correctness.
_SEEN_MAX = 4096


class EventConsumer:
    """Dispatches recognized events to the diff pipeline."""

    def __init__(self, config: Config, registry=None, *, pipeline_factory=None,
                 permissions=None, core=None):
        self.config = config
        #: Optional core client, used only to acknowledge erasures. Optional so
        #: existing constructions and tests need not grow a dependency they do
        #: not use; without it an erasure is still honoured — the diff children
        #: are removed — and simply goes unacknowledged, so the core keeps
        #: offering it. Unacknowledged is visible; silently-uncomplied is not.
        self.core = core
        #: Optional PermissionGate to evict on governance events. The worker and
        #: the API share one in a combined process; when the worker runs alone
        #: there is no cache to evict and this stays None.
        self.permissions = permissions
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

    # ---------------------------------------------------------------- erasure
    def _honour_erasure(self, tenant: str, pipe, file_uid: str, erasure_id: str) -> bool:
        """Remove any diff children and acknowledge. Returns whether to ack the event."""
        try:
            removed = pipe.cascade_delete(file_uid)
        except Exception as e:            # noqa: BLE001 — the reason must reach the record
            log.exception("erasure %s: could not remove diff children of %s",
                          erasure_id or "(event)", file_uid)
            self._acknowledge(tenant, erasure_id, False, f"cascade failed: {e}")
            # Retryable: a core that was briefly unreachable is exactly the case
            # redelivery fixes, and the erasure stays outstanding meanwhile.
            return False
        log.info("erased diff children for %s (removed=%s)", file_uid, removed)
        self._acknowledge(tenant, erasure_id, True,
                          f"removed {removed} diff rendition(s); no local store")
        return True

    def _acknowledge(self, tenant: str, erasure_id: str, complied: bool, detail: str) -> None:
        core = getattr(self, "core", None)
        if not erasure_id or core is None:
            return
        try:
            state = core.acknowledge_erasure(erasure_id, ERASURE_PARTICIPANT,
                                             complied=complied, detail=detail, tenant=tenant)
            log.info("acknowledged erasure %s (complied=%s) -> %s", erasure_id, complied, state)
        except Exception as e:            # noqa: BLE001
            # The removal already happened. A lost ack delays completion, which is
            # the safe direction; the sweep re-offers it.
            log.warning("erasure %s honoured but ack failed: %s", erasure_id, e)

    def sweep_erasures(self, tenants, limit: int = 100) -> int:
        """The guarantee path (§5.4.5) — the event bus is fail-open by design."""
        core = getattr(self, "core", None)
        if core is None:
            return 0
        done = 0
        for tenant in tenants:
            try:
                pending = core.list_pending_erasures(ERASURE_PARTICIPANT, limit=limit,
                                                     tenant=tenant)
            except Exception as e:        # noqa: BLE001
                log.warning("erasure sweep: could not list pending for %s: %s", tenant, e)
                continue
            for item in pending:
                try:
                    pipe = self.pipeline(tenant)
                except Exception:         # noqa: BLE001
                    log.exception("erasure sweep: no pipeline for %s", tenant)
                    continue
                if self._honour_erasure(tenant, pipe, item["uid"], item["erasure_id"]):
                    done += 1
        return done

    def _start_erasure_sweeper(self) -> None:
        """Poll for erasures we owe, forever, in a daemon thread. Never fatal.

        getattr rather than self.core: run_forever is exercised against
        minimally-constructed consumers in the tests, and the sweeper is an
        addition to the loop rather than a precondition for it.
        """
        if getattr(self, "core", None) is None:
            log.warning("erasure sweeper not started: no core client")
            return
        import threading

        interval = int(getattr(self.config, "erasure_sweep_interval_s", 60) or 60)
        raw = getattr(self.config, "erasure_sweep_tenants", "") or ""
        tenants = [t.strip() for t in raw.split(",") if t.strip()] or \
                  [getattr(self.config, "tenant", "default") or "default"]

        def loop() -> None:
            while True:
                try:
                    done = self.sweep_erasures(tenants)
                    if done:
                        log.info("erasure sweep honoured %d outstanding erasure(s)", done)
                except Exception:
                    log.exception("erasure sweep failed; retrying next tick")
                time.sleep(interval)

        threading.Thread(target=loop, name="erasure-sweep", daemon=True).start()
        log.info("erasure sweeper started (every %ss, tenants=%s, participant=%s)",
                 interval, ",".join(tenants), ERASURE_PARTICIPANT)

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

        if etype in GOVERNANCE:
            self._invalidate(etype, event)
            return True

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

        if etype == "file.erased":
            # This service keeps no database of its own: comparison manifests and
            # diff renditions are children under the file's uid IN THE CORE, and
            # the core's own erasure already destroys its rendition children. So
            # the removal here is belt-and-braces (idempotent — a file whose
            # children are already gone yields nothing), and the acknowledgement
            # is the part that matters.
            #
            # No local tombstone, deliberately, and not from laziness: there is
            # nowhere to put one, and nothing for it to guard. A diff needs two
            # versions of a file and erasure destroys every version, so a late
            # job cannot regenerate anything — the core has nothing left to
            # compare. The other consumers need tombstones because they hold
            # their own copies of the content; this one does not.
            return self._honour_erasure(tenant, pipe, file_uid,
                                        event.get("erasure_id", ""))

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

    # --------------------------------------------------------- governance
    def _invalidate(self, etype: str, event: dict) -> None:
        """Evict cached READ decisions a governance event invalidates.

        Scoped as narrowly as the event allows: ``acl.changed`` names a resource,
        the role events name a member, and ``role.deleted`` names neither (its
        members are unknown by then) so the whole tenant is dropped. Erring wider
        costs a few extra core round trips; erring narrower serves access that has
        been revoked."""
        gate = self.permissions
        if gate is None:
            return
        tenant = event.get("tenant") or "default"
        try:
            if etype == "acl.changed":
                gate.invalidate_resource(tenant, event.get("file_uid") or "")
            elif etype in ("role.assigned", "role.member_removed"):
                member = event.get("member") or ""
                if member:
                    gate.invalidate_member(tenant, member)
                else:
                    gate.invalidate_tenant(tenant)
            elif etype == "role.deleted":
                gate.invalidate_tenant(tenant)
        except Exception:
            # A cache that cannot be evicted is a correctness risk, so clear it
            # wholesale rather than carrying on with entries that may be stale.
            log.warning("permission invalidation failed; clearing the cache",
                        exc_info=True)
            try:
                gate.clear()
            except Exception:
                pass

    # ------------------------------------------------------------------- loop
    def run_forever(self, source: RedisEventSource) -> None:
        source.ensure_group()
        log.info("difference_service consumer started (stream=%s group=%s consumer=%s)",
                 source.stream, source.group, source.consumer)
        # The erasure guarantee path (§5.4.5), on a timer beside the event loop:
        # the triggering event is fail-open and drop-oldest by design.
        self._start_erasure_sweeper()
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
    # A core client, so erasures can actually be ACKNOWLEDGED. Without one the
    # diff children are still removed but the core is never told, so every
    # erasure this service participates in stays outstanding for ever.
    try:
        from .core_client import agent_client
        core = agent_client(config)
    except Exception:               # noqa: BLE001
        core = None
        log.warning("no core client: erasures will be honoured but not acknowledged",
                    exc_info=True)
    consumer = EventConsumer(config, core=core)
    source = RedisEventSource(config, config.consumer_name)
    consumer.run_forever(source)


if __name__ == "__main__":  # pragma: no cover
    main()

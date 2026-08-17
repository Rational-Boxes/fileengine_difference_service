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

"""Plugin registry + dispatch (SPECIFICATION.md §4).

Registration order is priority — the first plugin whose ``supports()`` accepts the
MIME type wins, so specific plugins are registered ahead of general ones, mirroring
``convert_search_ai``'s ``PluginRegistry``.

The registry is where the spec's "never fail the file" rule is *enforced* rather
than merely requested. A plugin is expected to degrade internally, but a plugin
that raises anyway — or one whose external tool is missing — is caught here and
converted into a ``failed`` DiffResult. The worker therefore always has something
to commit, which is what keeps the §7.1.1 contract honest: every attempted pair
ends with a manifest, so "attempted and failed" never masquerades as "not yet
computed".
"""
from __future__ import annotations

import logging
from typing import List, Optional

from .base import DiffPlugin, DiffResult, SourceRef

log = logging.getLogger("difference_service.plugins")


class PluginRegistry:
    def __init__(self, plugins: Optional[List[DiffPlugin]] = None,
                 enabled: Optional[set] = None):
        #: ``enabled`` (DIFF_ENABLED_PLUGINS) restricts which registered plugins are
        #: active for a deployment; empty/None means all of them.
        self._plugins: List[DiffPlugin] = list(plugins or [])
        self._enabled = set(enabled or ())

    def register(self, plugin: DiffPlugin) -> None:
        self._plugins.append(plugin)

    @property
    def plugins(self) -> List[DiffPlugin]:
        """Active plugins, in priority order."""
        if not self._enabled:
            return list(self._plugins)
        return [p for p in self._plugins if p.name in self._enabled]

    def for_mime(self, mime: str) -> Optional[DiffPlugin]:
        """First active plugin that supports ``mime``, else ``None``.

        A plugin whose ``supports()`` raises is skipped rather than allowed to
        break dispatch for every other format."""
        for p in self.plugins:
            try:
                if p.supports(mime):
                    return p
            except Exception:
                log.warning("plugin %s raised in supports(%r); skipping",
                            getattr(p, "name", p), mime, exc_info=True)
                continue
        return None

    def supports(self, mime: str) -> bool:
        return self.for_mime(mime) is not None

    def diff(self, base: SourceRef, target: SourceRef):
        """Run the plugin matching the *target's* MIME type.

        Returns ``(plugin, result)``. ``plugin`` is ``None`` when no plugin claims
        the type — an unsupported format is NOT a failure and must not produce a
        ``failed`` manifest, because there is nothing to retry and nothing went
        wrong; raster images are the intended example (§5.3, front-end flip).

        A MIME change between the two versions is itself a degradation case: the
        pair is dispatched on the target's type, and a plugin that cannot make
        sense of the base is expected to fall back rather than raise."""
        plugin = self.for_mime(target.mime)
        if plugin is None:
            return None, None
        try:
            result = plugin.diff(base, target)
        except Exception as e:
            log.exception("plugin %s raised diffing %s (%s -> %s)",
                          plugin.name, target.uid, base.version, target.version)
            return plugin, DiffResult.failed(
                stage="plugin", reason=f"{type(e).__name__}: {e}")
        if not isinstance(result, DiffResult):
            log.error("plugin %s returned %r, not a DiffResult", plugin.name, type(result))
            return plugin, DiffResult.failed(
                stage="plugin", reason="plugin returned a non-DiffResult")
        return plugin, result


def default_registry(config=None) -> PluginRegistry:
    """The standard plugin set.

    The PDF (M2) and 3D (M3) plugins register here as they land, most specific
    first. An empty registry is a valid, working service: every pair reports
    "unsupported", which is exactly the M0/M1 contract.

    ``passthrough-text`` is a validation aid, not a product feature, so it is
    included ONLY when a deployment names it in ``DIFF_ENABLED_PLUGINS`` —
    production must not start diffing text files just because nothing else claims
    them."""
    enabled = getattr(config, "enabled_plugins", None) if config is not None else None
    plugins: List[DiffPlugin] = []
    if enabled and "passthrough-text" in enabled:
        from .passthrough import PassthroughTextPlugin
        plugins.append(PassthroughTextPlugin())
    return PluginRegistry(plugins, enabled=enabled)

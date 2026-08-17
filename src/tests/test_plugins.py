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

"""Plugin framework unit tests (SPECIFICATION.md §4) — no core/LDAP/redis needed."""
from difference_service.plugins import (
    DiffChild, DiffMode, DiffPlugin, DiffResult, DiffStatus, PluginRegistry, SourceRef,
)


def _src(mime="application/pdf", version="v1", data=b"x"):
    return SourceRef(uid="F", version=version, data=data, mime=mime, name="doc.pdf")


def _child(index=0, mode=DiffMode.VECTOR, kind="page"):
    return DiffChild(kind=kind, index=index, data=b"<svg/>", mime="image/svg+xml",
                     ext="svg", mode=mode)


class Stub(DiffPlugin):
    name = "stub"
    version = 1

    def __init__(self, mimes=("application/pdf",), result=None, raises=None):
        self.mimes = set(mimes)
        self.result = result
        self.raises = raises
        self.calls = 0

    def supports(self, mime):
        return mime in self.mimes

    def diff(self, base, target):
        self.calls += 1
        if self.raises:
            raise self.raises
        return self.result if self.result is not None else DiffResult(children=[_child()])


# ------------------------------------------------------------------ dispatch
def test_dispatches_on_the_target_mime():
    p = Stub()
    plugin, result = PluginRegistry([p]).diff(_src(), _src(version="v2"))
    assert plugin is p and result.is_ready and p.calls == 1


def test_registration_order_is_priority():
    first, second = Stub(), Stub()
    plugin, _ = PluginRegistry([first, second]).diff(_src(), _src())
    assert plugin is first and second.calls == 0


def test_unsupported_mime_is_not_a_failure():
    # §5.3: raster images get a front-end flip, not a server diff. "Nothing to do"
    # must not look like "tried and failed", or the FE would show an error.
    plugin, result = PluginRegistry([Stub()]).diff(
        _src(mime="image/png"), _src(mime="image/png"))
    assert plugin is None and result is None


def test_enabled_allowlist_filters_plugins():
    p = Stub()
    reg = PluginRegistry([p], enabled={"other"})
    assert reg.plugins == []
    assert reg.for_mime("application/pdf") is None


def test_supports_raising_does_not_break_dispatch():
    class Bad(Stub):
        def supports(self, mime):
            raise RuntimeError("boom")

    good = Stub()
    plugin, _ = PluginRegistry([Bad(), good]).diff(_src(), _src())
    assert plugin is good


# ------------------------------------------------------------- never fail
def test_a_raising_plugin_becomes_a_failed_result():
    p = Stub(raises=ValueError("kaboom"))
    plugin, result = PluginRegistry([p]).diff(_src(), _src())
    assert plugin is p
    assert result.status == DiffStatus.FAILED
    assert result.failure.stage == "plugin"
    assert "kaboom" in result.failure.reason


def test_a_plugin_returning_junk_becomes_a_failed_result():
    p = Stub()
    p.result = "not a DiffResult"
    _, result = PluginRegistry([p]).diff(_src(), _src())
    assert result.status == DiffStatus.FAILED


# ------------------------------------------------------------- result modes
def test_single_tier_reports_that_tier():
    r = DiffResult(children=[_child(0), _child(1)])
    assert r.mode == DiffMode.VECTOR


def test_mixed_tiers_report_mixed():
    # A PDF with vector and scanned pages is the normal case, not an error — the
    # FE must consult the per-unit map rather than assume one view engine.
    r = DiffResult(children=[_child(0, DiffMode.VECTOR), _child(1, DiffMode.RASTER)])
    assert r.mode == DiffMode.MIXED


def test_units_map_is_index_ordered():
    r = DiffResult(children=[_child(2), _child(0), _child(1)])
    assert [u["index"] for u in r.units()] == [0, 1, 2]


def test_ready_requires_children():
    # A "ready" diff with nothing to show is not ready.
    assert not DiffResult(children=[]).is_ready
    assert DiffResult(children=[_child()]).is_ready


def test_failed_helper_records_the_attempted_tiers():
    r = DiffResult.failed("render", "no backend", tiers=[DiffMode.VECTOR, DiffMode.RASTER])
    assert r.status == DiffStatus.FAILED and not r.is_ready
    assert r.failure.as_dict()["tiers_attempted"] == ["vector", "raster"]

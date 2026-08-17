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

"""The request surface (SPECIFICATION.md §8, M4) — hermetic.

Every branch of ``GET /files/{uid}/diff`` matters to the front end, because each
one implies a different action: render, poll, fall back to side-by-side, or flip
between versions itself. These pin all of them, plus the READ gate that guards the
lot.
"""
import pytest
from fastapi.testclient import TestClient

from difference_service.app import build_app
from difference_service.config import Config
from difference_service.ldap_auth import Identity
from difference_service.manifest import Manifest, manifest_name
from difference_service.permissions import PermissionGate
from difference_service.plugins import DiffPlugin, DiffResult, PluginRegistry
from difference_service.plugins.base import DiffChild, DiffMode, DiffStatus
from difference_service.token_store import TokenStore

ROOT = "F"


class Stub(DiffPlugin):
    name = "stub"
    version = 1

    def supports(self, mime):
        return mime == "application/pdf"

    def diff(self, base, target):
        return DiffResult(children=[DiffChild(
            kind="page", index=0, data=b"<svg/>", mime="image/svg+xml", ext="svg",
            mode=DiffMode.VECTOR)])


class Entry:
    def __init__(self, uid, name):
        self.uid, self.name = uid, name


class FakeMF:
    def __init__(self, children=None, content=None):
        self._children = children or {}
        self._content = content or {}

    def dir(self, uid, tenant=None):
        return [Entry(u, n) for u, n in self._children.items()]

    def get(self, uid, back=0, tenant=None):
        import io
        return io.BytesIO(self._content.get(uid, b""))


class Rev:
    def __init__(self, v):
        self.version = v


class FakeCore:
    """Stands in for the per-user CoreClient the endpoint builds."""
    versions = ["v2", "v1"]
    allow = True
    children = {}
    content = {}
    mime = "application/pdf"

    def __init__(self, config=None, tenant="default", identity=None):
        self.tenant = tenant
        self.actor = getattr(identity, "user", "") or "svc"
        self.mf = FakeMF(type(self).children, type(self).content)

    def _client(self):
        return self.mf

    def revisions(self, uid):
        return [Rev(v) for v in type(self).versions]

    def check_permission(self, uid, permission="READ"):
        return type(self).allow

    def read_prefix(self, uid, n=8192):
        return b"%PDF-1.4"

    def stat(self, uid):
        class I:
            name = "doc.pdf"
        return I()


class FakeJobs:
    def __init__(self):
        self.submitted = []
        self.sweeps = []
        self.accept = True

    def submit(self, tenant, file_uid, target="", base=""):
        self.submitted.append((tenant, file_uid, target, base))
        return self.accept

    def submit_sweep(self, tenant, *, max_files=None, registry=None, config=None):
        self.sweeps.append((tenant, max_files))
        return True


@pytest.fixture(autouse=True)
def _reset():
    FakeCore.versions = ["v2", "v1"]
    FakeCore.allow = True
    FakeCore.children = {}
    FakeCore.content = {}
    yield


@pytest.fixture
def client(monkeypatch):
    import difference_service.api as api

    monkeypatch.setattr(api, "CoreClient", FakeCore)
    store = TokenStore(ttl_seconds=60)
    jobs = FakeJobs()
    app = build_app(Config(), token_store=store, registry=PluginRegistry([Stub()]),
                    permissions=PermissionGate(0), jobs=jobs)
    # MIME resolution would otherwise sniff through the fake core; keep it explicit.
    app.state.mime_resolver = lambda core: type("R", (), {
        "resolve": staticmethod(lambda uid: FakeCore.mime)})()
    c = TestClient(app)
    c.token_store = store
    c.jobs = jobs
    return c


def _auth(client, user="alice", roles=("users",)):
    token = client.token_store.issue(
        Identity(user=user, roles=list(roles), tenant="default", authenticated=True))
    return {"Authorization": f"Bearer {token}"}


def _store_manifest(status=DiffStatus.READY, children=1, plugin="stub", version=1):
    """Put a manifest (and its children) into the fake core's hidden children."""
    result = DiffResult(
        children=[DiffChild(kind="page", index=i, data=b"<svg/>",
                            mime="image/svg+xml", ext="svg", mode=DiffMode.VECTOR)
                  for i in range(children)],
        status=status)
    if status == DiffStatus.FAILED:
        result = DiffResult.failed("render", "no backend", tiers=["vector"])
    m = Manifest.from_result(result, file_uid=ROOT, base_version="v1",
                             target_version="v2", plugin=plugin, plugin_version=version)
    kids, content = {}, {}
    for i, name in enumerate(m.expected):
        kids[f"c{i}"] = name
        content[f"c{i}"] = b"<svg/>"
    kids["mani"] = manifest_name(m.key)
    content["mani"] = m.to_bytes()
    FakeCore.children = kids
    FakeCore.content = content
    return m


# --------------------------------------------------------------------- auth
def test_diff_requires_authentication(client):
    assert client.get(f"/files/{ROOT}/diff").status_code == 401


def test_diff_requires_read_permission(client):
    FakeCore.allow = False
    r = client.get(f"/files/{ROOT}/diff", headers=_auth(client))
    assert r.status_code == 403


def test_the_read_gate_fails_closed_when_the_core_errors(client, monkeypatch):
    # A permission cache that guesses "allow" on error is a vulnerability.
    def boom(self, uid, permission="READ"):
        raise RuntimeError("core down")

    monkeypatch.setattr(FakeCore, "check_permission", boom)
    assert client.get(f"/files/{ROOT}/diff", headers=_auth(client)).status_code == 403


# ------------------------------------------------------------------- ready
def test_a_stored_diff_is_served_with_child_references(client):
    m = _store_manifest(children=2)
    r = client.get(f"/files/{ROOT}/diff", headers=_auth(client))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["manifest"]["key"] == m.key
    assert [c["index"] for c in body["children"]] == [0, 1]
    # The FE needs a uid to fetch and the per-unit mode for that page.
    assert all(c["uid"] and c["mode"] for c in body["children"])


def test_child_references_carry_the_per_unit_mode(client):
    _store_manifest(children=1)
    body = client.get(f"/files/{ROOT}/diff", headers=_auth(client)).json()
    assert body["children"][0]["mode"] == DiffMode.VECTOR
    assert body["children"][0]["kind"] == "page"


# ----------------------------------------------------------------- pending
def test_an_uncomputed_pair_is_202_and_queues_generation(client):
    r = client.get(f"/files/{ROOT}/diff", headers=_auth(client))
    assert r.status_code == 202
    assert r.json()["status"] == "pending"
    assert client.jobs.submitted == [("default", ROOT, "v2", "v1")]


def test_an_incomplete_result_is_pending_not_partial(client):
    # §7.1.1: a manifest whose children are missing must NEVER be served.
    m = _store_manifest(children=2)
    FakeCore.children = {k: v for k, v in FakeCore.children.items() if v != m.expected[0]}
    r = client.get(f"/files/{ROOT}/diff", headers=_auth(client))
    assert r.status_code == 202


def test_a_stale_plugin_version_recomputes_rather_than_serving(client):
    # §6: a bumped plugin version changes the key, so the old result is not found.
    _store_manifest(children=1, version=99)
    assert client.get(f"/files/{ROOT}/diff", headers=_auth(client)).status_code == 202


def test_a_full_queue_still_answers_202(client):
    # The client keeps polling and the sweep will catch it; refusing the submission
    # is not an error the caller must handle.
    client.jobs.accept = False
    r = client.get(f"/files/{ROOT}/diff", headers=_auth(client))
    assert r.status_code == 202 and r.json()["queued"] is False


# ------------------------------------------------------------------ failed
def test_a_failed_diff_is_422_with_the_reason(client):
    # The FE falls back to a side-by-side rather than rendering a broken diff.
    _store_manifest(status=DiffStatus.FAILED)
    r = client.get(f"/files/{ROOT}/diff", headers=_auth(client))
    assert r.status_code == 422
    body = r.json()
    assert body["status"] == "failed"
    assert body["failure"]["stage"] == "render"


# ------------------------------------------------------- defined non-diffs
def test_an_unsupported_type_is_200_not_an_error(client):
    # §5.3: raster images are a front-end flip. "Nothing to compute" must not read
    # as "this broke", or the FE shows an error for a working file.
    FakeCore.mime = "image/png"
    try:
        r = client.get(f"/files/{ROOT}/diff", headers=_auth(client))
        assert r.status_code == 200
        assert r.json()["status"] == "unsupported"
        assert client.jobs.submitted == []
    finally:
        FakeCore.mime = "application/pdf"


def test_a_first_version_reports_none_and_queues_nothing(client):
    # Polling forever for something that can never exist is the failure to avoid.
    FakeCore.versions = ["v1"]
    r = client.get(f"/files/{ROOT}/diff", headers=_auth(client))
    assert r.status_code == 200
    assert r.json()["reason"] == "no_predecessor"
    assert client.jobs.submitted == []


def test_an_unknown_version_is_404(client):
    r = client.get(f"/files/{ROOT}/diff?version=nope", headers=_auth(client))
    assert r.status_code == 404


def test_an_explicit_base_newer_than_the_target_is_rejected(client):
    # Would invert old/new so additions render as deletions.
    r = client.get(f"/files/{ROOT}/diff?version=v1&base=v2", headers=_auth(client))
    assert r.status_code == 404


# --------------------------------------------------------------- reconcile
def test_reconcile_requires_tenant_admin(client):
    r = client.post("/diff/reconcile", json={}, headers=_auth(client))
    assert r.status_code == 403
    assert client.jobs.sweeps == []


def test_an_admin_can_start_a_sweep(client):
    r = client.post("/diff/reconcile", json={},
                    headers=_auth(client, roles=("users", "administrators")))
    assert r.status_code == 202
    assert r.json()["status"] == "started"
    assert client.jobs.sweeps and client.jobs.sweeps[0][0] == "default"


def test_a_sweep_honours_an_explicit_max_files(client):
    client.post("/diff/reconcile", json={"max_files": 10},
                headers=_auth(client, roles=("administrators",)))
    assert client.jobs.sweeps[0][1] == 10

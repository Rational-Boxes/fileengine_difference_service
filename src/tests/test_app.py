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

"""HTTP surface unit tests (SPECIFICATION.md §8) — hermetic, no core/LDAP."""
import pytest
from fastapi.testclient import TestClient

from difference_service.app import build_app
from difference_service.config import Config
from difference_service.ldap_auth import Identity
from difference_service.plugins import DiffPlugin, DiffResult, PluginRegistry
from difference_service.token_store import TokenStore


class Stub(DiffPlugin):
    name = "stub"
    version = 7

    def supports(self, mime):
        return mime == "application/pdf"

    def diff(self, base, target):
        return DiffResult()


@pytest.fixture
def client():
    store = TokenStore(ttl_seconds=60)
    app = build_app(Config(), token_store=store,
                    registry=PluginRegistry([Stub()]))
    c = TestClient(app)
    c.token_store = store
    return c


def _auth(client, user="alice", roles=("users",)):
    """Issue a bearer token directly — the LDAP bind itself is covered by the
    @live harness; these tests exercise the surface, not the directory."""
    token = client.token_store.issue(
        Identity(user=user, roles=list(roles), tenant="default", authenticated=True))
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------- health
def test_healthz_is_open_and_names_the_service(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["service"] == "difference_service"


def test_poolz_reports_the_active_plugin_count(client):
    assert client.get("/poolz").json()["plugins"] == 1


def test_monitoring_allowlist_blocks_a_foreign_client(monkeypatch):
    # The endpoints already bind loopback; the allowlist is defence in depth for a
    # deployment that exposes them further.
    monkeypatch.setenv("FILEENGINE_MONITORING_ALLOW_IPS", "10.9.9.9")
    c = TestClient(build_app(Config()))
    assert c.get("/healthz").status_code == 403
    # A non-monitoring path is unaffected by the allowlist.
    assert c.get("/whoami").status_code == 401


# --------------------------------------------------------------------- auth
def test_whoami_requires_authentication(client):
    assert client.get("/whoami").status_code == 401


def test_whoami_reflects_the_bearer_identity(client):
    r = client.get("/whoami", headers=_auth(client))
    assert r.status_code == 200
    assert r.json() == {"user": "alice", "roles": ["users"], "tenant": "default"}


def test_a_bad_token_is_rejected(client):
    r = client.get("/whoami", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_auth_token_rejects_bad_credentials(client):
    # No LDAP here, so the bind cannot succeed — the point is that failure is a
    # clean 401 and never leaks a token.
    r = client.post("/auth/token", json={"username": "x", "password": "y"})
    assert r.status_code == 401
    assert "access_token" not in r.json()


def test_x_tenant_header_scopes_the_identity(client):
    headers = _auth(client)
    headers["X-Tenant"] = "acme"
    assert client.get("/whoami", headers=headers).json()["tenant"] == "acme"


# ------------------------------------------------------------------ plugins
def test_plugins_endpoint_requires_authentication(client):
    assert client.get("/plugins").status_code == 401


def test_plugins_endpoint_exposes_name_and_version(client):
    # plugin_version is part of the cache key (§6), so an operator needs to see
    # which generation a deployment will produce.
    r = client.get("/plugins", headers=_auth(client))
    assert r.json()["plugins"] == [{"name": "stub", "version": 7}]


def test_an_empty_registry_is_a_valid_service(client):
    # M0 ships no format plugins; the service must still come up and answer.
    c = TestClient(build_app(Config(), registry=PluginRegistry([])))
    assert c.get("/healthz").status_code == 200
    assert c.get("/poolz").json()["plugins"] == 0

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

"""@live harness — exercises the real dev core + LDAP (DEVELOPMENT_PLAN M0).

Deselected by default (``addopts = -m 'not live'``); run with::

    pytest -m live

Bring the dev stack up first (``scripts/start_backend_services.sh``) — LDAP on
:1389 and the core gRPC on :50051. Each test skips individually when the
dependency it needs is absent, so a partial stack gives a useful partial signal
rather than a wall of errors: LDAP-only tests still run when the core is down.

Credentials come from the environment so this file carries none:
    DIFF_LIVE_USER / DIFF_LIVE_PASSWORD   (default: the dev test user)
"""
import os
import socket
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from difference_service.app import build_app
from difference_service.config import Config
from difference_service.core_client import CoreClient, agent_identity
from difference_service.ldap_auth import authenticate

pytestmark = pytest.mark.live

LIVE_USER = os.environ.get("DIFF_LIVE_USER", "testuser@rationalboxes.com")
LIVE_PASSWORD = os.environ.get("DIFF_LIVE_PASSWORD", "")

#: The core's root sentinel (all-zeros UUID).
ROOT_UID = "00000000-0000-0000-0000-000000000000"


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def _config() -> Config:
    cfg = Config()
    # The worker principal is the live user for harness purposes, so /readyz's
    # LDAP probe (which binds as the agent) has something real to bind with.
    cfg.agent_user = LIVE_USER
    cfg.agent_password = LIVE_PASSWORD
    return cfg


@pytest.fixture(scope="module")
def config():
    if not LIVE_PASSWORD:
        pytest.skip("set DIFF_LIVE_PASSWORD to run the @live harness")
    return _config()


@pytest.fixture(scope="module")
def ldap_up(config):
    parsed = urlparse(config.ldap_uri)
    host = parsed.hostname or "localhost"
    port = parsed.port or (636 if parsed.scheme == "ldaps" else 389)
    if not _port_open(host, port):
        pytest.skip(f"LDAP not reachable at {config.ldap_uri}")
    return True


@pytest.fixture(scope="module")
def core_up(config):
    host, port = config.grpc_host, int(config.grpc_port)
    if not _port_open(host, port):
        pytest.skip(f"core gRPC not reachable at {config.grpc_address} "
                    "(start it with scripts/start_backend_services.sh)")
    return True


# ---------------------------------------------------------------------- LDAP
def test_ldap_bind_resolves_roles(config, ldap_up):
    ident = authenticate(config, LIVE_USER, LIVE_PASSWORD)
    assert ident.authenticated, "live LDAP bind failed — check DIFF_LIVE_* credentials"
    assert ident.user == LIVE_USER
    assert ident.roles, "expected at least one role from LDAP group membership"


def test_ldap_rejects_a_bad_password(config, ldap_up):
    assert not authenticate(config, LIVE_USER, "definitely-not-the-password").authenticated


def test_agent_identity_binds(config, ldap_up):
    assert agent_identity(config).authenticated


def test_auth_token_issues_a_usable_bearer(config, ldap_up):
    client = TestClient(build_app(config))
    r = client.post("/auth/token", json={"username": LIVE_USER, "password": LIVE_PASSWORD})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]

    who = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert who.status_code == 200
    assert who.json()["user"] == LIVE_USER


# ---------------------------------------------------------------------- core
def test_core_channel_is_reachable(config, core_up):
    import grpc
    channel = grpc.insecure_channel(config.grpc_address)
    try:
        grpc.channel_ready_future(channel).result(timeout=5)
    finally:
        channel.close()


def test_core_client_reads_the_root_as_the_user(config, core_up, ldap_up):
    """The end-user client path (§2): identity-bound reads, not the worker.

    NB the core reports the root sentinel with an EMPTY ``uid`` (it echoes back
    name/type but not the all-zeros id), so identity is asserted on what the core
    actually returns rather than on the uid round-tripping."""
    ident = authenticate(config, LIVE_USER, LIVE_PASSWORD)
    assert ident.authenticated
    core = CoreClient(config, ident.tenant, identity=ident)
    info = core.stat(ROOT_UID)
    assert getattr(info, "name", "") == "root"
    assert getattr(info, "is_dir", False) is True


def test_read_permission_gate_answers_for_the_root(config, core_up, ldap_up):
    ident = authenticate(config, LIVE_USER, LIVE_PASSWORD)
    core = CoreClient(config, ident.tenant, identity=ident)
    assert core.check_permission(ROOT_UID, "READ") is True


def test_permission_check_fails_closed_when_the_core_is_unreachable(config, ldap_up):
    """Fail-closed is the whole point of the gate: an *error* must deny, not allow.

    Deliberately not asserted via a nonexistent UID — the dev test user resolves to
    ``system_admin``, which bypasses ACL evaluation in the core, so a bogus UID
    answers True and would prove nothing. Pointing at a dead endpoint exercises the
    real failure path."""
    ident = authenticate(config, LIVE_USER, LIVE_PASSWORD)
    cfg = _config()
    cfg.grpc_host, cfg.grpc_port = "127.0.0.1", "1"
    cfg.grpc_address = f"{cfg.grpc_host}:{cfg.grpc_port}"
    core = CoreClient(cfg, ident.tenant, identity=ident)
    assert core.check_permission(ROOT_UID, "READ") is False


def test_the_dev_admin_user_resolves_to_system_admin(config, ldap_up):
    """Pins the CURRENT mapping so a change to it is a visible test failure.

    ``ldap_auth`` maps the tenant ``administrators`` group to the core's
    ``system_admin`` role — which is a full ACL bypass in the core — matching
    convert_search_ai / folder_actions / discussion. Worth knowing when reading
    any permission result for an admin: it is not evidence the ACL allowed it."""
    ident = authenticate(config, LIVE_USER, LIVE_PASSWORD)
    assert "administrators" in ident.roles
    assert "system_admin" in ident.roles


# --------------------------------------------------------------------- ready
def test_readyz_reports_ok_against_the_live_stack(config, core_up, ldap_up):
    client = TestClient(build_app(config))
    r = client.get("/readyz")
    assert r.status_code == 200, r.text
    assert r.json()["checks"] == {"core": True, "ldap": True}


def test_readyz_is_degraded_when_the_core_is_unreachable(config, ldap_up):
    """Readiness must actually fail when a dependency is gone, or it is theatre."""
    cfg = _config()
    cfg.grpc_host, cfg.grpc_port = "127.0.0.1", "1"      # nothing listens on :1
    cfg.grpc_address = f"{cfg.grpc_host}:{cfg.grpc_port}"
    r = TestClient(build_app(cfg)).get("/readyz")
    assert r.status_code == 503
    assert r.json()["checks"]["core"] is False

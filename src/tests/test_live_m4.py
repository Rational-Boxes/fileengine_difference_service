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

"""@live end-to-end for M4: the HTTP surface against a real core.

Drives the whole request contract the front end will use — 202 while computing,
200 with child references once ready, and the READ gate — through the real app,
real LDAP auth and real renditions. The hermetic tests prove each branch; this
proves they agree with the pipeline actually running underneath.
"""
import os
import socket
import time

import pytest
from fastapi.testclient import TestClient

from difference_service.app import build_app
from difference_service.config import Config
from difference_service.core_client import client_for
from difference_service.ldap_auth import authenticate
from tests.fixtures import pdf as F

pytestmark = pytest.mark.live

LIVE_USER = os.environ.get("DIFF_LIVE_USER", "testuser@rationalboxes.com")
LIVE_PASSWORD = os.environ.get("DIFF_LIVE_PASSWORD", "")
ROOT_UID = "00000000-0000-0000-0000-000000000000"


def _port_open(host, port, timeout=1.0):
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def config():
    if not LIVE_PASSWORD:
        pytest.skip("set DIFF_LIVE_PASSWORD to run the @live harness")
    cfg = Config()
    if not _port_open(cfg.grpc_host, int(cfg.grpc_port)):
        pytest.skip(f"core gRPC not reachable at {cfg.grpc_address}")
    cfg.agent_user, cfg.agent_password = LIVE_USER, LIVE_PASSWORD
    return cfg


@pytest.fixture(scope="module")
def identity(config):
    ident = authenticate(config, LIVE_USER, LIVE_PASSWORD)
    if not ident.authenticated:
        pytest.skip("live LDAP bind failed")
    return ident


@pytest.fixture
def client(config):
    app = build_app(config)
    c = TestClient(app)
    c.app_ref = app
    yield c
    app.state.jobs.shutdown(wait=False)


@pytest.fixture
def auth(client):
    r = client.post("/auth/token", json={"username": LIVE_USER, "password": LIVE_PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def pdf_file(config, identity):
    """A real file with two PDF versions; removed afterwards."""
    mf = client_for(identity, config)
    tenant = identity.tenant
    created = []

    def _make(fixture="inserted_object"):
        before, after = F.PAIRS[fixture][0]()
        f = mf.touch(ROOT_UID, f"m4-{int(time.time() * 1000)}.pdf", tenant=tenant)
        uid = getattr(f, "uid", f)
        created.append(uid)
        mf.put(uid, before, tenant=tenant)
        time.sleep(1.05)
        mf.put(uid, after, tenant=tenant)
        return uid

    yield _make

    for uid in created:
        try:
            mf.remove(uid, tenant=tenant)
        except Exception:
            pass


def _poll_until_ready(client, auth, uid, timeout_s=90):
    """Follow the documented FE flow: 202 while computing, then 200."""
    deadline = time.time() + timeout_s
    saw_pending = False
    while time.time() < deadline:
        r = client.get(f"/files/{uid}/diff", headers=auth)
        if r.status_code == 202:
            saw_pending = True
            time.sleep(1.0)
            continue
        return r, saw_pending
    pytest.fail(f"diff for {uid} did not become ready within {timeout_s}s")


# ---------------------------------------------------------------------- auth
def test_the_diff_endpoint_requires_authentication(client, pdf_file):
    uid = pdf_file()
    assert client.get(f"/files/{uid}/diff").status_code == 401


# ------------------------------------------------------- the request flow
def test_an_uncomputed_diff_computes_and_then_serves(client, auth, pdf_file):
    uid = pdf_file()

    first = client.get(f"/files/{uid}/diff", headers=auth)
    assert first.status_code == 202, first.text
    assert first.json()["status"] == "pending"

    ready, saw_pending = _poll_until_ready(client, auth, uid)
    assert saw_pending, "the first request should have reported pending"
    assert ready.status_code == 200, ready.text

    body = ready.json()
    assert body["status"] == "ready"
    assert body["manifest"]["plugin"] == "pdf"
    assert body["children"], "the FE needs child references to fetch"
    assert all(c["uid"] for c in body["children"])


def test_the_child_references_are_fetchable_from_the_core(client, auth, pdf_file,
                                                          identity, config):
    uid = pdf_file()
    ready, _ = _poll_until_ready(client, auth, uid)
    child = ready.json()["children"][0]

    # The bytes come from the ordinary file surface, inheriting the source ACLs.
    mf = client_for(identity, config)
    data = mf.get(child["uid"], tenant=identity.tenant).read()
    assert data.startswith(b"<svg")
    assert b'id="diff-changes"' in data


def test_a_second_request_is_served_from_the_stored_result(client, auth, pdf_file):
    uid = pdf_file()
    _poll_until_ready(client, auth, uid)
    again = client.get(f"/files/{uid}/diff", headers=auth)
    assert again.status_code == 200
    assert again.json()["status"] == "ready"


def test_a_first_version_reports_none_rather_than_polling_forever(client, auth,
                                                                  identity, config):
    mf = client_for(identity, config)
    f = mf.touch(ROOT_UID, f"m4-single-{int(time.time() * 1000)}.pdf",
                 tenant=identity.tenant)
    uid = getattr(f, "uid", f)
    try:
        mf.put(uid, F.unchanged_pair()[0], tenant=identity.tenant)
        r = client.get(f"/files/{uid}/diff", headers=auth)
        assert r.status_code == 200
        assert r.json()["reason"] == "no_predecessor"
    finally:
        try:
            mf.remove(uid, tenant=identity.tenant)
        except Exception:
            pass


def test_an_unknown_version_is_404(client, auth, pdf_file):
    uid = pdf_file()
    r = client.get(f"/files/{uid}/diff?version=not-a-version", headers=auth)
    assert r.status_code == 404


# ------------------------------------------------------------------ sweep
def test_reconcile_is_admin_gated_and_accepted(client, auth, identity):
    # The dev test user is a tenant administrator, so this exercises the accept
    # path; the 403 path is covered hermetically.
    if not identity.is_admin:
        pytest.skip("the live user is not a tenant admin")
    r = client.post("/diff/reconcile", json={"max_files": 1}, headers=auth)
    assert r.status_code == 202
    assert r.json()["tenant"] == identity.tenant

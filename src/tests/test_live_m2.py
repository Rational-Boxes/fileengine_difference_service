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

"""@live end-to-end for M2: real PDF versions → stored per-page SVG diffs.

Drives the *whole* path — the registry picking the PDF plugin, the tier ladder, the
rendition writer and the manifest — against a running core, using the fixture
corpus as input. The unit tests prove the plugin in isolation; this proves the
pieces agree once storage and the real registry are involved.
"""
import os
import socket
import time

import pytest

from difference_service.config import Config
from difference_service.core_client import CoreClient, client_for
from difference_service.ldap_auth import authenticate
from difference_service.pipeline import DiffPipeline, Outcome
from difference_service.plugins.base import DiffMode
from difference_service.plugins.registry import default_registry
from difference_service.renditions import DiffRenditionStore
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
    return cfg


@pytest.fixture(scope="module")
def identity(config):
    ident = authenticate(config, LIVE_USER, LIVE_PASSWORD)
    if not ident.authenticated:
        pytest.skip("live LDAP bind failed")
    return ident


@pytest.fixture
def core(config, identity):
    return CoreClient(config, identity.tenant, identity=identity)


@pytest.fixture
def pipeline(config, core):
    # The REAL registry — this is also the assertion that the PDF plugin is wired
    # in and claims application/pdf in a running deployment.
    return DiffPipeline(config, default_registry(config), core)


@pytest.fixture
def pdf_pair(config, identity):
    """Upload a fixture pair as two versions of one real file; clean up after."""
    mf = client_for(identity, config)
    tenant = identity.tenant
    created = []

    def _make(fixture_name):
        before, after = F.PAIRS[fixture_name][0]()
        f = mf.touch(ROOT_UID, f"m2-{fixture_name}-{int(time.time() * 1000)}.pdf",
                     tenant=tenant)
        uid = getattr(f, "uid", f)
        created.append(uid)
        mf.put(uid, before, tenant=tenant)
        time.sleep(1.05)                    # version ids are second-resolution
        mf.put(uid, after, tenant=tenant)
        return uid

    yield _make

    for uid in created:
        try:
            mf.remove(uid, tenant=tenant)
        except Exception:
            pass


def test_the_pdf_plugin_is_registered_in_a_real_deployment(config):
    names = {p.name for p in default_registry(config).plugins}
    assert "pdf" in names


def test_a_vector_pdf_diff_is_computed_and_stored(pipeline, core, pdf_pair):
    uid = pdf_pair("inserted_object")
    report = pipeline.run(uid)

    assert report.outcome == Outcome.COMPUTED, report.detail
    assert report.plugin == "pdf"
    assert report.manifest.mode == DiffMode.VECTOR

    names = DiffRenditionStore(core).child_names(uid)
    assert all(expected in names for expected in report.manifest.expected)


def test_the_stored_svg_honours_the_layer_contract(pipeline, core, pdf_pair):
    uid = pdf_pair("inserted_object")
    report = pipeline.run(uid)
    store = DiffRenditionStore(core)
    svg = core._client().get(
        store.child_uid(uid, report.manifest.expected[0]), tenant=core.tenant).read().decode()

    for layer in ("diff-old", "diff-new", "diff-changes"):
        assert f'id="{layer}"' in svg
    assert 'data-diff-mode="vector"' in svg
    assert "<text" not in svg                  # glyph outlines only (§5.1)
    assert 'data-diff-state="added"' in svg


def test_a_mixed_document_stores_a_per_page_tier_map(pipeline, pdf_pair):
    # The manifest is what tells the front end to use a different view engine per
    # page; a document-wide mode alone would be a lie here.
    uid = pdf_pair("mixed_tier")
    report = pipeline.run(uid)
    assert report.manifest.mode == DiffMode.MIXED
    assert [u["mode"] for u in report.manifest.units] == [DiffMode.VECTOR, DiffMode.RASTER]
    assert len(report.manifest.expected) == 2


def test_a_scanned_document_degrades_to_raster_and_still_commits(pipeline, pdf_pair):
    uid = pdf_pair("scanned")
    report = pipeline.run(uid)
    assert report.outcome == Outcome.COMPUTED
    assert report.manifest.mode == DiffMode.RASTER
    assert report.manifest.status == "ready"


def test_a_pdf_diff_is_a_cache_hit_on_re_run(pipeline, pdf_pair):
    uid = pdf_pair("added_object")
    assert pipeline.run(uid).outcome == Outcome.COMPUTED
    assert pipeline.run(uid).outcome == Outcome.CACHED

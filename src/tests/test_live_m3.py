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

"""@live end-to-end for M3: real 3D versions → stored XKT + MetaModel."""
import json
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
from tests.fixtures import gltf as G, ifc as I

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
    pytest.importorskip("ifcopenshell", reason="ifcopenshell not installed")
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
    return DiffPipeline(config, default_registry(config), core)


@pytest.fixture
def model_pair(config, identity):
    """Upload a fixture pair as two versions of a real file; clean up after."""
    mf = client_for(identity, config)
    tenant = identity.tenant
    created = []

    def _make(pair, ext):
        before, after = pair
        f = mf.touch(ROOT_UID, f"m3-{int(time.time() * 1000)}.{ext}", tenant=tenant)
        uid = getattr(f, "uid", f)
        created.append(uid)
        mf.put(uid, before, tenant=tenant)
        time.sleep(1.05)                     # version ids are second-resolution
        mf.put(uid, after, tenant=tenant)
        return uid

    yield _make

    for uid in created:
        try:
            mf.remove(uid, tenant=tenant)
        except Exception:
            pass


def _metamodel(core, uid, manifest):
    store = DiffRenditionStore(core)
    name = [n for n in manifest.expected if n.endswith(".json")][0]
    raw = core._client().get(store.child_uid(uid, name), tenant=core.tenant).read()
    return json.loads(raw)


def test_the_3d_plugin_is_registered(config):
    assert "3d" in {p.name for p in default_registry(config).plugins}


def test_an_ifc_diff_is_computed_and_stored(pipeline, core, model_pair):
    uid = model_pair(I.combined_pair(), "ifc")
    report = pipeline.run(uid)

    assert report.outcome == Outcome.COMPUTED, report.detail
    assert report.plugin == "3d"
    assert report.manifest.mode == DiffMode.XKT

    names = DiffRenditionStore(core).child_names(uid)
    assert all(expected in names for expected in report.manifest.expected)
    kinds = {u["kind"] for u in report.manifest.units}
    assert kinds == {"model", "metamodel"}


def test_the_stored_metamodel_carries_the_three_groups(pipeline, core, model_pair):
    uid = model_pair(I.combined_pair(), "ifc")
    report = pipeline.run(uid)
    meta = _metamodel(core, uid, report.manifest)

    layers = [o["id"] for o in meta["metaObjects"] if o["type"] == "Layer"]
    assert layers == ["old", "new", "difference"]
    assert meta["diffSummary"]["tier"] == "stable-id"


def test_a_property_only_change_stores_no_visual_delta(pipeline, core, model_pair):
    # The §5.2 rule surviving all the way into storage: recorded, not painted.
    uid = model_pair(I.property_only_pair(), "ifc")
    report = pipeline.run(uid)
    meta = _metamodel(core, uid, report.manifest)

    assert meta["diffSummary"]["propertyChanges"] == 1
    assert meta["diffSummary"]["geometryChanges"] == 0
    assert [o for o in meta["metaObjects"] if o.get("parent") == "difference"] == []


def test_a_gltf_diff_uses_geometry_matching(pipeline, core, model_pair):
    uid = model_pair(G.added_mesh_pair(), "glb")
    report = pipeline.run(uid)
    assert report.outcome == Outcome.COMPUTED
    meta = _metamodel(core, uid, report.manifest)
    assert meta["diffSummary"]["tier"] == "geometry"
    assert meta["diffSummary"]["added"] == 1


def test_a_3d_diff_is_a_cache_hit_on_re_run(pipeline, model_pair):
    uid = model_pair(I.added_element_pair(), "ifc")
    assert pipeline.run(uid).outcome == Outcome.COMPUTED
    assert pipeline.run(uid).outcome == Outcome.CACHED

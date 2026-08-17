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

"""@live end-to-end for M1: real versions → real renditions → real manifest.

Exercises the whole M1 path against a running core: write two versions of a file,
run the pipeline as the worker would, and verify the stored result — children
present, manifest last, cache hit on re-run, prune on regeneration, cascade delete.

Uses the passthrough-text plugin so a real plugin produces real children without
depending on any M2/M3 toolchain.

Run with ``DIFF_LIVE_PASSWORD=… pytest -m live``. Every file it creates is removed
in a fixture teardown, including on failure.
"""
import os
import socket

import pytest

from difference_service.config import Config
from difference_service.core_client import CoreClient, client_for
from difference_service.ldap_auth import authenticate
from difference_service.manifest import manifest_name
from difference_service.pipeline import DiffPipeline, Outcome
from difference_service.plugins.passthrough import PassthroughTextPlugin
from difference_service.plugins.registry import PluginRegistry
from difference_service.renditions import DiffRenditionStore, is_diff_child

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
def core(config, identity):
    return CoreClient(config, identity.tenant, identity=identity)


@pytest.fixture
def pipeline(config, core):
    return DiffPipeline(config, PluginRegistry([PassthroughTextPlugin()]), core)


@pytest.fixture
def text_file(config, identity, request):
    """A real file with two text versions; removed afterwards."""
    import time
    mf = client_for(identity, config)
    tenant = identity.tenant
    created = []

    def _make(versions, name=None):
        name = name or f"difftest-{int(time.time() * 1000)}.txt"
        f = mf.touch(ROOT_UID, name, tenant=tenant)
        uid = getattr(f, "uid", f)
        created.append(uid)
        for payload in versions:
            mf.put(uid, payload, tenant=tenant)
            time.sleep(1.05)     # version ids are second-resolution timestamps
        return uid

    yield _make

    for uid in created:
        try:
            mf.remove(uid, tenant=tenant)
        except Exception:
            pass


# ------------------------------------------------------------------- e2e
def test_diff_is_computed_and_stored(pipeline, core, text_file):
    uid = text_file([b"alpha\nbeta\ngamma\n", b"alpha\nBETA\ngamma\ndelta\n"])
    report = pipeline.run(uid)
    assert report.outcome == Outcome.COMPUTED, report.detail
    assert report.plugin == "passthrough-text"

    store = DiffRenditionStore(core)
    names = store.child_names(uid)
    assert manifest_name(report.manifest.key) in names
    for expected in report.manifest.expected:
        assert expected in names


def test_the_stored_manifest_round_trips(pipeline, core, text_file):
    uid = text_file([b"one\n", b"two\n"])
    report = pipeline.run(uid)
    stored = DiffRenditionStore(core).read_manifest(uid, report.manifest.key)
    assert stored is not None
    assert stored.as_dict() == report.manifest.as_dict()
    assert stored.status == "ready"
    assert stored.mode == "vector"


def test_the_svg_child_carries_the_layer_contract(pipeline, core, text_file):
    # §7.2: three stable layer ids + per-element state, no baked-in colours.
    uid = text_file([b"keep\ndrop\n", b"keep\nadd\n"])
    report = pipeline.run(uid)
    store = DiffRenditionStore(core)
    child_uid = store.child_uid(uid, report.manifest.expected[0])
    svg = core._client().get(child_uid, tenant=core.tenant).read().decode()

    for layer in ("diff-old", "diff-new", "diff-changes"):
        assert f'id="{layer}"' in svg
    assert 'data-diff-state="deleted"' in svg
    assert 'data-diff-state="added"' in svg
    assert 'data-diff-mode="vector"' in svg
    assert "fill=" not in svg and "stroke=" not in svg      # FE styles state


def test_rerun_is_a_cache_hit(pipeline, text_file):
    uid = text_file([b"a\n", b"b\n"])
    assert pipeline.run(uid).outcome == Outcome.COMPUTED
    assert pipeline.run(uid).outcome == Outcome.CACHED


def test_a_new_version_supersedes_and_prunes_the_previous_diff(pipeline, core,
                                                               identity, config, text_file):
    import time
    uid = text_file([b"v1\n", b"v2\n"])
    first = pipeline.run(uid)
    assert first.outcome == Outcome.COMPUTED

    client_for(identity, config).put(uid, b"v3\n", tenant=identity.tenant)
    time.sleep(1.05)
    second = pipeline.run(uid)
    assert second.outcome == Outcome.COMPUTED
    assert second.manifest.key != first.manifest.key

    names = DiffRenditionStore(core).child_names(uid)
    ours = {n for n in names if is_diff_child(n)}
    # Only the current pair's children survive — no unbounded history.
    assert ours == set(second.manifest.expected) | {manifest_name(second.manifest.key)}


def test_first_version_produces_nothing(pipeline, text_file):
    uid = text_file([b"only one version\n"])
    report = pipeline.run(uid)
    assert report.outcome == Outcome.NO_PREDECESSOR
    assert report.manifest is None


def test_plugin_version_bump_regenerates_against_the_real_store(config, core, text_file):
    uid = text_file([b"x\n", b"y\n"])
    p1 = PassthroughTextPlugin()
    first = DiffPipeline(config, PluginRegistry([p1]), core).run(uid)
    assert first.outcome == Outcome.COMPUTED

    class Bumped(PassthroughTextPlugin):
        version = 2

    second = DiffPipeline(config, PluginRegistry([Bumped()]), core).run(uid)
    assert second.outcome == Outcome.COMPUTED
    assert second.manifest.plugin_version == 2


def test_cascade_delete_removes_the_diff_children(pipeline, core, text_file):
    uid = text_file([b"p\n", b"q\n"])
    pipeline.run(uid)
    store = DiffRenditionStore(core)
    assert any(is_diff_child(n) for n in store.child_names(uid))

    pipeline.cascade_delete(uid)
    assert not any(is_diff_child(n) for n in store.child_names(uid))


def test_an_unsupported_type_writes_nothing(config, core, identity, text_file):
    """A type no plugin claims must leave no trace — not even a failed manifest."""
    uid = text_file([b"\x89PNG\r\n\x1a\n one", b"\x89PNG\r\n\x1a\n two"],
                    name=f"difftest-image-{os.getpid()}.png")
    report = DiffPipeline(config, PluginRegistry([PassthroughTextPlugin()]), core).run(uid)
    assert report.outcome == Outcome.UNSUPPORTED
    assert not any(is_diff_child(n) for n in DiffRenditionStore(core).child_names(uid))

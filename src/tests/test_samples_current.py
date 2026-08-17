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

"""The committed samples still match the fixtures they came from.

``samples/`` is checked in so anyone can repeat the manual validation without
regenerating anything (MANUAL_TESTING.md). That convenience carries one risk: a
committed file is a *copy*, and a copy can outlive the fixture it was made from.
Someone tightens a fixture, CI stays green because the tests use the fixtures
directly, and the next person validates by hand against a stale sample — reaching
a confident conclusion about code that no longer exists.

This closes that gap: it regenerates each sample in memory and compares bytes. If
they diverge, the fix is one command:

    python3 tools/export_samples.py

The CAD samples are excluded from the strict check on purpose — they come from
OpenCASCADE, whose output legitimately varies by version, so requiring byte
equality would fail for a reason that has nothing to do with drift. They are
checked for presence instead.
"""
import os

import pytest

from tests.fixtures import gltf, ifc, pdf

SAMPLES = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "samples"))

#: corpus -> (module, extension). CAD is handled separately (see the docstring).
DETERMINISTIC = {"pdf": (pdf, "pdf"), "ifc": (ifc, "ifc"), "gltf": (gltf, "glb")}

pytestmark = pytest.mark.skipif(
    not os.path.isdir(SAMPLES),
    reason="samples/ not present — run tools/export_samples.py")


def _cases():
    for corpus, (module, ext) in sorted(DETERMINISTIC.items()):
        for name in module.PAIRS:
            yield corpus, module, ext, name


@pytest.mark.parametrize("corpus,module,ext,name", list(_cases()),
                         ids=[f"{c}/{n}" for c, _m, _e, n in _cases()])
def test_committed_sample_matches_the_fixture(corpus, module, ext, name):
    before, after = module.PAIRS[name][0]()
    filename = f"{corpus}-{name}.{ext}"

    for side, expected in (("v1", before), ("v2", after)):
        path = os.path.join(SAMPLES, side, filename)
        assert os.path.isfile(path), (
            f"{side}/{filename} is missing — run tools/export_samples.py")
        with open(path, "rb") as fh:
            actual = fh.read()
        assert actual == expected, (
            f"{side}/{filename} no longer matches the fixture it was generated "
            f"from. Regenerate with: python3 tools/export_samples.py")


def test_every_committed_sample_has_both_sides():
    # The upload flow depends on v1/ and v2/ holding the SAME names — that is what
    # makes the second upload a new version rather than a new file. A one-sided
    # sample would quietly become a file with no comparison to make.
    v1 = set(os.listdir(os.path.join(SAMPLES, "v1")))
    v2 = set(os.listdir(os.path.join(SAMPLES, "v2")))
    assert v1 == v2, f"only in v1: {sorted(v1 - v2)}; only in v2: {sorted(v2 - v1)}"


def test_the_two_sides_actually_differ_where_they_should():
    # A sample pair whose sides are identical would demonstrate nothing — except
    # for the `unchanged` controls, where identical IS the point.
    for side_name in sorted(os.listdir(os.path.join(SAMPLES, "v1"))):
        with open(os.path.join(SAMPLES, "v1", side_name), "rb") as fh:
            a = fh.read()
        with open(os.path.join(SAMPLES, "v2", side_name), "rb") as fh:
            b = fh.read()
        if "unchanged" in side_name:
            assert a == b, f"{side_name}: the 'unchanged' control must be identical"
        else:
            assert a != b, f"{side_name}: both versions are identical — nothing to compare"


def test_the_runbook_is_shipped_with_the_samples():
    # Samples without the document that explains what each should report are just
    # files; the pass conditions are the actual deliverable.
    assert os.path.isfile(os.path.join(SAMPLES, "README.txt"))
    assert os.path.isfile(os.path.join(os.path.dirname(SAMPLES), "MANUAL_TESTING.md"))

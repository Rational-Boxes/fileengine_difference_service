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

"""Tests for the FIXTURES themselves.

A fixture that does not encode the difference it claims silently invalidates every
M2/M3 result measured against it — a broken ruler is worse than no ruler. These
assert three things for each corpus: the files are structurally valid, each pair
actually differs (or actually does not), and the specific change is the documented
one.
"""
import json
import struct

import pytest

from tests.fixtures import CORPORA, gltf, ifc, pdf


# ------------------------------------------------------------------ generic
@pytest.mark.parametrize("corpus_name,corpus", sorted(CORPORA.items()))
def test_every_pair_builds_and_is_documented(corpus_name, corpus):
    for name, (build, truth) in corpus.PAIRS.items():
        before, after = build()
        assert isinstance(before, bytes) and isinstance(after, bytes), name
        assert before and after, name
        assert truth and len(truth) > 10, f"{corpus_name}.{name} lacks ground truth"


@pytest.mark.parametrize("corpus_name,corpus", sorted(CORPORA.items()))
def test_pairs_differ_except_the_declared_unchanged_ones(corpus_name, corpus):
    for name, (build, _truth) in corpus.PAIRS.items():
        before, after = build()
        if name == "unchanged":
            assert before == after, f"{corpus_name}.{name} must be byte-identical"
        else:
            assert before != after, f"{corpus_name}.{name} does not actually differ"


@pytest.mark.parametrize("corpus_name,corpus", sorted(CORPORA.items()))
def test_builders_are_deterministic(corpus_name, corpus):
    # Non-deterministic fixtures make a failing matcher test unreproducible.
    for name, (build, _t) in corpus.PAIRS.items():
        assert build() == build(), f"{corpus_name}.{name} is not deterministic"


# ---------------------------------------------------------------------- PDF
def _pdf_is_wellformed(data: bytes) -> bool:
    return (data.startswith(b"%PDF-") and b"/Type /Catalog" in data
            and b"xref" in data and data.rstrip().endswith(b"%%EOF"))


def test_pdf_fixtures_are_structurally_valid():
    for name, (build, _t) in pdf.PAIRS.items():
        for side in build():
            assert _pdf_is_wellformed(side), name


def test_pdf_xref_offsets_point_at_their_objects():
    # A parser will use these; a wrong offset would fail M2 for a reason that has
    # nothing to do with diffing.
    data = pdf.added_object_pair()[0]
    start = data.rindex(b"startxref")
    xref_at = int(data[start + len(b"startxref"):].split()[0])
    assert data[xref_at:xref_at + 4] == b"xref"
    # lines: 0="xref", 1="0 N", 2=the free entry for object 0, 3+ = objects 1..N
    lines = data[xref_at:].split(b"\n")
    for i, line in enumerate(lines[3:], start=1):
        if not line.strip() or line.startswith(b"trailer"):
            break
        offset = int(line.split()[0])
        assert data[offset:offset + len(b"%d 0 obj" % i)] == b"%d 0 obj" % i


def test_added_object_only_appends():
    before, after = pdf.added_object_pair()
    # The "easy add": nothing before the addition may shift.
    assert b"re S" in after
    assert after.count(b" re ") == before.count(b" re ") + 1


def test_inserted_object_keeps_the_trailing_objects_identical():
    # The §5.1 trap: the trailing draw ops must be textually unchanged, so a
    # matcher reporting them modified is wrong about the FIXTURE, not just the diff.
    before, after = pdf.inserted_object_pair()
    tail = b"200 100 re"
    assert tail in before and tail in after
    assert b"(Revision B) Tj" in after and b"(Revision B) Tj" not in before


def test_shifted_page_moves_every_object_by_the_same_delta():
    before, after = pdf.shifted_page_pair(dy=24)
    assert b"72 760 Td" in before and b"72 736 Td" in after
    assert b"72 600 200 100 re" in before and b"72 576 200 100 re" in after
    # Nothing retains its original position — a partial shift would let a naive
    # matcher pass by luck.
    assert b"72 760 Td" not in after


def test_moved_and_relocated_are_different_magnitudes():
    # They bracket the displacement threshold, so they must not be similar.
    _b1, a1 = pdf.moved_object_pair(dx=12)
    _b2, a2 = pdf.relocated_object_pair()
    assert b"84 600" in a1                      # small delta
    assert b"430 120" in a2                     # far away


def test_page_count_changes_are_real():
    before, after = pdf.inserted_page_pair()
    assert before.count(b"/Type /Page ") + 1 == after.count(b"/Type /Page ")
    before, after = pdf.deleted_page_pair()
    assert before.count(b"/Type /Page ") - 1 == after.count(b"/Type /Page ")


def test_reordered_pages_have_identical_content_streams():
    before, after = pdf.reordered_page_pair()
    assert b"(Alpha) Tj" in before and b"(Alpha) Tj" in after
    assert before.index(b"(Alpha) Tj") < before.index(b"(Beta) Tj")
    assert after.index(b"(Beta) Tj") < after.index(b"(Alpha) Tj")


def test_scanned_pages_carry_no_text_operators():
    # Tier 1 and 2 must be impossible here, or the raster fallback is never taken.
    for side in pdf.scanned_pair():
        assert b" Tj" not in side
        assert b"BI " in side and b" EI" in side


def test_mixed_tier_pair_has_one_vector_and_one_raster_page():
    before, _after = pdf.mixed_tier_pair()
    assert before.count(b"/Type /Page ") == 2
    assert b"(Vector Page) Tj" in before      # vector page
    assert b"BI " in before                   # image-only page


def test_restyled_object_changes_only_style():
    before, after = pdf.restyled_object_pair()
    assert b"72 600 200 100 re" in before and b"72 600 200 100 re" in after
    assert b"1 w" in before and b"6 w" in after


def test_edited_text_changes_only_the_string():
    before, after = pdf.edited_text_pair()
    assert b"72 720 Td" in before and b"72 720 Td" in after     # same position
    assert b"(Issued for construction)" in before
    assert b"(Issued for tender)" in after


# ---------------------------------------------------------------------- IFC
def _ifc_is_wellformed(data: bytes) -> bool:
    t = data.decode("utf-8")
    return (t.startswith("ISO-10303-21;") and "FILE_SCHEMA(('IFC4'))" in t
            and "DATA;" in t and t.rstrip().endswith("END-ISO-10303-21;"))


def test_ifc_fixtures_are_structurally_valid():
    for name, (build, _t) in ifc.PAIRS.items():
        for side in build():
            assert _ifc_is_wellformed(side), name


def test_ifc_entity_numbering_is_contiguous():
    text = ifc.added_element_pair()[1].decode()
    ids = [int(line.split("=")[0][1:]) for line in text.splitlines()
           if line.startswith("#")]
    assert ids == list(range(1, len(ids) + 1))


def test_added_element_introduces_exactly_one_new_globalid():
    before, after = ifc.added_element_pair()
    assert ifc.WALL_C.encode() not in before
    assert ifc.WALL_C.encode() in after


def test_moved_element_keeps_its_globalid():
    # The point of tier 1: identity survives a geometry change.
    before, after = ifc.moved_element_pair()
    assert ifc.WALL_B.encode() in before and ifc.WALL_B.encode() in after
    assert b"(0.,5.000,0.)" in before.replace(b"(0.000,5.000,0.)", b"(0.,5.000,0.)") \
        or b"5.000" in before
    assert b"7.500" in after


def test_resized_element_changes_the_profile_not_the_placement():
    before, after = ifc.resized_element_pair()
    assert b"IFCRECTANGLEPROFILEDEF(.AREA.,'wall',#" in before
    assert b",5.000,0.200)" in before
    assert b",5.000,0.400)" in after


def test_property_only_pair_has_identical_geometry():
    # THE fixture: if geometry bytes differ at all, a correct implementation would
    # be right to paint it orange and the test would be measuring the wrong thing.
    before, after = ifc.property_only_pair()
    def geometry_only(data):
        return [l for l in data.decode().splitlines()
                if "IFCPROPERTY" not in l and "IFCRELDEFINES" not in l]
    assert geometry_only(before) == geometry_only(after)
    assert b"REI 60" in before and b"REI 120" in after


def test_renamed_element_changes_only_the_name():
    before, after = ifc.renamed_element_pair()
    assert b"'Wall A'" in before and b"'Wall A (revised)'" in after
    assert ifc.WALL_A.encode() in before and ifc.WALL_A.encode() in after


def test_reordered_entities_preserves_the_globalid_set():
    before, after = ifc.reordered_entities_pair()
    def guids(d):
        return {g for g in (ifc.WALL_A, ifc.WALL_B, ifc.WALL_C) if g.encode() in d}
    assert guids(before) == guids(after) == {ifc.WALL_A, ifc.WALL_B, ifc.WALL_C}
    assert before != after     # ...but the files differ, so order really did change


def test_combined_pair_encodes_all_three_changes():
    before, after = ifc.combined_pair()
    assert ifc.WALL_A.encode() in before and ifc.WALL_A.encode() not in after   # deleted
    assert ifc.WALL_C.encode() not in before and ifc.WALL_C.encode() in after   # added
    assert ifc.WALL_B.encode() in before and ifc.WALL_B.encode() in after       # moved


# --------------------------------------------------------------------- glTF
def _parse_glb(data: bytes):
    assert data[:4] == b"glTF"
    version, total = struct.unpack("<II", data[4:12])
    assert version == 2 and total == len(data)
    length, kind = struct.unpack("<I", data[12:16])[0], data[16:20]
    assert kind == b"JSON"
    return json.loads(data[20:20 + length])


def test_gltf_fixtures_are_valid_glb():
    for name, (build, _t) in gltf.PAIRS.items():
        for side in build():
            doc = _parse_glb(side)
            assert doc["asset"]["version"] == "2.0", name
            assert doc["buffers"][0]["byteLength"] > 0, name


def test_gltf_chunks_are_four_byte_aligned():
    # A misaligned chunk is invalid GLB and real loaders reject it.
    data = gltf.added_mesh_pair()[1]
    json_len = struct.unpack("<I", data[12:16])[0]
    assert json_len % 4 == 0
    bin_off = 20 + json_len
    assert struct.unpack("<I", data[bin_off:bin_off + 4])[0] % 4 == 0
    assert data[bin_off + 4:bin_off + 8] == b"BIN\x00"


def test_added_mesh_adds_a_node_and_a_mesh():
    before, after = gltf.added_mesh_pair()
    b, a = _parse_glb(before), _parse_glb(after)
    assert len(a["nodes"]) == len(b["nodes"]) + 1
    assert len(a["meshes"]) == len(b["meshes"]) + 1


def test_renamed_node_changes_only_the_name():
    # The trap: names are not identity. Geometry must be byte-identical here.
    before, after = gltf.renamed_node_pair()
    b, a = _parse_glb(before), _parse_glb(after)
    assert b["nodes"][0]["name"] != a["nodes"][0]["name"]
    assert b["accessors"] == a["accessors"]
    assert before[-32:] == after[-32:]      # identical BIN payload tail


def test_moved_mesh_changes_accessor_bounds():
    before, after = gltf.moved_mesh_pair()
    b, a = _parse_glb(before), _parse_glb(after)
    assert b["accessors"][0]["min"] != a["accessors"][0]["min"]


def test_scaled_mesh_grows_the_bounding_box():
    before, after = gltf.scaled_mesh_pair()
    b, a = _parse_glb(before), _parse_glb(after)
    assert a["accessors"][0]["max"][0] == pytest.approx(2 * b["accessors"][0]["max"][0])


def test_reordered_nodes_keeps_the_same_geometry_set():
    before, after = gltf.reordered_nodes_pair()
    b, a = _parse_glb(before), _parse_glb(after)
    assert sorted(x["name"] for x in b["nodes"]) == sorted(x["name"] for x in a["nodes"])
    assert before != after

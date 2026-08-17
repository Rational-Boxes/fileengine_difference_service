#!/usr/bin/env python3
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

"""Write the fixture corpus out as uploadable sample files, for manual review.

Same fixtures the automated tests use — so what you verify by hand is exactly what
CI asserts, with no second sample set to drift out of step.

The layout is dictated by how you make a *version*: re-uploading a file under the
same name adds a new version to the existing file. So the two sides are written
into sibling folders under identical names:

    samples/v1/pdf-inserted_object.pdf      <- upload these first
    samples/v2/pdf-inserted_object.pdf      <- then these, same names

Upload everything in ``v1/``, then everything in ``v2/``, and every file ends up
with two versions ready to compare. Two bulk uploads, no per-file bookkeeping.

``README.txt`` lists what each comparison is supposed to report, so a result can
be judged rather than just looked at.

    python3 tools/export_samples.py [--out DIR] [--only pdf|ifc|gltf|cad]
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "src")))

from tests.fixtures import cad, gltf, ifc, pdf  # noqa: E402


# --------------------------------------------------------------------------
# Composite review scenarios.
#
# The fixture pairs each isolate ONE change, which is what makes them good test
# assertions and poor demonstrations: a real review is several changes at once,
# across several pages, where the interesting question is whether the unchanged
# parts stay quiet. These compose the same primitives into documents that look
# like something a reviewer would actually receive.
#
# They live here rather than in tests/fixtures because they carry no single
# ground truth to assert — they are for looking at.
# --------------------------------------------------------------------------

def _revision_drawing() -> tuple:
    """A 3-page drawing set: a revised title block, an added detail, an untouched
    page, and a page whose whole content shifted down."""
    P = pdf
    title_a = (P.text(72, 780, "GENERAL ARRANGEMENT") + P.text(72, 756, "Rev A")
               + P.rect(60, 60, 475, 700) + P.line(60, 120, 535, 120)
               + P.text(72, 92, "Issued for tender"))
    title_b = (P.text(72, 780, "GENERAL ARRANGEMENT") + P.text(72, 756, "Rev B")
               + P.rect(60, 60, 475, 700) + P.line(60, 120, 535, 120)
               + P.text(72, 92, "Issued for construction"))

    plan_a = (P.text(72, 780, "FLOOR PLAN") + P.rect(100, 400, 200, 240)
              + P.rect(320, 400, 160, 240) + P.line(100, 380, 480, 380))
    # ...same plan, plus a new room, and nothing else disturbed.
    plan_b = plan_a + P.rect(100, 200, 380, 160) + P.text(110, 250, "PLANT ROOM")

    notes = (P.text(72, 780, "NOTES") + P.text(72, 740, "1. All dimensions in mm.")
             + P.text(72, 716, "2. Verify on site.")
             + P.text(72, 692, "3. Refer to structural drawings."))
    notes_shifted = (P.text(72, 756, "NOTES") + P.text(72, 716, "1. All dimensions in mm.")
                     + P.text(72, 692, "2. Verify on site.")
                     + P.text(72, 668, "3. Refer to structural drawings."))

    return (P._pdf([title_a, plan_a, notes]), P._pdf([title_b, plan_b, notes_shifted]))


def _mixed_report() -> tuple:
    """A report whose first page is vector and second is a scan — the case that
    makes a document report mode 'mixed'."""
    P = pdf
    cover_a = (P.text(72, 760, "SITE SURVEY") + P.text(72, 730, "Draft")
               + P.rect(72, 500, 300, 180))
    cover_b = (P.text(72, 760, "SITE SURVEY") + P.text(72, 730, "Final")
               + P.rect(72, 500, 300, 180) + P.line(72, 470, 460, 470))
    return (P._pdf([cover_a, P._image_page(b"\x20")]),
            P._pdf([cover_b, P._image_page(b"\xd0")]))


def _building_model() -> tuple:
    """An IFC with one wall added, one moved, one deleted and one whose property
    changed — so the property-only element can be checked NOT to light up while
    the others do."""
    I = ifc
    A = (I.WALL_A, "External wall", 0.0, 0.0, 6.0, 0.3, 3.0)
    B = (I.WALL_B, "Party wall", 0.0, 6.0, 6.0, 0.2, 3.0)
    C = (I.WALL_C, "Internal partition", 6.0, 0.0, 4.0, 0.1, 3.0)
    B_moved = (I.WALL_B, "Party wall", 0.0, 7.2, 6.0, 0.2, 3.0)
    before = I._model([A, B, C], {I.WALL_A: "REI 60"})
    after = I._model([A, B_moved], {I.WALL_A: "REI 120"})
    return (before, after)


SCENARIOS = {
    "revision-drawing.pdf": (
        _revision_drawing,
        "3 pages: Rev A->B title block + issue status changed; a plant room added to "
        "the plan; the notes page shifted down but is otherwise identical (it must "
        "NOT read as rewritten)."),
    "mixed-report.pdf": (
        _mixed_report,
        "2 pages: a vector cover (Draft->Final, a rule added) and a scanned page "
        "whose image changed. The document should report mode 'mixed'."),
    "building-model.ifc": (
        _building_model,
        "IFC: internal partition deleted, party wall moved, external wall's fire "
        "rating changed. The fire-rating change must NOT colour the wall."),
}

#: corpus -> (module, file extension)
CORPORA = {
    "pdf": (pdf, "pdf"),
    "ifc": (ifc, "ifc"),
    "gltf": (gltf, "glb"),
    "cad": (cad, "step"),
}

#: Cases worth calling out — each exists to catch a specific wrong answer, so
#: they are the ones where "looks plausible" is not good enough.
NOTABLE = {
    "pdf/shifted_page":
        "every object moved by the same amount; a naive tool reports the WHOLE page "
        "modified. Expect: nothing changed.",
    "pdf/inserted_object":
        "one object inserted mid-page; a naive tool re-flows everything after it. "
        "Expect: exactly one addition, nothing else touched.",
    "pdf/mixed_tier":
        "one vector page + one scanned page in the same document; each page should "
        "be handled on its own terms.",
    "ifc/property_only":
        "a property changed, geometry did not. Expect the model to look UNCHANGED — "
        "the change is recorded, not painted. Colouring it would be the bug.",
    "ifc/reordered_entities":
        "the same elements written in a different order. Expect: nothing changed.",
    "gltf/renamed_node":
        "only a node name changed. Expect: nothing changed — glTF names are not "
        "identity, and exporters rewrite them freely.",
}


def write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(HERE, "..", "samples"),
                    help="output directory (default: difference_service/samples)")
    ap.add_argument("--only", default="", help="limit to one corpus (pdf/ifc/gltf/cad)")
    args = ap.parse_args()

    root = os.path.abspath(args.out)
    rows: list = []
    skipped: list = []

    for corpus, (module, ext) in CORPORA.items():
        if args.only and args.only != corpus:
            continue
        for name, (build, truth) in module.PAIRS.items():
            pair = build()
            if pair is None:                       # toolchain-dependent (cad)
                skipped.append(f"{corpus}/{name}")
                continue
            before, after = pair
            filename = f"{corpus}-{name}.{ext}"
            write(os.path.join(root, "v1", filename), before)
            write(os.path.join(root, "v2", filename), after)
            rows.append((f"{corpus}/{name}", filename, truth, len(before), len(after)))

    # Composite review scenarios — richer documents for a by-eye pass.
    if not args.only:
        for filename, (build, truth) in SCENARIOS.items():
            before, after = build()
            write(os.path.join(root, "v1", filename), before)
            write(os.path.join(root, "v2", filename), after)
            rows.append((f"scenario/{filename}", filename, truth, len(before), len(after)))

    _write_readme(root, rows, skipped)

    print(f"wrote {len(rows)} sample pairs to {root}")
    print(f"  {os.path.join(root, 'v1')}  <- upload these first")
    print(f"  {os.path.join(root, 'v2')}  <- then these (same names -> version 2)")
    if skipped:
        print(f"  skipped (generator unavailable): {', '.join(skipped)}")
    print(f"  {os.path.join(root, 'README.txt')}  <- what each comparison should report")
    return 0


def _write_readme(root: str, rows: list, skipped: list) -> None:
    lines = [
        "difference_service — manual verification samples",
        "=" * 48,
        "",
        "HOW TO USE",
        "  1. Upload everything in v1/ into a folder in the web UI.",
        "  2. Upload everything in v2/ into the SAME folder.",
        "     The names match, so each upload becomes version 2 of the existing",
        "     file rather than a new file.",
        "  3. Open a file's details -> Versions -> 'compare' on the newest version.",
        "",
        "Colour convention: red = deleted, green = added, orange = modified.",
        "",
        "These are the same fixtures the automated tests run against, so what you",
        "see here is what CI asserts.",
        "",
        "CASES THAT ARE EASY TO GET WRONG",
        "-" * 48,
    ]
    for key, why in NOTABLE.items():
        lines.append(f"  {key}")
        lines.append(f"      {why}")
        lines.append("")

    lines += ["ALL SAMPLES", "-" * 48,
              f"  {'file':<34} {'expected result'}"]
    for key, filename, truth, n_before, n_after in rows:
        lines.append(f"  {filename:<34} {truth}")
    lines.append("")

    if skipped:
        lines += ["NOT GENERATED (needs OpenCASCADE DRAWEXE on this machine)",
                  "-" * 48]
        lines += [f"  {s}" for s in skipped] + [""]

    lines += [
        "NOTES",
        "-" * 48,
        "  * A first version has no predecessor, so 'compare' is only offered from",
        "    the second version onward.",
        "  * A comparison runs on the server and can take a few seconds; the UI",
        "    shows it computing and then loads the result.",
        "  * gltf/*.glb and cad/*.step have no stable element ids, so a moved or",
        "    resized object is reported as removed + added volume rather than",
        "    'the same thing, changed'. That is the honest answer for those",
        "    formats — only IFC carries durable identity (GlobalId).",
        "",
    ]
    with open(os.path.join(root, "README.txt"), "w") as fh:
        fh.write("\n".join(lines))


if __name__ == "__main__":
    raise SystemExit(main())

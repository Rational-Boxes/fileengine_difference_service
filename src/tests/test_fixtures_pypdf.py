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

"""PDF fixture validation against a REAL parser (the M2 toolchain).

Split into its own module so the dependency-free fixture tests in
``test_fixtures.py`` still run when pypdf is absent — a module-level
``importorskip`` would silently skip those too.

Structural self-checks can agree with a bug in the generator. Parsing with the
library M2 will actually use is the independent confirmation that these are real
PDFs and not merely PDF-shaped.
"""
import io

import pytest

from tests.fixtures import pdf

pypdf = pytest.importorskip("pypdf", reason="pypdf not installed (pip install '.[pdf]')")


def _read(data: bytes):
    return pypdf.PdfReader(io.BytesIO(data))


def test_every_pdf_side_parses():
    for name, (build, _t) in pdf.PAIRS.items():
        for i, side in enumerate(build()):
            assert len(_read(side).pages) >= 1, f"{name}[{i}]"


def test_declared_page_counts_are_what_a_parser_sees():
    before, after = pdf.inserted_page_pair()
    assert (len(_read(before).pages), len(_read(after).pages)) == (2, 3)
    before, after = pdf.deleted_page_pair()
    assert (len(_read(before).pages), len(_read(after).pages)) == (3, 2)


def test_edited_text_is_extractable():
    before, after = pdf.edited_text_pair()
    assert "Issued for construction" in _read(before).pages[0].extract_text()
    assert "Issued for tender" in _read(after).pages[0].extract_text()


def test_a_scanned_page_yields_no_text():
    # Confirms the raster fixtures genuinely defeat text extraction, which is what
    # FORCES the tier-3 fallback rather than merely suggesting it.
    for side in pdf.scanned_pair():
        assert not _read(side).pages[0].extract_text().strip()


def test_the_vector_page_of_the_mixed_pair_still_yields_text():
    before, _after = pdf.mixed_tier_pair()
    reader = _read(before)
    assert "Vector Page" in reader.pages[0].extract_text()
    assert not reader.pages[1].extract_text().strip()

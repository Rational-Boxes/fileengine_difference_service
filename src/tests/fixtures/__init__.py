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

"""Diff fixtures — version pairs with documented ground truth.

Targets for the format plugins: PDF vector/raster (M2, §5.1), IFC and glTF
(M3, §5.2). Every pair is *generated*, never a committed binary, so the difference
it encodes is one readable edit rather than an opaque blob, and so the corpus needs
no toolchain to produce.

Usage::

    from tests.fixtures import pdf
    before, after = pdf.inserted_object_pair()

Each module exposes ``PAIRS``: ``{name: (builder, ground_truth_text)}``. The
ground-truth strings are the contract a matcher is scored against — several pairs
exist specifically to *fail* a naive implementation (a whole-page shift, a
mid-stream insertion, a renamed glTF node), so treat those as the interesting
cases rather than the edge cases.
"""
from . import gltf, ifc, pdf

#: All corpora, for tests that sweep every fixture.
CORPORA = {"pdf": pdf, "ifc": ifc, "gltf": gltf}

__all__ = ["pdf", "ifc", "gltf", "CORPORA"]

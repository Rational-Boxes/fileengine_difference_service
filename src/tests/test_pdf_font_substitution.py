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

"""Proprietary font name -> open-source substitute (SPECIFICATION.md §5.1).

These are pure name-mapping tests: they assert which *face and style* a BaseFont
resolves to, never that a particular file is installed, so they hold on a build
box with no fonts at all.
"""
import pytest

from difference_service.plugins.pdf_glyphs import (
    _candidates, _face_for, _family_for, _normalise, _style_of,
)


# --------------------------------------------------------------- name parsing

@pytest.mark.parametrize("raw, expected", [
    ("ABCDEF+Arial-BoldMT", "arialboldmt"),      # subset tag is per-document noise
    ("/F1", "f1"),
    ("Arial,Bold", "arialbold"),                 # the comma form MS Word emits
    ("  Times-Roman  ", "timesroman"),
])
def test_normalise_strips_subset_tags_and_punctuation(raw, expected):
    assert _normalise(raw) == expected


@pytest.mark.parametrize("raw, style", [
    ("Arial", "regular"),
    ("Arial-BoldMT", "bold"),
    ("Helvetica-Oblique", "italic"),
    ("TimesNewRomanPS-BoldItalicMT", "bolditalic"),
    ("Arial-SemiBold", "bold"),
    # URW/ITC spell their bold weight "Demi"...
    ("AvantGarde-Demi", "bold"),
    ("URWBookman-DemiItalic", "bolditalic"),
    # ...but "Medium" and "Light" are regular weights, not bold ones.
    ("Z003-MediumItalic", "italic"),
    ("Calibri-Light", "regular"),
])
def test_style_detection(raw, style):
    assert _style_of(_normalise(raw)) == style


# ------------------------------------------------------------------ mapping

@pytest.mark.parametrize("raw, face", [
    # Metric-compatible clones — the substitution that keeps run widths honest.
    ("ArialMT", "liberation-sans"),
    ("Helvetica", "liberation-sans"),
    ("TimesNewRomanPSMT", "liberation-serif"),
    ("CourierNew", "liberation-mono"),
    ("Calibri", "carlito"),
    ("Cambria", "caladea"),
    # The base-35 PostScript names get their URW clones.
    ("Courier", "nimbus-mono"),
    ("Palatino-Roman", "p052"),
    ("NewCenturySchlbk-Roman", "c059"),
    ("AvantGarde-Book", "urw-gothic"),
    ("Bookman-Light", "urw-bookman"),
    ("ZapfChancery-MediumItalic", "z003"),
    ("Symbol", "standard-symbols"),
    ("ZapfDingbats", "dingbats"),
    ("Wingdings", "dingbats"),
    # Nearest open face where no metric clone exists.
    ("Verdana", "dejavu-sans"),
    ("SegoeUI", "open-sans"),
    ("Consolas", "dejavu-mono"),
])
def test_known_faces_map_to_their_substitute(raw, face):
    assert _face_for(_normalise(raw)) == face


def test_more_specific_names_win():
    """Ordering in _SUBSTITUTIONS is load-bearing: "arial" must not shadow
    "arialnarrow", or a condensed run silently gets full-width glyphs."""
    assert _face_for(_normalise("ArialNarrow-Bold")) == "nimbus-sans-narrow"
    assert _face_for(_normalise("Arial-Bold")) == "liberation-sans"
    assert _face_for(_normalise("CourierNewPS-BoldMT")) == "liberation-mono"


@pytest.mark.parametrize("raw, face", [
    ("SomeHouseFaceMono", "liberation-mono"),
    ("UnknownSerifThing", "liberation-serif"),
    ("WhoKnows", "liberation-sans"),
    ("/F7", "liberation-sans"),
])
def test_unknown_names_fall_back_on_shape_words_then_sans(raw, face):
    assert _face_for(_normalise(raw)) == face


def test_family_key_separates_styles():
    """The key is what the provider caches on — Arial and Arial-Bold must not
    share one loaded font."""
    assert _family_for("ArialMT") == "liberation-sans|regular"
    assert _family_for("Arial-BoldMT") == "liberation-sans|bold"
    assert _family_for("ABCDEF+Calibri-BoldItalic") == "carlito|bolditalic"


# ---------------------------------------------------------------- fallbacks

def test_candidates_prefer_exact_face_and_style_first():
    chain = _candidates("carlito|bold")
    assert chain[0].endswith("Carlito-Bold.ttf")


def test_candidates_exhaust_the_face_before_leaving_it():
    """A roman Carlito is a better Calibri-Bold than a bold Liberation Sans:
    same metrics, wrong weight, versus wrong metrics."""
    chain = _candidates("carlito|bolditalic")
    first_foreign = next(i for i, p in enumerate(chain) if "carlito" not in p.lower())
    assert all("carlito" in p.lower() for p in chain[:first_foreign])
    assert first_foreign >= 4


def test_candidates_always_end_in_a_generic_chain():
    """A font package the image happens not to ship must cost typeface fidelity
    and never the whole vector tier."""
    for family in ("carlito|regular", "p052|italic", "dejavu-mono|bold",
                   "z003|bold", "nimbus-sans-narrow|bolditalic"):
        chain = _candidates(family)
        assert any("liberation-" in p for p in chain), family


def test_mono_and_serif_fall_back_within_their_own_classification():
    """A mono face must never degrade to a proportional one — a monospaced run
    laid out with proportional advances is misaligned everywhere."""
    assert all("Mono" in p or "mono" in p.lower()
               for p in _candidates("nimbus-mono|regular"))
    serif = _candidates("p052|regular")
    assert any("LiberationSerif" in p for p in serif)
    assert not any("LiberationSans" in p for p in serif)

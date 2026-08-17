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

"""Version-pair resolution tests (SPECIFICATION.md §3).

The core returns revisions NEWEST FIRST and ``get(back=N)`` reads ``revisions[N]``
(verified against a live core). A larger index is therefore an OLDER version — the
inversion that would silently reverse a diff, so it is pinned here.
"""
import pytest

from difference_service.versions import (
    NoPredecessor, VersionError, resolve_pair, version_list,
)


class Rev:
    def __init__(self, version):
        self.version = version


class FakeCore:
    """Newest-first revisions, like the real core."""
    def __init__(self, versions):
        self._versions = list(versions)

    def revisions(self, uid):
        return [Rev(v) for v in self._versions]


# v3 newest -> v1 oldest
CORE = FakeCore(["v3", "v2", "v1"])


def test_version_list_preserves_core_order():
    assert version_list(CORE, "F") == ["v3", "v2", "v1"]


def test_default_is_newest_against_its_predecessor():
    p = resolve_pair(CORE, "F")
    assert (p.target, p.base) == ("v3", "v2")
    assert (p.target_back, p.base_back) == (0, 1)


def test_named_target_uses_its_own_predecessor():
    p = resolve_pair(CORE, "F", target="v2")
    assert (p.target, p.base) == ("v2", "v1")
    assert (p.target_back, p.base_back) == (1, 2)


def test_back_indices_match_the_newest_first_ordering():
    # The whole point: back=N must address revisions[N], or the pipeline reads the
    # wrong bytes and produces a plausible-looking but wrong diff.
    p = resolve_pair(CORE, "F", target="v3", base="v1")
    assert p.target_back == 0 and p.base_back == 2


def test_explicit_base_may_skip_versions():
    p = resolve_pair(CORE, "F", target="v3", base="v1")
    assert (p.base, p.target) == ("v1", "v3")


def test_first_version_has_no_predecessor():
    # Not an error: a file's first version legitimately has nothing to diff.
    with pytest.raises(NoPredecessor):
        resolve_pair(CORE, "F", target="v1")


def test_single_version_file_has_no_predecessor():
    with pytest.raises(NoPredecessor):
        resolve_pair(FakeCore(["only"]), "F")


def test_a_base_newer_than_the_target_is_rejected():
    # Would invert old/new so additions render as deletions.
    with pytest.raises(VersionError):
        resolve_pair(CORE, "F", target="v1", base="v3")


def test_base_equal_to_target_is_rejected():
    with pytest.raises(VersionError):
        resolve_pair(CORE, "F", target="v2", base="v2")


def test_unknown_target_is_an_error():
    with pytest.raises(VersionError):
        resolve_pair(CORE, "F", target="nope")


def test_unknown_base_is_an_error():
    with pytest.raises(VersionError):
        resolve_pair(CORE, "F", target="v3", base="nope")


def test_a_file_with_no_versions_is_an_error():
    with pytest.raises(VersionError):
        resolve_pair(FakeCore([]), "F")

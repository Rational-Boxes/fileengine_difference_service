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

"""difference_service — version-comparison back end for FileEngine.

Generates colour-coded visual diffs between two versions of a file: per-page SVG
for 2D (PDF), a Xeokit XKT with old/new/difference layers for 3D. Diffs are
computed by format-specific plugins, precomputed on version events, and stored as
hidden-child renditions of the source file, every request gated by the caller's
FileEngine READ permission.

See SPECIFICATION.md and DEVELOPMENT_PLAN.md.
"""

__version__ = "0.1.0"

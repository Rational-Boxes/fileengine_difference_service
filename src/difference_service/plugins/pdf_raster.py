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

"""Page rasterization for the fallback tier (SPECIFICATION.md §5.1 tier 3).

Uses poppler's ``pdftoppm`` through a subprocess rather than a Python binding:
the binding (pdf2image) only shells out to the same tool, and PyMuPDF — the usual
alternative — would be a heavyweight extra. Shelling out keeps the dependency a
single, widely-packaged binary that the config can point at
(``DIFF_POPPLER_PATH``), consistent with how the other services treat tool paths.

Everything here degrades to ``None``: a missing binary, a timeout, or a page that
will not render is a *tier* outcome, never an exception. The plugin decides what to
do about it.

The overlay is computed with Pillow when available — a per-pixel difference mask is
what makes "changed region" (the raster tier's definition of *modified*, §5.1)
visible rather than making the reviewer flip between two near-identical images.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from typing import Optional, Tuple

log = logging.getLogger("difference_service.plugins.pdf_raster")

#: Render resolution. 150 DPI is legible for review without producing renditions
#: several megabytes per page.
DEFAULT_DPI = 150


class PopplerRasterizer:
    """Renders individual PDF pages to PNG via ``pdftoppm``."""

    def __init__(self, poppler_path: str = "", dpi: int = DEFAULT_DPI,
                 timeout_s: int = 60):
        self.poppler_path = poppler_path or ""
        self.dpi = dpi
        self.timeout_s = timeout_s

    # ------------------------------------------------------------ discovery
    def _binary(self) -> Optional[str]:
        if self.poppler_path:
            candidate = os.path.join(self.poppler_path, "pdftoppm")
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
            if os.path.isfile(self.poppler_path):
                return self.poppler_path
        return shutil.which("pdftoppm")

    @property
    def available(self) -> bool:
        return self._binary() is not None

    # ---------------------------------------------------------------- render
    def render(self, data: bytes, page_index: int) -> Optional[bytes]:
        """PNG bytes for ``page_index`` (0-based), or ``None``."""
        binary = self._binary()
        if binary is None or not data:
            return None

        with tempfile.TemporaryDirectory(prefix="diffsvc-") as tmp:
            src = os.path.join(tmp, "in.pdf")
            with open(src, "wb") as fh:
                fh.write(data)
            out_prefix = os.path.join(tmp, "page")
            page_no = page_index + 1                  # pdftoppm is 1-based
            cmd = [binary, "-png", "-r", str(self.dpi),
                   "-f", str(page_no), "-l", str(page_no), src, out_prefix]
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=self.timeout_s)
            except (subprocess.TimeoutExpired, OSError):
                log.warning("pdftoppm failed for page %d", page_index, exc_info=True)
                return None
            if proc.returncode != 0:
                log.info("pdftoppm exited %d for page %d: %s", proc.returncode,
                         page_index, (proc.stderr or b"")[:200])
                return None
            for name in sorted(os.listdir(tmp)):
                if name.startswith("page") and name.endswith(".png"):
                    with open(os.path.join(tmp, name), "rb") as fh:
                        return fh.read()
        return None

    # --------------------------------------------------------------- overlay
    def difference_mask(self, old_png: Optional[bytes],
                        new_png: Optional[bytes]) -> Tuple[Optional[bytes], bool]:
        """``(overlay_png, changed)`` highlighting the regions that differ.

        The overlay is transparent except where the two renders disagree, so it
        composites over either page. Without Pillow it returns ``(None, True)`` —
        the two page images are still shown, just without a computed highlight,
        which is a fidelity loss rather than a correctness one."""
        if not old_png or not new_png:
            return None, True
        try:
            import io

            from PIL import Image, ImageChops
        except Exception:
            return None, True

        try:
            a = Image.open(io.BytesIO(old_png)).convert("RGB")
            b = Image.open(io.BytesIO(new_png)).convert("RGB")
            if a.size != b.size:
                b = b.resize(a.size)
            diff = ImageChops.difference(a, b).convert("L")
            bbox = diff.getbbox()
            if bbox is None:
                return None, False                    # genuinely identical renders
            # Alpha = magnitude of difference, so unchanged pixels are transparent
            # and the front end's CSS tint shows only where content moved.
            mask = diff.point(lambda v: 255 if v > 16 else 0)
            overlay = Image.new("RGBA", a.size, (0, 0, 0, 0))
            overlay.putalpha(mask)
            buf = io.BytesIO()
            overlay.save(buf, format="PNG", optimize=True)
            return buf.getvalue(), True
        except Exception:
            log.debug("difference mask failed", exc_info=True)
            return None, True


def default_rasterizer(config=None) -> Optional[PopplerRasterizer]:
    """A rasterizer from config, or ``None`` when the backend is unavailable."""
    r = PopplerRasterizer(poppler_path=getattr(config, "poppler_path", "") or "")
    return r if r.available else None

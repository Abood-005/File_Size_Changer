"""Everything else - executables, text, CSV, databases, unknown binaries.

There is no way to make an arbitrary file smaller and still have the same file,
so this engine only ever does lossless container compression and tells you
plainly when the target is out of reach.
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

from ..core import Fidelity, Result, human
from .base import Engine


class GenericEngine(Engine):
    name = "lossless"
    kind = "file"

    def handles(self, path: Path) -> bool:
        return True  # last in the chain

    def compress(self, src, dst, target, settings, progress) -> Result:
        original_size = src.stat().st_size
        archive = dst.with_suffix(dst.suffix + ".zip") if dst.suffix != ".zip" else dst

        progress(0.25, "compressing losslessly (deflate)")
        _write_zip(src, archive, zipfile.ZIP_DEFLATED, 9)
        size = archive.stat().st_size
        method = "deflate (opens anywhere)"

        if size > target:
            progress(0.65, "retrying with LZMA")
            candidate = archive.with_suffix(".lzma.zip")
            try:
                _write_zip(src, candidate, zipfile.ZIP_LZMA, None)
                if candidate.stat().st_size < size:
                    archive.unlink(missing_ok=True)
                    candidate.replace(archive)
                    size = archive.stat().st_size
                    method = "LZMA (needs 7-Zip or WinRAR to open)"
                else:
                    candidate.unlink(missing_ok=True)
            except Exception:
                candidate.unlink(missing_ok=True)

        if size >= original_size:
            # Incompressible (already-compressed or high-entropy) data. Handing
            # back a .zip that is bigger than the input helps nobody.
            archive.unlink(missing_ok=True)
            shutil.copy2(src, dst)
            return Result(
                src, dst, original_size, original_size, target, Fidelity.UNCHANGED,
                engine=self.name,
                detail="this data is already incompressible, copied as-is",
                metric=f"{human(original_size)} is its lossless floor - "
                       "any smaller and it is no longer the same file",
            )

        fits = size <= target
        return Result(
            src, archive, original_size, size, target, Fidelity.LOSSLESS,
            engine=self.name,
            detail=f"wrapped in a zip using {method}",
            metric=(
                "unzips to the exact original file"
                if fits else
                f"{human(size)} is this file's lossless floor - "
                "shrinking it further would corrupt it"
            ),
        )


def _write_zip(src: Path, dst: Path, method: int, level: int | None) -> None:
    kwargs = {"compresslevel": level} if level is not None else {}
    with zipfile.ZipFile(dst, "w", method, **kwargs) as archive:
        archive.write(src, arcname=src.name)

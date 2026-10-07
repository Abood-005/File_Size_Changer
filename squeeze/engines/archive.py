"""Zip-family containers: .zip, .docx, .xlsx, .pptx, .odt, .epub, .jar ...

Tier 1 repacks every entry at maximum deflate, which is bit-for-bit lossless.
Tier 2 recompresses the images inside the package, keeping each one in its
original format so relationships and file extensions stay valid.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from PIL import Image

from ..core import Fidelity, Result, search_quality
from .base import Engine

IMAGE_ENTRY_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp"}
MIN_ENTRY_BYTES = 8 * 1024


class ArchiveEngine(Engine):
    name = "archive"
    kind = "package"
    exts = frozenset({".zip", ".docx", ".xlsx", ".pptx", ".docm", ".xlsm", ".pptm",
                      ".odt", ".ods", ".odp", ".epub", ".jar", ".apk", ".cbz"})

    def handles(self, path: Path) -> bool:
        return path.suffix.lower() in self.exts and zipfile.is_zipfile(path)

    def compress(self, src, dst, target, settings, progress) -> Result:
        original_size = src.stat().st_size
        data = src.read_bytes()

        progress(0.2, "repacking at maximum compression")
        repacked = _repack(data, quality=None)
        if len(repacked) > original_size:
            repacked = data

        if len(repacked) <= target:
            dst.write_bytes(repacked)
            return Result(
                src, dst, original_size, len(repacked), target,
                Fidelity.LOSSLESS if len(repacked) < original_size else Fidelity.UNCHANGED,
                engine=self.name, detail="every entry repacked at deflate level 9",
                metric="contents byte-for-byte identical",
            )

        count = _image_entry_count(repacked)
        if count:
            progress(0.5, f"recompressing {count} embedded images")

            def measure(param: int) -> int:
                return len(_repack(repacked, quality=param))

            param, _ = search_quality(measure, 0, 100, target, iterations=7)
            if param is not None:
                out = _repack(repacked, quality=param)
                dst.write_bytes(out)
                return Result(
                    src, dst, original_size, len(out), target, Fidelity.LOSSY,
                    engine=self.name,
                    detail=f"{count} embedded images re-encoded (knob {param}/100)",
                    metric="text, formatting and structure untouched",
                )

        out = _repack(repacked, quality=0) if count else repacked
        dst.write_bytes(out)
        return Result(
            src, dst, original_size, len(out), target,
            Fidelity.LOSSY if count else Fidelity.LOSSLESS,
            engine=self.name, detail="already at the floor for this package",
            metric="no further safe reduction available",
        )


# --------------------------------------------------------------------------

def _knobs(param: int) -> tuple[int, int]:
    return int(min(90, max(30, 30 + 0.6 * param))), int(300 + param * 20)


def _image_entry_count(data: bytes) -> int:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return sum(
            1 for item in archive.infolist()
            if Path(item.filename).suffix.lower() in IMAGE_ENTRY_EXTS
            and item.file_size >= MIN_ENTRY_BYTES
        )


def _shrink_entry(blob: bytes, suffix: str, param: int) -> bytes:
    quality, cap = _knobs(param)
    try:
        with Image.open(io.BytesIO(blob)) as image:
            image.load()
            fmt = (image.format or "").upper()
            if max(image.size) > cap:
                scale = cap / max(image.size)
                image = image.resize(
                    (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                    Image.Resampling.LANCZOS,
                )
            buffer = io.BytesIO()
            # Same format in, same format out: never break a package reference.
            if fmt in ("JPEG", "MPO"):
                image.convert("RGB").save(buffer, "JPEG", quality=quality,
                                          optimize=True, progressive=True)
            elif fmt == "PNG":
                colors = max(8, int(16 + param * 2.4))
                mode = Image.Quantize.FASTOCTREE if image.mode in ("RGBA", "LA") else Image.Quantize.MEDIANCUT
                image.quantize(colors=min(256, colors), method=mode).save(
                    buffer, "PNG", optimize=True, compress_level=9)
            elif fmt == "WEBP":
                image.save(buffer, "WEBP", quality=quality, method=5)
            else:
                return blob
            new_blob = buffer.getvalue()
        return new_blob if len(new_blob) < len(blob) * 0.92 else blob
    except Exception:
        return blob


def _repack(data: bytes, quality: int | None) -> bytes:
    """Rewrite the archive. quality=None means lossless repack only."""
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source:
        items = source.infolist()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as target:
            for index, item in enumerate(items):
                blob = source.read(item)
                suffix = Path(item.filename).suffix.lower()
                if (quality is not None and suffix in IMAGE_ENTRY_EXTS
                        and len(blob) >= MIN_ENTRY_BYTES):
                    blob = _shrink_entry(blob, suffix, quality)

                info = zipfile.ZipInfo(item.filename, date_time=item.date_time)
                info.external_attr = item.external_attr
                info.internal_attr = item.internal_attr
                info.create_system = item.create_system
                # EPUB requires the mimetype entry first and stored uncompressed.
                if index == 0 and item.filename == "mimetype":
                    info.compress_type = zipfile.ZIP_STORED
                elif item.is_dir():
                    info.compress_type = zipfile.ZIP_STORED
                else:
                    info.compress_type = zipfile.ZIP_DEFLATED
                target.writestr(info, blob)
    return output.getvalue()

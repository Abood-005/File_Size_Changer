"""PDF.

Three escalating tiers, and we stop at the first one that fits:

1. Lossless: garbage-collect dead objects, subset fonts, recompress streams.
   Text, vectors and images are untouched.
2. Image recompression: only the embedded raster images are downsampled and
   re-encoded. Text stays selectable and vectors stay sharp - this is what
   "without losing context" actually means for a PDF.
3. Rasterise whole pages (opt-in only): text becomes pixels.
"""

from __future__ import annotations

import io
from pathlib import Path

import pikepdf
import pymupdf
from PIL import Image

from ..core import Fidelity, Result, human, search_quality
from .base import Engine

MIN_IMAGE_BYTES = 6 * 1024  # below this, recompression is noise


class PdfEngine(Engine):
    name = "pdf"
    kind = "document"
    exts = frozenset({".pdf"})

    def compress(self, src, dst, target, settings, progress) -> Result:
        original_size = src.stat().st_size
        data = src.read_bytes()

        try:
            probe = pymupdf.open(stream=data, filetype="pdf")
        except Exception as exc:
            raise RuntimeError(f"cannot read PDF: {exc}") from exc
        if probe.needs_pass:
            probe.close()
            raise RuntimeError("PDF is password protected")
        page_count = probe.page_count
        probe.close()

        # ---- tier 1: lossless -------------------------------------------
        progress(0.15, "lossless clean-up (fonts, dead objects, streams)")
        lean = _lossless(data, settings.strip_metadata)
        if len(lean) > original_size:
            lean = data

        if len(lean) <= target:
            dst.write_bytes(lean)
            return Result(
                src, dst, original_size, len(lean), target,
                Fidelity.LOSSLESS if len(lean) < original_size else Fidelity.UNCHANGED,
                engine=self.name,
                detail=f"{page_count} pages, streams recompressed and fonts subset",
                metric="text, vectors and images untouched",
            )

        # ---- tier 2: downsample embedded images only ---------------------
        progress(0.3, "recompressing embedded images")
        image_count = _count_images(lean)
        if image_count:
            def measure(param: int) -> int:
                return len(_shrink_images(lean, param, settings.strip_metadata))

            param, _ = search_quality(measure, 0, 100, target, iterations=7)
            if param is not None:
                out = _shrink_images(lean, param, settings.strip_metadata)
                dst.write_bytes(out)
                quality, cap = _knobs(param)
                return Result(
                    src, dst, original_size, len(out), target, Fidelity.LOSSY,
                    engine=self.name,
                    detail=f"{image_count} images re-encoded at JPEG q{quality}, capped at {cap}px",
                    metric="text stays selectable, vectors stay sharp",
                )

        # ---- tier 3: rasterise (opt-in) ----------------------------------
        if settings.allow_rasterize_pdf:
            progress(0.7, "rasterising pages")

            def measure_raster(param: int) -> int:
                return len(_rasterize(lean, param))

            param, _ = search_quality(measure_raster, 0, 100, target, iterations=7)
            param = param if param is not None else 0
            out = _rasterize(lean, param)
            dst.write_bytes(out)
            dpi, quality = _raster_knobs(param)
            return Result(
                src, dst, original_size, len(out), target, Fidelity.LOSSY,
                engine=self.name,
                detail=f"pages rasterised at {dpi} DPI, JPEG q{quality}",
                metric="text is no longer selectable",
            )

        # Best honest effort: hardest image pass we allow, target not met.
        best = _shrink_images(lean, 0, settings.strip_metadata) if image_count else lean
        if len(best) > len(lean):
            best = lean
        dst.write_bytes(best)
        hint = (
            "target needs page rasterisation - enable it in Advanced"
            if not settings.allow_rasterize_pdf
            else "this PDF is mostly text/vectors, already at its floor"
        )
        return Result(
            src, dst, original_size, len(best), target,
            Fidelity.LOSSY if len(best) < original_size else Fidelity.UNCHANGED,
            engine=self.name,
            detail=f"smallest safe result is {human(len(best))}",
            metric=hint,
        )


# --------------------------------------------------------------------------

def _knobs(param: int) -> tuple[int, int]:
    """param 0..100 -> (jpeg quality, max image dimension in px)."""
    quality = int(min(92, max(30, 30 + 0.62 * param)))
    cap = int(320 + param * 22)
    return quality, cap


def _raster_knobs(param: int) -> tuple[int, int]:
    return int(45 + param * 2.55), int(min(90, 35 + 0.55 * param))


def _lossless(data: bytes, strip_metadata: bool) -> bytes:
    """MuPDF garbage collection + font subsetting, then pikepdf stream packing."""
    best = data
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
        try:
            doc.subset_fonts(verbose=False)
        except Exception:
            pass
        if strip_metadata:
            try:
                doc.set_metadata({})
                doc.del_xml_metadata()
            except Exception:
                pass
        out = doc.tobytes(garbage=4, deflate=True, deflate_images=True,
                          deflate_fonts=True, clean=True, use_objstms=1)
        doc.close()
        if 0 < len(out) < len(best):
            best = out
    except Exception:
        pass

    try:
        with pikepdf.open(io.BytesIO(best)) as pdf:
            if strip_metadata:
                try:
                    del pdf.Root.Metadata
                except Exception:
                    pass
            buffer = io.BytesIO()
            pdf.save(
                buffer,
                compress_streams=True,
                recompress_flate=True,
                object_stream_mode=pikepdf.ObjectStreamMode.generate,
                linearize=False,
            )
            packed = buffer.getvalue()
        if 0 < len(packed) < len(best):
            best = packed
    except Exception:
        pass
    return best


def _count_images(data: bytes) -> int:
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
        seen = set()
        for page in doc:
            for info in page.get_images(full=True):
                if info[0] not in seen and info[1] == 0:
                    seen.add(info[0])
        doc.close()
        return len(seen)
    except Exception:
        return 0


def _shrink_images(data: bytes, param: int, strip_metadata: bool) -> bytes:
    quality, cap = _knobs(param)
    doc = pymupdf.open(stream=data, filetype="pdf")
    seen: set[int] = set()

    for page in doc:
        for info in page.get_images(full=True):
            xref, smask = info[0], info[1]
            if xref in seen:
                continue
            seen.add(xref)
            if smask:
                # Transparency lives in a separate object; swapping the base
                # image risks breaking the pairing, so leave these alone.
                continue
            try:
                raw = doc.extract_image(xref)
            except Exception:
                continue
            blob = raw.get("image") or b""
            if len(blob) < MIN_IMAGE_BYTES:
                continue
            try:
                with Image.open(io.BytesIO(blob)) as image:
                    image.load()
                    if image.mode in ("RGBA", "LA", "P"):
                        continue  # alpha or palette: not safe as baseline JPEG
                    if image.mode == "CMYK":
                        image = image.convert("RGB")
                    elif image.mode not in ("RGB", "L"):
                        image = image.convert("RGB")
                    if max(image.size) > cap:
                        scale = cap / max(image.size)
                        image = image.resize(
                            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                            Image.Resampling.LANCZOS,
                        )
                    buffer = io.BytesIO()
                    image.save(buffer, "JPEG", quality=quality, optimize=True, progressive=True)
                    new_blob = buffer.getvalue()
            except Exception:
                continue

            if len(new_blob) < len(blob) * 0.92:
                try:
                    page.replace_image(xref, stream=new_blob)
                except Exception:
                    continue

    if strip_metadata:
        try:
            doc.set_metadata({})
            doc.del_xml_metadata()
        except Exception:
            pass
    out = doc.tobytes(garbage=4, deflate=True, deflate_images=True,
                      deflate_fonts=True, clean=True, use_objstms=1)
    doc.close()
    return out


def _rasterize(data: bytes, param: int) -> bytes:
    dpi, quality = _raster_knobs(param)
    source = pymupdf.open(stream=data, filetype="pdf")
    output = pymupdf.open()
    try:
        for page in source:
            pixmap = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB)
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=quality, optimize=True, progressive=True)
            new_page = output.new_page(width=page.rect.width, height=page.rect.height)
            new_page.insert_image(new_page.rect, stream=buffer.getvalue())
        return output.tobytes(garbage=4, deflate=True, use_objstms=1)
    finally:
        source.close()
        output.close()

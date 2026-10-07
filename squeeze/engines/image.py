"""Still and animated images.

Strategy: try a genuinely lossless re-encode first. Only if that overshoots the
target do we walk a ladder that prefers dropping resolution over grinding the
quality knob into the ground, because a clean smaller image beats a full-size
one full of blocking artifacts at the same byte count.
"""

from __future__ import annotations

import io
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageOps, ImageSequence

from .. import quality as qmod
from ..core import Fidelity, Result, Settings, scale_ladder, search_quality
from .base import Engine

# Generous, but still a guard against decompression bombs.
Image.MAX_IMAGE_PIXELS = 500_000_000

# Pillow nags when a quantised palette carries byte transparency; that is
# exactly what we want for small PNGs, so the notice is just noise.
warnings.filterwarnings("ignore", message=".*Palette images with Transparency.*")

try:  # optional: iPhone HEIC/HEIF and AVIF support
    import pillow_heif

    pillow_heif.register_heif_opener()
    try:
        pillow_heif.register_avif_opener()
    except Exception:
        pass
    HEIF_OK = True
except Exception:  # pragma: no cover - plugin simply not installed
    HEIF_OK = False


@dataclass
class Codec:
    fmt: str                                        # Pillow format name
    param_low: int
    param_high: int
    param_name: str                                 # "quality" | "colors"
    comfort: int                                    # above this = no visible cost
    iterations: int = 7
    converts_to: Optional[str] = None               # set when the container changes


JPEG = Codec("JPEG", 30, 95, "quality", 82)
WEBP = Codec("WEBP", 25, 95, "quality", 80)
# For palette formats the knob is colour count, and dithered palettes hold up
# far better than a hard downscale does, so the comfort bar sits lower.
PNG = Codec("PNG", 8, 256, "colors", 96, iterations=9)
GIF = Codec("GIF", 4, 256, "colors", 48, iterations=8)
TIFF = Codec("TIFF", 30, 95, "quality", 82)


class ImageEngine(Engine):
    name = "image"
    kind = "image"
    exts = frozenset(
        {
            ".jpg", ".jpeg", ".jpe", ".jfif",
            ".png", ".webp", ".gif",
            ".bmp", ".dib", ".tif", ".tiff",
            ".tga", ".ppm", ".pgm", ".pbm", ".ico",
            ".heic", ".heif", ".avif",
        }
    )

    def handles(self, path: Path) -> bool:
        suffix = path.suffix.lower()
        if suffix in {".heic", ".heif", ".avif"} and not HEIF_OK:
            return False
        return suffix in self.exts

    # -- main entry ---------------------------------------------------------

    def compress(self, src, dst, target, settings, progress) -> Result:
        original_size = src.stat().st_size
        progress(0.05, "reading image")

        with Image.open(src) as opened:
            frame_count = getattr(opened, "n_frames", 1)
            animated = frame_count > 1
            source_format = (opened.format or "").upper()
            info = dict(opened.info)
            if animated:
                frames = [f.convert("RGBA") for f in ImageSequence.Iterator(opened)]
                base = frames[0]
            else:
                base = ImageOps.exif_transpose(opened) or opened
                base = base.copy()
                frames = [base]

        codec, out_suffix, converted = _pick_codec(src.suffix.lower(), base)
        if out_suffix != dst.suffix.lower():
            dst = dst.with_suffix(out_suffix)

        meta = _metadata(info, settings)
        encode = _make_encoder(codec, frames, meta, info)

        # ---- 1. lossless / near-lossless first ----------------------------
        progress(0.2, "trying lossless re-encode")
        lossless_bytes, best_kind = _lossless(base, frames, codec, meta, info,
                                              source_format, src)
        best_lossless = lossless_bytes
        copied = False
        if original_size <= len(best_lossless) and not converted:
            best_lossless, best_kind, copied = src.read_bytes(), Fidelity.LOSSLESS, True

        if len(best_lossless) <= target:
            dst.write_bytes(best_lossless)
            if copied:
                detail = "already smaller than the target, copied untouched"
            else:
                detail = f"{codec.fmt} re-encoded losslessly at full resolution"
            return Result(
                src, dst, original_size, len(best_lossless), target,
                best_kind if len(best_lossless) < original_size else Fidelity.UNCHANGED,
                engine=self.name, detail=detail,
                metric="identical pixels" if best_kind is Fidelity.LOSSLESS
                else "original quantisation tables preserved",
            )

        # ---- 2. quality/resolution ladder ---------------------------------
        width, height = base.size
        ladder = scale_ladder(settings.allow_downscale)
        low = codec.param_low
        if codec.param_name == "quality":
            low = max(settings.quality_floor, 5)
        comfort = codec.comfort
        if codec.param_name == "quality":
            comfort = max(settings.comfort_quality, settings.quality_floor)

        best: Optional[tuple[float, int]] = None
        for index, scale in enumerate(ladder):
            progress(0.25 + 0.6 * index / len(ladder),
                     f"searching {codec.param_name} at {int(scale * 100)}% scale")
            if scale < 1.0 and min(int(width * scale), int(height * scale)) < 16:
                break
            param, _ = search_quality(
                lambda p, s=scale: len(encode(s, p)),
                low, codec.param_high, target, codec.iterations,
            )
            if param is None:
                continue
            best = (scale, param)
            if param >= comfort:
                break

        if best is None:
            # Target is below what this format can physically do. Deliver the
            # smallest honest attempt and let the caller report the miss.
            best = (ladder[-1], low)

        scale, param = best
        progress(0.9, "final encode")
        data = encode(scale, param, final=True)
        if len(data) > target:
            # The higher-effort encoder overshot: keep whatever the search saw.
            search_data = encode(scale, param)
            if len(search_data) < len(data):
                data = search_data
        dst.write_bytes(data)

        final_size = len(data)
        detail_parts = []
        if scale < 1.0:
            detail_parts.append(f"resized to {int(width * scale)}x{int(height * scale)}")
        else:
            detail_parts.append(f"full resolution {width}x{height}")
        detail_parts.append(f"{codec.param_name} {param}")
        if converted:
            detail_parts.append(f"converted to {codec.fmt}")

        metric = ""
        try:
            with Image.open(io.BytesIO(data)) as check:
                metric = qmod.describe(base.convert("RGB"), check.convert("RGB"))
                db = qmod.psnr(base.convert("RGB"), check.convert("RGB"))
                if db != float("inf"):
                    metric += f" ({qmod.verdict(db)})"
        except Exception:
            pass

        fidelity = Fidelity.LOSSY
        if scale == 1.0 and codec.param_name == "quality" and param >= 92:
            fidelity = Fidelity.NEAR_LOSSLESS

        return Result(
            src, dst, original_size, final_size, target, fidelity,
            engine=self.name, detail=", ".join(detail_parts), metric=metric,
        )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _pick_codec(suffix: str, image: Image.Image) -> tuple[Codec, str, bool]:
    """Return (codec, output suffix, whether the container had to change)."""
    if suffix in {".jpg", ".jpeg", ".jpe", ".jfif"}:
        return JPEG, suffix, False
    if suffix == ".png":
        return PNG, ".png", False
    if suffix == ".webp":
        return WEBP, ".webp", False
    if suffix == ".gif":
        return GIF, ".gif", False
    if suffix in {".tif", ".tiff"}:
        return TIFF, suffix, False
    # Formats with no useful compression of their own, or read-only plugins:
    # move to something that can actually hold a target size.
    has_alpha = image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in (image.info or {})
    )
    if has_alpha:
        return PNG, ".png", True
    return JPEG, ".jpg", True


def _metadata(info: dict, settings: Settings) -> dict:
    """ICC profiles are colour accuracy, so they stay; EXIF is optional."""
    meta: dict = {}
    if info.get("icc_profile"):
        meta["icc_profile"] = info["icc_profile"]
    if not settings.strip_metadata and info.get("exif"):
        meta["exif"] = info["exif"]
    return meta


def _flatten(image: Image.Image) -> Image.Image:
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.split()[-1])
        return canvas
    if image.mode not in ("RGB", "L", "CMYK"):
        return image.convert("RGB")
    return image


def _resize(image: Image.Image, scale: float,
            filt: Image.Resampling = Image.Resampling.LANCZOS) -> Image.Image:
    if scale >= 0.999:
        return image
    size = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
    return image.resize(size, filt)


def _master_palette(frames: list[Image.Image], colors: int) -> Image.Image:
    """One palette for the whole animation, sampled across frames.

    Deriving it from frame 0 alone misses colours that only appear later, and a
    per-frame palette would defeat GIF's frame-delta compression entirely.
    """
    sample = frames[:: max(1, len(frames) // 8)][:8]
    width, height = sample[0].size
    strip = Image.new("RGB", (width, height * len(sample)))
    for index, frame in enumerate(sample):
        strip.paste(frame.convert("RGB"), (0, index * height))
    return strip.quantize(colors=max(2, min(256, colors)), method=Image.Quantize.MEDIANCUT)


def _to_bytes(image: Image.Image, fmt: str, **kwargs) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, fmt, **kwargs)
    return buffer.getvalue()


def _quantize(image: Image.Image, colors: int, fast: bool = False) -> Image.Image:
    """Palette reduction. FASTOCTREE is ~50x quicker than MEDIANCUT on large
    images, so the size search uses it and only the final encode pays for the
    better palette."""
    colors = max(2, min(256, colors))
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        return image.convert("RGBA").quantize(
            colors=colors, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.FLOYDSTEINBERG
        )
    method = Image.Quantize.FASTOCTREE if fast else Image.Quantize.MEDIANCUT
    return image.convert("RGB").quantize(
        colors=colors, method=method, dither=Image.Dither.FLOYDSTEINBERG
    )


def _make_encoder(
    codec: Codec,
    frames: list[Image.Image],
    meta: dict,
    info: dict,
) -> Callable[[float, int], bytes]:
    """Build encode(scale, param) -> bytes for this codec."""

    if len(frames) > 1:
        duration = info.get("duration", 80)
        loop = info.get("loop", 0)
        # A single shared palette keeps GIF frame deltas intact; per-frame local
        # colour tables would cost more bytes than the colours are worth.
        opaque = all(f.getextrema()[3][0] == 255 for f in frames)

        def encode_animated(scale: float, param: int, final: bool = False) -> bytes:
            if codec.fmt == "WEBP":
                scaled = [_resize(f, scale) for f in frames]
                return _to_bytes(
                    scaled[0], "WEBP", save_all=True, append_images=scaled[1:],
                    quality=param, method=6 if final else 3, duration=duration, loop=loop,
                )
            # Area-average, not Lanczos: sharpening ringing invents hundreds of
            # new colours, which on a palette format makes the downscaled file
            # *larger* than the original. BOX keeps size monotone in scale.
            scaled = [_resize(f, scale, Image.Resampling.BOX) for f in frames]
            if opaque:
                master = _master_palette(scaled, param)
                # No dithering here: on animation it doubles the file (every
                # frame differs everywhere, so nothing delta-compresses) and
                # shows up as shimmer between frames.
                reduced = [f.convert("RGB").quantize(
                    palette=master, dither=Image.Dither.NONE) for f in scaled]
            else:
                reduced = [_quantize(f, param, fast=not final) for f in scaled]
            return _to_bytes(
                reduced[0], "GIF", save_all=True, append_images=reduced[1:],
                optimize=True, duration=duration, loop=loop, disposal=2,
            )

        return encode_animated

    base = frames[0]

    def encode_still(scale: float, param: int, final: bool = False) -> bytes:
        image = _resize(base, scale)
        if codec.fmt == "JPEG":
            return _to_bytes(
                _flatten(image), "JPEG", quality=param, optimize=True,
                progressive=True, subsampling=0 if param >= 90 else 2, **meta,
            )
        if codec.fmt == "WEBP":
            return _to_bytes(image, "WEBP", quality=param, method=6 if final else 4, **meta)
        if codec.fmt == "TIFF":
            return _to_bytes(_flatten(image), "TIFF", compression="jpeg", quality=param)
        if codec.fmt == "GIF":
            return _to_bytes(_quantize(image, param, fast=not final), "GIF", optimize=True)
        # PNG: the "quality" knob is palette size.
        return _to_bytes(_quantize(image, param, fast=not final), "PNG",
                         optimize=True, compress_level=9, **meta)

    return encode_still


def _lossless(
    base: Image.Image,
    frames: list[Image.Image],
    codec: Codec,
    meta: dict,
    info: dict,
    source_format: str,
    src: Path,
) -> tuple[bytes, Fidelity]:
    """Best byte-preserving (or table-preserving) encode we can manage."""
    animated = len(frames) > 1
    try:
        if animated:
            if codec.fmt == "WEBP":
                return _to_bytes(
                    frames[0], "WEBP", save_all=True, append_images=frames[1:],
                    lossless=True, method=4, duration=info.get("duration", 80),
                    loop=info.get("loop", 0),
                ), Fidelity.LOSSLESS
            return src.read_bytes(), Fidelity.LOSSLESS

        if codec.fmt == "PNG":
            return _to_bytes(base, "PNG", optimize=True, compress_level=9, **meta), Fidelity.LOSSLESS
        if codec.fmt == "WEBP":
            return _to_bytes(base, "WEBP", lossless=True, method=6, **meta), Fidelity.LOSSLESS
        if codec.fmt == "TIFF":
            return _to_bytes(base, "TIFF", compression="tiff_deflate"), Fidelity.LOSSLESS
        if codec.fmt == "GIF":
            return _to_bytes(base, "GIF", optimize=True), Fidelity.LOSSLESS
        if codec.fmt == "JPEG":
            if source_format == "JPEG":
                try:
                    # quality="keep" reuses the original quantisation tables, so
                    # this only re-packs entropy coding: no new generation loss
                    # beyond one decode/encode round trip.
                    return _to_bytes(
                        base, "JPEG", quality="keep", optimize=True,
                        progressive=True, **meta,
                    ), Fidelity.NEAR_LOSSLESS
                except Exception:
                    pass
            return _to_bytes(
                _flatten(base), "JPEG", quality=95, optimize=True,
                progressive=True, subsampling=0, **meta,
            ), Fidelity.NEAR_LOSSLESS
    except Exception:
        pass
    return src.read_bytes(), Fidelity.LOSSLESS

"""Shared types, helpers and the parameter search used by every engine."""

from __future__ import annotations

import enum
import math
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

KB = 1024
MB = 1024 * KB
GB = 1024 * MB

# Progress reporter: fraction in 0..1, plus a short status line.
Progress = Callable[[float, str], None]


def noop_progress(fraction: float, message: str) -> None:  # pragma: no cover
    pass


class Fidelity(enum.IntEnum):
    """How much of the original was given up to hit the target."""

    LOSSLESS = 0       # decoded content is bit for bit identical
    NEAR_LOSSLESS = 1  # re-encoded at high quality, no resampling
    LOSSY = 2          # quality and/or resolution was reduced
    UNCHANGED = 3      # nothing beat the original, copied as is
    FAILED = 4

    @property
    def label(self) -> str:
        return {
            Fidelity.LOSSLESS: "Lossless",
            Fidelity.NEAR_LOSSLESS: "Near-lossless",
            Fidelity.LOSSY: "Lossy",
            Fidelity.UNCHANGED: "Unchanged",
            Fidelity.FAILED: "Failed",
        }[self]


@dataclass
class Settings:
    """Everything the UI can tune, in one bag passed down to the engines."""

    mode: str = "absolute"          # "absolute" | "percent"
    target_bytes: int = 2 * MB
    percent: float = 50.0

    output_dir: Optional[Path] = None
    suffix: str = "_small"

    allow_downscale: bool = True    # images/video may lose pixels
    quality_floor: int = 40         # never encode below this
    comfort_quality: int = 78       # prefer downscaling over dropping under this
    strip_metadata: bool = True
    allow_rasterize_pdf: bool = False   # last resort, kills selectable text
    video_codec: str = "h264"       # "h264" | "h265"

    def target_for(self, original_size: int) -> int:
        if self.mode == "percent":
            return max(4 * KB, int(original_size * self.percent / 100.0))
        return max(1, int(self.target_bytes))


@dataclass
class Result:
    src: Path
    dst: Optional[Path]
    original: int
    final: int
    target: int
    fidelity: Fidelity
    engine: str = ""
    detail: str = ""
    metric: str = ""
    error: Optional[str] = None

    @property
    def met_target(self) -> bool:
        return self.dst is not None and self.final <= self.target

    @property
    def saved(self) -> int:
        return max(0, self.original - self.final)

    @property
    def ratio(self) -> float:
        return (self.final / self.original) if self.original else 1.0


class CompressionError(Exception):
    pass


# size formatting / parsing

def human(size: float) -> str:
    if size is None:
        return "-"
    size = float(size)
    for unit, step in (("GB", GB), ("MB", MB), ("KB", KB)):
        if size >= step:
            value = size / step
            return f"{value:.2f} {unit}" if value < 100 else f"{value:.0f} {unit}"
    return f"{int(size)} B"


_SIZE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([kmgb]*)\s*$", re.I)
_UNITS = {"": 1, "b": 1, "k": KB, "kb": KB, "m": MB, "mb": MB, "g": GB, "gb": GB}


def parse_size(text: str) -> Optional[int]:
    """'2.5 MB' / '900kb' / '1200' -> bytes. None when unparseable."""
    match = _SIZE_RE.match(text or "")
    if not match:
        return None
    number, unit = match.groups()
    unit = unit.lower()
    if unit not in _UNITS:
        return None
    try:
        return max(1, int(float(number) * _UNITS[unit]))
    except ValueError:
        return None


# parameter search

def search_quality(
    measure: Callable[[int], int],
    low: int,
    high: int,
    target: int,
    iterations: int = 7,
) -> tuple[Optional[int], Optional[int]]:
    """Highest integer parameter in [low, high] whose output fits `target`.

    `measure(param) -> byte count` must be monotonically non-decreasing, which
    holds well enough for encoder quality knobs. Returns (param, size), or
    (None, None) when even `low` overshoots.
    """
    cache: dict[int, int] = {}

    def size_at(param: int) -> int:
        if param not in cache:
            cache[param] = measure(param)
        return cache[param]

    if size_at(high) <= target:
        return high, cache[high]
    if size_at(low) > target:
        return None, None

    best = low
    lo, hi = low, high
    for _ in range(iterations):
        if hi - lo <= 1:
            break
        mid = (lo + hi) // 2
        if size_at(mid) <= target:
            best, lo = mid, mid
        else:
            hi = mid
    return best, cache[best]


def scale_ladder(allow_downscale: bool) -> list[float]:
    if not allow_downscale:
        return [1.0]
    return [1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.42, 0.35, 0.28, 0.22]


# subprocess helper (no flashing console windows on Windows)

def popen_kwargs() -> dict:
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return kwargs


def unique_path(path: Path) -> Path:
    """Append ' (2)', ' (3)'... until the name is free."""
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    for index in range(2, 1000):
        candidate = parent / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
    return parent / f"{stem} ({math.floor(path.stat().st_mtime)}){suffix}"

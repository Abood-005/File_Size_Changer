"""Format-specific compression engines, tried in registration order."""

from __future__ import annotations

from .archive import ArchiveEngine
from .base import Engine
from .generic import GenericEngine
from .image import ImageEngine
from .media import MediaEngine
from .pdf import PdfEngine

#: Ordered most specific -> most general. GenericEngine handles anything left.
ENGINES: list[Engine] = [
    ImageEngine(),
    PdfEngine(),
    MediaEngine(),
    ArchiveEngine(),
    GenericEngine(),
]

__all__ = [
    "ENGINES",
    "Engine",
    "ImageEngine",
    "PdfEngine",
    "MediaEngine",
    "ArchiveEngine",
    "GenericEngine",
]

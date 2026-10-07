"""Squeeze - compress any file to a target size, losslessly where possible."""

from .core import Fidelity, Result, Settings, human, parse_size
from .pipeline import compress_file, destination_for, pick_engine

__version__ = "1.0.0"

__all__ = [
    "Fidelity",
    "Result",
    "Settings",
    "human",
    "parse_size",
    "compress_file",
    "destination_for",
    "pick_engine",
    "__version__",
]

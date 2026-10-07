"""Engine interface."""

from __future__ import annotations

from pathlib import Path

from ..core import Progress, Result, Settings


class Engine:
    name = "base"
    exts: frozenset[str] = frozenset()
    #: Shown in the UI when this engine is picked for a file.
    kind = "file"

    def handles(self, path: Path) -> bool:
        return path.suffix.lower() in self.exts

    def compress(
        self,
        src: Path,
        dst: Path,
        target: int,
        settings: Settings,
        progress: Progress,
    ) -> Result:
        raise NotImplementedError

"""Engine selection and the one call the UI actually makes."""

from __future__ import annotations

import shutil
import traceback
from pathlib import Path

from .core import Fidelity, Progress, Result, Settings, noop_progress, unique_path
from .engines import ENGINES, Engine


def pick_engine(path: Path) -> Engine:
    for engine in ENGINES:
        try:
            if engine.handles(path):
                return engine
        except Exception:
            continue
    return ENGINES[-1]


def destination_for(src: Path, settings: Settings) -> Path:
    folder = settings.output_dir or src.parent
    folder.mkdir(parents=True, exist_ok=True)
    return unique_path(folder / f"{src.stem}{settings.suffix}{src.suffix}")


def compress_file(src: Path, settings: Settings, progress: Progress = noop_progress) -> Result:
    src = Path(src)
    original_size = src.stat().st_size
    target = settings.target_for(original_size)
    engine = pick_engine(src)
    dst = destination_for(src, settings)

    try:
        progress(0.02, f"{engine.name} engine")
        result = engine.compress(src, dst, target, settings, progress)
    except Exception as exc:
        for stray in {dst, dst.with_suffix(dst.suffix + ".zip")}:
            try:
                if stray.exists() and stray != src:
                    stray.unlink()
            except OSError:
                pass
        return Result(
            src, None, original_size, 0, target, Fidelity.FAILED,
            engine=engine.name, error=f"{type(exc).__name__}: {exc}",
            detail=traceback.format_exc(limit=2).strip().splitlines()[-1],
        )

    # Never hand back something bigger than what we started with - but only
    # swap in the original when the output is the same kind of file, otherwise
    # we would produce e.g. a .zip that is really a raw binary.
    same_kind = bool(result.dst) and result.dst.suffix.lower() == src.suffix.lower()
    if result.dst and result.dst.exists() and result.final > original_size and same_kind:
        if result.dst != src:
            shutil.copy2(src, result.dst)
        result.final = original_size
        if result.fidelity is not Fidelity.FAILED:
            result.fidelity = Fidelity.UNCHANGED
            result.detail = "nothing beat the original, copied as-is"

    progress(1.0, "done")
    return result

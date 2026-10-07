"""Video and audio via ffmpeg.

Unlike images, media has an exact lever: bitrate x duration = size. So instead
of a blind search we solve for the bitrate, encode two-pass (which actually
respects the number), then apply at most two correction passes.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..core import Fidelity, Result, human, popen_kwargs
from .base import Engine

VIDEO_EXTS = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpg",
                        ".mpeg", ".wmv", ".flv", ".ts", ".3gp", ".ogv"})
AUDIO_EXTS = frozenset({".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus",
                        ".wma", ".aiff", ".aif"})

#: video bitrate (kbps) -> sensible max vertical resolution
RESOLUTION_LADDER = [
    (6000, 2160), (3500, 1440), (2000, 1080), (1100, 720),
    (600, 540), (320, 480), (150, 360), (0, 270),
]

AUDIO_ENCODERS = {
    ".mp3": ("libmp3lame", ".mp3"),
    ".m4a": ("aac", ".m4a"),
    ".aac": ("aac", ".m4a"),
    ".ogg": ("libvorbis", ".ogg"),
    ".opus": ("libopus", ".opus"),
    ".wma": ("aac", ".m4a"),
    ".wav": ("aac", ".m4a"),
    ".flac": ("aac", ".m4a"),
    ".aiff": ("aac", ".m4a"),
    ".aif": ("aac", ".m4a"),
}


def _tool(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    if root.is_dir():
        for candidate in root.glob(f"**/bin/{name}.exe"):
            return str(candidate)
    return None


FFMPEG = _tool("ffmpeg")
FFPROBE = _tool("ffprobe")


class MediaEngine(Engine):
    name = "media"
    kind = "media"
    exts = VIDEO_EXTS | AUDIO_EXTS

    def handles(self, path: Path) -> bool:
        return bool(FFMPEG) and path.suffix.lower() in self.exts

    def compress(self, src, dst, target, settings, progress) -> Result:
        original_size = src.stat().st_size
        info = _probe(src)
        duration = info["duration"]
        if duration <= 0:
            raise RuntimeError("could not read a duration from this file")

        if src.suffix.lower() in AUDIO_EXTS and not info["has_video"]:
            return self._audio(src, dst, target, settings, progress, info, original_size)
        return self._video(src, dst, target, settings, progress, info, original_size)

    # -- video --------------------------------------------------------------

    def _video(self, src, dst, target, settings, progress, info, original_size) -> Result:
        duration = info["duration"]
        codec = "libx265" if settings.video_codec == "h265" else "libx264"
        budget_kbps = target * 8 / 1000.0 / duration

        attempt_note = ""
        for attempt in range(3):
            usable = budget_kbps * 0.955          # muxing overhead
            audio_kbps = _audio_budget(usable) if info["has_audio"] else 0
            video_kbps = max(24, usable - audio_kbps)

            height = _cap_height(video_kbps, info["height"]) if settings.allow_downscale else info["height"]
            fps = 30 if (info["fps"] > 31 and video_kbps < 1500) else None

            base_progress = 0.05 + attempt * 0.28
            out = _run_two_pass(
                src, dst, codec, int(video_kbps), int(audio_kbps), height, fps,
                duration, settings, progress, base_progress,
            )
            size = out.stat().st_size
            ratio = size / target if target else 1.0

            if size <= target and ratio >= 0.80:
                break
            if attempt == 2:
                break
            if size > target:
                budget_kbps *= (target / size) * 0.97
                attempt_note = "corrected down after overshoot"
            else:
                budget_kbps *= min(1.6, (target / size) * 0.97)
                attempt_note = "raised bitrate to use the full budget"

        size = dst.stat().st_size
        detail = f"{codec.replace('lib', '')} {int(video_kbps)} kbps"
        if info["has_audio"]:
            detail += f" + {int(audio_kbps)} kbps audio"
        if height and height < info["height"]:
            detail += f", {info['height']}p -> {height}p"
        if fps:
            detail += ", 30 fps"
        if attempt_note:
            detail += f" ({attempt_note})"

        fidelity = Fidelity.LOSSY
        if size >= original_size:
            shutil.copy2(src, dst)
            size = original_size
            fidelity = Fidelity.UNCHANGED

        return Result(
            src, dst, original_size, size, target, fidelity, engine=self.name,
            detail=detail,
            metric=f"{_minutes(duration)} kept in full, no frames dropped",
        )

    # -- audio --------------------------------------------------------------

    def _audio(self, src, dst, target, settings, progress, info, original_size) -> Result:
        duration = info["duration"]
        suffix = src.suffix.lower()

        # Tier 1: lossless FLAC for uncompressed / already-lossless sources.
        if suffix in {".wav", ".flac", ".aiff", ".aif"}:
            progress(0.2, "trying lossless FLAC")
            flac_path = dst.with_suffix(".flac")
            _run([FFMPEG, "-y", "-i", str(src), "-c:a", "flac",
                  "-compression_level", "12", str(flac_path)], duration, progress, 0.2, 0.6)
            if flac_path.exists() and flac_path.stat().st_size <= target:
                size = flac_path.stat().st_size
                return Result(
                    src, flac_path, original_size, size, target, Fidelity.LOSSLESS,
                    engine=self.name, detail="FLAC, maximum compression level",
                    metric="sample-for-sample identical audio",
                )
            flac_path.unlink(missing_ok=True)

        encoder, out_suffix = AUDIO_ENCODERS.get(suffix, ("aac", ".m4a"))
        if dst.suffix.lower() != out_suffix:
            dst = dst.with_suffix(out_suffix)

        kbps = max(24, target * 8 / 1000.0 / duration * 0.96)
        channels = 1 if (kbps < 48 and info["channels"] > 1) else info["channels"]
        rate = 22050 if kbps < 32 else None

        for attempt in range(3):
            command = [FFMPEG, "-y", "-i", str(src), "-vn", "-c:a", encoder,
                       "-b:a", f"{int(kbps)}k"]
            if channels == 1:
                command += ["-ac", "1"]
            if rate:
                command += ["-ar", str(rate)]
            command.append(str(dst))
            _run(command, duration, progress, 0.3 + attempt * 0.2, 0.5)
            size = dst.stat().st_size
            if size <= target or attempt == 2:
                break
            kbps = max(24, kbps * (target / size) * 0.96)

        size = dst.stat().st_size
        detail = f"{encoder} {int(kbps)} kbps"
        if channels == 1 and info["channels"] > 1:
            detail += ", downmixed to mono"
        if rate:
            detail += f", {rate} Hz"
        return Result(
            src, dst, original_size, size, target, Fidelity.LOSSY, engine=self.name,
            detail=detail, metric=f"full {_minutes(duration)} of audio retained",
        )


# --------------------------------------------------------------------------

def _probe(path: Path) -> dict:
    result = {"duration": 0.0, "has_video": False, "has_audio": False,
              "width": 0, "height": 0, "fps": 30.0, "channels": 2}
    if not FFPROBE:
        return result
    try:
        raw = subprocess.run(
            [FFPROBE, "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=60, **popen_kwargs(),
        ).stdout
        data = json.loads(raw)
    except Exception:
        return result

    result["duration"] = float(data.get("format", {}).get("duration", 0) or 0)
    for stream in data.get("streams", []):
        kind = stream.get("codec_type")
        if kind == "video" and stream.get("disposition", {}).get("attached_pic") != 1:
            result["has_video"] = True
            result["width"] = int(stream.get("width") or 0)
            result["height"] = int(stream.get("height") or 0)
            rate = stream.get("avg_frame_rate") or "30/1"
            try:
                num, _, den = rate.partition("/")
                result["fps"] = float(num) / float(den or 1)
            except Exception:
                pass
            if not result["duration"]:
                result["duration"] = float(stream.get("duration", 0) or 0)
        elif kind == "audio":
            result["has_audio"] = True
            result["channels"] = int(stream.get("channels") or 2)
    return result


def _audio_budget(total_kbps: float) -> float:
    for threshold, value in ((900, 128), (450, 96), (220, 64), (120, 48)):
        if total_kbps >= threshold:
            return value
    return 32.0


def _cap_height(video_kbps: float, source_height: int) -> int:
    for threshold, height in RESOLUTION_LADDER:
        if video_kbps >= threshold:
            return min(source_height, height) if source_height else height
    return source_height


def _minutes(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _run(command, duration, progress, start, span, label="encoding"):
    """Run ffmpeg, translating its -progress output into our fraction."""
    command = command[:1] + ["-hide_banner", "-loglevel", "error",
                             "-progress", "pipe:1", "-nostats"] + command[1:]
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, **popen_kwargs(),
    )
    for line in process.stdout:
        match = re.match(r"out_time_us=(\d+)", line.strip())
        if match and duration > 0:
            done = int(match.group(1)) / 1_000_000 / duration
            progress(start + span * min(1.0, done), label)
    process.wait()
    if process.returncode != 0:
        error = (process.stderr.read() or "").strip().splitlines()
        raise RuntimeError("ffmpeg failed: " + (error[-1] if error else "unknown error"))


def _run_two_pass(src, dst, codec, video_kbps, audio_kbps, height, fps,
                  duration, settings, progress, base):
    filters = []
    if height:
        filters.append(f"scale=-2:{height}")
    if fps:
        filters.append(f"fps={fps}")
    video_filter = ["-vf", ",".join(filters)] if filters else []

    with tempfile.TemporaryDirectory() as workdir:
        log = str(Path(workdir) / "pass")
        null = "NUL" if os.name == "nt" else "/dev/null"
        common = [FFMPEG, "-y", "-i", str(src), "-c:v", codec,
                  "-b:v", f"{video_kbps}k", "-preset", "medium", *video_filter]
        pass_flag = "-x265-params" if codec == "libx265" else "-pass"

        if codec == "libx265":
            first = common + ["-x265-params", f"pass=1:stats={log}", "-an", "-f", "null", null]
            second = common + ["-x265-params", f"pass=2:stats={log}"]
        else:
            first = common + ["-pass", "1", "-passlogfile", log, "-an", "-f", "null", null]
            second = common + ["-pass", "2", "-passlogfile", log]

        _run(first, duration, progress, base, 0.12, "analysing (pass 1)")

        if audio_kbps:
            second += ["-c:a", "aac", "-b:a", f"{audio_kbps}k"]
        else:
            second += ["-an"]
        second += ["-movflags", "+faststart", str(dst)]
        _run(second, duration, progress, base + 0.12, 0.14, "encoding (pass 2)")
    return dst

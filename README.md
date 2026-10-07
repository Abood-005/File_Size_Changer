# Squeeze

A desktop app that compresses files to a size you pick — and tells you honestly
what that cost.

Drop files in, type a target (`500 KB`, `8 MB`, or "50% of original"), hit
Compress. Every file gets an engine chosen by type, a real quality measurement,
and a plain-English note about what was given up.

```bash
python app.py
```

Or double-click `Squeeze.bat`.

## The honest bit, first

"Compress to any size without losing quality" is not physically possible.
Information theory says so: an arbitrary file cannot be shrunk to an arbitrary
size and still be the same file. So Squeeze does the next best thing — it works
in tiers and always tells you which one it landed on:

| Tier | Meaning |
|---|---|
| **Lossless** | Decoded content is bit-for-bit identical. Nothing was lost. |
| **Near-lossless** | Re-encoded at full resolution and top quality. No visible difference. |
| **Lossy** | Quality and/or resolution were reduced. You get a PSNR/SSIM number and a verdict. |
| **Unchanged** | Nothing beat the original, so it was copied untouched. |

Every file tries lossless **first**. It only escalates when the target demands
it. When a target simply cannot be reached without destroying the file, Squeeze
says so instead of producing garbage.

## What it does per format

**Images** — `.jpg .png .webp .gif .bmp .tiff .heic .avif` and friends.
Lossless re-encode first. If that's too big it walks a ladder that prefers
*reducing resolution over grinding the quality knob down*, because a clean
smaller image beats a full-size one full of blocking artifacts at the same byte
count. Reports PSNR and SSIM measured against the original at full resolution,
so resolution loss shows up in the score rather than hiding behind it. Animated
GIF/WebP keep every frame; GIFs get a shared palette so frame-delta compression
survives. ICC colour profiles are preserved.

**PDF** — three escalating tiers:
1. Garbage-collect dead objects, subset fonts, recompress streams. Fully lossless.
2. Downsample and re-encode only the *embedded raster images*. Text stays
   selectable and vectors stay sharp — this is what "keeping the content" really
   means for a document.
3. Rasterise whole pages. Opt-in only, because it turns text into pixels.

**Video & audio** — via ffmpeg. Bitrate is *solved* from `target ÷ duration`
rather than guessed, encoded two-pass so the number is respected, then corrected
up to twice if it over- or undershoots. Full duration is always kept — no frames
dropped, no clipping. Resolution steps down only when the bitrate is too thin to
justify the pixels. `.wav`/`.flac` try lossless FLAC before anything lossy.

**Office & zip packages** — `.docx .xlsx .pptx .epub .zip .jar` …
Repacked at maximum deflate (lossless, contents identical). If that's not
enough, only the embedded images are recompressed, each kept in its original
format so nothing inside the package breaks. Text and formatting are never
touched. EPUB's stored-first `mimetype` entry is handled correctly.

**Everything else** — lossless zip (deflate, falling back to LZMA). If the data
is incompressible it says so and copies the original rather than handing you a
`.zip` that's bigger than what you started with.

## Setup

```bash
pip install -r requirements.txt
```

For video and audio you also need ffmpeg on PATH:

```bash
winget install Gyan.FFmpeg
```

The header shows a chip per capability — green means available. Missing ffmpeg
only disables video/audio; everything else still works.

## Options

- **Allow resizing** — off keeps every image and video at native resolution, so
  only the quality knob moves.
- **Quality floor** — the encoder will never go below this. Raise it if you'd
  rather miss a target than accept mush.
- **Strip metadata** — drops EXIF/XMP. ICC colour profiles are always kept.
- **Let PDFs be rasterised** — enables PDF tier 3. Off by default.
- **Video codec** — H.265 is roughly 30% smaller than H.264 at equal quality,
  and several times slower to encode.

Results land beside each original with a `_small` suffix, or in a folder you
choose. Double-click any finished row to reveal the output in Explorer.

## Layout

```
app.py                    the UI
squeeze/
  core.py                 shared types, size parsing, the parameter search
  quality.py              PSNR / SSIM measurement
  pipeline.py             engine selection, one call the UI makes
  engines/
    image.py  pdf.py  media.py  archive.py  generic.py
```

Engines are tried in order and the first one that claims a file wins;
`generic.py` catches everything else. Adding a format means adding one class
with a `handles()` and a `compress()`.

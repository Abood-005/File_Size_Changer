"""Objective quality measurement, so the app can tell you what a target cost you."""

from __future__ import annotations

import numpy as np
from PIL import Image


def _as_luma_array(image: Image.Image, size: tuple[int, int]) -> np.ndarray:
    if image.size != size:
        image = image.resize(size, Image.Resampling.LANCZOS)
    return np.asarray(image.convert("L"), dtype=np.float64)


def psnr(original: Image.Image, candidate: Image.Image) -> float:
    """Peak signal-to-noise ratio in dB, compared at the original resolution.

    The candidate is upscaled back when it was downsampled, so resolution loss
    shows up in the number instead of being hidden by it.
    """
    reference = _as_luma_array(original, original.size)
    test = _as_luma_array(candidate, original.size)
    mse = float(np.mean((reference - test) ** 2))
    if mse <= 1e-9:
        return float("inf")
    return 20.0 * float(np.log10(255.0 / np.sqrt(mse)))


def ssim(original: Image.Image, candidate: Image.Image) -> float:
    """Global SSIM on luma using an 8x8 block approximation (no SciPy needed)."""
    reference = _as_luma_array(original, original.size)
    test = _as_luma_array(candidate, original.size)

    block = 8
    height = reference.shape[0] // block * block
    width = reference.shape[1] // block * block
    if height < block or width < block:
        return float("nan")

    def blocks(array: np.ndarray) -> np.ndarray:
        trimmed = array[:height, :width]
        return trimmed.reshape(height // block, block, width // block, block).transpose(0, 2, 1, 3)

    a, b = blocks(reference), blocks(test)
    mu_a, mu_b = a.mean(axis=(2, 3)), b.mean(axis=(2, 3))
    var_a, var_b = a.var(axis=(2, 3)), b.var(axis=(2, 3))
    cov = (a * b).mean(axis=(2, 3)) - mu_a * mu_b

    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    numerator = (2 * mu_a * mu_b + c1) * (2 * cov + c2)
    denominator = (mu_a**2 + mu_b**2 + c1) * (var_a + var_b + c2)
    return float(np.mean(numerator / denominator))


def describe(original: Image.Image, candidate: Image.Image) -> str:
    """Short human-readable fidelity note for the results table."""
    try:
        value = psnr(original, candidate)
    except Exception:
        return ""
    if value == float("inf"):
        return "identical pixels"
    try:
        structural = ssim(original, candidate)
        return f"PSNR {value:.1f} dB · SSIM {structural:.3f}"
    except Exception:
        return f"PSNR {value:.1f} dB"


def verdict(psnr_db: float) -> str:
    if psnr_db >= 45:
        return "visually identical"
    if psnr_db >= 38:
        return "excellent"
    if psnr_db >= 32:
        return "good"
    if psnr_db >= 27:
        return "noticeable softening"
    return "visible artifacts"

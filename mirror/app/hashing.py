"""Non-biometric image fingerprinting and similarity.

Uses whole-image perceptual hashes (pHash/dHash/wHash), colour histograms and a multi-scale
crop search. No face detection or identity features are computed anywhere.
"""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

import imagehash
import numpy as np
from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = 60_000_000  # decompression-bomb guard

ALLOWED_FORMATS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif", "BMP": "bmp"}


class InvalidImage(Exception):
    pass


@dataclass
class Fingerprint:
    sha256: str
    phash: imagehash.ImageHash
    dhash: imagehash.ImageHash
    whash: imagehash.ImageHash
    hist: np.ndarray
    width: int
    height: int


def load_image(data: bytes) -> tuple[Image.Image, str]:
    """Decode and validate by content (never trust extension / client MIME)."""
    try:
        with Image.open(io.BytesIO(data)) as probe:
            fmt = probe.format or ""
            probe.verify()
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:  # noqa: BLE001
        raise InvalidImage("file is not a valid image") from e
    if fmt not in ALLOWED_FORMATS:
        raise InvalidImage(f"unsupported format {fmt or 'unknown'}")
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        bg.paste(rgba, mask=rgba.split()[-1])
        img = bg
    return img.convert("RGB"), fmt


def sanitize(img: Image.Image, max_side: int = 2048) -> bytes:
    """Re-encode as JPEG: strips EXIF/GPS metadata and any embedded payloads."""
    img = img.copy()
    img.thumbnail((max_side, max_side))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=92)
    return out.getvalue()


def _hist(img: Image.Image) -> np.ndarray:
    small = img.resize((64, 64))
    arr = np.asarray(small).reshape(-1, 3) // 32  # 8 bins per channel
    idx = arr[:, 0] * 64 + arr[:, 1] * 8 + arr[:, 2]
    h = np.bincount(idx, minlength=512).astype(float)
    return h / h.sum()


def fingerprint(img: Image.Image, raw: bytes | None = None) -> Fingerprint:
    return Fingerprint(
        sha256=hashlib.sha256(raw).hexdigest() if raw is not None else "",
        phash=imagehash.phash(img), dhash=imagehash.dhash(img), whash=imagehash.whash(img),
        hist=_hist(img), width=img.width, height=img.height,
    )


def _combined_distance(a: Image.Image, fp: Fingerprint) -> int:
    return int(round(((imagehash.phash(a) - fp.phash) + (imagehash.dhash(a) - fp.dhash)) / 2))


@dataclass
class Comparison:
    match_type: str  # exact | near_duplicate | cropped | similar | different
    distance: int
    similarity: float  # 0..1


def compare(query: Fingerprint, query_img: Image.Image, cand_img: Image.Image) -> Comparison:
    cand = fingerprint(cand_img)
    d = int(round(((cand.phash - query.phash) + (cand.dhash - query.dhash) + (cand.whash - query.whash)) / 3))
    hist_sim = float(np.minimum(cand.hist, query.hist).sum())
    q_ar, c_ar = query.width / query.height, cand.width / cand.height
    if d <= 3 and abs(q_ar - c_ar) < 0.03:
        return Comparison("exact", d, 1 - d / 64)
    if d <= 10:
        return Comparison("near_duplicate", d, 1 - d / 64)

    # Crop search: candidate may be a crop of the query, or the query a crop of the candidate.
    best = 64
    for big, small_fp, small_img in ((query_img, cand, cand_img), (cand_img, query, query_img)):
        best = min(best, _crop_search(big, small_fp, small_img.width / small_img.height))
    if best <= 10:
        return Comparison("cropped", best, 1 - best / 64)

    sim = max(0.0, 0.6 * (1 - d / 32) + 0.4 * hist_sim)
    return Comparison("similar" if sim >= 0.45 else "different", d, round(sim, 3))


def _crop_search(big: Image.Image, small_fp: Fingerprint, small_ar: float) -> int:
    """Coarse-to-fine search for the window of `big` (with the small image's aspect ratio) that best
    matches the small image. Handles crops down to ~30% of the original side length."""
    work = big.copy()
    work.thumbnail((384, 384))
    W, H = work.size
    full_h = min(H, W / small_ar)

    def dist(x: float, y: float, h: float) -> int:
        w = h * small_ar
        x, y = max(0.0, min(x, W - w)), max(0.0, min(y, H - h))
        return _combined_distance(work.crop((int(x), int(y), int(x + w), int(y + h))), small_fp)

    cands = []
    scale = 0.95
    while scale >= 0.3:
        h = full_h * scale
        w = h * small_ar
        if w >= 24 and h >= 24:
            step = max(2.0, min(w, h) / 6)
            y = 0.0
            while y <= H - h + 0.5:
                x = 0.0
                while x <= W - w + 0.5:
                    cands.append((dist(x, y, h), x, y, h))
                    x += step
                y += step
        scale *= 0.88
    if not cands:
        return 64
    cands.sort(key=lambda t: t[0])
    best = cands[0][0]
    for d0, x0, y0, h0 in cands[:3]:  # local refinement around the best coarse windows
        cur = (d0, x0, y0, h0)
        for step in (h0 / 12, h0 / 24, h0 / 48):
            improved = True
            while improved and cur[0] > 2:
                improved = False
                _, x, y, h = cur
                for dx, dy, dh in ((step, 0, 0), (-step, 0, 0), (0, step, 0), (0, -step, 0),
                                   (0, 0, step), (0, 0, -step), (-step / 2, -step / 2, step), (step / 2, step / 2, -step)):
                    nh = min(full_h, max(24.0, h + dh))
                    d = dist(x + dx, y + dy, nh)
                    if d < cur[0]:
                        cur, improved = (d, x + dx, y + dy, nh), True
        best = min(best, cur[0])
        if best <= 2:
            break
    return best


def make_thumbnail(img: Image.Image, size: int = 320) -> bytes:
    t = img.copy()
    t.thumbnail((size, size))
    out = io.BytesIO()
    t.save(out, "JPEG", quality=80)
    return out.getvalue()

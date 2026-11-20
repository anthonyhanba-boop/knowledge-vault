import io
import numpy as np
from PIL import Image

def scene(seed=1, size=(640, 480)) -> Image.Image:
    rng = np.random.default_rng(seed)
    w, h = size
    y, x = np.mgrid[0:h, 0:w]
    arr = np.zeros((h, w, 3))
    for c in range(3):
        for _ in range(6):
            fx, fy, ph = rng.uniform(0.002, 0.03, 2).tolist() + [rng.uniform(0, 6)]
            arr[..., c] += np.sin(x * fx * 2 * np.pi / 2 + y * fy * 2 * np.pi / 2 + ph)
    arr = (arr - arr.min()) / (arr.max() - arr.min()) * 255
    img = Image.fromarray(arr.astype("uint8"))
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    for _ in range(12):
        x0, y0 = int(rng.integers(0, w - 60)), int(rng.integers(0, h - 60))
        d.rectangle([x0, y0, x0 + int(rng.integers(20, 120)), y0 + int(rng.integers(20, 120))],
                    fill=tuple(int(v) for v in rng.integers(0, 255, 3)))
    return img

def to_bytes(img, fmt="JPEG", **kw) -> bytes:
    b = io.BytesIO(); img.save(b, fmt, **kw); return b.getvalue()

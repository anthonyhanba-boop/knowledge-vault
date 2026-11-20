import io
from PIL import Image
from mirror.app import hashing
from .helpers import scene, to_bytes

def fp_of(img):
    return hashing.fingerprint(img, to_bytes(img))

def test_exact_and_recompressed():
    q = scene(1)
    c = Image.open(io.BytesIO(to_bytes(q, quality=60))).convert("RGB")
    assert hashing.compare(fp_of(q), q, c).match_type == "exact"

def test_resized_is_near_duplicate_or_exact():
    q = scene(2)
    c = q.resize((320, 240))
    assert hashing.compare(fp_of(q), q, c).match_type in ("exact", "near_duplicate")

def test_aspect_change_is_near_duplicate():
    q = scene(3)
    c = q.resize((640, 400))
    assert hashing.compare(fp_of(q), q, c).match_type == "near_duplicate"

def test_cropped_detected_both_directions():
    q = scene(4)
    crop = q.crop((100, 80, 500, 380))
    assert hashing.compare(fp_of(q), q, crop).match_type == "cropped"
    assert hashing.compare(fp_of(crop), crop, q).match_type == "cropped"

def test_unrelated_not_matched():
    q, other = scene(5), scene(99)
    assert hashing.compare(fp_of(q), q, other).match_type in ("similar", "different")
    assert hashing.compare(fp_of(q), q, other).match_type not in ("exact", "near_duplicate", "cropped")

def test_rejects_non_image():
    import pytest
    with pytest.raises(hashing.InvalidImage):
        hashing.load_image(b"<?php echo 1; ?>")

def test_sanitize_strips_exif():
    q = scene(6)
    exif = Image.Exif(); exif[0x010F] = "SecretCamera"
    raw = to_bytes(q, exif=exif.tobytes())
    img, _ = hashing.load_image(raw)
    out = Image.open(io.BytesIO(hashing.sanitize(img)))
    assert 0x010F not in out.getexif()

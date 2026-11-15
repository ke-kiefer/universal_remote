"""File-based storage. Folders under DATA_DIR are the single source of truth for labels."""

from __future__ import annotations

import io
import re
import secrets
import shutil
import time
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

# Training label = native device id of the active adapter (HA: "light.desk",
# openHAB: "WohnzimmerLampe", ...). Must be safe as a directory name.
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
DEVICE_RE = re.compile(r"[^A-Za-z0-9_-]")
IMAGE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
THUMB_SIZE = (240, 180)
# Training images that came from the remote's feedback (not labeled by hand).
FEEDBACK_SUFFIX = "_fb"


def is_feedback_image(path: Path | str) -> bool:
    return Path(path).stem.endswith(FEEDBACK_SUFFIX)


class InvalidImage(ValueError):
    pass


# Directories that belong to one adapter (labels are that backend's device ids).
ADAPTER_DIRS = ("inbox", "labeled", "rejected", "captures", "thumbs")


def migrate_legacy_layout(data_dir: Path) -> bool:
    """Move pre-adapter data (flat under DATA_DIR) into DATA_DIR/homeassistant/.

    Before the adapter split all data belonged to Home Assistant. The model weight
    cache (DATA_DIR/model/weights) stays shared across adapters; the built index
    moves along with the images it was built from."""
    data_dir = Path(data_dir)
    if not any((data_dir / d).is_dir() for d in ADAPTER_DIRS):
        return False
    target = data_dir / "homeassistant"
    target.mkdir(parents=True, exist_ok=True)
    for d in ADAPTER_DIRS:
        src = data_dir / d
        if src.is_dir() and not (target / d).exists():
            shutil.move(str(src), target / d)
    (target / "model").mkdir(exist_ok=True)
    for f in ("index.npz", "index.json", "eval.json"):
        src = data_dir / "model" / f
        if src.is_file() and not (target / "model" / f).exists():
            shutil.move(str(src), target / "model" / f)
    return True


def is_valid_device_id(device_id: str) -> bool:
    return bool(LABEL_RE.match(device_id))


def check_jpeg(data: bytes) -> tuple[int, int]:
    """Validate a JPEG upload, return (width, height)."""
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        raise InvalidImage("not a JPEG (missing SOI marker)")
    if b"\xff\xd9" not in data[-16:]:
        raise InvalidImage("truncated JPEG (missing EOI marker)")
    try:
        with Image.open(io.BytesIO(data)) as img:
            return img.size
    except Exception as e:
        raise InvalidImage(f"cannot decode image: {e}") from e


# EXIF orientation values that make viewers rotate the image clockwise by N degrees.
EXIF_ORIENTATION_CW = {90: 6, 180: 3, 270: 8}


def set_rotation(data: bytes, degrees: int) -> bytes:
    """Mark a JPEG to be displayed rotated clockwise by `degrees` (0/90/180/270).

    Lossless: inserts an EXIF orientation tag instead of re-encoding. Pillow
    (ImageOps.exif_transpose, used for embeddings and thumbnails) and browsers honor it.
    Images that already carry EXIF data (e.g. phone photos) are left untouched.
    """
    if degrees % 360 == 0:
        return data
    with Image.open(io.BytesIO(data)) as img:
        if img.getexif():
            return data
    exif = Image.Exif()
    exif[0x0112] = EXIF_ORIENTATION_CW[degrees % 360]
    payload = exif.tobytes()
    if not payload.startswith(b"Exif\x00\x00"):
        payload = b"Exif\x00\x00" + payload
    segment = b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload
    pos = 2
    if data[2:4] == b"\xff\xe0":  # keep the JFIF APP0 segment first
        pos = 4 + int.from_bytes(data[4:6], "big")
    return data[:pos] + segment + data[pos:]


class Storage:
    def __init__(self, data_dir: Path):
        self.root = Path(data_dir)
        self.inbox = self.root / "inbox"
        self.labeled = self.root / "labeled"
        self.rejected = self.root / "rejected"
        self.captures = self.root / "captures"
        self.thumbs = self.root / "thumbs"
        self.model = self.root / "model"
        for d in (self.inbox, self.labeled, self.rejected, self.captures, self.thumbs, self.model):
            d.mkdir(parents=True, exist_ok=True)

    # --- ids -------------------------------------------------------------------------------
    @staticmethod
    def new_id(device: str) -> str:
        device = DEVICE_RE.sub("", device)[:32] or "unknown"
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        return f"{ts}_{device}_{secrets.token_hex(2)}"

    @staticmethod
    def _check_id(image_id: str) -> str:
        if not IMAGE_ID_RE.match(image_id):
            raise ValueError(f"invalid image id {image_id!r}")
        return image_id

    # --- writing ---------------------------------------------------------------------------
    def save_inbox(self, data: bytes, device: str) -> str:
        image_id = self.new_id(device)
        (self.inbox / f"{image_id}.jpg").write_bytes(data)
        return image_id

    def save_capture(self, data: bytes, device: str) -> str:
        image_id = self.new_id(device)
        (self.captures / f"{image_id}.jpg").write_bytes(data)
        return image_id

    def capture_path(self, capture_id: str) -> Path | None:
        p = self.captures / f"{self._check_id(capture_id)}.jpg"
        return p if p.exists() else None

    def label_inbox(self, image_ids: list[str], entity_id: str) -> list[Path]:
        """Move inbox images to labeled/<entity_id>/. Returns the new paths."""
        if not is_valid_device_id(entity_id):
            raise ValueError(f"invalid device id {entity_id!r}")
        dest = self.labeled / entity_id
        dest.mkdir(parents=True, exist_ok=True)
        moved = []
        for image_id in image_ids:
            src = self.inbox / f"{self._check_id(image_id)}.jpg"
            if src.exists():
                target = dest / src.name
                shutil.move(src, target)
                moved.append(target)
        return moved

    def feedback_path(self, capture_id: str, entity_id: str) -> Path:
        """Where a confirmed capture is stored as training image (marked with _fb)."""
        if not is_valid_device_id(entity_id):
            raise ValueError(f"invalid device id {entity_id!r}")
        name = f"{self._check_id(capture_id)}{FEEDBACK_SUFFIX}.jpg"
        return self.labeled / entity_id / name

    def add_labeled_from_capture(self, capture_id: str, entity_id: str) -> Path:
        src = self.capture_path(capture_id)
        if src is None:
            raise FileNotFoundError(capture_id)
        target = self.feedback_path(capture_id, entity_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        return target

    def capture_to_inbox(self, capture_id: str) -> str:
        """Copy an identify capture into the inbox for manual labeling."""
        src = self.capture_path(capture_id)
        if src is None:
            raise FileNotFoundError(capture_id)
        shutil.copy2(src, self.inbox / src.name)
        return src.stem

    def reject_inbox(self, image_ids: list[str]) -> int:
        n = 0
        for image_id in image_ids:
            src = self.inbox / f"{self._check_id(image_id)}.jpg"
            if src.exists():
                shutil.move(src, self.rejected / src.name)
                n += 1
        return n

    def delete_labeled(self, entity_id: str, image_ids: list[str]) -> list[Path]:
        if not is_valid_device_id(entity_id):
            raise ValueError(f"invalid device id {entity_id!r}")
        deleted = []
        for image_id in image_ids:
            p = self.labeled / entity_id / f"{self._check_id(image_id)}.jpg"
            if p.exists():
                p.unlink()
                deleted.append(p)
        d = self.labeled / entity_id
        if d.exists() and not any(d.iterdir()):
            d.rmdir()
        return deleted

    def cleanup_captures(self, max_age_days: int) -> int:
        cutoff = time.time() - max_age_days * 86400
        n = 0
        for p in self.captures.glob("*.jpg"):
            if p.stat().st_mtime < cutoff:
                p.unlink()
                n += 1
        return n

    # --- reading ---------------------------------------------------------------------------
    def inbox_ids(self) -> list[str]:
        return sorted((p.stem for p in self.inbox.glob("*.jpg")), reverse=True)

    def labeled_images(self) -> list[tuple[str, Path]]:
        """All labeled images as (entity_id, path), sorted for reproducibility."""
        out = []
        for d in sorted(self.labeled.iterdir()):
            if d.is_dir() and is_valid_device_id(d.name):
                out.extend((d.name, p) for p in sorted(d.glob("*.jpg")))
        return out

    def entity_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entity_id, _ in self.labeled_images():
            counts[entity_id] = counts.get(entity_id, 0) + 1
        return counts

    def feedback_counts(self) -> dict[str, int]:
        """Per entity: how many training images came from the remote's feedback."""
        counts: dict[str, int] = {}
        for entity_id, p in self.labeled_images():
            if is_feedback_image(p):
                counts[entity_id] = counts.get(entity_id, 0) + 1
        return counts

    def rel(self, path: Path) -> str:
        """Path relative to DATA_DIR with forward slashes (used as stable index key)."""
        return path.relative_to(self.root).as_posix()

    def resolve_rel(self, rel: str) -> Path:
        """Resolve a DATA_DIR-relative path from a URL, refusing anything outside it."""
        p = (self.root / rel).resolve()
        if self.root.resolve() not in p.parents or p.suffix.lower() != ".jpg":
            raise ValueError("invalid path")
        return p

    def thumbnail(self, rel: str) -> Path:
        src = self.resolve_rel(rel)
        if not src.exists():
            raise FileNotFoundError(rel)
        thumb = self.thumbs / rel
        if not thumb.exists() or thumb.stat().st_mtime < src.stat().st_mtime:
            thumb.parent.mkdir(parents=True, exist_ok=True)
            with Image.open(src) as img:
                img.draft("RGB", (THUMB_SIZE[0] * 2, THUMB_SIZE[1] * 2))
                img = ImageOps.exif_transpose(img).convert("RGB")
                img.thumbnail(THUMB_SIZE)
                img.save(thumb, "JPEG", quality=80)
        return thumb

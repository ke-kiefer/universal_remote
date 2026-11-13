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

ENTITY_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
DEVICE_RE = re.compile(r"[^A-Za-z0-9_-]")
IMAGE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
THUMB_SIZE = (240, 180)


class InvalidImage(ValueError):
    pass


def is_valid_entity_id(entity_id: str) -> bool:
    return bool(ENTITY_RE.match(entity_id))


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
        if not is_valid_entity_id(entity_id):
            raise ValueError(f"invalid entity id {entity_id!r}")
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

    def add_labeled_from_capture(self, capture_id: str, entity_id: str) -> Path:
        if not is_valid_entity_id(entity_id):
            raise ValueError(f"invalid entity id {entity_id!r}")
        src = self.capture_path(capture_id)
        if src is None:
            raise FileNotFoundError(capture_id)
        dest = self.labeled / entity_id
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / src.name
        shutil.copy2(src, target)
        return target

    def reject_inbox(self, image_ids: list[str]) -> int:
        n = 0
        for image_id in image_ids:
            src = self.inbox / f"{self._check_id(image_id)}.jpg"
            if src.exists():
                shutil.move(src, self.rejected / src.name)
                n += 1
        return n

    def delete_labeled(self, entity_id: str, image_ids: list[str]) -> list[Path]:
        if not is_valid_entity_id(entity_id):
            raise ValueError(f"invalid entity id {entity_id!r}")
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
            if d.is_dir() and is_valid_entity_id(d.name):
                out.extend((d.name, p) for p in sorted(d.glob("*.jpg")))
        return out

    def entity_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entity_id, _ in self.labeled_images():
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

"""Image -> embedding vector.

Preprocessing never crops away context: the whole image is scaled into the model input
with preserved aspect ratio (letterboxed if the aspect differs). Optional extra views are
center crops (CROP_SCALES), always in addition to the full image.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
import numpy as np
from PIL import Image, ImageOps

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelSpec:
    url: str
    mean: tuple[float, float, float]
    std: tuple[float, float, float]
    patch: int


MODELS: dict[str, ModelSpec] = {
    "dinov2_small": ModelSpec(
        url="https://huggingface.co/onnx-community/dinov2-small/resolve/main/onnx/model.onnx",
        mean=(0.485, 0.456, 0.406),
        std=(0.229, 0.224, 0.225),
        patch=14,
    ),
}


class Embedder(Protocol):
    """Anything that turns an image into one L2-normalized vector per view."""

    scales: list[float]

    @property
    def config_key(self) -> dict:
        """Everything that changes the vectors; a mismatch means the index must be rebuilt."""
        ...

    def embed_bytes(self, data: bytes | Path) -> np.ndarray:
        """Return (len(scales), D) float32, L2-normalized rows, row i = view at scales[i]."""
        ...


# --- preprocessing (pure functions, no model needed) ---------------------------------------


def load_image(data: bytes | Path, min_size: tuple[int, int] | None = None) -> Image.Image:
    """Decode a JPEG, apply EXIF orientation, return RGB.

    If min_size is given, JPEG draft mode decodes at reduced resolution (power-of-two
    downscale) as long as the result stays at least min_size. Saves a lot of time on
    large photos without affecting the model input.
    """
    img = Image.open(io.BytesIO(data) if isinstance(data, bytes) else data)
    if min_size is not None and img.format == "JPEG":
        # EXIF rotation may swap the axes, so request the larger edge in both directions.
        m = max(min_size)
        img.draft("RGB", (m, m))
    img = ImageOps.exif_transpose(img)
    return img.convert("RGB")


def target_size(img_wh: tuple[int, int], input_wh: tuple[int, int]) -> tuple[int, int]:
    """Model input size for an image: landscape config, swapped for portrait images."""
    w, h = img_wh
    iw, ih = input_wh
    if (h > w) != (ih > iw):
        iw, ih = ih, iw
    return iw, ih


def center_crop(img: Image.Image, scale: float) -> Image.Image:
    if scale >= 1.0:
        return img
    w, h = img.size
    cw, ch = max(1, round(w * scale)), max(1, round(h * scale))
    left, top = (w - cw) // 2, (h - ch) // 2
    return img.crop((left, top, left + cw, top + ch))


def fit_letterbox(img: Image.Image, size: tuple[int, int], fill=(124, 116, 104)) -> Image.Image:
    """Scale the whole image into `size` keeping aspect ratio; pad the rest (no cropping)."""
    tw, th = size
    w, h = img.size
    s = min(tw / w, th / h)
    nw, nh = max(1, round(w * s)), max(1, round(h * s))
    resized = img.resize((nw, nh), Image.Resampling.BICUBIC, reducing_gap=3.0)
    if (nw, nh) == (tw, th):
        return resized
    canvas = Image.new("RGB", (tw, th), fill)
    canvas.paste(resized, ((tw - nw) // 2, (th - nh) // 2))
    return canvas


def make_views(
    img: Image.Image, scales: list[float], input_wh: tuple[int, int]
) -> list[Image.Image]:
    size = target_size(img.size, input_wh)
    return [fit_letterbox(center_crop(img, s), size) for s in scales]


def to_tensor(views: list[Image.Image], mean, std) -> np.ndarray:
    arr = np.stack([np.asarray(v, dtype=np.float32) / 255.0 for v in views])  # N,H,W,3
    arr = (arr - np.asarray(mean, np.float32)) / np.asarray(std, np.float32)
    return np.ascontiguousarray(arr.transpose(0, 3, 1, 2))  # N,3,H,W


def l2_normalize(x: np.ndarray) -> np.ndarray:
    return x / np.maximum(np.linalg.norm(x, axis=-1, keepdims=True), 1e-12)


# --- model ---------------------------------------------------------------------------------


def ensure_model(name: str, weights_dir: Path) -> Path:
    """Download the ONNX model into weights_dir/<name>/model.onnx if missing."""
    if name not in MODELS:
        raise ValueError(f"unknown MODEL_NAME {name!r}, known: {', '.join(MODELS)}")
    path = weights_dir / name / "model.onnx"
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    log.info("downloading model %s from %s", name, MODELS[name].url)
    with (
        httpx.stream("GET", MODELS[name].url, follow_redirects=True, timeout=60) as r,
        open(tmp, "wb") as f,
    ):
        r.raise_for_status()
        for chunk in r.iter_bytes(1 << 20):
            f.write(chunk)
    tmp.replace(path)
    log.info("model saved to %s (%.1f MB)", path, path.stat().st_size / 1e6)
    return path


class OnnxEmbedder:
    def __init__(
        self,
        name: str,
        model_path: Path,
        input_wh: tuple[int, int],
        scales: list[float],
        pooling: str = "cls_mean",
    ):
        import onnxruntime as ort

        self.spec = MODELS[name]
        self.name = name
        self.input_wh = input_wh
        self.scales = scales
        self.pooling = pooling
        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(
            str(model_path), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self.input_name = self.session.get_inputs()[0].name

    @property
    def config_key(self) -> dict:
        """Everything that changes the vectors; a mismatch means the index must be rebuilt."""
        return {
            "model": self.name,
            "input_size": f"{self.input_wh[0]}x{self.input_wh[1]}",
            "scales": self.scales,
            "pooling": self.pooling,
        }

    def min_decode_size(self) -> tuple[int, int]:
        """Smallest decoded size that still gives full detail for the smallest crop."""
        s = min(self.scales)
        return (int(self.input_wh[0] / s), int(self.input_wh[1] / s))

    def embed(self, img: Image.Image) -> np.ndarray:
        views = make_views(img, self.scales, self.input_wh)
        x = to_tensor(views, self.spec.mean, self.spec.std)
        (hidden,) = self.session.run(None, {self.input_name: x})  # N, 1+P, D
        cls = hidden[:, 0]
        if self.pooling == "cls_mean":
            vec = np.concatenate([cls, hidden[:, 1:].mean(axis=1)], axis=1)
        else:
            vec = cls
        return l2_normalize(vec.astype(np.float32))

    def embed_bytes(self, data: bytes | Path) -> np.ndarray:
        return self.embed(load_image(data, self.min_decode_size()))

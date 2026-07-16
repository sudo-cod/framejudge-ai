"""Local neural video fingerprints used for candidate retrieval only.

MobileNetV2 embeddings shortlist likely original frames.  A match is never
declared from neural similarity alone: visual.py performs the strict block
comparison on every shortlisted pair.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

from .frames import FrameSet
from .visual import _embedded_regions

MODEL_PATH = Path(__file__).resolve().parent / "models" / "mobilenetv2-7.onnx"
MODEL_NAME = "MobileNetV2-7/ONNX"
TOP_K = 24


@dataclass
class FingerprintResult:
    candidates: np.ndarray
    best_similarity: np.ndarray
    selected_region: tuple[int, int, int, int] | None
    cache_hit: bool
    model: str = MODEL_NAME


@lru_cache(maxsize=1)
def _session() -> ort.InferenceSession:
    if not MODEL_PATH.is_file():
        raise RuntimeError(f"AI 指纹模型不存在：{MODEL_PATH}")
    return ort.InferenceSession(str(MODEL_PATH), providers=["CPUExecutionProvider"])


def _prepare(image: np.ndarray) -> np.ndarray:
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    rgb = cv2.resize(rgb, (224, 224), interpolation=cv2.INTER_AREA)
    value = rgb.astype(np.float32) / 255.0
    value = (value - np.array([0.485, 0.456, 0.406], np.float32)) / np.array(
        [0.229, 0.224, 0.225], np.float32)
    return value.transpose(2, 0, 1)[None]


def _embed_image(image: np.ndarray) -> np.ndarray:
    session = _session()
    output = session.run(None, {session.get_inputs()[0].name: _prepare(image)})[0]
    vector = np.asarray(output, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vector))
    return vector / max(norm, 1e-8)


def _embed_frames(frames: FrameSet,
                  region: tuple[int, int, int, int] | None = None,
                  indices: np.ndarray | None = None) -> np.ndarray:
    selected = np.arange(len(frames.paths)) if indices is None else indices
    vectors = []
    for index in selected:
        image = cv2.imread(frames.paths[int(index)])
        if image is None:
            raise RuntimeError(f"无法读取指纹帧：{frames.paths[int(index)]}")
        if region is not None:
            x0, y0, x1, y1 = region
            image = image[y0:y1, x0:x1]
        vectors.append(_embed_image(image))
    return np.stack(vectors).astype(np.float32)


def _cache_key(frames: FrameSet) -> str:
    digest = hashlib.sha256()
    digest.update(f"{MODEL_NAME}:{len(frames.paths)}".encode())
    sample = np.linspace(0, len(frames.paths) - 1,
                         min(8, len(frames.paths))).astype(int)
    for index in sample:
        digest.update(Path(frames.paths[int(index)]).read_bytes())
    return digest.hexdigest()[:24]


def _original_embeddings(frames: FrameSet, cache_dir: Path
                         ) -> tuple[np.ndarray, bool]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"original_{_cache_key(frames)}.npz"
    if cache_file.is_file():
        with np.load(cache_file) as data:
            return data["embeddings"].astype(np.float32), True
    embeddings = _embed_frames(frames)
    np.savez_compressed(cache_file, embeddings=embeddings,
                        model=np.array(MODEL_NAME))
    return embeddings, False


def _choose_region(suspect: FrameSet, original_embeddings: np.ndarray
                   ) -> tuple[int, int, int, int] | None:
    regions: list[tuple[int, int, int, int] | None] = [None]
    regions.extend(_embedded_regions(suspect))
    if len(regions) == 1:
        return None
    sample = np.linspace(0, len(suspect.paths) - 1,
                         min(10, len(suspect.paths))).astype(int)
    best_region = None
    best_score = -1.0
    for region in regions:
        embeddings = _embed_frames(suspect, region, sample)
        score = float(np.median((embeddings @ original_embeddings.T).max(axis=1)))
        if score > best_score:
            best_score, best_region = score, region
    return best_region


def retrieve_candidates(suspect: FrameSet, original: FrameSet,
                        cache_dir: Path, top_k: int = TOP_K
                        ) -> FingerprintResult:
    original_embeddings, cache_hit = _original_embeddings(original, cache_dir)
    region = _choose_region(suspect, original_embeddings)
    suspect_embeddings = _embed_frames(suspect, region)
    similarities = suspect_embeddings @ original_embeddings.T
    k = min(top_k, similarities.shape[1])
    candidates = np.argpartition(similarities, -k, axis=1)[:, -k:]
    row = np.arange(len(candidates))[:, None]
    order = np.argsort(similarities[row, candidates], axis=1)[:, ::-1]
    candidates = candidates[row, order]
    return FingerprintResult(
        candidates=candidates.astype(np.int32),
        best_similarity=similarities.max(axis=1),
        selected_region=region,
        cache_hit=cache_hit,
    )

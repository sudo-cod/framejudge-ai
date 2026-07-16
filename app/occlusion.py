"""模块一 1.2：物理遮挡与伪装检测。

- 非原生字幕遮挡：RapidOCR 扫描涉案帧下 1/3 区域，与匹配到的原版帧同位置比对，
  原版无该文字 → 二次字幕。
- 边角水印遮挡：对匹配帧对的角部区域比较拉普拉斯方差（模糊）与色块均匀度，
  涉案侧显著更糊/出现纯色块 → 高斯模糊或二次色块覆盖痕迹。
"""
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
import re

import cv2
import numpy as np

from . import scoring
from .frames import FrameSet
from .visual import VisualMatch

MAX_OCR_FRAMES = 24          # 抽样帧数上限（控制耗时）
OCR_MIN_CONFIDENCE = 0.82    # 低置信度 OCR 易产生错误字母，不进入字幕判定
SUBTITLE_MIN_HITS = 3        # 至少 N 帧检出二次字幕才计分
WATERMARK_MIN_HITS = 4       # 同一个角至少持续命中 N 个采样帧
BLUR_RATIO_THRESHOLD = 0.28  # 涉案角部清晰度低于原版的 28% 才考虑局部模糊
LOCALIZED_BLUR_RATIO = 0.55
LOW_FREQUENCY_SIMILARITY = 0.62
FLAT_STD_THRESHOLD = 8.0     # 角部像素标准差极低 → 纯色块覆盖
CORNERS = {  # (x0, y0, x1, y1) 相对坐标，重点右上角
    "右上角": (0.70, 0.02, 0.99, 0.22),
    "左上角": (0.01, 0.02, 0.30, 0.22),
    "右下角": (0.70, 0.72, 0.99, 0.95),
    "左下角": (0.01, 0.72, 0.30, 0.95),
}

_ocr_engine = None


def _ocr(img: np.ndarray) -> list[str]:
    global _ocr_engine
    if _ocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr_engine = RapidOCR()
    # Small burned-in subtitles are common in compressed videos. Upscaling
    # improves OCR stability without treating the OCR text as evidence itself.
    height, width = img.shape[:2]
    if min(height, width) < 720:
        scale = min(2.0, 720 / max(min(height, width), 1))
        img = cv2.resize(img, None, fx=scale, fy=scale,
                         interpolation=cv2.INTER_CUBIC)
    result, _ = _ocr_engine(img)
    if not result:
        return []
    texts = []
    for _, text, confidence in result:
        cleaned = re.sub(r"\s+", " ", str(text).strip())
        key = _ocr_key(cleaned)
        if confidence >= OCR_MIN_CONFIDENCE and len(key) >= 3:
            texts.append(cleaned)
    return texts


def _ocr_key(text: str) -> str:
    """Normalize OCR text for comparison, never for user-facing transcription."""
    return re.sub(r"[^\w]+", "", text.casefold(), flags=re.UNICODE)


def _same_ocr_text(left: str, right: str) -> bool:
    """Treat small OCR spelling errors as the same underlying subtitle."""
    a, b = _ocr_key(left), _ocr_key(right)
    if not a or not b:
        return False
    shorter, longer = sorted((a, b), key=len)
    if len(shorter) >= 4 and shorter in longer:
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.72


@dataclass
class OcclusionEvidence:
    subtitle_hits: list[dict] = field(default_factory=list)
    watermark_hits: list[dict] = field(default_factory=list)


def _sample_matched_indices(match: VisualMatch, k: int) -> np.ndarray:
    idx = np.flatnonzero(match.matched)
    if len(idx) <= k:
        return idx
    return idx[np.linspace(0, len(idx) - 1, k).astype(int)]


def _bottom_third(img: np.ndarray) -> np.ndarray:
    h = img.shape[0]
    return img[int(h * 2 / 3):, :]


def detect_subtitle_occlusion(suspect: FrameSet, original: FrameSet,
                              match: VisualMatch) -> list[dict]:
    hits = []
    for i in _sample_matched_indices(match, MAX_OCR_FRAMES):
        s_img = cv2.imread(suspect.paths[i])
        o_img = cv2.imread(original.paths[match.best_original[i]])
        if s_img is None or o_img is None:
            continue
        s_texts = set(_ocr(_bottom_third(s_img)))
        if not s_texts:
            continue
        o_texts = set(_ocr(_bottom_third(o_img)))
        extra = {t for t in s_texts
                 if not any(_same_ocr_text(t, ot) for ot in o_texts)}
        if extra:
            hits.append({
                "suspect_time": float(suspect.timestamps[i]),
                "original_time": float(original.timestamps[match.best_original[i]]),
                "texts": sorted(extra),
                "suspect_frame": suspect.paths[i],
                "original_frame": original.paths[match.best_original[i]],
            })
    return hits


def _box_region(img: np.ndarray, box: tuple) -> np.ndarray:
    h, w = img.shape[:2]
    x0, y0, x1, y1 = (int(box[0] * w), int(box[1] * h),
                      int(box[2] * w), int(box[3] * h))
    return cv2.cvtColor(img[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)


def _corner_metrics(img: np.ndarray, box: tuple) -> tuple[float, float]:
    region = _box_region(img, box)
    sharpness = cv2.Laplacian(region, cv2.CV_64F).var()
    flatness = float(region.std())
    return sharpness, flatness


def _frame_sharpness(img: np.ndarray) -> float:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _frame_std(img: np.ndarray) -> float:
    return float(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).std())


def _low_frequency_similarity(a: np.ndarray, b: np.ndarray,
                              box: tuple) -> float:
    a_region = cv2.resize(_box_region(a, box), (24, 16),
                          interpolation=cv2.INTER_AREA).astype(np.float32)
    b_region = cv2.resize(_box_region(b, box), (24, 16),
                          interpolation=cv2.INTER_AREA).astype(np.float32)
    a_region -= a_region.mean()
    b_region -= b_region.mean()
    denominator = float(np.linalg.norm(a_region) * np.linalg.norm(b_region))
    return float(np.dot(a_region.ravel(), b_region.ravel())
                 / max(denominator, 1e-6))


def detect_watermark_occlusion(suspect: FrameSet, original: FrameSet,
                               match: VisualMatch) -> list[dict]:
    hits = []
    for i in _sample_matched_indices(match, MAX_OCR_FRAMES):
        # Fixed corner coordinates are not comparable after crop, PIP, or a
        # geometric recovery. Comparing them would label different scene
        # content as deliberate blur.
        if match.embedded[i] or match.geometric[i]:
            continue
        s_img = cv2.imread(suspect.paths[i])
        o_img = cv2.imread(original.paths[match.best_original[i]])
        if s_img is None or o_img is None:
            continue
        if match.mirrored[i]:
            s_img = cv2.flip(s_img, 1)  # 还原镜像，保证角部对得上
        global_ratio = _frame_sharpness(s_img) / max(
            _frame_sharpness(o_img), 1e-6)
        global_flat_ratio = _frame_std(s_img) / max(_frame_std(o_img), 1e-6)
        for corner, box in CORNERS.items():
            s_sharp, s_flat = _corner_metrics(s_img, box)
            o_sharp, o_flat = _corner_metrics(o_img, box)
            if o_sharp < 20:  # 原版该角本身缺乏纹理，比较无意义
                continue
            corner_ratio = s_sharp / max(o_sharp, 1e-6)
            low_similarity = _low_frequency_similarity(s_img, o_img, box)
            blurred = (
                corner_ratio < BLUR_RATIO_THRESHOLD
                and corner_ratio < global_ratio * LOCALIZED_BLUR_RATIO
                and low_similarity >= LOW_FREQUENCY_SIMILARITY
            )
            flat_ratio = s_flat / max(o_flat, 1e-6)
            solid = (
                s_flat < FLAT_STD_THRESHOLD
                and o_flat > FLAT_STD_THRESHOLD * 3
                and flat_ratio < global_flat_ratio * LOCALIZED_BLUR_RATIO
            )
            if blurred or solid:
                hits.append({
                    "corner": corner,
                    "kind": "高斯模糊/马赛克痕迹" if blurred else "二次色块覆盖",
                    "suspect_time": float(suspect.timestamps[i]),
                    "sharpness_ratio": round(corner_ratio, 3),
                    "global_sharpness_ratio": round(global_ratio, 3),
                    "global_flatness_ratio": round(global_flat_ratio, 3),
                    "low_frequency_similarity": round(low_similarity, 3),
                    "suspect_frame": suspect.paths[i],
                    "original_frame": original.paths[match.best_original[i]],
                })
    return hits


def score_occlusion(suspect: FrameSet, original: FrameSet,
                    match: VisualMatch,
                    thresholds: dict | None = None
                    ) -> tuple[list[scoring.ScoreItem], OcclusionEvidence]:
    config = {**scoring.STANDARD_THRESHOLDS, **(thresholds or {})}
    ev = OcclusionEvidence()
    if match.matched.any():
        ev.subtitle_hits = detect_subtitle_occlusion(suspect, original, match)
        raw_watermark_hits = detect_watermark_occlusion(
            suspect, original, match)
        corner_counts = Counter(hit["corner"] for hit in raw_watermark_hits)
        persistent_corners = {
            corner for corner, count in corner_counts.items()
            if count >= config["watermark_min_hits"]
        }
        ev.watermark_hits = [
            hit for hit in raw_watermark_hits
            if hit["corner"] in persistent_corners
        ]

    sub_ok = len(ev.subtitle_hits) >= config["subtitle_min_hits"]
    wm_ok = bool(ev.watermark_hits)

    wm_corners = sorted({h["corner"] for h in ev.watermark_hits})
    items = [
        scoring.ScoreItem(
            key="subtitle_occlusion",
            label="【画面对抗】非原生字幕遮挡惩罚",
            measured=(f"检出疑似二次固化字幕 {len(ev.subtitle_hits)} 帧（以截图和时间点为准）"
                      if sub_ok else "未检出"),
            points=scoring.PENALTY_SUBTITLE if sub_ok else 0,
            triggered=sub_ok,
            detail="图像算法抓取到人为固化的二次区域字幕覆盖原片固有画面，"
                   "推定被告存在故意添加干扰物以对抗系统特征流比对的主观恶意痕迹",
        ),
        scoring.ScoreItem(
            key="watermark_occlusion",
            label="【画面对抗】边角水印遮挡惩罚",
            measured=(f"检出遮挡痕迹 {len(ev.watermark_hits)} 处（{'、'.join(wm_corners)}）"
                      if wm_ok else "未检出"),
            points=scoring.PENALTY_WATERMARK if wm_ok else 0,
            triggered=wm_ok,
            detail="系统在原片版权水印坐标处检测到高斯模糊或马赛克，"
                   "蓄意抹除权利人版权标识的反侦查行为物证",
        ),
    ]
    return items, ev

"""模块一 1.1：分块缩略图帧匹配（对局部遮挡鲁棒）。

涉案与原版均按 3fps 抽帧，每帧切 6×6 块。逐块比较 8×8 灰度缩略图的 MAE，
且只在双方都有纹理的块上计算命中比例（被字幕/水印/白块遮挡或纯色的块两端剔除），
比例 ≥ 阈值即判为一致帧。原版帧库含水平翻转特征防镜像绕过。
输出每帧的匹配结果供时间轴模块复用。
"""
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image

from . import scoring
from .frames import FrameSet, _tile_features, flipped_tiles


@dataclass
class VisualMatch:
    matched: np.ndarray        # bool (N,) 涉案每帧是否为一致帧
    best_original: np.ndarray  # int (N,) 匹配到的原版帧下标（未匹配为 -1）
    best_distance: np.ndarray  # int (N,) 有效块中的失配块数（越小越吻合）
    match_tiles: np.ndarray    # int (N,) 命中的有效块数
    valid_tiles: np.ndarray    # int (N,) 参与比对的有效块数（双方都有纹理）
    mirrored: np.ndarray       # bool (N,) 是否命中翻转帧库
    overlap_ratio: float       # 一致帧占涉案总帧数比例
    embedded: np.ndarray       # bool (N,) 是否通过画中画/背景图区域命中
    geometric: np.ndarray      # bool (N,) 是否通过局部特征+几何验证命中
    embedded_region: tuple[int, int, int, int] | None = None


GEOMETRIC_TOP_K = 8
GEOMETRIC_MIN_GOOD = 10
GEOMETRIC_MIN_INLIERS = 8
GEOMETRIC_MIN_INLIER_RATIO = 0.35
GEOMETRIC_MIN_COVERAGE = 0.025
GEOMETRIC_STRONG_INLIERS = 16


def _embedded_regions(frames: FrameSet) -> list[tuple[int, int, int, int]]:
    """Locate a moving video rectangle placed over a mostly static image.

    A background image has near-zero temporal variance, while the embedded
    video changes over time.  The detected moving bounds are expanded to the
    normalized 16:9 frame ratio; a few padding variants tolerate quiet edges.
    """
    if len(frames.paths) < 4:
        return []
    sample_idx = np.linspace(0, len(frames.paths) - 1,
                             min(24, len(frames.paths))).astype(int)
    images = []
    for i in sample_idx:
        im = cv2.imread(frames.paths[int(i)], cv2.IMREAD_GRAYSCALE)
        if im is not None:
            images.append(im)
    if len(images) < 4:
        return []
    stack = np.stack(images).astype(np.float32)
    motion = (stack.std(axis=0) >= 6).astype(np.uint8) * 255
    motion = cv2.morphologyEx(
        motion, cv2.MORPH_CLOSE, np.ones((11, 21), np.uint8))
    ys, xs = np.nonzero(motion)
    if len(xs) < motion.size * 0.02:
        return []
    height, width = motion.shape
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    if (x1 - x0) * (y1 - y0) >= width * height * 0.88:
        return []

    regions = []
    # Preserve the detected rectangle's actual aspect ratio first. Repost
    # tools often resize width and height independently; forcing 16:9 would
    # include background pixels and discard copied pixels.
    for padding in (-0.03, 0.0, 0.03, 0.08):
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        rw = (x1 - x0) * (1 + 2 * padding)
        rh = (y1 - y0) * (1 + 2 * padding)
        desired_w = min(width, int(round(rw)))
        desired_h = min(height, int(round(rh)))
        left = max(0, min(width - desired_w,
                          int(round(cx - desired_w / 2))))
        top = max(0, min(height - desired_h,
                         int(round(cy - desired_h / 2))))
        region = (left, top, left + desired_w, top + desired_h)
        if (region not in regions
                and desired_w * desired_h >= width * height * 0.08):
            regions.append(region)

    # Retain 16:9 alternatives when the motion mask covers only the active
    # center of a normally scaled embedded video.
    target_aspect = width / height
    for padding in (-0.03, 0.04, 0.12):
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        rw = (x1 - x0) * (1 + 2 * padding)
        rh = (y1 - y0) * (1 + 2 * padding)
        if rw / rh < target_aspect:
            rw = rh * target_aspect
        else:
            rh = rw / target_aspect
        left = max(0, int(round(cx - rw / 2)))
        top = max(0, int(round(cy - rh / 2)))
        right = min(width, int(round(cx + rw / 2)))
        bottom = min(height, int(round(cy + rh / 2)))
        # Re-anchor at an edge if clipping shortened the requested rectangle.
        desired_w, desired_h = min(width, int(round(rw))), min(height, int(round(rh)))
        left = min(left, width - desired_w)
        top = min(top, height - desired_h)
        region = (left, top, left + desired_w, top + desired_h)
        if region not in regions and desired_w * desired_h >= width * height * 0.08:
            regions.append(region)
    return regions


def _region_features(frames: FrameSet, region: tuple[int, int, int, int]
                     ) -> tuple[np.ndarray, np.ndarray]:
    tiles = np.zeros_like(frames.tiles)
    valid = np.zeros_like(frames.tile_valid)
    x0, y0, x1, y1 = region
    for i, path in enumerate(frames.paths):
        im = cv2.imread(path)
        if im is None:
            continue
        crop = im[y0:y1, x0:x1]
        if crop.size:
            rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
            tiles[i], valid[i] = _tile_features(Image.fromarray(rgb))
    return tiles, valid


def _sift_features(sift, image: np.ndarray
                   ) -> tuple[np.ndarray, np.ndarray | None]:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    keypoints, descriptors = sift.detectAndCompute(gray, None)
    points = np.array([point.pt for point in keypoints], dtype=np.float32)
    return points, descriptors


def _geometric_score(matcher, suspect_features, original_features,
                     suspect_shape: tuple[int, ...]) -> tuple[int, int, float] | None:
    points_s, desc_s = suspect_features
    points_o, desc_o = original_features
    if desc_s is None or desc_o is None or len(desc_s) < 4 or len(desc_o) < 4:
        return None
    pairs = matcher.knnMatch(desc_s, desc_o, k=2)
    good = [first for first, second in pairs if first.distance < 0.72 * second.distance]
    if len(good) < GEOMETRIC_MIN_GOOD:
        return None
    source = np.float32([points_s[item.queryIdx] for item in good]).reshape(-1, 1, 2)
    target = np.float32([points_o[item.trainIdx] for item in good]).reshape(-1, 1, 2)
    _, mask = cv2.findHomography(source, target, cv2.RANSAC, 4.0)
    if mask is None:
        return None
    inlier_mask = mask.ravel().astype(bool)
    inliers = int(inlier_mask.sum())
    ratio = inliers / len(good)
    if inliers < GEOMETRIC_MIN_INLIERS or ratio < GEOMETRIC_MIN_INLIER_RATIO:
        return None
    inlier_points = source.reshape(-1, 2)[inlier_mask]
    hull = cv2.convexHull(inlier_points)
    coverage = float(cv2.contourArea(hull)) / max(
        1.0, suspect_shape[0] * suspect_shape[1])
    if coverage < GEOMETRIC_MIN_COVERAGE:
        return None
    return inliers, len(good), coverage


def _geometric_verify(suspect: FrameSet, original: FrameSet,
                      candidates: np.ndarray, matched: np.ndarray,
                      best_original: np.ndarray, best_match: np.ndarray,
                      best_valid: np.ndarray, mirrored: np.ndarray,
                      embedded: np.ndarray,
                      fingerprint_region: tuple[int, int, int, int] | None
                      ) -> np.ndarray:
    """Verify AI candidates using SIFT correspondences and a RANSAC homography."""
    geometric = np.zeros(len(suspect.paths), dtype=bool)
    strengths = np.zeros(len(suspect.paths), dtype=np.int16)
    sift = cv2.SIFT_create(nfeatures=600, contrastThreshold=0.025)
    matcher = cv2.BFMatcher(cv2.NORM_L2)
    original_cache: dict[int, tuple[np.ndarray, np.ndarray | None]] = {}

    def original_features(index: int):
        if index not in original_cache:
            image = cv2.imread(original.paths[index])
            original_cache[index] = _sift_features(sift, image)
        return original_cache[index]

    regions: list[tuple[int, int, int, int] | None] = [None]
    if fingerprint_region is not None:
        regions.append(fingerprint_region)

    for i in np.flatnonzero(~matched):
        full = cv2.imread(suspect.paths[int(i)])
        if full is None:
            continue
        best = None
        for region in regions:
            if region is None:
                crop = full
            else:
                x0, y0, x1, y1 = region
                crop = full[y0:y1, x0:x1]
            if crop.size == 0:
                continue
            for is_mirrored in (False, True):
                image = cv2.flip(crop, 1) if is_mirrored else crop
                features = _sift_features(sift, image)
                for original_index in candidates[int(i), :GEOMETRIC_TOP_K]:
                    j = int(original_index)
                    score = _geometric_score(
                        matcher, features, original_features(j), image.shape)
                    if score is None:
                        continue
                    inliers, good, coverage = score
                    rank = inliers + coverage * 20
                    if best is None or rank > best[0]:
                        best = (rank, j, inliers, good, is_mirrored,
                                region is not None)
        if best is not None:
            _, j, inliers, good, is_mirrored, is_embedded = best
            matched[i] = geometric[i] = True
            strengths[i] = inliers
            best_original[i] = j
            best_match[i], best_valid[i] = inliers, good
            mirrored[i], embedded[i] = is_mirrored, is_embedded

    # Weak geometric hits must participate in a locally coherent sequence.
    for i in np.flatnonzero(geometric & (strengths < GEOMETRIC_STRONG_INLIERS)):
        coherent = False
        for delta in (-2, -1, 1, 2):
            neighbor = int(i + delta)
            if neighbor < 0 or neighbor >= len(matched) or not matched[neighbor]:
                continue
            original_delta = best_original[neighbor] - best_original[i]
            if abs(original_delta - delta) <= 4:
                coherent = True
                break
        if not coherent:
            matched[i] = geometric[i] = False
            best_original[i] = -1
    return geometric


def _tile_match_counts(chunk_t: np.ndarray, chunk_v: np.ndarray,
                       lib_t: np.ndarray, lib_v: np.ndarray
                       ) -> tuple[np.ndarray, np.ndarray]:
    """返回 (命中有效块数 (b,No), 双方有效块数 (b,No))。

    逐块比较缩略图平均绝对误差(MAE)，MAE ≤ 阈值记命中；仅在“双方该块都有纹理”时纳入，
    无纹理块两端剔除，从而对局部遮挡（白块/字幕条）免疫 —— 相似度只在双方都有内容处计算。
    """
    T = chunk_t.shape[1]
    b, no = chunk_t.shape[0], lib_t.shape[0]
    matched = np.zeros((b, no), dtype=np.int16)
    valid = np.zeros((b, no), dtype=np.int16)
    for t in range(T):
        jv = chunk_v[:, t][:, None] & lib_v[:, t][None, :]          # (b,No) 双方有效
        diff = np.abs(chunk_t[:, t, :][:, None, :].astype(np.int16)
                      - lib_t[:, t, :][None, :, :].astype(np.int16))
        mae = diff.mean(axis=2)                                     # (b,No)
        matched += (mae <= scoring.TILE_MAE_THRESHOLD) & jv
        valid += jv
    return matched, valid


def _best_frac(mc: np.ndarray, vc: np.ndarray) -> np.ndarray:
    """匹配比例 = 命中有效块 / 双方有效块；有效块过少的帧比例记 0（无法判定）。"""
    frac = mc / np.maximum(vc, 1)
    frac[vc < scoring.TILE_MIN_VALID] = 0.0
    return frac


def match_frames(suspect: FrameSet, original: FrameSet,
                 batch: int = 256,
                 candidates: np.ndarray | None = None,
                 fingerprint_region: tuple[int, int, int, int] | None = None
                 ) -> VisualMatch:
    orig_t, orig_v = original.tiles, original.tile_valid
    flip_t, flip_v = flipped_tiles(original)
    n = len(suspect.tiles)
    best_orig = np.full(n, -1, dtype=int)
    best_mc = np.zeros(n, dtype=int)
    best_vc = np.zeros(n, dtype=int)
    best_frac = np.zeros(n, dtype=float)
    mirrored = np.zeros(n, dtype=bool)
    embedded = np.zeros(n, dtype=bool)

    def compare_feature_set(all_tiles: np.ndarray, all_valid: np.ndarray,
                            is_embedded: bool) -> None:
        if candidates is not None:
            for i in range(n):
                candidate_ids = candidates[i]
                ch, cv = all_tiles[i:i + 1], all_valid[i:i + 1]
                mc_n, vc_n = _tile_match_counts(
                    ch, cv, orig_t[candidate_ids], orig_v[candidate_ids])
                mc_f, vc_f = _tile_match_counts(
                    ch, cv, flip_t[candidate_ids], flip_v[candidate_ids])
                fr_n, fr_f = _best_frac(mc_n, vc_n), _best_frac(mc_f, vc_f)
                local_n, local_f = fr_n[0].argmax(), fr_f[0].argmax()
                use_flip = fr_f[0, local_f] > fr_n[0, local_n]
                local = local_f if use_flip else local_n
                j = int(candidate_ids[local])
                frac = fr_f[0, local] if use_flip else fr_n[0, local]
                if frac > best_frac[i]:
                    mc = mc_f[0, local] if use_flip else mc_n[0, local]
                    vc = vc_f[0, local] if use_flip else vc_n[0, local]
                    best_orig[i], mirrored[i] = j, use_flip
                    best_mc[i], best_vc[i], best_frac[i] = mc, vc, frac
                    embedded[i] = is_embedded
            return
        for s in range(0, n, batch):
            ch = all_tiles[s:s + batch]
            cv = all_valid[s:s + batch]
            mc_n, vc_n = _tile_match_counts(ch, cv, orig_t, orig_v)
            mc_f, vc_f = _tile_match_counts(ch, cv, flip_t, flip_v)
            fr_n, fr_f = _best_frac(mc_n, vc_n), _best_frac(mc_f, vc_f)
            for row in range(ch.shape[0]):
                i = s + row
                j_n, j_f = fr_n[row].argmax(), fr_f[row].argmax()
                use_flip = fr_f[row][j_f] > fr_n[row][j_n]
                j = j_f if use_flip else j_n
                frac = fr_f[row][j] if use_flip else fr_n[row][j]
                if frac > best_frac[i]:
                    mc = mc_f[row][j] if use_flip else mc_n[row][j]
                    vc = vc_f[row][j] if use_flip else vc_n[row][j]
                    best_orig[i], mirrored[i] = j, use_flip
                    best_mc[i], best_vc[i], best_frac[i] = mc, vc, frac
                    embedded[i] = is_embedded

    compare_feature_set(suspect.tiles, suspect.tile_valid, False)
    regions = _embedded_regions(suspect)
    selected_region = None
    best_embedded_hits = 0
    for region in regions:
        before = embedded.copy()
        region_tiles, region_valid = _region_features(suspect, region)
        compare_feature_set(region_tiles, region_valid, True)
        new_hits = int((embedded & ~before).sum())
        if new_hits > best_embedded_hits:
            best_embedded_hits = new_hits
            selected_region = region
        provisional = ((best_frac >= scoring.TILE_MATCH_FRACTION)
                       & (best_vc >= scoring.TILE_MIN_VALID))
        if provisional.mean() >= 0.90:
            break

    matched = (best_frac >= scoring.TILE_MATCH_FRACTION) & (best_vc >= scoring.TILE_MIN_VALID)
    geometric = np.zeros(n, dtype=bool)
    if candidates is not None:
        geometric = _geometric_verify(
            suspect, original, candidates, matched, best_orig, best_mc,
            best_vc, mirrored, embedded, fingerprint_region)
        if selected_region is None and np.any(geometric & embedded):
            selected_region = fingerprint_region
    best_orig[~matched] = -1
    return VisualMatch(
        matched=matched,
        best_original=best_orig,
        best_distance=(best_vc - best_mc).astype(int),
        match_tiles=best_mc,
        valid_tiles=best_vc,
        mirrored=mirrored & matched,
        overlap_ratio=float(matched.mean()) if n else 0.0,
        embedded=embedded & matched,
        geometric=geometric & matched,
        embedded_region=selected_region,
    )


def overlap_measured(match: VisualMatch) -> str:
    """一致帧占比的展示字符串（供勘验日志/报告用）。"""
    mirror_note = ""
    if match.mirrored.any():
        mirror_note = f"（其中 {int(match.mirrored.sum())} 帧命中水平翻转帧库，存在镜像规避痕迹）"
    embedded_note = ""
    if match.embedded.any():
        embedded_note = f"（其中 {int(match.embedded.sum())} 帧通过画中画区域命中）"
    geometric_note = ""
    if match.geometric.any():
        geometric_note = f"（其中 {int(match.geometric.sum())} 帧通过几何验证命中）"
    return (f"一致帧占涉案总帧数 {match.overlap_ratio:.1%}"
            f"{mirror_note}{embedded_note}{geometric_note}")

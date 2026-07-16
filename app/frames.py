"""ffmpeg 抽帧与分块特征计算。

每帧切成 GRID 网格，每块用 8×8 灰度缩略图描述，帧匹配时逐块比较平均绝对误差(MAE)，
并且只在「双方都有纹理的块」上计算相似度：被局部遮挡（底部二次字幕、边角水印、
画面局部白块覆盖）或纯色的块两端剔除，不计入分子分母，故对局部遮挡天然免疫 ——
搬运帧不会因为加了一条字幕或一块马赛克就整帧漏配。原版帧库同时含水平翻转特征，防镜像绕过。
"""
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

FRAME_SIZE = (320, 180)  # 抽帧归一化尺寸（证据截图用）
GRID = (6, 6)            # 分块网格：6 行 × 6 列 = 36 块（网格越细，局部遮挡占比越小）
_THUMB = 8               # 每块灰度缩略图边长
TILES = GRID[0] * GRID[1]


_STD_FLOOR = 6.0  # 块灰度标准差低于此→无纹理块（纯色/遮挡白块/黑字幕条），匹配时忽略


@lru_cache(maxsize=1)
def ffmpeg_executable() -> str:
    """Return the project-bundled FFmpeg; no system PATH setup is needed."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise RuntimeError(
            "FFmpeg 不可用，请在项目目录执行 `uv sync` 后重试。"
        ) from exc


def _run_ffmpeg(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run([ffmpeg_executable(), *args], **kwargs)


@dataclass
class FrameSet:
    timestamps: np.ndarray   # 每帧时间戳（秒）
    tiles: np.ndarray        # uint8 (N, TILES, 64) 每块 8×8 灰度缩略图（展平）
    tile_valid: np.ndarray   # bool (N, TILES) 该块是否有纹理（无纹理块不参与匹配）
    paths: list[str]         # 帧图片路径（证据截图用）
    duration: float
    fps: float


def _tile_features(im: Image.Image) -> tuple[np.ndarray, np.ndarray]:
    """返回 (tiles (TILES,64) uint8 灰度缩略图, valid (TILES,) bool)。

    valid=False 的块为无纹理块（纯色平面 / 被白块或字幕条覆盖），匹配时两端剔除 ——
    “只在双方都有纹理的区域比对相似度”，对局部遮挡免疫。
    """
    rows, cols = GRID
    g = np.asarray(
        im.convert("L").resize((cols * _THUMB, rows * _THUMB), Image.LANCZOS),
        dtype=np.float32)
    # (rows*_THUMB, cols*_THUMB) → (TILES, _THUMB*_THUMB)
    tiles = (g.reshape(rows, _THUMB, cols, _THUMB)
             .transpose(0, 2, 1, 3)
             .reshape(TILES, _THUMB * _THUMB))
    valid = tiles.std(axis=1) > _STD_FLOOR
    return tiles.astype(np.uint8), valid


def video_duration(path: str) -> float:
    capture = cv2.VideoCapture(path)
    try:
        fps = capture.get(cv2.CAP_PROP_FPS)
        frame_count = capture.get(cv2.CAP_PROP_FRAME_COUNT)
    finally:
        capture.release()
    if fps <= 0 or frame_count <= 0:
        raise RuntimeError(f"无法读取视频时长：{path}")
    return frame_count / fps


def extract_frames(video: str, outdir: Path, fps: float) -> FrameSet:
    outdir.mkdir(parents=True, exist_ok=True)
    pattern = str(outdir / "f%06d.jpg")
    _run_ffmpeg(
        ["-y", "-i", video, "-vf",
         f"fps={fps},scale={FRAME_SIZE[0]}:{FRAME_SIZE[1]}",
         "-qscale:v", "3", pattern],
        capture_output=True, check=True)
    paths = sorted(str(p) for p in outdir.glob("f*.jpg"))
    n = len(paths)
    if n == 0:
        raise RuntimeError(f"未能从 {video} 抽出任何帧")
    # ffmpeg fps filter: 第 k 帧（1-based）时间戳约为 (k-1)/fps
    timestamps = np.arange(n) / fps
    tiles = np.zeros((n, TILES, _THUMB * _THUMB), dtype=np.uint8)
    valid = np.zeros((n, TILES), dtype=bool)
    for i, p in enumerate(paths):
        with Image.open(p) as im:
            tiles[i], valid[i] = _tile_features(im)
    return FrameSet(timestamps=timestamps, tiles=tiles, tile_valid=valid,
                    paths=paths, duration=video_duration(video), fps=fps)


def flipped_tiles(frameset: FrameSet) -> tuple[np.ndarray, np.ndarray]:
    """原版帧水平翻转后的分块缩略图与有效性掩码（防镜像绕过补丁）。"""
    t = np.zeros_like(frameset.tiles)
    v = np.zeros_like(frameset.tile_valid)
    for i, p in enumerate(frameset.paths):
        with Image.open(p) as im:
            t[i], v[i] = _tile_features(im.transpose(Image.FLIP_LEFT_RIGHT))
    return t, v


def extract_audio(video: str, out_wav: Path) -> Path:
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(
        ["-y", "-i", video, "-vn", "-ac", "1", "-ar", "16000",
         "-f", "wav", str(out_wav)],
        capture_output=True, check=True)
    return out_wav


def has_audio_stream(video: str) -> bool:
    # `ffmpeg -i` lists all streams in stderr and does not modify the input.
    out = _run_ffmpeg(["-hide_banner", "-i", video],
                      capture_output=True, text=True)
    return "Audio:" in out.stderr

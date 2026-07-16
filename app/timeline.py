"""模块三：时间轴结构特征。

- 最长连续相同片段（允许 1-2 秒容错断层）—— 第一轮【时长熔断】依据
- 盗用浓度：1 秒窗口内 ≥3 帧一致帧 → 盗用污染秒 —— 第一轮【浓度熔断】依据
- 时序一致性：离散重合片段在原片时间戳上的正序对率 —— 第二轮【时序对抗】依据
"""
import numpy as np

from . import scoring
from .visual import VisualMatch


def polluted_seconds(match: VisualMatch, timestamps: np.ndarray,
                     duration: float) -> np.ndarray:
    """bool 数组：涉案视频每一秒是否为“盗用污染秒”。"""
    n_sec = max(1, int(np.ceil(duration)))
    counts = np.zeros(n_sec, dtype=int)
    secs = np.minimum(timestamps.astype(int), n_sec - 1)
    np.add.at(counts, secs[match.matched], 1)
    return counts >= scoring.FRAMES_PER_POLLUTED_SECOND


def longest_run_seconds(polluted: np.ndarray) -> int:
    """最长连续污染秒数，容忍 CONTINUOUS_GAP_TOLERANCE 秒断层。"""
    gap_tol = scoring.CONTINUOUS_GAP_TOLERANCE
    best = run_len = gap = 0
    for hit in polluted:
        if hit:
            run_len += (gap if run_len else 0) + 1  # 段内断层被容错吸收
            gap = 0
            best = max(best, run_len)
        else:
            gap += 1
            if gap > gap_tol:
                run_len = 0
                gap = 0
    return best


def sequential_order_rate(match: VisualMatch) -> tuple[float, int]:
    """离散重合片段在原片物理时间戳上的正序对率（时序一致性）。

    将涉案侧连续的一致帧聚合为“离散重合片段”，取每段对应原片帧下标的中位数为
    代表时间戳；涉案片段本身按时间先后排列，统计所有片段两两组合中
    “原片时间戳也保持正序”的比例。≥80% 视为顺着原片剧情脉络流水账剧透。

    返回 (正序对率, 离散片段数)。
    """
    idx = np.flatnonzero(match.matched)
    if len(idx) == 0:
        return 0.0, 0

    # 涉案帧下标连续者归为同一离散片段
    segments: list[list[int]] = []
    run = [int(idx[0])]
    for k in idx[1:]:
        if k == run[-1] + 1:
            run.append(int(k))
        else:
            segments.append(run)
            run = [int(k)]
    segments.append(run)

    # 每段的原片代表下标（中位数）；涉案片段已按时间先后排列
    reps = [float(np.median(match.best_original[np.array(seg)])) for seg in segments]
    n = len(reps)
    if n < scoring.SEQUENTIAL_MIN_SEGMENTS:
        return 0.0, n

    concordant = total = 0
    for i in range(n):
        for j in range(i + 1, n):
            total += 1
            if reps[i] < reps[j]:
                concordant += 1
    return (concordant / total if total else 0.0), n


def analyze_timeline(match: VisualMatch, timestamps: np.ndarray,
                     suspect_duration: float) -> dict:
    """返回时间轴各项原始测量值 + 污染秒 bool 数组。"""
    polluted = polluted_seconds(match, timestamps, suspect_duration)
    longest = longest_run_seconds(polluted)
    order_rate, n_seg = sequential_order_rate(match)
    return {
        "longest_seconds": longest,
        "polluted": polluted,
        "polluted_count": int(polluted.sum()),
        "order_rate": order_rate,
        "segment_count": n_seg,
    }


def sequential_penalty(order_rate: float, n_seg: int) -> scoring.ScoreItem:
    """第二轮【时序对抗】剧情顺叙替代惩罚 —— 固定 +45 分。"""
    triggered = (n_seg >= scoring.SEQUENTIAL_MIN_SEGMENTS
                 and order_rate >= scoring.SEQUENTIAL_ORDER_THRESHOLD)
    return scoring.ScoreItem(
        key="sequential",
        label="【时序对抗】剧情顺叙替代惩罚",
        measured=(f"离散重合片段 {n_seg} 段，原片时间戳正序对率 {order_rate:.1%}"
                  if n_seg else "无离散重合片段"),
        points=scoring.PENALTY_SEQUENTIAL if triggered else 0,
        triggered=triggered,
        detail="离散重合片段在原片中的物理时间戳正序对率≥80%，判定被告顺着原片剧情脉络"
               "“流水账剧透”，构成对原片剧情骨架的实质性替代",
    )

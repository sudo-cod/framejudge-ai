"""模块二：声音声学特征。

2.1 声学指纹重合度：频谱峰值地标哈希（audfprint 思路），
    按时间偏移直方图投票，标记涉案音轨中逐秒的“重合原声”。
2.2 解说替代检测（防规避项）：声学重合度极低但画面重合度极高时，
    用能量+过零率 VAD 判断是否存在连续人声/配音。
"""
from collections import defaultdict
from dataclasses import dataclass

import librosa
import numpy as np
from scipy.signal import correlate

from . import scoring

SR = 16000
N_FFT = 1024
HOP = 512                    # 32ms/帧
FRAMES_PER_SEC = SR / HOP    # ≈31.25
PEAK_NEIGHBORHOOD = (15, 11)  # (freq bins, time frames) 局部极大值窗口
FANOUT = 8                   # 每个锚点向后配对的目标峰数
TARGET_DT = (2, 64)          # 配对峰的时间距离范围（帧）
MIN_VOTES_PER_SEC = 4        # 每秒最少命中哈希数才算“重合原声秒”
MIX_CORRELATION_THRESHOLD = 0.30  # 原声保留但叠加 BGM 时的波形包含阈值
VISUAL_GUIDED_MIX_THRESHOLD = 0.12  # 画面已严格对齐时，可检测更低音量原声
MIX_MIN_RMS = 2e-4


@dataclass
class AudioResult:
    overlap_ratio: float
    matched_seconds: np.ndarray  # bool per second
    duration: float
    has_speech: bool             # 涉案音轨是否存在连续人声
    silent: bool                 # 涉案是否基本无声


def _spectral_peaks(y: np.ndarray) -> np.ndarray:
    """返回 (freq_bin, frame) 峰值坐标数组。"""
    S = np.abs(librosa.stft(y, n_fft=N_FFT, hop_length=HOP))
    S = librosa.amplitude_to_db(S, ref=np.max)
    from scipy.ndimage import maximum_filter
    local_max = maximum_filter(S, size=PEAK_NEIGHBORHOOD) == S
    threshold = np.median(S) + 10
    peaks = np.argwhere(local_max & (S > threshold))
    return peaks[np.argsort(peaks[:, 1])]  # 按时间排序


def _landmarks(peaks: np.ndarray):
    """生成 (hash, anchor_frame)：hash = (f1, f2, dt)。"""
    for i in range(len(peaks)):
        f1, t1 = peaks[i]
        paired = 0
        for j in range(i + 1, len(peaks)):
            f2, t2 = peaks[j]
            dt = t2 - t1
            if dt < TARGET_DT[0]:
                continue
            if dt > TARGET_DT[1] or paired >= FANOUT:
                break
            yield (int(f1) << 20) | (int(f2) << 8) | int(dt), int(t1)
            paired += 1


def _waveform_overlap(y_s: np.ndarray, y_o: np.ndarray, n_sec: int,
                      frame_offsets: list[int],
                      visual_offset_seconds: np.ndarray | None = None
                      ) -> np.ndarray:
    """Detect a clean original waveform contained in an additive audio mix.

    Landmark hashes provide precise candidate offsets. At each offset we use
    normalized waveform correlation, which remains high for `original + BGM`
    but low for unrelated audio. Silence is excluded from both sides.
    """
    matched = np.zeros(n_sec, dtype=bool)
    length = int(SR * 0.85)
    for frame_offset in frame_offsets:
        sample_offset = int(frame_offset * HOP)
        for sec in range(n_sec):
            s0 = sec * SR
            o0 = s0 + sample_offset
            if s0 < 0 or o0 < 0 or s0 + length > len(y_s) or o0 + length > len(y_o):
                continue
            suspect = y_s[s0:s0 + length].astype(np.float64)
            original = y_o[o0:o0 + length].astype(np.float64)
            suspect -= suspect.mean()
            original -= original.mean()
            s_rms = float(np.sqrt(np.mean(suspect * suspect)))
            o_rms = float(np.sqrt(np.mean(original * original)))
            if s_rms < MIX_MIN_RMS or o_rms < MIX_MIN_RMS:
                continue
            correlation = abs(float(np.dot(suspect, original))) / (
                len(original) * s_rms * o_rms)
            if correlation >= MIX_CORRELATION_THRESHOLD:
                matched[sec] = True
    # Edited videos can have dozens of offsets. Visual verification already
    # establishes the original timestamp for many suspect seconds, so use that
    # mapping to perform a local audio containment check around each cut.
    if visual_offset_seconds is not None:
        margin = int(SR * 0.55)
        for sec, offset_seconds in enumerate(visual_offset_seconds[:n_sec]):
            if matched[sec] or not np.isfinite(offset_seconds):
                continue
            s0 = sec * SR
            o_center = int((sec + float(offset_seconds)) * SR)
            if s0 + length > len(y_s):
                continue
            left = max(0, o_center - margin)
            right = min(len(y_o), o_center + length + margin)
            suspect = y_s[s0:s0 + length].astype(np.float64)
            search = y_o[left:right].astype(np.float64)
            if len(search) < length:
                continue
            suspect -= suspect.mean()
            s_energy = float(np.dot(suspect, suspect))
            if s_energy < (MIX_MIN_RMS ** 2) * length:
                continue
            dots = correlate(search, suspect, mode="valid", method="fft")
            prefix = np.concatenate(([0.0], np.cumsum(search)))
            prefix2 = np.concatenate(([0.0], np.cumsum(search * search)))
            sums = prefix[length:] - prefix[:-length]
            energies = (prefix2[length:] - prefix2[:-length]
                        - (sums * sums) / length)
            denom = np.sqrt(np.maximum(energies * s_energy, 1e-12))
            if float(np.max(np.abs(dots) / denom)) >= VISUAL_GUIDED_MIX_THRESHOLD:
                matched[sec] = True
    return matched


def fingerprint_overlap(suspect_wav: str, original_wav: str,
                        suspect_duration: float,
                        visual_offset_seconds: np.ndarray | None = None
                        ) -> tuple[float, np.ndarray]:
    y_o, _ = librosa.load(original_wav, sr=SR, mono=True)
    y_s, _ = librosa.load(suspect_wav, sr=SR, mono=True)

    index: dict[int, list[int]] = defaultdict(list)
    for h, t in _landmarks(_spectral_peaks(y_o)):
        index[h].append(t)

    n_sec = max(1, int(np.ceil(suspect_duration)))
    # 投票：STFT 帧级 offset -> {suspect_sec: hit_count}
    offset_votes: dict[int, np.ndarray] = defaultdict(
        lambda: np.zeros(n_sec, dtype=int))
    for h, t_s in _landmarks(_spectral_peaks(y_s)):
        for t_o in index.get(h, ()):
            offset = int(t_o - t_s)
            sec = min(int(t_s / FRAMES_PER_SEC), n_sec - 1)
            offset_votes[offset][sec] += 1

    matched = np.zeros(n_sec, dtype=bool)
    # 允许多段来自原片不同位置的拼接：对每个稳定偏移分别标记
    for votes in offset_votes.values():
        if votes.sum() >= MIN_VOTES_PER_SEC * 2:
            matched |= votes >= MIN_VOTES_PER_SEC
    # Exact landmark density can fall when a new BGM masks some spectral peaks.
    # Re-check the strongest offsets using additive-mixture waveform containment.
    ranked_offsets = sorted(offset_votes,
                            key=lambda key: int(offset_votes[key].sum()),
                            reverse=True)
    plausible_offsets = [key for key in ranked_offsets[:8]
                         if offset_votes[key].sum() >= 2]
    if 0 not in plausible_offsets:
        plausible_offsets.append(0)
    matched |= _waveform_overlap(y_s, y_o, n_sec, plausible_offsets,
                                 visual_offset_seconds)
    return float(matched.mean()), matched


def detect_speech(wav_path: str) -> tuple[bool, bool]:
    """能量 + 过零率 VAD：返回 (存在连续人声/配音, 基本无声)。"""
    y, _ = librosa.load(wav_path, sr=SR, mono=True)
    if len(y) == 0:
        return False, True
    rms = librosa.feature.rms(y=y, frame_length=N_FFT, hop_length=HOP)[0]
    silent = float(rms.mean()) < 1e-3
    zcr = librosa.feature.zero_crossing_rate(y, frame_length=N_FFT, hop_length=HOP)[0]
    active = rms > max(1e-3, np.percentile(rms, 30))
    speech_like = active & (zcr > 0.02) & (zcr < 0.35)
    # 持续人声/配音判定：容忍自然语句间停顿（≤0.5秒），
    # 合并后的最长类语音段 ≥2 秒即视为存在解说/配音
    gap_tol = int(FRAMES_PER_SEC * 0.5)
    best = run = gap = 0
    for f in speech_like:
        if f:
            run += (gap if run else 0) + 1  # 段内短停顿被容错吸收
            gap = 0
            best = max(best, run)
        else:
            gap += 1
            if gap > gap_tol:
                run = gap = 0
    return best >= FRAMES_PER_SEC * 2, silent


def analyze_audio(suspect_wav: str | None, original_wav: str | None,
                  suspect_duration: float,
                  visual_offset_seconds: np.ndarray | None = None) -> AudioResult:
    n_sec = max(1, int(np.ceil(suspect_duration)))
    if not suspect_wav or not original_wav:
        return AudioResult(0.0, np.zeros(n_sec, dtype=bool),
                           suspect_duration, False, True)
    ratio, matched = fingerprint_overlap(
        suspect_wav, original_wav, suspect_duration, visual_offset_seconds)
    has_speech, silent = detect_speech(suspect_wav)
    return AudioResult(ratio, matched, suspect_duration, has_speech, silent)


def audio_score(res: AudioResult) -> float:
    """声音得分 = 重合原声时长占比 × 100（用于第二轮解说替代触发判定）。"""
    return min(100.0, res.overlap_ratio * 100)


def commentary_penalty(res: AudioResult, visual_overlap: float) -> scoring.ScoreItem:
    """第二轮【音频对抗】解说替代规避惩罚 —— 固定 +35 分。

    触发条件：画面一致帧占比 ≥40% 且声音得分 <30（剥离原声），
    且 VAD 检出持续第三方/AI 人声配音。
    """
    a_score = audio_score(res)
    triggered = (visual_overlap >= scoring.COMMENTARY_VISUAL_MIN
                 and a_score < scoring.COMMENTARY_AUDIO_SCORE_MAX
                 and res.has_speech)
    kind = "连续人声（解说/配音）" if res.has_speech else (
        "静音处理" if res.silent else "替换为BGM等非原声音频")
    if triggered:
        measured = f"画面一致帧占比 {visual_overlap:.1%}、声音得分 {a_score:.0f}，VAD检出{kind}"
    else:
        measured = f"未触发（画面 {visual_overlap:.1%} / 声音得分 {a_score:.0f} / 人声{'有' if res.has_speech else '无'}）"
    return scoring.ScoreItem(
        key="commentary",
        label="【音频对抗】解说替代规避惩罚",
        measured=measured,
        points=scoring.PENALTY_COMMENTARY if triggered else 0,
        triggered=triggered,
        detail="高危“消音洗片”对抗：画面一致帧占比≥40%且声音得分<30，"
               "VAD检出持续第三方/AI人声，利用新音轨掩盖剽窃画面事实，主观规避故意明显",
    )

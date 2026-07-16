"""分析总调度：抽帧 → 画面匹配 → 时间轴 → 声音 → 遮挡
   → 第一轮熔断 / 第二轮惩罚累加 → 分级证据 → 报告。"""
import json
import traceback
from pathlib import Path

import numpy as np

from . import audio as audio_mod
from . import scoring
from .frames import extract_audio, extract_frames, has_audio_stream
from .fingerprint import retrieve_candidates
from .occlusion import score_occlusion
from .report import (copy_occlusion_evidence, generate_report,
                     make_evidence_pairs)
from .timeline import analyze_timeline, sequential_penalty
from .visual import match_frames, overlap_measured

STAGES = ["抽帧与预处理", "AI视频指纹检索", "画面主轨勘验", "时间轴主轨勘验",
          "声音主轨勘验", "副轨遮挡与伪装检测", "两轮判定与报告生成"]


def run_job(job_dir: Path, original_path: str, suspect_path: str,
            progress: dict, include_report: bool = True,
            thresholds: dict | None = None) -> None:
    """progress 为共享 dict：{stage, status, error}；结果写入 job_dir/result.json。"""
    try:
        progress.update(stage=STAGES[0], status="running")
        original = extract_frames(original_path, job_dir / "frames_original",
                                  fps=scoring.ORIGINAL_FPS)
        suspect = extract_frames(suspect_path, job_dir / "frames_suspect",
                                 fps=scoring.SUSPECT_FPS)

        progress.update(stage=STAGES[1])
        fingerprint = retrieve_candidates(
            suspect, original, job_dir.parent / "_fingerprint_cache")

        progress.update(stage=STAGES[2])
        match = match_frames(suspect, original,
                             candidates=fingerprint.candidates,
                             fingerprint_region=fingerprint.selected_region)

        progress.update(stage=STAGES[3])
        tl = analyze_timeline(match, suspect.timestamps, suspect.duration)
        # 【浓度熔断】口径：污染秒数占【原片】总时长
        density_ratio = tl["polluted_count"] / max(1.0, original.duration)

        progress.update(stage=STAGES[4])
        s_wav = o_wav = None
        if has_audio_stream(suspect_path):
            s_wav = str(extract_audio(suspect_path, job_dir / "suspect.wav"))
        if has_audio_stream(original_path):
            o_wav = str(extract_audio(original_path, job_dir / "original.wav"))
        n_audio_seconds = max(1, int(np.ceil(suspect.duration)))
        visual_audio_offsets = np.full(n_audio_seconds, np.nan)
        for sec in range(n_audio_seconds):
            indices = np.flatnonzero(
                match.matched & (suspect.timestamps.astype(int) == sec))
            if len(indices):
                original_indices = match.best_original[indices]
                visual_audio_offsets[sec] = float(np.median(
                    original.timestamps[original_indices]
                    - suspect.timestamps[indices]))
        audio_res = audio_mod.analyze_audio(
            s_wav, o_wav, suspect.duration, visual_audio_offsets)
        progress.update(stage=STAGES[5])
        occ_items, occ_ev = score_occlusion(
            suspect, original, match, thresholds)

        progress.update(stage=STAGES[6])
        metrics = scoring.Metrics(
            visual_ratio=match.overlap_ratio,
            audio_ratio=audio_res.overlap_ratio,
            longest_seconds=tl["longest_seconds"],
            density_ratio=density_ratio,
            polluted_seconds=tl["polluted_count"],
            original_duration=original.duration,
            sequential_order_rate=tl["order_rate"],
            segment_count=tl["segment_count"],
            notes={"mirrored_frames": int(match.mirrored.sum()),
                   "embedded_frames": int(match.embedded.sum()),
                   "geometric_frames": int(match.geometric.sum()),
                   "embedded_region": match.embedded_region,
                   "fingerprint_model": fingerprint.model,
                   "fingerprint_region": fingerprint.selected_region,
                   "fingerprint_cache_hit": fingerprint.cache_hit,
                   "fingerprint_mean_similarity": round(
                       float(fingerprint.best_similarity.mean()), 4),
                   "audio_matched_seconds": int(
                       audio_res.matched_seconds.sum()),
                   "visual_longest_seconds": tl["longest_seconds"]},
        )
        # 第二轮四个固定惩罚项：时序 / 解说 / 字幕 / 水印
        penalties = [
            sequential_penalty(tl["order_rate"], tl["segment_count"]),
            audio_mod.commentary_penalty(audio_res, match.overlap_ratio),
            *occ_items,
        ]
        result = scoring.evaluate(metrics, penalties, thresholds)
        result["thresholds"] = {**scoring.STANDARD_THRESHOLDS,
                                **(thresholds or {})}

        result["evidence"] = _tiered_evidence(
            result, suspect, original, match, occ_ev,
            tl["polluted"], audio_res.matched_seconds, job_dir)
        result["meta"] = {
            "suspect_duration": round(suspect.duration, 1),
            "original_duration": round(original.duration, 1),
            "suspect_frames": len(suspect.tiles),
            "original_frames": len(original.tiles),
            "visual_measured": overlap_measured(match),
            "fingerprint_model": fingerprint.model,
            "fingerprint_cache_hit": fingerprint.cache_hit,
            "fingerprint_mean_similarity": round(
                float(fingerprint.best_similarity.mean()), 4),
            "audio_matched_seconds": int(audio_res.matched_seconds.sum()),
            "audio_overlap_ratio": round(audio_res.overlap_ratio, 4),
        }
        if include_report:
            report_text, report_source = generate_report(result)
            result["report"] = {"text": report_text, "source": report_source}

        (job_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        progress.update(stage="完成", status="done")
    except Exception as e:
        traceback.print_exc()
        progress.update(status="error", error=f"{type(e).__name__}: {e}")


def _tiered_evidence(result: dict, suspect, original, match, occ_ev,
                     polluted: np.ndarray, audio_seconds: np.ndarray,
                     job_dir: Path) -> dict:
    """按风险标签分发物证固证锚点。

    红：熔断/最高重合点“原片vs涉案片”对齐截图 + 遮挡命中帧
    黄：仅时间轴重合热力图（不展示具体重合比例）
    灰：仅封存勘验日志，不进行截图提取
    """
    color = result["tier"]["color"]
    evidence_dir = job_dir / "evidence"
    n_sec = len(polluted)
    audio_sec = np.asarray(audio_seconds, dtype=bool)
    if len(audio_sec) < n_sec:
        audio_sec = np.pad(audio_sec, (0, n_sec - len(audio_sec)))
    heat = (polluted | audio_sec[:n_sec]).astype(int).tolist()

    ev: dict = {"heatmap": {
                    "seconds": heat,
                    "visual_seconds": polluted.astype(int).tolist(),
                    "audio_seconds": audio_sec[:n_sec].astype(int).tolist(),
                    "duration": n_sec,
                },
                "audit_log": _audit_log(result)}
    if color == "red":
        ev["pairs"] = make_evidence_pairs(suspect, original, match, evidence_dir)
        ev.update(copy_occlusion_evidence(occ_ev, evidence_dir))
    return ev


def _audit_log(result: dict) -> list[str]:
    """基础勘验操作日志（灰色标签仅封存此日志，保证证据链只读完整性）。"""
    log = ["勘验通道：画面pHash / 声学指纹 / 连续时长 / 盗用浓度 / 时序一致性 / 遮挡与VAD"]
    if result["fused"]:
        for f in result["fuses"]:
            log.append(f"第一轮主轨硬熔断触发：【{f['name']}】{f['detail']}")
    else:
        log.append(f"第一轮未触发熔断；第二轮总惩罚分 "
                   f"{result['round2']['penalty_total']}"
                   f"（阈值 {result['round2']['threshold']}）")
        for p in result["round2"]["penalties"]:
            flag = f"+{p['points']}" if p["triggered"] else "未触发"
            log.append(f"{p['label']}：{flag}｜{p['measured']}")
    log.append(f"风险标签判定：{result['tier']['label']}")
    return log

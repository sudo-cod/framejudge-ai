"""证据截图提取 + 《客观特征比对报告》生成（DeepSeek，失败降级为模板）。"""
import json
import os
import shutil
from pathlib import Path

import cv2
import numpy as np
from dotenv import load_dotenv

from .frames import FrameSet
from .visual import VisualMatch

# Load only this project's .env. Existing process environment variables take
# precedence because python-dotenv defaults to override=False.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

MAX_EVIDENCE_PAIRS = 4
CASE_SUMMARY_VERSION = 2

REPORT_SYSTEM_PROMPT = """你是一名视频比对取证系统的报告撰写助手。
根据给定的两轮判定JSON，撰写一份中文《客观特征比对报告》。

铁律（违反即报告无效）：
1. 只描述客观技术特征（帧匹配率、声学指纹重合度、连续时长、盗用浓度、时序一致性、
   检出的遮挡痕迹等），绝对不做法律定性 —— 禁止使用"侵权成立""构成侵权""抄袭者"
   "违法"等法律结论性措辞。风险标签只是系统客观计分标签，可以引用，
   但要注明"不构成法律意见，不越权定性"。
2. 数字必须与JSON一致，不得编造。
3. 判定为两轮制：第一轮主轨硬熔断（一票否决直接标红）；若未熔断，第二轮为固定惩罚分
   累加，总惩罚≥40标黄挂起，否则灰色。若 fused 为 true，须写明触发了哪条主轨硬熔断，
   且不再讨论第二轮分数。
4. 结构：一、比对概况；二、第一轮主轨熔断勘验（画面/声音/连续时长/浓度四项测量与是否踩线）；
   三、第二轮次级勘验惩罚（时序对抗/解说替代/字幕遮挡/水印遮挡，逐项列出是否触发及固定分）；
   四、风险标签与法务动作指引。语言严谨简洁，500-800字。"""


def make_evidence_pairs(suspect: FrameSet, original: FrameSet,
                        match: VisualMatch, outdir: Path) -> list[dict]:
    """挑选高危触发点，生成原版/涉案并排对比图。"""
    outdir.mkdir(parents=True, exist_ok=True)
    idx = np.flatnonzero(match.matched)
    if len(idx) == 0:
        return []
    # 取分布均匀的样本中距离最小（最吻合）的几帧
    sample = idx[np.linspace(0, len(idx) - 1, min(len(idx), 24)).astype(int)]
    best = sample[np.argsort(match.best_distance[sample])][:MAX_EVIDENCE_PAIRS]
    pairs = []
    for k, i in enumerate(sorted(best)):
        s_img = cv2.imread(suspect.paths[i])
        o_img = cv2.imread(original.paths[match.best_original[i]])
        if s_img is None or o_img is None:
            continue
        h = min(s_img.shape[0], o_img.shape[0])
        s_img = cv2.resize(s_img, (int(s_img.shape[1] * h / s_img.shape[0]), h))
        o_img = cv2.resize(o_img, (int(o_img.shape[1] * h / o_img.shape[0]), h))
        gap = np.full((h, 6, 3), 255, dtype=np.uint8)
        combo = np.hstack([o_img, gap, s_img])
        name = f"pair_{k:02d}.jpg"
        cv2.imwrite(str(outdir / name), combo)
        pairs.append({
            "image": name,
            "original_time": _tc(original.timestamps[match.best_original[i]]),
            "suspect_time": _tc(suspect.timestamps[i]),
            "match_tiles": f"{int(match.match_tiles[i])}/{int(match.valid_tiles[i])} 有效块命中",
            "mirrored": bool(match.mirrored[i]),
            "embedded": bool(match.embedded[i]),
            "geometric": bool(match.geometric[i]),
        })
    return pairs


def copy_occlusion_evidence(ev, outdir: Path) -> dict:
    """把遮挡检测命中的帧复制进证据目录。"""
    outdir.mkdir(parents=True, exist_ok=True)
    out = {"subtitle": [], "watermark": []}
    for k, h in enumerate(ev.subtitle_hits[:MAX_EVIDENCE_PAIRS]):
        name = f"subtitle_{k:02d}.jpg"
        shutil.copy(h["suspect_frame"], outdir / name)
        out["subtitle"].append({"image": name, "texts": h["texts"],
                                "suspect_time": _tc(h["suspect_time"])})
    for k, h in enumerate(ev.watermark_hits[:MAX_EVIDENCE_PAIRS]):
        name = f"watermark_{k:02d}.jpg"
        shutil.copy(h["suspect_frame"], outdir / name)
        out["watermark"].append({"image": name, "corner": h["corner"],
                                 "kind": h["kind"],
                                 "suspect_time": _tc(h["suspect_time"])})
    return out


def _tc(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60:02d}:{s % 60:02d}"


def _template_report(result: dict) -> str:
    m = result["metrics"]
    lines = ["《客观特征比对报告》（模板版）", "",
             "注：本报告仅呈现客观技术比对特征，风险标签为系统客观计分标签，"
             "不构成法律意见，不越权定性。", ""]
    lines.append(f"风险标签：{result['tier']['label']}（{result['tier']['color']}）")
    lines.append(f"检测结论：{result['tier']['conclusion']}")
    lines.append("")

    lines.append("■ 第一轮 · 主轨高危熔断勘验（一票否决）")
    fused_keys = {f["key"] for f in result["fuses"]}
    rows = [
        ("画面熔断", f"一致帧占涉案总帧数 {m['visual_ratio']:.1%}", "visual", "≥80%"),
        ("声音熔断", f"重合原声占涉案总时长 {m['audio_ratio']:.1%}", "audio", "≥70%"),
        ("时长熔断", f"最长连续相同片段 {m['longest_seconds']:.0f} 秒", "continuous", "≥30秒"),
        ("浓度熔断", f"污染秒数 {m['polluted_seconds']} 占原片总时长 {m['density_ratio']:.1%}",
         "density", "≥30%"),
    ]
    for name, measured, key, thr in rows:
        flag = "★踩线熔断" if key in fused_keys else "未踩线"
        lines.append(f"  - 【{name}】{measured}（阈值 {thr}）→ {flag}")
    lines.append("")

    if result["fused"]:
        lines.append("判定：已触发主轨硬熔断，一票否决直接标红，第二轮不再计算。")
    else:
        r2 = result["round2"]
        lines.append(f"■ 第二轮 · 次级勘验固定惩罚累加（基准 0 分）")
        for p in r2["penalties"]:
            flag = f"触发 +{p['points']}" if p["triggered"] else "未触发 +0"
            lines.append(f"  - {p['label']}：{flag}｜{p['measured']}")
            if p["triggered"]:
                lines.append(f"    法理依据：{p['detail']}")
        lines.append("")
        lines.append(f"第二轮总惩罚分：{r2['penalty_total']} 分"
                     f"（阈值 {r2['threshold']}：≥则黄色挂起，否则灰色）")
    lines.append("")
    lines.append(f"法务动作指引：{result['tier']['action']}")
    return "\n".join(lines)


def generate_report(result: dict) -> tuple[str, str]:
    """返回 (报告文本, 来源: deepseek|template)。"""
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if api_key:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
            resp = client.chat.completions.create(
                model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
                messages=[
                    {"role": "system", "content": REPORT_SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(result, ensure_ascii=False)},
                ],
                temperature=0.3, timeout=60,
            )
            text = resp.choices[0].message.content
            if text and text.strip():
                return text.strip(), "deepseek"
        except Exception:
            pass
    return _template_report(result), "template"


CASE_REPORT_SYSTEM_PROMPT = """你是视频版权技术取证系统的案件摘要助手。
输入包含一个案件、一个原视频，以及多个涉案视频的独立技术比对结果。
请只生成一段覆盖整个案件的中文总结，不要逐一复述表格数据，不要写章节、标题、列表或免责声明。
总结应指出总体风险分布、最值得复核的涉案视频、主要共同特征及明显异常值。
只描述客观技术特征，不作法律定性，不得编造输入中没有的数据。
全文控制在 150-250 个汉字，便于与图表一起快速阅读。"""


def _template_case_report(case: dict, results: list[dict],
                          errors: list[dict]) -> str:
    counts = {"red": 0, "yellow": 0, "gray": 0}
    for item in results:
        color = item["result"]["tier"]["color"]
        counts[color] = counts.get(color, 0) + 1
    high_risk = [item["filename"] for item in results
                 if item["result"]["tier"]["color"] == "red"]
    focus = "、".join(high_risk[:3]) or "暂无高风险视频"
    failed = f"，另有 {len(errors)} 个分析失败" if errors else ""
    return (
        f"本案共完成 {len(results)} 个涉案视频比对{failed}。风险分布为高风险 "
        f"{counts['red']} 个、中风险 {counts['yellow']} 个、低风险 {counts['gray']} 个。"
        f"建议优先复核：{focus}。重点结合总览指标、逐秒时间轴及代表性对齐截图，"
        "核验画面、原声和连续片段的重合情况。"
    )


def generate_case_report(case: dict, results: list[dict],
                         errors: list[dict]) -> tuple[str, str]:
    """Generate exactly one consolidated report for an entire case."""
    # Send only report-relevant metrics—not evidence images, heatmap arrays,
    # local paths, extracted frames, or uploaded media—to the LLM.
    comparisons = []
    for item in results:
        result = item["result"]
        metrics = result["metrics"]
        comparisons.append({
            "filename": item["filename"],
            "tier": result["tier"],
            "decision": result["decision"],
            "fuses": result.get("fuses", []),
            "metrics": {
                "visual_ratio": metrics["visual_ratio"],
                "audio_ratio": metrics["audio_ratio"],
                "longest_seconds": metrics["longest_seconds"],
                "polluted_seconds": metrics["polluted_seconds"],
                "density_ratio": metrics["density_ratio"],
                "sequential_order_rate": metrics.get("sequential_order_rate"),
                "segment_count": metrics.get("segment_count"),
                "mirrored_frames": metrics.get("mirrored_frames", 0),
                "embedded_frames": metrics.get("embedded_frames", 0),
                "geometric_frames": metrics.get("geometric_frames", 0),
            },
            "round2": result.get("round2", {}),
        })
    payload = {"case": case, "comparisons": comparisons, "errors": errors}
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if api_key:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
            response = client.chat.completions.create(
                model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
                messages=[
                    {"role": "system", "content": CASE_REPORT_SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                temperature=0.2,
                timeout=60,
            )
            text = response.choices[0].message.content
            if text and text.strip():
                return text.strip(), "deepseek"
        except Exception:
            pass
    return _template_case_report(case, results, errors), "template"

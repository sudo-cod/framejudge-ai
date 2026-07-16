"""计分规则与阈值表 —— 全系统唯一的阈值定义处（《评分标准·两轮制》）。

两轮判定，系统只输出三级客观风险标签，不做法律定性：

第一轮 · 主轨高危熔断（一票否决，不看权重不看综合分）：
  任一踩线 → 直接熔断，输出红色「高度侵权风险」。
    【画面熔断】一致帧占涉案总帧数 ≥80%
    【声音熔断】重合原声占涉案总时长 ≥70%
    【时长熔断】最长连续相同片段 ≥30 秒
    【浓度熔断】盗用污染秒数占【原片】总时长 ≥30%

第二轮 · 次级勘验（仅当未触发第一轮熔断）：
  初始基准 0 分，固定惩罚分累加；总惩罚 ≥40 → 黄色挂起，<40 → 灰色。
    【时序对抗】剧情顺叙替代  +45
    【音频对抗】解说替代规避  +35
    【画面对抗】非原生字幕遮挡 +20
    【画面对抗】边角水印遮挡   +10
"""
from dataclasses import dataclass, field

from .settings import STANDARD_THRESHOLDS

# ---------- 第一轮：主轨硬熔断阈值 ----------
FUSE_VISUAL_RATIO = 0.80        # 一致帧占涉案总帧数
FUSE_AUDIO_RATIO = 0.70         # 重合原声占涉案总时长
FUSE_CONTINUOUS_SECONDS = 30    # 最长连续相同片段（秒）
FUSE_DENSITY_RATIO = 0.30       # 盗用污染秒数占【原片】总时长

# ---------- 第二轮：固定惩罚分 ----------
PENALTY_SEQUENTIAL = 45   # 【时序对抗】剧情顺叙替代
PENALTY_COMMENTARY = 35   # 【音频对抗】解说替代规避
PENALTY_SUBTITLE = 20     # 【画面对抗】非原生字幕遮挡
PENALTY_WATERMARK = 10    # 【画面对抗】边角水印遮挡
PENALTY_YELLOW_MIN = 40   # 总惩罚分 ≥ 此值 → 黄色挂起，否则灰色

# 第二轮触发条件
SEQUENTIAL_ORDER_THRESHOLD = 0.80  # 离散重合片段在原片时间戳上的正序对率 ≥80%
SEQUENTIAL_MIN_SEGMENTS = 2        # 至少 2 个离散片段才有时序可言
COMMENTARY_VISUAL_MIN = 0.40       # 画面一致帧占比 ≥40%
COMMENTARY_AUDIO_SCORE_MAX = 30    # 且声音得分 <30（重合原声占比×100）

# ---------- 分块缩略图匹配与抽帧 ----------
# 每帧切成 36 块（6×6），每块用 8×8 灰度缩略图；逐块比较平均绝对误差(MAE)，
# 且只在「双方都有纹理的块」上计算相似度：被遮挡（白块/字幕条）或纯色的块两端剔除，
# 不计入分子分母，故对局部遮挡免疫。
# 一致帧判定 = 有效块命中比例 ≥ TILE_MATCH_FRACTION 且有效块数 ≥ TILE_MIN_VALID。
TILE_MAE_THRESHOLD = 25       # 单块 8×8 灰度缩略图平均绝对误差 ≤25(0-255) 判为该块命中
TILE_MATCH_FRACTION = 0.65    # 有效块命中比例 ≥65% → 一致帧（对局部遮挡留裕度）
TILE_MIN_VALID = 8            # 有效块少于此数无法可靠判定 → 不计一致帧
SUSPECT_FPS = 3               # 涉案视频抽帧率（满足 1 秒 3 帧定义）
ORIGINAL_FPS = 3              # 原版帧库抽帧率（与涉案一致，避免快速运动画面因采样错位漏配）
FRAMES_PER_POLLUTED_SECOND = 2  # 3fps 中至少 2 帧一致 → 视觉污染秒（容忍单帧漏检）
CONTINUOUS_GAP_TOLERANCE = 2    # 连续片段允许 1-2 秒的系统容错断层

# ---------- 三级客观风险标签（前端去分数化封装）----------
TIERS = {
    "red": {
        "color": "red",
        "label": "高度侵权风险",
        "conclusion": "客观物理痕迹高度吻合。系统已成功穿透涉案视频的非线性剪辑、"
                      "消音和局部遮挡伪装。",
        "anchor": "系统自动提取触发熔断（或最高重合点）的画面关键帧，"
                  "生成“原片 vs 涉案片”对齐截图。",
        "action": "系统检测到客观痕迹高度吻合，建议法务团队审核后直接发起下架函发送"
                  "或批量诉讼立案准备。",
    },
    "yellow": {
        "color": "yellow",
        "label": "中等侵权风险",
        "conclusion": "音画存在部分拼接，或带有遮挡、替换音频等掩饰手段。"
                      "视频处于恶意洗片与公有领域引用的模糊交叉地带。",
        "anchor": "提供时间轴重合热力图，以黄色高亮标出涉案视频零星挪用原片素材的时间节点。",
        "action": "客观痕迹呈现部分重叠。由于可能涉及行业合理的碎片化二次创作，"
                  "系统已将该案件自动挂起至“人工复核池”，提示法务团队人工审查"
                  "其是否属于实质性替代，后再行制定维权策略。",
    },
    "gray": {
        "color": "gray",
        "label": "低风险 / 具备合理使用空间",
        "conclusion": "重叠比例极低且特征高度离散，未发现恶意洗片或反侦查遮挡痕迹。",
        "anchor": "仅封存基础勘验操作日志，确保证据链只读完整性，不进行截图提取。",
        "action": "客观痕迹有限，可能存在合理使用空间，请法务团队结合案件背景进一步审查。",
    },
}


@dataclass
class ScoreItem:
    """一个惩罚项 / 勘验项的展示单元。"""
    key: str
    label: str          # 项目名称
    measured: str       # 检测值（报告用，前端不展示）
    points: int         # 固定惩罚分（未触发为 0）
    triggered: bool
    detail: str = ""    # 底层判定 / 法理依据说明


@dataclass
class Metrics:
    """四条主轨的原始物理测量值 + 时序一致性。"""
    visual_ratio: float          # 一致帧占涉案总帧数比例
    audio_ratio: float           # 重合原声占涉案总时长比例
    longest_seconds: float       # 最长连续相同片段（秒）
    density_ratio: float         # 盗用污染秒数占【原片】总时长比例
    polluted_seconds: int        # 盗用污染秒数（涉案侧计数）
    original_duration: float     # 原片总时长（秒）
    sequential_order_rate: float # 离散重合片段正序对率
    segment_count: int           # 离散重合片段数
    notes: dict = field(default_factory=dict)


def _thresholds(values: dict | None) -> dict:
    return {**STANDARD_THRESHOLDS, **(values or {})}


def fuse_checks(m: Metrics, thresholds: dict | None = None) -> list[dict]:
    """第一轮：四条主轨硬熔断的逐项判定（含未触发项，供前端完整展示）。"""
    config = _thresholds(thresholds)
    visual = config["fuse_visual_ratio"]
    audio = config["fuse_audio_ratio"]
    continuous = config["fuse_continuous_seconds"]
    density = config["fuse_density_ratio"]
    rows = [
        ("visual", "画面熔断", m.visual_ratio >= visual,
         f"一致帧占涉案总帧数 {m.visual_ratio:.1%}", f"≥{visual:.0%}"),
        ("audio", "声音熔断", m.audio_ratio >= audio,
         f"重合原声占涉案总时长 {m.audio_ratio:.1%}", f"≥{audio:.0%}"),
        ("continuous", "时长熔断", m.longest_seconds >= continuous,
         f"最长连续画面确认片段 {m.longest_seconds:.0f} 秒",
         f"≥{continuous}秒"),
        ("density", "浓度熔断", m.density_ratio >= density,
         f"污染秒数 {m.polluted_seconds} 占原片总时长 {m.density_ratio:.1%}",
         f"≥{density:.0%}"),
    ]
    return [{"key": k, "name": name, "triggered": bool(hit),
             "measured": measured, "threshold": thr,
             "detail": f"{measured}（阈值 {thr}）"
                       + ("→ 直接熔断，输出红色" if hit else "→ 未踩线")}
            for k, name, hit, measured, thr in rows]


def check_fuses(m: Metrics, thresholds: dict | None = None) -> list[dict]:
    """仅返回已触发的熔断项（任一非空即红）。"""
    return [c for c in fuse_checks(m, thresholds) if c["triggered"]]


def _decision_summary(fuses: list[dict], penalty_total: int, color: str,
                      yellow_min: int) -> str:
    """一句话说明本次判定走的是哪条路径、为什么落到该标签。"""
    if fuses:
        names = "、".join(f["name"] for f in fuses)
        return (f"第一轮主轨硬熔断触发【{names}】，一票否决直接判定为"
                f"“{TIERS['red']['label']}”。")
    if color == "yellow":
        return (f"第一轮未触发任何熔断；第二轮次级勘验固定惩罚合计 {penalty_total} 分"
                f"（≥{yellow_min} 阈值），判定为“{TIERS['yellow']['label']}”。")
    return (f"第一轮未触发任何熔断；第二轮固定惩罚合计 {penalty_total} 分"
            f"（<{yellow_min} 阈值），判定为“{TIERS['gray']['label']}”。")


def evaluate(m: Metrics, penalties: list[ScoreItem],
             thresholds: dict | None = None) -> dict:
    """两轮判定 → 风险标签。

    penalties: 第二轮四个固定惩罚项（时序/解说/字幕/水印）。
    """
    config = _thresholds(thresholds)
    yellow_min = config["penalty_yellow_min"]
    checks = fuse_checks(m, config)
    fuses = [c for c in checks if c["triggered"]]
    penalty_total = sum(p.points for p in penalties if p.triggered)

    if fuses:
        color = "red"
        round2_reached = False
    elif penalty_total >= yellow_min:
        color = "yellow"
        round2_reached = True
    else:
        color = "gray"
        round2_reached = True

    return {
        "fused": bool(fuses),
        "fuses": fuses,
        "fuse_checks": checks,
        "decision": _decision_summary(fuses, penalty_total, color, yellow_min),
        "tier": dict(TIERS[color]),
        "round2": {
            "reached": round2_reached,
            "penalty_total": penalty_total,
            "threshold": yellow_min,
            "penalties": [vars(p) for p in penalties],
        },
        "metrics": {
            "visual_ratio": round(m.visual_ratio, 4),
            "audio_ratio": round(m.audio_ratio, 4),
            "longest_seconds": round(m.longest_seconds, 1),
            "density_ratio": round(m.density_ratio, 4),
            "polluted_seconds": m.polluted_seconds,
            "original_duration": round(m.original_duration, 1),
            "sequential_order_rate": round(m.sequential_order_rate, 4),
            "segment_count": m.segment_count,
            **m.notes,
        },
    }

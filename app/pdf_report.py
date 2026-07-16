"""Generate one polished, persistent PDF report for each case."""
from __future__ import annotations

import re
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.graphics.shapes import Drawing, Line, Rect, String
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (Image as ReportImage, KeepTogether, PageBreak,
                                Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

PDF_FILENAME = "case-report.pdf"
PDF_VERSION = 8
MAX_PDF_EVIDENCE_PER_VIDEO = 2
_FONT_REGULAR = "FrameJudgeCN"
_FONT_BOLD = "FrameJudgeCNBold"

_NAVY = colors.HexColor("#0F172A")
_SLATE = colors.HexColor("#334155")
_MUTED = colors.HexColor("#64748B")
_LINE = colors.HexColor("#DDE3EA")
_SOFT = colors.HexColor("#F8FAFC")
_BLUE = colors.HexColor("#1D4ED8")
_BLUE_SOFT = colors.HexColor("#EFF6FF")
_RED = colors.HexColor("#DC2626")
_RED_SOFT = colors.HexColor("#FEF2F2")
_AMBER = colors.HexColor("#CA8A04")
_AMBER_SOFT = colors.HexColor("#FEFCE8")
_GRAY_RISK = colors.HexColor("#64748B")
_GRAY_SOFT = colors.HexColor("#F1F5F9")


def _register_fonts() -> tuple[str, str]:
    windows = Path("C:/Windows/Fonts")
    regular = windows / "msyh.ttc"
    bold = windows / "msyhbd.ttc"
    try:
        if regular.is_file() and bold.is_file():
            pdfmetrics.registerFont(TTFont(_FONT_REGULAR, str(regular)))
            pdfmetrics.registerFont(TTFont(_FONT_BOLD, str(bold)))
            return _FONT_REGULAR, _FONT_BOLD
    except Exception:
        pass
    # Portable fallback supported by ReportLab's CJK font machinery.
    fallback = "STSong-Light"
    if fallback not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(fallback))
    return fallback, fallback


def _clean(value) -> str:
    text = str(value or "")
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)


def _paragraph(text: str, style) -> Paragraph:
    return Paragraph(escape(_clean(text)).replace("\n", "<br/>"), style)


def _page_chrome(canvas, doc) -> None:
    canvas.saveState()
    width, height = A4

    canvas.setFillColor(_NAVY)
    canvas.rect(0, height - 18 * mm, width, 18 * mm, fill=1, stroke=0)
    canvas.setFillColor(_BLUE)
    canvas.roundRect(18 * mm, height - 13 * mm, 8 * mm, 8 * mm,
                     1.5 * mm, fill=1, stroke=0)
    canvas.setFillColor(colors.white)
    canvas.setFont(doc.framejudge_bold_font, 10)
    canvas.drawCentredString(22 * mm, height - 10.6 * mm, "帧")
    canvas.setFont(doc.framejudge_bold_font, 10)
    canvas.drawString(29 * mm, height - 9.7 * mm, "FrameJudge")
    canvas.setFillColor(colors.HexColor("#94A3B8"))
    canvas.setFont(doc.framejudge_regular_font, 7.5)
    canvas.drawString(29 * mm, height - 13 * mm, "VIDEO FORENSIC ANALYSIS")
    canvas.setFillColor(colors.HexColor("#CBD5E1"))
    canvas.setFont(doc.framejudge_regular_font, 7.5)
    canvas.drawRightString(width - 18 * mm, height - 10.5 * mm,
                           f"案件编号  {doc.framejudge_case_id}")

    canvas.setStrokeColor(_LINE)
    canvas.line(18 * mm, 14 * mm, A4[0] - 18 * mm, 14 * mm)
    canvas.setFillColor(_MUTED)
    canvas.setFont(doc.framejudge_regular_font, 8)
    canvas.drawString(18 * mm, 9 * mm, "FrameJudge 技术比对报告")
    canvas.drawRightString(A4[0] - 18 * mm, 9 * mm, f"第 {doc.page} 页")
    canvas.restoreState()


def _display_datetime(value) -> str:
    text = _clean(value).replace("T", " ")
    return re.sub(r"([+-]\d\d:\d\d|Z)$", "", text) or "未记录"


def _section_heading(number: str, title: str, styles: dict) -> Table:
    badge = Table([[Paragraph(number, styles["section_number"])]],
                  colWidths=[9 * mm], rowHeights=[9 * mm])
    badge.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _BLUE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("ROUNDEDCORNERS", [2 * mm]),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    heading = Table([[badge, Paragraph(title, styles["section_title"])]],
                    colWidths=[12 * mm, 158 * mm])
    heading.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return heading


def _narrative_flowables(text: str, styles: dict) -> list:
    flowables = []
    for raw in _clean(text).splitlines():
        line = raw.strip()
        if not line:
            flowables.append(Spacer(1, 3 * mm))
            continue
        if line in ("《案件视频客观特征综合比对报告》",
                    "《案件视频客观特征综合比对报告》（模板版）"):
            continue
        if line.startswith("本报告仅呈现客观技术比对特征"):
            # A consistently styled disclaimer is appended after the table.
            continue
        if line.startswith("### "):
            flowables.append(_paragraph(line[4:], styles["h3"]))
        elif line.startswith("## "):
            flowables.append(_paragraph(line[3:], styles["h2"]))
        elif line.startswith("# "):
            flowables.append(_paragraph(line[2:], styles["h1"]))
        elif re.match(r"^[一二三四五六七八九十]+[、.]", line):
            flowables.append(_paragraph(line, styles["h2"]))
        elif line.startswith(("- ", "* ", "• ")):
            flowables.append(_paragraph("• " + line[2:], styles["bullet"]))
        else:
            flowables.append(_paragraph(line, styles["body"]))
    return flowables


def _timecode(seconds: float) -> str:
    value = max(0, int(round(seconds)))
    return f"{value // 60:02d}:{value % 60:02d}"


def _timeline_drawing(heatmap: dict, regular_font: str) -> Drawing:
    width = 170 * mm
    height = 22 * mm
    drawing = Drawing(width, height)
    visual = np.asarray(heatmap.get("visual_seconds") or
                        heatmap.get("seconds") or [], dtype=bool)
    audio = np.asarray(heatmap.get("audio_seconds") or
                       np.zeros(len(visual), dtype=bool), dtype=bool)
    total = max(len(visual), len(audio), 1)
    if len(visual) < total:
        visual = np.pad(visual, (0, total - len(visual)))
    if len(audio) < total:
        audio = np.pad(audio, (0, total - len(audio)))
    colors_by_state = {
        (False, False): colors.HexColor("#E2E8F0"),
        (True, False): colors.HexColor("#F59E0B"),
        (False, True): colors.HexColor("#0EA5E9"),
        (True, True): colors.HexColor("#7C3AED"),
    }
    bins = min(total, 240)
    bar_y, bar_h = 25, 11
    for index in range(bins):
        start = int(index * total / bins)
        end = max(start + 1, int((index + 1) * total / bins))
        v = bool(visual[start:end].mean() >= 0.34)
        a = bool(audio[start:end].mean() >= 0.34)
        x0 = index * width / bins
        x1 = (index + 1) * width / bins
        drawing.add(Rect(x0, bar_y, max(0.6, x1 - x0 + 0.1), bar_h,
                         fillColor=colors_by_state[(v, a)], strokeColor=None))
    drawing.add(Rect(0, bar_y, width, bar_h, fillColor=None,
                     strokeColor=colors.HexColor("#CBD5E1"), strokeWidth=.5))
    duration = float(heatmap.get("duration") or total)
    for fraction in (0, .25, .5, .75, 1):
        x = fraction * width
        drawing.add(Line(x, bar_y - 2, x, bar_y,
                         strokeColor=colors.HexColor("#8A949D"), strokeWidth=.4))
        anchor = "start" if fraction == 0 else "end" if fraction == 1 else "middle"
        drawing.add(String(x, 10, _timecode(duration * fraction),
                           fontName=regular_font, fontSize=7,
                           fillColor=_MUTED, textAnchor=anchor))
    legend = [
        ("画面命中", colors_by_state[(True, False)]),
        ("原声命中", colors_by_state[(False, True)]),
        ("音画同时命中", colors_by_state[(True, True)]),
        ("未命中", colors_by_state[(False, False)]),
    ]
    x = 0
    for label, color in legend:
        drawing.add(Rect(x, 48, 8, 6, fillColor=color, strokeColor=None))
        drawing.add(String(x + 11, 47, label, fontName=regular_font, fontSize=7,
                           fillColor=_MUTED))
        x += 94
    return drawing


def _metric_bar(value: float, color: colors.Color,
                regular_font: str) -> Drawing:
    width, height = 22 * mm, 7 * mm
    drawing = Drawing(width, height)
    ratio = max(0.0, min(1.0, float(value or 0)))
    drawing.add(Rect(0, 4, width, 9, fillColor=colors.HexColor("#E2E8F0"),
                     strokeColor=None))
    drawing.add(Rect(0, 4, width * ratio, 9, fillColor=color,
                     strokeColor=None))
    drawing.add(String(width, 16, f"{ratio:.0%}", fontName=regular_font,
                       fontSize=7, fillColor=_SLATE, textAnchor="end"))
    return drawing


def _risk_rank(item: dict) -> int:
    return {"red": 0, "yellow": 1, "gray": 2}.get(
        item.get("result", {}).get("tier", {}).get("color"), 9)


def _evidence_cards(case_dir: Path, item: dict, styles: dict) -> list:
    result = item["result"]
    evidence = result.get("evidence", {})
    result_id = _clean(item.get("result_id"))
    evidence_dir = case_dir / result_id / "evidence"
    records: list[tuple[Path, str]] = []
    for pair in evidence.get("pairs", []):
        flags = []
        if pair.get("mirrored"):
            flags.append("镜像")
        if pair.get("embedded"):
            flags.append("画中画/背景图")
        if pair.get("geometric"):
            flags.append("几何验证")
        suffix = f"（{'、'.join(flags)}）" if flags else ""
        records.append((
            evidence_dir / Path(_clean(pair.get("image"))).name,
            (f"原片 {pair.get('original_time', '')} / 涉案片 "
             f"{pair.get('suspect_time', '')}{suffix}"),
        ))
    for subtitle in evidence.get("subtitle", []):
        records.append((
            evidence_dir / Path(_clean(subtitle.get("image"))).name,
            f"疑似二次字幕 @{subtitle.get('suspect_time', '')}",
        ))
    for watermark in evidence.get("watermark", []):
        records.append((
            evidence_dir / Path(_clean(watermark.get("image"))).name,
            (f"{watermark.get('corner', '')}{watermark.get('kind', '')} "
            f"@{watermark.get('suspect_time', '')}"),
        ))
    records = records[:MAX_PDF_EVIDENCE_PER_VIDEO]

    cells = []
    for path, caption in records:
        if not path.is_file() or path.parent.resolve() != evidence_dir.resolve():
            continue
        image = ReportImage(str(path))
        scale = min((79 * mm) / image.imageWidth,
                    (34 * mm) / image.imageHeight, 1.0)
        image.drawWidth = image.imageWidth * scale
        image.drawHeight = image.imageHeight * scale
        cells.append([image, Spacer(1, 2 * mm),
                      _paragraph(caption, styles["caption"])])
    if not cells:
        return [_paragraph("本视频未生成截图证据；时间轴与检测指标仍保留。",
                           styles["muted"])]
    rows = []
    for index in range(0, len(cells), 2):
        row = [cells[index]]
        row.append(cells[index + 1] if index + 1 < len(cells) else "")
        rows.append(row)
    gallery = Table(rows, colWidths=[84 * mm, 84 * mm], hAlign="LEFT")
    gallery.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, -1), _SOFT),
        ("BOX", (0, 0), (-1, -1), .5, _LINE),
        ("INNERGRID", (0, 0), (-1, -1), .5, _LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return [gallery]


def generate_case_pdf(case_dir: Path, case: dict, results: list[dict],
                      errors: list[dict], report_text: str,
                      report_source: str) -> Path:
    """Write and return the single PDF belonging to a case."""
    regular, bold = _register_fonts()
    output = case_dir / PDF_FILENAME
    doc = SimpleDocTemplate(
        str(output), pagesize=A4, rightMargin=18 * mm, leftMargin=18 * mm,
        topMargin=26 * mm, bottomMargin=20 * mm,
        title=f"{case.get('name', '案件')} - 综合比对报告",
        author="FrameJudge",
        subject="视频客观特征综合比对技术报告",
    )
    doc.framejudge_regular_font = regular
    doc.framejudge_bold_font = bold
    doc.framejudge_case_id = _clean(case.get("case_id", case_dir.name))
    sample = getSampleStyleSheet()
    styles = {
        "eyebrow": ParagraphStyle("Eyebrow", fontName=bold, fontSize=8,
            leading=11, textColor=_BLUE, spaceAfter=2 * mm, wordWrap="CJK"),
        "title": ParagraphStyle("CaseTitle", parent=sample["Title"],
            fontName=bold, fontSize=23, leading=31, textColor=_NAVY,
            alignment=TA_LEFT, spaceAfter=2 * mm, wordWrap="CJK"),
        "subtitle": ParagraphStyle("Subtitle", fontName=regular, fontSize=9,
            leading=14, textColor=_MUTED, alignment=TA_LEFT,
            spaceAfter=7 * mm, wordWrap="CJK"),
        "section_number": ParagraphStyle("SectionNumber", fontName=bold,
            fontSize=8, leading=9, textColor=colors.white,
            alignment=TA_CENTER, wordWrap="CJK"),
        "section_title": ParagraphStyle("SectionTitle", fontName=bold,
            fontSize=15, leading=20, textColor=_NAVY, wordWrap="CJK"),
        "h1": ParagraphStyle("H1CN", fontName=bold, fontSize=16, leading=22,
            textColor=_NAVY, spaceBefore=5 * mm,
            spaceAfter=3 * mm, wordWrap="CJK"),
        "h2": ParagraphStyle("H2CN", fontName=bold, fontSize=13, leading=19,
            textColor=_SLATE, spaceBefore=4 * mm,
            spaceAfter=2 * mm, wordWrap="CJK"),
        "h3": ParagraphStyle("H3CN", fontName=bold, fontSize=11, leading=17,
            textColor=_SLATE, spaceBefore=3 * mm,
            spaceAfter=1.5 * mm, wordWrap="CJK"),
        "body": ParagraphStyle("BodyCN", fontName=regular, fontSize=10,
            leading=17, textColor=_SLATE, alignment=TA_LEFT,
            spaceAfter=1.8 * mm, wordWrap="CJK"),
        "bullet": ParagraphStyle("BulletCN", fontName=regular, fontSize=10,
            leading=16, leftIndent=5 * mm, firstLineIndent=-3 * mm,
            textColor=_SLATE, spaceAfter=1.5 * mm,
            wordWrap="CJK"),
        "cell": ParagraphStyle("CellCN", fontName=regular, fontSize=8.5,
            leading=12, textColor=_SLATE, wordWrap="CJK"),
        "cell_bold": ParagraphStyle("CellBoldCN", fontName=bold, fontSize=8.5,
            leading=12, textColor=colors.white, wordWrap="CJK"),
        "meta_label": ParagraphStyle("MetaLabel", fontName=regular, fontSize=8,
            leading=11, textColor=_MUTED, wordWrap="CJK"),
        "meta_value": ParagraphStyle("MetaValue", fontName=bold, fontSize=8.8,
            leading=13, textColor=_NAVY, wordWrap="CJK"),
        "summary": ParagraphStyle("Summary", fontName=regular, fontSize=10,
            leading=17, textColor=_SLATE, wordWrap="CJK"),
        "risk_number_red": ParagraphStyle("RiskNumberRed", fontName=bold,
            fontSize=19, leading=21, textColor=_RED, alignment=TA_LEFT),
        "risk_number_amber": ParagraphStyle("RiskNumberAmber", fontName=bold,
            fontSize=19, leading=21, textColor=_AMBER, alignment=TA_LEFT),
        "risk_number_gray": ParagraphStyle("RiskNumberGray", fontName=bold,
            fontSize=19, leading=21, textColor=_GRAY_RISK, alignment=TA_LEFT),
        "risk_label": ParagraphStyle("RiskLabel", fontName=regular,
            fontSize=8, leading=11, textColor=_MUTED, alignment=TA_LEFT),
        "video_title": ParagraphStyle("VideoTitle", fontName=bold,
            fontSize=11.5, leading=16, textColor=_NAVY, wordWrap="CJK"),
        "video_index": ParagraphStyle("VideoIndex", fontName=bold,
            fontSize=8, leading=10, textColor=_BLUE, alignment=TA_CENTER),
        "decision": ParagraphStyle("Decision", fontName=regular,
            fontSize=8.5, leading=13, textColor=_MUTED, wordWrap="CJK"),
        "caption": ParagraphStyle("CaptionCN", fontName=regular, fontSize=8,
            leading=12, textColor=_MUTED,
            alignment=TA_CENTER, wordWrap="CJK"),
        "muted": ParagraphStyle("MutedCN", fontName=regular, fontSize=9,
            leading=14, textColor=_MUTED, wordWrap="CJK"),
        "risk_red": ParagraphStyle("RiskRedCN", fontName=bold, fontSize=8.5,
            leading=12, textColor=colors.white, wordWrap="CJK"),
        "risk_yellow": ParagraphStyle("RiskYellowCN", fontName=bold, fontSize=8.5,
            leading=12, textColor=colors.white, wordWrap="CJK"),
        "risk_gray": ParagraphStyle("RiskGrayCN", fontName=bold, fontSize=8.5,
            leading=12, textColor=colors.white, wordWrap="CJK"),
    }

    counts = {"red": 0, "yellow": 0, "gray": 0}
    for item in results:
        color = item["result"]["tier"]["color"]
        counts[color] = counts.get(color, 0) + 1
    sorted_results = sorted(results, key=_risk_rank)

    story = [
        Paragraph("FRAMEJUDGE / 技术取证报告", styles["eyebrow"]),
        Paragraph("案件视频客观特征综合比对报告", styles["title"]),
        Paragraph("AI 辅助视频比对取证 - 客观指标、证据截图与时间轴综合呈现", styles["subtitle"]),
    ]
    metadata = [
        [_paragraph("案件名称", styles["meta_label"]),
         _paragraph(case.get("name", "未命名案件"), styles["meta_value"]),
         _paragraph("案件编号", styles["meta_label"]),
         _paragraph(case.get("case_id", case_dir.name), styles["meta_value"])],
        [_paragraph("原视频", styles["meta_label"]),
         _paragraph(case.get("original_filename", "未记录"), styles["meta_value"]),
         _paragraph("涉案视频", styles["meta_label"]),
         _paragraph(f"{len(results) + len(errors)} 个", styles["meta_value"])],
        [_paragraph("创建时间", styles["meta_label"]),
         _paragraph(_display_datetime(case.get("created_at")), styles["meta_value"]),
         _paragraph("报告来源", styles["meta_label"]),
         _paragraph("DeepSeek" if report_source == "deepseek" else "本地模板", styles["meta_value"])],
        [_paragraph("阈值方案", styles["meta_label"]),
         _paragraph(case.get("threshold_preset_label", "标准"), styles["meta_value"]),
         _paragraph("配置版本", styles["meta_label"]),
         _paragraph(f"v{case.get('threshold_revision', 1)}", styles["meta_value"])],
    ]
    meta_table = Table(metadata, colWidths=[24 * mm, 61 * mm, 24 * mm, 61 * mm])
    meta_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("BACKGROUND", (0, 0), (0, -1), _SOFT),
        ("BACKGROUND", (2, 0), (2, -1), _SOFT),
        ("FONTNAME", (0, 0), (-1, -1), regular),
        ("BOX", (0, 0), (-1, -1), 0.6, _LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.35, _LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 7.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7.5),
    ]))
    story.extend([meta_table, Spacer(1, 5 * mm),
                  Paragraph("风险分布", styles["h2"])])

    risk_specs = [
        ("高风险", counts["red"], "risk_number_red", _RED_SOFT, _RED),
        ("中风险", counts["yellow"], "risk_number_amber", _AMBER_SOFT, _AMBER),
        ("低风险", counts["gray"], "risk_number_gray", _GRAY_SOFT, _GRAY_RISK),
        ("分析失败", len(errors), "risk_number_gray", _SOFT, _LINE),
    ]
    risk_cards = []
    for label, count, number_style, background, accent in risk_specs:
        card = Table([[
            Paragraph(str(count), styles[number_style]),
            Paragraph(label, styles["risk_label"]),
        ]], colWidths=[13 * mm, 24 * mm], rowHeights=[15 * mm])
        card.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), background),
            ("LINEABOVE", (0, 0), (-1, 0), 2, accent),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        risk_cards.append(card)
    risk_table = Table([risk_cards], colWidths=[42.5 * mm] * 4)
    risk_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    summary_card = Table([[_paragraph(report_text, styles["summary"])]],
                         colWidths=[170 * mm])
    summary_card.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _BLUE_SOFT),
        ("LINEBEFORE", (0, 0), (0, -1), 3, _BLUE),
        ("BOX", (0, 0), (-1, -1), .4, colors.HexColor("#BFDBFE")),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.extend([risk_table, Spacer(1, 5 * mm),
                  Paragraph("执行摘要", styles["h2"]),
                  summary_card, Spacer(1, 7 * mm),
                  _section_heading("01", "涉案视频指标总览", styles),
                  Spacer(1, 3 * mm)])
    rows = [[_paragraph(value, styles["cell_bold"]) for value in
             ("风险标签", "涉案视频", "画面", "声音", "最长连续", "原片占比")]]
    risk_backgrounds = {
        "red": _RED,
        "yellow": _AMBER,
        "gray": _GRAY_RISK,
    }
    summary_styles = []
    for row_index, item in enumerate(sorted_results, 1):
        result = item["result"]
        metrics = result["metrics"]
        risk_color = result["tier"]["color"]
        risk_style = styles.get(f"risk_{risk_color}", styles["cell"])
        rows.append([
            _paragraph(result["tier"]["label"], risk_style),
            _paragraph(item["filename"], styles["cell"]),
            _metric_bar(metrics["visual_ratio"], _BLUE, regular),
            _metric_bar(metrics["audio_ratio"], colors.HexColor("#0EA5E9"), regular),
            _paragraph(f"{metrics['longest_seconds']:.0f} 秒", styles["cell"]),
            _metric_bar(metrics["density_ratio"], colors.HexColor("#7C3AED"), regular),
        ])
        if risk_color in risk_backgrounds:
            summary_styles.append(
                ("BACKGROUND", (0, row_index), (0, row_index),
                 risk_backgrounds[risk_color]))
    summary = Table(rows, repeatRows=1,
                    colWidths=[31 * mm, 39 * mm, 25 * mm, 25 * mm, 25 * mm, 25 * mm])
    summary.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _NAVY),
        ("BOX", (0, 0), (-1, -1), 0.5, _LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.35, _LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (2, 0), (-1, 0), "CENTER"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, _SOFT]),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ] + summary_styles))
    story.append(summary)
    if errors:
        failed = [Paragraph("未完成项目", styles["h2"])]
        failed.extend(_paragraph(f"• {x['filename']}：{x['error']}", styles["bullet"])
                      for x in errors)
        story.extend([Spacer(1, 5 * mm), KeepTogether(failed)])

    story.extend([PageBreak(),
                  _section_heading("02", "时间轴与代表性证据", styles),
                  Spacer(1, 2 * mm),
                  _paragraph("按风险等级排列。时间轴展示画面、原声及音画同时命中的分布，截图用于呈现代表性对齐证据。",
                             styles["muted"]),
                  Spacer(1, 3 * mm)])
    for index, item in enumerate(sorted_results, 1):
        result = item["result"]
        evidence = result.get("evidence", {})
        heatmap = evidence.get("heatmap", {})
        risk_color = result.get("tier", {}).get("color", "gray")
        risk_style = styles.get(f"risk_{risk_color}", styles["risk_gray"])
        risk_background = risk_backgrounds.get(risk_color, _GRAY_RISK)
        index_badge = Table([[Paragraph(f"{index:02d}", styles["video_index"])]],
                            colWidths=[9 * mm], rowHeights=[9 * mm])
        index_badge.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), _BLUE_SOFT),
            ("BOX", (0, 0), (-1, -1), .4, colors.HexColor("#BFDBFE")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        decision = result.get("decision") or result.get("tier", {}).get("conclusion") or "暂无判定说明。"
        result_header = Table([[
            index_badge,
            [Paragraph(_clean(item["filename"]), styles["video_title"]),
             _paragraph(decision, styles["decision"])],
            Paragraph(_clean(result.get("tier", {}).get("label", "未分类")), risk_style),
        ]], colWidths=[13 * mm, 127 * mm, 30 * mm])
        result_header.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (1, 0), _SOFT),
            ("BACKGROUND", (2, 0), (2, 0), risk_background),
            ("BOX", (0, 0), (-1, -1), .5, _LINE),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (2, 0), (2, 0), "CENTER"),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ]))
        block = [
            result_header,
            Spacer(1, 2.5 * mm),
            _timeline_drawing(heatmap, regular),
            Spacer(1, 2 * mm),
        ]
        block.extend(_evidence_cards(case_dir, item, styles))
        story.append(KeepTogether(block))
        story.append(Spacer(1, 6 * mm))

    disclaimer = Table([[
        _paragraph("本报告仅呈现客观技术比对特征，供法务及律师取证参考，"
                   "不构成法律意见，不对是否构成侵权作法律定性。", styles["body"]),
    ]], colWidths=[170 * mm])
    disclaimer.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _GRAY_SOFT),
        ("LINEBEFORE", (0, 0), (0, -1), 3, _GRAY_RISK),
        ("BOX", (0, 0), (-1, -1), .4, _LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.extend([Spacer(1, 4 * mm),
                  _section_heading("03", "报告声明", styles),
                  Spacer(1, 3 * mm), disclaimer])
    doc.build(story, onFirstPage=_page_chrome, onLaterPages=_page_chrome)
    return output

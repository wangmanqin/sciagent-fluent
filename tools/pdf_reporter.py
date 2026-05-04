"""
tools/pdf_reporter.py — W4 子模块 2: 自动 PDF 技术报告
============================================================
W1 出 report.txt + result.json + temperature_contour.png 三个产物.
W4 把这三个合成一份 PDF, 让导师 / 答辩台上能一键打开看完整的工程报告.

为什么用 reportlab 不用 weasyprint:
  reportlab 全 Python, 无外部依赖 (weasyprint 要 GTK + Pango, Windows 配起来烦).
  reportlab 4.x 中文字体直接走 TTF 注册, 思源宋体 / 微软雅黑都能用.

为什么不让 LLM 写 PDF:
  架构决策 #1: LLM 只输出 JSON. 模板渲染走 Python — 跟 result_analyzer.summary_text
  同样的哲学.

入口:
  from tools.pdf_reporter import generate_pdf_report
  pdf_path = generate_pdf_report(result_dict, iter_history=[...], out_dir="reports/run_xxx/")

测试:
  python -m tools.pdf_reporter   # 用 reports/ 下最新一次 run 生成 PDF
"""
import datetime
import glob
import json
import os
import sys
from typing import Optional


# ============================================================
# 字体注册 — 中文用 Windows 自带的微软雅黑
# ============================================================

def _register_chinese_font() -> str:
    """
    注册一个中文 TTF, 返回 fontName. 三层 fallback:
      1. C:/Windows/Fonts/msyh.ttc (微软雅黑, Win10/11 自带)
      2. C:/Windows/Fonts/simhei.ttf (黑体)
      3. reportlab 默认 Helvetica (中文会变方块)
    """
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    candidates = [
        ("MSYH", r"C:\Windows\Fonts\msyh.ttc"),
        ("SimHei", r"C:\Windows\Fonts\simhei.ttf"),
    ]
    for name, path in candidates:
        if os.path.isfile(path):
            try:
                pdfmetrics.registerFont(TTFont(name, path))
                return name
            except Exception:
                continue
    return "Helvetica"


# ============================================================
# 公开 API: generate_pdf_report
# ============================================================

def generate_pdf_report(
    result: dict,
    iter_history: Optional[list] = None,
    out_dir: Optional[str] = None,
    pdf_name: str = "report.pdf",
) -> str:
    """
    把 result + iter_history 渲染成 PDF.

    Args:
        result:        result_analyzer.summarize 返回的 dict (含 summary_text /
                       max_temp_K / outlet_avg_temp_C / contour_image_path 等)
        iter_history:  graph 的 state["iter_history"], W3 多轮才有, W1/W2 单程传 None.
        out_dir:       输出目录. None = 用 result["report_text_path"] 的目录.
        pdf_name:      PDF 文件名.

    Returns:
        生成的 PDF 绝对路径.
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle, PageBreak
    )

    # 决定输出位置
    if out_dir is None:
        rp = result.get("report_text_path")
        if rp and os.path.isfile(rp):
            out_dir = os.path.dirname(rp)
        else:
            out_dir = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "..", "reports", "pdf_only"
            )
    os.makedirs(out_dir, exist_ok=True)
    pdf_path = os.path.abspath(os.path.join(out_dir, pdf_name))

    # 字体 + 样式
    cn_font = _register_chinese_font()
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "CnTitle", parent=styles["Title"],
        fontName=cn_font, fontSize=18, leading=24, alignment=1,
    )
    h2_style = ParagraphStyle(
        "CnH2", parent=styles["Heading2"],
        fontName=cn_font, fontSize=13, leading=18, spaceBefore=12, spaceAfter=6,
    )
    body_style = ParagraphStyle(
        "CnBody", parent=styles["BodyText"],
        fontName=cn_font, fontSize=10, leading=15, spaceAfter=4,
    )
    mono_style = ParagraphStyle(
        "CnMono", parent=styles["Code"],
        fontName=cn_font, fontSize=9, leading=12,
    )

    doc = SimpleDocTemplate(
        pdf_path, pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
    )

    story = []

    # ---------- 封面区 ----------
    story.append(Paragraph("SciAgent-Fluent 仿真技术报告", title_style))
    story.append(Spacer(1, 0.3 * cm))
    gen_time = result.get("generated_at") or datetime.datetime.now().isoformat(timespec="seconds")
    case_mode = result.get("case_mode", "mixing_elbow")
    cover_lines = [
        f"<b>生成时间</b>: {gen_time}",
        f"<b>案例模式</b>: {case_mode}",
        f"<b>判定</b>: {'达标 [PASS]' if result.get('within_spec') else '未达标 [FAIL]'}",
    ]
    if iter_history:
        cover_lines.append(f"<b>迭代轮数</b>: {len(iter_history)} (W3 多轮)")
    for l in cover_lines:
        story.append(Paragraph(l, body_style))
    story.append(Spacer(1, 0.5 * cm))

    # ---------- 1. 输入参数 ----------
    story.append(Paragraph("1. 输入参数", h2_style))
    params = result.get("params_echo") or {}
    param_data = [
        ["字段", "值", "说明"],
        ["chip_power_watts", _fmt(params.get("chip_power_watts")), "芯片功耗 (W)"],
        ["inlet_velocity_ms", _fmt(params.get("inlet_velocity_ms")), "入口风速 (m/s)"],
        ["max_iterations", _fmt(params.get("max_iterations")), "最大迭代步数"],
        ["chip_material", _fmt(params.get("chip_material")), "芯片材料"],
        ["limit_C", _fmt(params.get("limit_C")) or "(默认)", "最高温度警戒线 (°C)"],
        ["outlet_limit_C", _fmt(params.get("outlet_limit_C")) or "(默认)", "出口平均警戒线 (°C)"],
        ["mesh_quality", _fmt(params.get("mesh_quality")), "网格档位"],
    ]
    story.append(_make_table(param_data, cn_font, col_widths=[4.5 * cm, 4 * cm, 8 * cm]))

    # ---------- 2. 求解结果 ----------
    story.append(Paragraph("2. 求解结果", h2_style))
    result_data = [
        ["指标", "值"],
        ["状态", "收敛" if result.get("converged") else "未收敛"],
        ["实际迭代步数", _fmt(result.get("iterations"))],
        ["全域最高温度 (K)", _fmt(result.get("max_temp_K"))],
        ["全域最高温度 (°C)", _fmt(result.get("max_temp_C"))],
        ["出口平均温度 (K)", _fmt(result.get("outlet_avg_temp_K"))],
        ["出口平均温度 (°C)", _fmt(result.get("outlet_avg_temp_C"))],
        ["耗时 (s)", _fmt(result.get("elapsed_seconds"))],
    ]
    if case_mode == "manifold_cht":
        result_data += [
            ["chip_power 是否真生效", str(result.get("chip_power_applied"))],
            ["体积热源 Q (W/m^3)", _fmt(result.get("chip_power_Q_volumetric"))],
        ]
    story.append(_make_table(result_data, cn_font, col_widths=[7 * cm, 9.5 * cm]))

    # ---------- 3. 警戒线判定 ----------
    story.append(Paragraph("3. 警戒线判定", h2_style))
    judge_data = [
        ["指标", "值"],
        ["max_temp 警戒线 (°C)", _fmt(result.get("limit_C"))],
        ["max_temp 是否达标", str(result.get("within_spec"))],
        ["max_temp 余量 (°C)", _fmt(result.get("margin_C"))],
        ["outlet 警戒线 (°C)", _fmt(result.get("outlet_limit_C"))],
        ["outlet 是否达标", str(result.get("outlet_within_spec"))],
        ["outlet 余量 (°C)", _fmt(result.get("outlet_margin_C"))],
    ]
    story.append(_make_table(judge_data, cn_font, col_widths=[7 * cm, 9.5 * cm]))

    # ---------- 4. W3 迭代历史 ----------
    if iter_history:
        story.append(Paragraph("4. 迭代历史 (W3)", h2_style))
        metric_label = "outlet_avg_temp_C" if case_mode == "mixing_elbow" else "max_temp_C"
        within_key = "outlet_within_spec" if case_mode == "mixing_elbow" else "within_spec"
        rows = [["round", "inlet_v (m/s)", f"{metric_label} (°C)", "达标", "action"]]
        for h in iter_history:
            kpi = h.get("result_kpi") or {}
            plan = h.get("plan") or {}
            rows.append([
                str(h.get("round", "?")),
                _fmt(h.get("params", {}).get("inlet_velocity_ms")),
                _fmt(kpi.get(metric_label)),
                str(kpi.get(within_key)),
                plan.get("action") or "—",
            ])
        story.append(_make_table(rows, cn_font, col_widths=[1.5 * cm, 3.5 * cm, 5 * cm, 3 * cm, 3.5 * cm]))

    # ---------- 5. 温度云图 ----------
    contour = result.get("contour_image_path")
    if contour and os.path.isfile(contour):
        story.append(PageBreak())
        story.append(Paragraph("5. 温度云图", h2_style))
        try:
            story.append(Image(contour, width=16 * cm, height=12 * cm, kind="proportional"))
        except Exception as e:
            story.append(Paragraph(f"(云图加载失败: {e})", body_style))
    else:
        story.append(Paragraph("5. 温度云图", h2_style))
        story.append(Paragraph(
            f"(未生成: {result.get('contour_skipped_reason', '未知')})", body_style
        ))

    # ---------- 6. 完整中文摘要 ----------
    summary_text = result.get("summary_text")
    if summary_text:
        story.append(PageBreak())
        story.append(Paragraph("6. 完整文字摘要", h2_style))
        # summary_text 里 = 分隔符太宽不利于 PDF 排版 — 按行 Paragraph 包
        for line in summary_text.split("\n"):
            line = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            story.append(Paragraph(line or "&nbsp;", mono_style))

    doc.build(story)
    print(f"[pdf_reporter] PDF 生成: {pdf_path}")
    return pdf_path


# ============================================================
# 辅助
# ============================================================

def _fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        if v != v:  # NaN
            return "—"
        return f"{v:.2f}"
    return str(v)


def _make_table(data, cn_font, col_widths=None):
    from reportlab.lib import colors
    from reportlab.platypus import Table, TableStyle
    t = Table(data, colWidths=col_widths)
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), cn_font),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("FONTSIZE", (0, 0), (-1, 0), 10),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


# ============================================================
# 自测试: 用 reports/ 下最新一次 run 生成 PDF
# ============================================================

def _selftest() -> int:
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    reports_dir = os.path.join(project_root, "reports")
    if not os.path.isdir(reports_dir):
        print(f"[FAIL] {reports_dir} 不存在")
        return 1
    # 找最新一次 run_* 目录
    runs = sorted(
        [d for d in glob.glob(os.path.join(reports_dir, "run_*")) if os.path.isdir(d)],
        reverse=True,
    )
    if not runs:
        print("[FAIL] reports/ 下没有 run_* 目录, 先跑 acceptance_iter.py")
        return 2
    latest = runs[0]
    print(f"[selftest] 用最新 run: {latest}")
    json_path = os.path.join(latest, "result.json")
    if not os.path.isfile(json_path):
        print(f"[FAIL] {json_path} 不存在")
        return 3
    with open(json_path, encoding="utf-8") as f:
        result = json.load(f)
    iter_history = result.get("iter_history")  # 可能没有, 单程 run 就是 None
    pdf_path = generate_pdf_report(result, iter_history=iter_history, out_dir=latest)
    if os.path.isfile(pdf_path) and os.path.getsize(pdf_path) > 1000:
        print(f"[PASS] PDF 生成成功: {pdf_path} ({os.path.getsize(pdf_path)/1024:.1f} KB)")
        return 0
    print(f"[FAIL] PDF 文件大小异常")
    return 4


if __name__ == "__main__":
    sys.exit(_selftest())

"""
app.py - SciAgent-Fluent Streamlit 前端
============================================================
启动: streamlit run app.py
布局:
  侧边栏: 历史记录 + 案例库浏览
  左主栏: 输入 (query / 案例 / 双目标约束 / 多轮 / dry-run)
  右主栏: 输出 (案例推荐 / 几何识别 / 双约束 / KPI / 云图 / 迭代历史 / PDF / RAG)
"""
import os
import sys

import streamlit as st

_ROOT = os.path.abspath(os.path.dirname(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _fmt(v):
    if v is None:
        return "-"
    if isinstance(v, float):
        if v != v:
            return "-"
        return "{:.2f}".format(v)
    return str(v)


st.set_page_config(page_title="SciAgent-Fluent", page_icon=":microscope:", layout="wide")

# ============================================================
# 现代扁平样式: Linear / Vercel 风, 高信息密度, 不工业不深色
# ============================================================
st.markdown("""
<style>
/* ============================================================
   Keynote 高对比展示风 — 白底 / 大字 / 深黑 + 深蓝 accent
   重点: 迭代过程 hero 时间轴放在视觉焦点
   ============================================================ */
:root {
  --bg:         #fbfbfd;
  --panel:      #ffffff;
  --panel-2:    #f6f7fa;
  --border:     #e8e9ed;
  --border-2:   #d8dae1;
  --text:       #3f4654;        /* 中性深灰, 不刺眼 */
  --text-2:     #585f6d;
  --muted:      #8a91a0;
  --accent:     #8b92c9;        /* 雾霾薰衣草紫蓝 */
  --accent-2:   #a5acd8;
  --accent-bg:  #eef0fa;
  --ok:         #5b8d71;        /* 柔雾松绿 */
  --ok-bg:      #eaf4ee;
  --ok-border:  #b8d4c2;
  --bad:        #a86b6b;        /* 柔陶土红 */
  --bad-bg:     #f7ecec;
  --bad-border: #d9b8b8;
  --warn:       #a8895b;        /* 柔米驼 */
  --warn-bg:    #f6efe4;
  --warn-border: #dec6a0;
  --info:       #7085a8;        /* 柔石板蓝 */
  --info-bg:    #eaeef4;
  --info-border: #bfc9d8;
  --mono: "JetBrains Mono", "SF Mono", "Consolas", ui-monospace, monospace;
  --sans: "Inter", -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
}
.stApp { background: var(--bg); color: var(--text); font-family: var(--sans); font-size: 14px; }
section.main > div { padding-top: 1rem !important; }
.block-container { padding: 22px 32px 40px !important; max-width: 1600px; }
header[data-testid="stHeader"] { background: transparent; height: 0; }
footer, #MainMenu { visibility: hidden; }

/* 主标题: 柔和 keynote */
.block-container > div:first-child h1 {
  font-size: 36px !important; font-weight: 600 !important;
  letter-spacing: -0.025em !important; margin: 0 0 4px 0 !important;
  color: var(--text) !important;
}
.block-container > div:first-child [data-testid="stCaptionContainer"] {
  font-size: 15px !important; color: var(--muted) !important; font-weight: 400;
}
h2 { font-size: 20px !important; font-weight: 600 !important; letter-spacing: -0.015em !important; margin: 22px 0 10px 0 !important; color: var(--text) !important; }
h3 { font-size: 15px !important; font-weight: 600 !important; color: var(--text-2) !important; }

[data-testid="stMarkdownContainer"] p { margin: 6px 0 !important; line-height: 1.65; }
[data-testid="stMarkdownContainer"] strong { color: var(--text); font-weight: 700; }

/* sidebar */
section[data-testid="stSidebar"] {
  background: var(--panel-2) !important;
  border-right: 1px solid var(--border);
}
section[data-testid="stSidebar"] .block-container { padding: 20px 16px !important; }
section[data-testid="stSidebar"] h1,
section[data-testid="stSidebar"] h2 {
  font-size: 14px !important; font-weight: 700 !important;
  text-transform: uppercase !important; letter-spacing: 0.06em !important;
  color: var(--muted) !important; margin: 12px 0 8px 0 !important;
}

/* ===== Hero 时间轴卡片 — 柔和扁平 ===== */
.iter-timeline {
  display: flex; gap: 12px; margin: 18px 0 24px 0;
  padding: 4px 2px; overflow-x: auto;
}
.iter-card {
  flex: 1 1 0; min-width: 200px; background: var(--panel);
  border: 1.5px solid var(--border); border-radius: 14px;
  padding: 22px 20px; position: relative;
  box-shadow: none;
  transition: border-color .15s ease;
}
.iter-card:hover { border-color: var(--accent-2); }
.iter-card.pass { border-color: var(--ok-border); background: var(--ok-bg); }
.iter-card.fail { border-color: var(--bad-border); background: var(--bad-bg); }
.iter-card.final { border-width: 2px; border-color: var(--accent); }
.iter-card-head {
  font-size: 11px; font-weight: 600; color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.1em; margin-bottom: 6px;
}
.iter-card-round {
  font-size: 30px; font-weight: 600; color: var(--text);
  letter-spacing: -0.02em; margin: 0 0 12px 0; font-family: var(--mono);
}
.iter-card-metric {
  font-size: 11px; color: var(--muted); text-transform: uppercase;
  letter-spacing: 0.06em; font-weight: 600; margin-top: 8px;
}
.iter-card-value {
  font-size: 26px; font-weight: 600; color: var(--text);
  font-family: var(--mono); letter-spacing: -0.015em; margin: 2px 0;
}
.iter-card-value.temp-pass { color: var(--ok); }
.iter-card-value.temp-fail { color: var(--bad); }
.iter-card-velocity {
  font-size: 15px; font-weight: 500; color: var(--accent);
  font-family: var(--mono); margin-top: 4px;
}
.iter-card-action {
  margin-top: 14px; padding: 5px 10px; border-radius: 6px;
  font-size: 12px; font-weight: 500; display: inline-block;
}
.iter-card-action.continue { background: var(--accent-bg); color: var(--accent); }
.iter-card-action.stop { background: var(--panel-2); color: var(--muted); }
.iter-arrow {
  align-self: center; color: var(--border-2); font-size: 22px;
  padding: 0 2px; user-select: none;
}

/* Hero verdict 柔和扁平 */
.hero-verdict {
  padding: 18px 24px; border-radius: 12px; margin: 16px 0 24px 0;
  border: 1.5px solid; display: flex; align-items: center; gap: 16px;
}
.hero-verdict.pass { background: var(--ok-bg); border-color: var(--ok-border); color: var(--ok); }
.hero-verdict.fail { background: var(--bad-bg); border-color: var(--bad-border); color: var(--bad); }
.hero-verdict-icon { font-size: 26px; font-weight: 600; line-height: 1; opacity: 0.75; }
.hero-verdict-title { font-size: 18px; font-weight: 600; letter-spacing: -0.015em; margin-bottom: 2px; }
.hero-verdict-subtitle { font-size: 13px; opacity: 0.8; font-weight: 400; }

/* alerts (现代柔色, 高对比文字) */
div[data-testid="stAlert"] {
  border-radius: 10px !important; border: 1px solid var(--border) !important;
  padding: 12px 16px !important; font-size: 14px !important; box-shadow: none !important;
  font-weight: 500 !important;
}
div[data-testid="stAlert"][kind="success"] { background: var(--ok-bg) !important; border-color: var(--ok-border) !important; color: var(--ok) !important; }
div[data-testid="stAlert"][kind="error"]   { background: var(--bad-bg) !important; border-color: var(--bad-border) !important; color: var(--bad) !important; }
div[data-testid="stAlert"][kind="warning"] { background: var(--warn-bg) !important; border-color: var(--warn-border) !important; color: var(--warn) !important; }
div[data-testid="stAlert"][kind="info"]    { background: var(--info-bg) !important; border-color: var(--info-border) !important; color: var(--info) !important; }

/* button — 柔和深灰 primary */
.stButton > button {
  background: var(--text) !important; color: #ffffff !important;
  border: none !important; border-radius: 10px !important;
  padding: 11px 20px !important; font-weight: 500 !important; font-size: 14px !important;
  letter-spacing: 0 !important; box-shadow: none !important;
  transition: opacity .15s ease;
}
.stButton > button:hover { opacity: 0.82; background: var(--text) !important; }
.stDownloadButton > button {
  background: var(--panel) !important; color: var(--text) !important;
  border: 1.5px solid var(--border-2) !important; border-radius: 10px !important;
  font-size: 13px !important; padding: 10px 16px !important;
  font-weight: 500 !important; box-shadow: none !important;
}
.stDownloadButton > button:hover { background: var(--panel-2) !important; }

/* inputs */
.stTextArea textarea, .stTextInput input, .stNumberInput input {
  background: var(--panel) !important; color: var(--text) !important;
  border: 1.5px solid var(--border) !important; border-radius: 10px !important;
  font-size: 14px !important; font-weight: 500 !important;
}
.stTextArea textarea:focus, .stTextInput input:focus, .stNumberInput input:focus {
  border-color: var(--accent) !important;
  box-shadow: 0 0 0 3px rgba(29, 78, 216, 0.15) !important;
  outline: none !important;
}
.stSelectbox div[data-baseweb="select"] > div {
  background: var(--panel) !important; color: var(--text) !important;
  border: 1.5px solid var(--border) !important; border-radius: 10px !important;
  font-size: 14px !important; min-height: 40px !important; font-weight: 500 !important;
}
.stCheckbox label { color: var(--text) !important; font-size: 14px !important; font-weight: 500 !important; }

/* ============================================================
   Pill / 气泡式选项 — 低饱和莫兰迪扁平风
   选中: 雾霾紫蓝底 + 深紫蓝字, 无阴影无渐变
   ============================================================ */
.stCheckbox {
  background: var(--panel-2) !important;
  border: 1.5px solid var(--border) !important;
  border-radius: 999px !important;
  padding: 10px 20px 10px 16px !important;
  margin-bottom: 10px !important;
  transition: background .15s ease, border-color .15s ease !important;
  box-shadow: none !important;
  cursor: pointer;
}
.stCheckbox:hover {
  background: var(--accent-bg) !important;
  border-color: var(--accent-2) !important;
}
/* 选中态: 雾霾紫蓝扁平 */
.stCheckbox:has(input:checked) {
  background: var(--accent-bg) !important;
  border-color: var(--accent) !important;
  box-shadow: none !important;
}
.stCheckbox:has(input:checked) label,
.stCheckbox:has(input:checked) label *,
.stCheckbox:has(input:checked) [data-testid="stMarkdownContainer"] p {
  color: var(--accent) !important;
  font-weight: 600 !important;
}
/* 选中时勾框: 白底 + 深紫勾 */
.stCheckbox:has(input:checked) span[data-baseweb="checkbox"] > span:first-child {
  border-color: var(--accent) !important;
  background: #ffffff !important;
}
.stCheckbox:has(input:checked) span[data-baseweb="checkbox"] svg {
  fill: var(--accent) !important; stroke: var(--accent) !important;
}
label, .stTextArea label, .stNumberInput label, .stSelectbox label {
  color: var(--text-2) !important; font-size: 13px !important; font-weight: 600 !important;
  text-transform: none !important; letter-spacing: 0 !important;
}

/* metric — 柔和扁平 KPI */
div[data-testid="stMetric"] {
  background: var(--panel) !important; border: 1.5px solid var(--border) !important;
  border-radius: 14px !important; padding: 16px 18px !important;
  transition: border-color .15s ease;
}
div[data-testid="stMetric"]:hover { border-color: var(--accent-2) !important; }
div[data-testid="stMetricLabel"] {
  color: var(--muted) !important; font-size: 11.5px !important;
  text-transform: uppercase !important; letter-spacing: 0.06em !important;
  font-weight: 600 !important;
}
div[data-testid="stMetricValue"] {
  color: var(--text) !important; font-family: var(--mono) !important;
  font-weight: 600 !important; letter-spacing: -0.02em !important; font-size: 28px !important;
}

/* dataframe */
div[data-testid="stDataFrame"] {
  border: 1.5px solid var(--border) !important; border-radius: 12px !important;
  overflow: hidden;
}
div[data-testid="stDataFrame"] * { font-family: var(--mono) !important; font-size: 13px !important; }

/* expander */
.streamlit-expanderHeader, details > summary, summary[role="button"] {
  background: var(--panel) !important; border: 1.5px solid var(--border) !important;
  border-radius: 10px !important; font-size: 13px !important; font-weight: 600 !important;
  color: var(--text) !important; text-transform: none !important; letter-spacing: 0 !important;
  padding: 11px 16px !important;
}
details[open] > summary { border-bottom-left-radius: 0 !important; border-bottom-right-radius: 0 !important; }
.streamlit-expanderContent {
  border: 1.5px solid var(--border) !important; border-top: none !important;
  border-radius: 0 0 10px 10px !important; padding: 14px 16px !important;
  background: var(--panel) !important;
}

/* json blocks */
[data-testid="stJson"] {
  background: var(--panel-2) !important; border: 1px solid var(--border) !important;
  border-radius: 10px !important; font-size: 12px !important;
}

/* code */
code {
  background: var(--accent-bg) !important; color: var(--accent) !important;
  padding: 2px 7px !important; border-radius: 5px !important;
  font-family: var(--mono) !important; font-size: 12.5px !important; font-weight: 600 !important;
}
pre code { background: var(--panel) !important; color: var(--text) !important; padding: 0 !important; }
pre { background: var(--panel-2) !important; border: 1px solid var(--border) !important; border-radius: 10px !important; }

/* divider */
hr { border: none !important; border-top: 1.5px solid var(--border) !important; margin: 16px 0 !important; }

/* tighten vertical gaps */
div[data-testid="stVerticalBlock"] > div { gap: 12px !important; }

/* ============================================================
   历史记录 pill — 按状态浅色区分
   ============================================================ */
.history-pill {
  display: block; margin: 6px 0;
  border-radius: 10px; border: 1.5px solid var(--border);
  background: var(--panel-2); overflow: hidden;
  transition: border-color .15s ease;
}
.history-pill:hover { border-color: var(--accent-2); }
.history-pill.pass { background: var(--ok-bg); border-color: var(--ok-border); }
.history-pill.fail { background: var(--bad-bg); border-color: var(--bad-border); }
.history-pill.mesh { background: var(--info-bg); border-color: var(--info-border); }
.history-pill.unknown { background: var(--panel-2); border-color: var(--border); }
.history-pill > summary {
  list-style: none; padding: 9px 14px !important; cursor: pointer;
  display: flex; align-items: center; gap: 8px;
  font-size: 12.5px !important; font-weight: 500 !important;
  color: var(--text) !important; background: transparent !important;
  border: none !important; border-radius: 10px !important;
}
.history-pill > summary::-webkit-details-marker { display: none; }
.history-pill > summary::before {
  content: "▸"; color: var(--muted); font-size: 10px;
  transition: transform .15s ease; display: inline-block;
}
.history-pill[open] > summary::before { transform: rotate(90deg); }
.history-pill.pass > summary { color: var(--ok) !important; }
.history-pill.fail > summary { color: var(--bad) !important; }
.history-pill.mesh > summary { color: var(--info) !important; }
.history-pill-badge {
  font-size: 10px; font-weight: 600; padding: 2px 7px;
  border-radius: 99px; letter-spacing: 0.04em;
  text-transform: uppercase; flex-shrink: 0;
}
.history-pill.pass .history-pill-badge { background: rgba(91, 141, 113, 0.15); color: var(--ok); }
.history-pill.fail .history-pill-badge { background: rgba(168, 107, 107, 0.15); color: var(--bad); }
.history-pill.mesh .history-pill-badge { background: rgba(112, 133, 168, 0.15); color: var(--info); }
.history-pill.unknown .history-pill-badge { background: rgba(138, 145, 160, 0.15); color: var(--muted); }
.history-pill-time {
  font-family: var(--mono); font-size: 11px; color: var(--muted); font-weight: 400;
  flex-shrink: 0;
}
.history-pill-query {
  font-size: 12px; color: var(--text-2); font-weight: 500;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  flex: 1 1 auto; min-width: 0;
}
.history-pill-body {
  padding: 6px 14px 12px 14px;
  border-top: 1px solid rgba(255, 255, 255, 0.5);
  background: rgba(255, 255, 255, 0.5);
  font-size: 11.5px; color: var(--text-2); line-height: 1.7;
}
.history-pill-body .k { color: var(--muted); font-weight: 600; }
.history-pill-body .v { font-family: var(--mono); color: var(--text); }
.history-pill-body pre {
  font-size: 10.5px !important; margin: 6px 0 0 0 !important;
  padding: 8px 10px !important; border-radius: 6px !important;
  background: rgba(255, 255, 255, 0.7) !important;
}

::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border-2); border-radius: 5px; }
::-webkit-scrollbar-thumb:hover { background: var(--muted); }
</style>
""", unsafe_allow_html=True)

st.title("SciAgent-Fluent")
st.caption("一句中文驱动 Ansys Fluent 仿真 · Agent 自主调参多轮迭代")


# ============================================================
# Sidebar: 历史记录 + 案例库浏览
# ============================================================
with st.sidebar:
    # ---- 历史记录 ----
    st.header("历史记录")
    try:
        from agents.history_store import load_history, history_stats, clear_history, get_history_file_path
        stats = history_stats()
        if stats["total"] > 0:
            stats_cols = st.columns(3)
            stats_cols[0].metric("总次数", stats["total"])
            stats_cols[1].metric("达标", stats["pass_count"])
            stats_cols[2].metric("平均耗时", str(stats["avg_elapsed_seconds"]) + "s")
            st.caption("达标率: " + "{:.1%}".format(stats["pass_rate"]))

            recent = load_history(limit=10)
            for i, e in enumerate(recent):
                ts = (e.get("timestamp") or "")[11:19]  # 时分秒
                q_short = (e.get("query") or "")[:32]
                run_type = e.get("run_type") or "simulation"

                # 状态 → CSS class + badge 文本
                if run_type == "mesh_study":
                    cls = "mesh"
                    conv = e.get("mesh_study_convergence_status")
                    badge = ("MESH-OK" if conv == "converged"
                             else ("MESH-?" if conv == "not_converged" else "MESH"))
                else:
                    passed = e.get("all_within_spec")
                    if passed is True:
                        cls, badge = "pass", "PASS"
                    elif passed is False:
                        cls, badge = "fail", "FAIL"
                    else:
                        cls, badge = "unknown", "?"

                # 详情区 (HTML)
                body_lines = []
                if run_type == "mesh_study":
                    body_lines.append(
                        f'<div><span class="k">case_mode:</span> '
                        f'<span class="v">{e.get("case_mode")}</span></div>')
                    body_lines.append(
                        f'<div><span class="k">推荐档位:</span> '
                        f'<span class="v">{e.get("mesh_study_recommended_level")}</span></div>')
                    body_lines.append(
                        f'<div><span class="k">收敛:</span> '
                        f'<span class="v">{e.get("mesh_study_convergence_status")}</span></div>')
                    if e.get("mesh_study_recommended_reason"):
                        body_lines.append(
                            f'<div><span class="k">理由:</span> {e.get("mesh_study_recommended_reason")}</div>')
                    if e.get("elapsed_seconds") is not None:
                        body_lines.append(
                            f'<div><span class="k">耗时:</span> '
                            f'<span class="v">{e.get("elapsed_seconds")} s</span></div>')
                    rows = e.get("mesh_study_rows") or []
                    if rows:
                        rows_text = ""
                        for r in rows:
                            src = "R" if r.get("source") == "real" else "X"
                            rows_text += (
                                f"{src} {r.get('level')}  "
                                f"{(r.get('cell_count') or 0):>8,}c  "
                                f"T={r.get('max_temp_C')}°C  "
                                f"ΔP={r.get('pressure_drop_pa')}Pa\n"
                            )
                        body_lines.append(f'<pre>{rows_text}</pre>')
                else:
                    body_lines.append(
                        f'<div><span class="k">case_mode:</span> '
                        f'<span class="v">{e.get("case_mode")}</span></div>')
                    if e.get("max_temp_C") is not None:
                        body_lines.append(
                            f'<div><span class="k">max_temp:</span> '
                            f'<span class="v">{e.get("max_temp_C")} °C</span></div>')
                    if e.get("outlet_avg_temp_C") is not None:
                        body_lines.append(
                            f'<div><span class="k">outlet:</span> '
                            f'<span class="v">{e.get("outlet_avg_temp_C")} °C</span></div>')
                    if e.get("elapsed_seconds") is not None:
                        body_lines.append(
                            f'<div><span class="k">耗时:</span> '
                            f'<span class="v">{e.get("elapsed_seconds")} s</span></div>')
                    if e.get("retrieved_case_id"):
                        body_lines.append(
                            f'<div><span class="k">推荐案例:</span> '
                            f'<span class="v">{e.get("retrieved_case_id")}</span></div>')
                    if e.get("iter_rounds"):
                        body_lines.append(
                            f'<div><span class="k">迭代:</span> '
                            f'<span class="v">{e.get("iter_rounds")} 轮 · {e.get("stop_reason")}</span></div>')
                    if e.get("failed_constraints"):
                        body_lines.append(
                            f'<div><span class="k">未达标:</span> '
                            f'<span class="v">{e.get("failed_constraints")}</span></div>')
                    pdf = e.get("pdf_report_path")
                    if pdf:
                        body_lines.append(
                            f'<div><span class="k">PDF:</span> '
                            f'<span class="v">{os.path.basename(pdf)}</span></div>')

                body_html = '<div class="history-pill-body">' + "".join(body_lines) + '</div>'

                # 渲染 pill
                st.markdown(
                    f'<details class="history-pill {cls}">'
                    f'<summary>'
                    f'<span class="history-pill-badge">{badge}</span>'
                    f'<span class="history-pill-time">{ts}</span>'
                    f'<span class="history-pill-query">{q_short}</span>'
                    f'</summary>'
                    f'{body_html}'
                    f'</details>',
                    unsafe_allow_html=True,
                )

            if st.button("清空历史", use_container_width=True):
                clear_history()
                st.rerun()
            st.caption("文件位置: " + os.path.relpath(get_history_file_path(), _ROOT))
        else:
            st.caption("(暂无历史, 跑一次仿真后会自动记录)")
    except Exception as e:
        st.caption("历史记录加载失败: " + str(e))

    st.markdown("---")

    # ---- 案例库 ----
    st.header("案例库")
    st.caption("knowledge/cases/")
    try:
        from agents.case_retriever import list_all_cases
        cases = list_all_cases()
        for c in cases:
            with st.expander(c.title):
                st.markdown("**case_id**: `" + c.case_id + "`")
                st.markdown("**case_mode**: " + c.case_mode)
                st.markdown("**tags**: " + ", ".join(c.tags))
                st.markdown("**描述**: " + c.description)
                st.json(c.baseline_params, expanded=False)
    except Exception as e:
        st.caption("案例库加载失败: " + str(e))


# ============================================================
# 主区: 输入 + 输出
# ============================================================
col_left, col_right = st.columns([1, 2])


with col_left:
    st.subheader("需求描述")
    query = st.text_area(
        "中文描述你的仿真需求",
        value="硅芯片 50W, 风速 1 m/s, 跑 150 步",
        height=120,
    )
    case_mode = st.selectbox(
        "案例选择",
        options=["manifold_cht", "mixing_elbow"],
        format_func=lambda x: {
            "manifold_cht": "芯片共轭传热 (W2 - chip_power 真驱动温度)",
            "mixing_elbow": "冷热水混流 (W1)",
        }[x],
        index=0,
    )

    st.markdown("""
<div style="margin: 14px 0 8px 0">
  <div style="font-size:14px; font-weight:600; color:#3f4654; letter-spacing:0">启用选项</div>
  <div style="font-size:12px; color:#8a91a0; margin-top:2px">选中的选项会被激活, 浅紫气泡 = 已启用</div>
</div>
""", unsafe_allow_html=True)

    # 约束 1: 芯片基底 max_temp
    use_max = st.checkbox(
        "启用: 芯片基底最高温度 ≤ X °C",
        value=True,
        help="对应 result.limit_C, manifold_cht 案例里就是 solid_up 的最高温度.",
    )
    max_default = {"manifold_cht": 1800.0, "mixing_elbow": 50.0}[case_mode]
    limit_C_input = st.number_input(
        "芯片基底最高温度上限 (°C)",
        min_value=0.0, max_value=5000.0,
        value=max_default, step=10.0,
        disabled=not use_max,
    )

    # 约束 2: 出口平均温度
    use_outlet = st.checkbox(
        "启用: 出口平均温度 ≤ Y °C",
        value=False,
        help="对应 result.outlet_limit_C. 注意 manifold 是热气流, outlet 本身几百度; "
             "mixing_elbow 是冷水管, outlet 20°C 左右.",
    )
    outlet_default = {"manifold_cht": 700.0, "mixing_elbow": 22.0}[case_mode]
    outlet_limit_input = st.number_input(
        "出口平均温度上限 (°C)",
        min_value=0.0, max_value=5000.0,
        value=outlet_default, step=1.0,
        disabled=not use_outlet,
    )
    use_iter = st.checkbox(
        "启用多轮迭代 (W3, 最多 3 轮)",
        value=False,
    )
    max_rounds = 3 if use_iter else 1

    # [W4+ mesh study] 网格独立性研究
    mesh_study_enabled = False
    if case_mode == "manifold_cht":
        mesh_study_enabled = st.checkbox(
            "启用网格独立性研究 (W4+, 3 档真跑 + 2 档外推, 约 15 分钟)",
            value=False,
            help="跑 L1(290K) / L2(580K) / L3(1.16M) 三档真仿真, "
                 "L4/L5 用 Richardson 外推. "
                 "自动挑再加密变化 < 1% 的档位作为推荐. "
                 "会覆盖多轮迭代选项 (mesh_study 走独立路径).",
        )
        if mesh_study_enabled and use_iter:
            st.warning("多轮迭代 + 网格独立性研究不能同时启用, 将只跑 mesh study.")

    dry_run = st.checkbox("Dry-run (跳过 Fluent)", value=False)

    st.markdown("---")
    submit_label = ("Run Mesh Study (3 档真仿真 ~15 min)"
                    if mesh_study_enabled else "Run Simulation")
    submit = st.button(submit_label, type="primary", use_container_width=True)
    if not submit:
        if mesh_study_enabled:
            st.info("网格独立性研究: 3 档真仿真 + 2 档外推, 真跑约 15 分钟.")
        else:
            st.info("点击上方按钮开始. 真跑 Fluent 每轮约 40 秒.")


with col_right:
    st.subheader("仿真结果")

    if not submit:
        st.info("等待输入...")
        st.markdown(
            "**界面会展示** (按从上到下顺序):\n"
            "1. 相似历史案例推荐\n"
            "2. 自动识别的芯片几何\n"
            "3. 双约束达标徽章\n"
            "4. DeepSeek 解析参数\n"
            "5. 核心 KPI 4 宫格\n"
            "6. 温度云图\n"
            "7. 迭代历史表 (W3)\n"
            "8. PDF 报告下载\n"
            "9. RAG 工程引文\n"
        )
        st.stop()

    # 把两个约束拼进 query (DeepSeek 会解析到 limit_C / outlet_limit_C)
    phrases = []
    if use_max and limit_C_input > 0:
        phrases.append("芯片基底温度不超过 " + str(limit_C_input) + " 度")
    if use_outlet and outlet_limit_input > 0:
        phrases.append("出口温度低于 " + str(outlet_limit_input) + " 度")
    if phrases:
        effective_query = query.rstrip("。.") + ", " + ", ".join(phrases)
    else:
        effective_query = query

    with st.expander("实际发给 LLM 的 query (含目标拼接)", expanded=False):
        st.code(effective_query, language=None)

    # [W4+ UI streaming] 实时进度面板 - st.status() 容器边跑边追加节点日志.
    # 节点级粒度 (intent / case_retriever / simulate Round N / iter_planner / report
    # 或 mesh study 的 L1/L2/L3/外推/推荐). 不劫持 stdout, 不开线程.
    status_label = ("Agent 工作中 · 网格独立性研究"
                    if mesh_study_enabled
                    else "Agent 工作中 · 流水线运行中")
    status_box = st.status(status_label, expanded=True, state="running")

    # 事件类型 → emoji + 颜色 (UI 端纯展示, 后端 _emit 不感知)
    _ICONS = {
        "intent.start":            "🧠",
        "intent.done":             "✓",
        "intent.error":            "✗",
        "case_retriever.start":    "🔎",
        "case_retriever.done":     "✓",
        "simulate.start":          "⚙️",
        "simulate.done":           "✓",
        "simulate.error":          "✗",
        "iter_planner.continue":   "🔁",
        "iter_planner.stop":       "🛑",
        "report.start":            "📝",
        "report.done":             "✓",
        "report.error":            "✗",
        "mesh_study.start":        "📐",
        "mesh_study.dry_run":      "🧪",
        "mesh_study.recommended":  "★",
        "mesh_study.error":        "✗",
        "mesh_level.start":        "▶",
        "mesh_level.done":         "✓",
        "mesh_level.error":        "✗",
        "mesh_extrapolate":        "📈",
    }

    # 累积事件到 session, UI 重渲也保留
    events_log = []

    def _on_progress(event: dict):
        """pipeline 节点回调 - 同步往 status_box 写一行."""
        etype = event.get("type", "")
        msg = event.get("message", "")
        icon = _ICONS.get(etype, "·")
        line = f"{icon} **{etype}** — {msg}"
        events_log.append(line)
        # 把累积日志写进 status box (同步, 立即可见)
        status_box.markdown("  \n".join(events_log))

    try:
        from graph.pipeline import run_pipeline
        final = run_pipeline(
            query=effective_query,
            dry_run=dry_run,
            case_mode=case_mode,
            max_rounds=max_rounds,
            mesh_study=mesh_study_enabled,
            progress=_on_progress,
        )
        # 跑完: 收紧 status box 状态
        if final.get("error"):
            status_box.update(label=f"流水线出错: {final.get('error')}",
                              state="error", expanded=True)
        else:
            status_box.update(label="✓ 流水线完成", state="complete",
                              expanded=False)
    except Exception as e:
        status_box.update(label=f"流水线异常: {type(e).__name__}",
                          state="error", expanded=True)
        st.error("流水线异常: " + type(e).__name__ + ": " + str(e))
        st.exception(e)
        st.stop()

    if final.get("error"):
        st.error("流水线出错: " + str(final.get("error")))
        st.stop()

    # [W4+ mesh study] 特殊渲染路径 — 不走下面的 result / history / PDF / RAG
    if mesh_study_enabled and final.get("mesh_study"):
        study = final["mesh_study"]
        rec = study.get("recommended_level")
        reason = study.get("recommended_reason", "")
        status = study.get("convergence_status", "")

        if status == "converged":
            st.success(f"✓ 推荐网格档位: {rec}  —  {reason}")
        else:
            st.warning(f"⚠ 未真正收敛: {reason}  (当前推 {rec})")

        rows = study.get("rows", [])
        # 表格: 推荐行星号标记
        display_rows = []
        for r in rows:
            display_rows.append({
                "档位": r.get("level") + (" ★" if r.get("level") == rec else ""),
                "cell 数": f"{(r.get('cell_count') or 0):,}",
                "最高温度 (°C)": _fmt(r.get("max_temp_C")),
                "出口平均 (°C)": _fmt(r.get("outlet_avg_temp_C")),
                "压降 (Pa)": _fmt(r.get("pressure_drop_pa")),
                "耗时 (s)": _fmt(r.get("elapsed_s")),
                "数据来源": "真仿真" if r.get("source") == "real" else "Richardson 外推",
            })
        st.markdown("**网格独立性对比表** (★ = 推荐档)")
        st.dataframe(display_rows, use_container_width=True, hide_index=True)

        # 收敛曲线 PNG
        png_path = (study.get("artifacts") or {}).get("mesh_convergence_png")
        if png_path and os.path.isfile(png_path):
            st.markdown("**收敛曲线** (温度 / 压降 vs cell 数)")
            st.image(png_path, use_column_width=True)

        # 推荐档位详细解释卡片
        rec_row = next((r for r in rows if r.get("level") == rec), None)
        next_idx = next((i for i, r in enumerate(rows)
                         if r.get("level") == rec), -1) + 1
        next_row = rows[next_idx] if 0 < next_idx < len(rows) else None
        if rec_row and next_row:
            def _pct(a, b):
                if a is None or b is None or a == 0:
                    return "—"
                return f"{abs(b - a) / abs(a) * 100:.2f}%"
            st.info(
                f"**推荐 {rec} ({rec_row.get('cell_count'):,} cells) 的理由**:\n"
                f"\n- 与下一档 {next_row.get('level')} 相比:\n"
                f"  - 最高温度变化 {_pct(rec_row.get('max_temp_C'), next_row.get('max_temp_C'))}\n"
                f"  - 出口平均变化 {_pct(rec_row.get('outlet_avg_temp_C'), next_row.get('outlet_avg_temp_C'))}\n"
                f"  - 压降变化 {_pct(rec_row.get('pressure_drop_pa'), next_row.get('pressure_drop_pa'))}\n"
                f"- 均 < {study.get('eps', 0.01)*100:.0f}% 阈值, 再加密收益不值得."
            )

        # 下载 artifact
        art = study.get("artifacts") or {}
        dl_cols = st.columns(3)
        for i, (label, key, mime) in enumerate([
            ("下载 summary.json", "mesh_study_summary_json", "application/json"),
            ("下载 table.txt", "mesh_study_table_txt", "text/plain"),
            ("下载 convergence.png", "mesh_convergence_png", "image/png"),
        ]):
            path = art.get(key)
            if path and os.path.isfile(path):
                with open(path, "rb") as f:
                    dl_cols[i].download_button(
                        label, f.read(),
                        file_name=os.path.basename(path), mime=mime,
                        use_container_width=True, key=f"dl_{key}",
                    )

        with st.expander("完整 mesh_study dict (排错)"):
            st.json(study, expanded=False)
        st.stop()

    result = final.get("result") or {}
    history = final.get("iter_history") or []
    last_plan = final.get("last_plan") or {}

    # ---------- (B) 案例推荐 ----------
    rid = final.get("retrieved_case_id")
    if rid:
        st.success(
            "[案例推荐] 检索到相似案例 `" + rid + "` "
            + "(score=" + str(final.get("retrieved_case_score", 0)) + ") - "
            + str(final.get("retrieved_case_title", ""))
            + "\n\n已用此案例 baseline_params 填补 query 没说的字段."
        )
    else:
        st.caption("[案例推荐] 无相似历史案例命中")

    # ---------- (A) 几何识别 ----------
    hotspot = result.get("hotspot")
    if hotspot and hotspot.get("zone"):
        st.info(
            "[几何识别] 自动识别 chip_zone = `"
            + str(hotspot["zone"])
            + "` (geometry_inspector.find_chip_zone, 不再 hardcode)"
        )

    # ---------- (C) 多目标约束 ----------
    st.markdown("**约束达标判定 (双目标)**")
    active = result.get("active_constraints") or []
    failed = result.get("failed_constraints") or []
    all_within = result.get("all_within_spec")
    if all_within is True and active:
        st.success(
            "所有 " + str(len(active)) + " 个约束都达标: " + str(active)
        )
    elif all_within is False:
        st.error(
            str(len(failed)) + " 个约束未达标: " + str(failed)
            + " (active=" + str(active) + ")"
        )
    else:
        st.warning("无 active 约束")

    badge_cols = st.columns(2)
    with badge_cols[0]:
        st.markdown("**最高温度约束**")
        if "max_temp" not in active:
            # 用户没设 limit_C, 这条约束就不是 active, 不给结论只显示数值
            st.caption("(未启用 - 用户未指定警戒线)")
            st.caption("max_temp = " + _fmt(result.get("max_temp_C")) + " °C")
        else:
            within = result.get("within_spec")
            if within is True:
                st.success("达标 margin=" + _fmt(result.get("margin_C")) + " °C")
            elif within is False:
                st.error("未达标 margin=" + _fmt(result.get("margin_C")) + " °C")
            else:
                st.warning("未知")
            st.caption("limit_C = " + _fmt(result.get("limit_C")) + " °C")
    with badge_cols[1]:
        st.markdown("**出口平均约束**")
        if "outlet" not in active:
            st.caption("(未启用 - 用户未指定警戒线)")
            st.caption("outlet_avg = " + _fmt(result.get("outlet_avg_temp_C")) + " °C")
        else:
            ow = result.get("outlet_within_spec")
            if ow is True:
                st.success("达标 margin=" + _fmt(result.get("outlet_margin_C")) + " °C")
            elif ow is False:
                st.error("未达标 margin=" + _fmt(result.get("outlet_margin_C")) + " °C")
            else:
                st.warning("未知")
            st.caption("outlet_limit_C = " + _fmt(result.get("outlet_limit_C")) + " °C")

    # ---------- DeepSeek 参数 ----------
    st.markdown("**DeepSeek 解析的参数**")
    params = final.get("params") or result.get("params_echo") or {}
    st.json(params, expanded=False)

    # ---------- KPI ----------
    st.markdown("**核心指标**")
    kpi_cols = st.columns(4)
    kpi_cols[0].metric("最高温度 (K)", _fmt(result.get("max_temp_K")))
    kpi_cols[1].metric("最高温度 (°C)", _fmt(result.get("max_temp_C")))
    kpi_cols[2].metric("出口平均 (°C)", _fmt(result.get("outlet_avg_temp_C")))
    kpi_cols[3].metric("耗时 (s)", _fmt(result.get("elapsed_seconds")))

    if case_mode == "manifold_cht":
        cpa = result.get("chip_power_applied")
        if cpa is True:
            zone_name = hotspot.get("zone") if hotspot else "chip_zone"
            st.success(
                "chip_power 已真加到 `" + str(zone_name) + "`, Q = "
                + _fmt(result.get("chip_power_Q_volumetric")) + " W/m^3"
            )
        elif cpa is False:
            st.warning("chip_power 未生效")

    # ---------- 云图 ----------
    contour = result.get("contour_image_path")
    if contour and os.path.isfile(contour):
        st.markdown("**温度云图**")
        st.image(contour, use_column_width=True)
    elif result.get("contour_skipped_reason"):
        st.caption("云图未生成: " + str(result["contour_skipped_reason"]))

    # ---------- 迭代过程 HERO 时间轴 (展示焦点) ----------
    if len(history) >= 1:
        metric_key = "outlet_avg_temp_C" if case_mode == "mixing_elbow" else "max_temp_C"
        limit_label = "outlet_limit" if case_mode == "mixing_elbow" else "max_limit"

        # Hero 标题
        multi = len(history) > 1
        if multi:
            st.markdown(
                "<h2 style='margin-top:18px'>Agent 自主调参轨迹 · "
                f"<span style='color:#8b92c9'>{len(history)} 轮</span></h2>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                "<h2 style='margin-top:18px'>仿真结果</h2>",
                unsafe_allow_html=True,
            )

        # 构造时间轴 HTML
        cards_html = ['<div class="iter-timeline">']
        for i, h in enumerate(history):
            kpi = h.get("result_kpi") or {}
            plan = h.get("plan") or {}
            within_key = "outlet_within_spec" if "outlet" in metric_key else "within_spec"
            passed = kpi.get(within_key)
            vel = h.get("params", {}).get("inlet_velocity_ms")
            metric_val = kpi.get(metric_key)
            action = plan.get("action") or "—"
            is_final = (i == len(history) - 1)

            classes = "iter-card"
            if passed is True:
                classes += " pass"
            elif passed is False:
                classes += " fail"
            if is_final and multi:
                classes += " final"

            val_class = "iter-card-value"
            if passed is True:
                val_class += " temp-pass"
            elif passed is False:
                val_class += " temp-fail"

            metric_str = f"{metric_val:.2f}" if isinstance(metric_val, (int, float)) else "—"
            vel_str = f"{vel:.2f} m/s" if isinstance(vel, (int, float)) else "—"
            action_class = "continue" if action == "continue" else "stop"
            action_label = {
                "continue": "加风速 · 继续",
                "stop": "终止",
                "—": "—",
            }.get(action, action)
            verdict = "✓ 达标" if passed is True else ("✗ 未达标" if passed is False else "—")

            cards_html.append(f'''
<div class="{classes}">
  <div class="iter-card-head">Round</div>
  <div class="iter-card-round">#{h.get("round", "—")}</div>
  <div class="iter-card-metric">入口风速</div>
  <div class="iter-card-velocity">{vel_str}</div>
  <div class="iter-card-metric" style="margin-top:10px">{metric_key.replace("_", " ")}</div>
  <div class="{val_class}">{metric_str}<span style="font-size:14px;color:#64748b;margin-left:4px">°C</span></div>
  <div style="font-size:13px;font-weight:600;margin-top:4px;color:{'#5b8d71' if passed is True else ('#a86b6b' if passed is False else '#8a91a0')}">{verdict}</div>
  <span class="iter-card-action {action_class}">{action_label}</span>
</div>''')
            if i < len(history) - 1:
                cards_html.append('<div class="iter-arrow">→</div>')
        cards_html.append('</div>')
        st.markdown("".join(cards_html), unsafe_allow_html=True)

        # Hero 最终判定
        if multi:
            final_pass = (last_plan.get("stop_reason") == "within_spec")
            verdict_class = "pass" if final_pass else "fail"
            verdict_icon = "✓" if final_pass else "✗"
            verdict_title = "Agent 自主调参达标" if final_pass else "达预算上限, 未完全达标"
            verdict_sub = last_plan.get("reasoning") or ""
            st.markdown(
                f'<div class="hero-verdict {verdict_class}">'
                f'<div class="hero-verdict-icon">{verdict_icon}</div>'
                f'<div>'
                f'<div class="hero-verdict-title">{verdict_title}</div>'
                f'<div class="hero-verdict-subtitle">{verdict_sub}</div>'
                f'</div></div>',
                unsafe_allow_html=True,
            )

    # ---------- PDF 下载 ----------
    pdf_path = result.get("pdf_report_path")
    if pdf_path and os.path.isfile(pdf_path):
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()
        st.download_button(
            label="下载 PDF 报告 (" + str(len(pdf_bytes) // 1024) + " KB)",
            data=pdf_bytes,
            file_name=os.path.basename(pdf_path),
            mime="application/pdf",
            use_container_width=True,
        )

    # ---------- 文字摘要 ----------
    if result.get("summary_text"):
        with st.expander("完整文字摘要"):
            st.text(result["summary_text"])

    # ---------- RAG 引文 ----------
    reasoning = last_plan.get("reasoning") or ""
    if reasoning:
        with st.expander("工程引文 (RAG 检索)"):
            try:
                from agents.rag_advisor import retrieve
                snippets = retrieve(reasoning, top_k=2)
                if snippets:
                    for s in snippets:
                        st.markdown("**" + s.topic + "** (score: " + str(s.score) + ")")
                        st.caption("source: " + s.source)
                        st.markdown(s.body)
                        st.markdown("---")
                else:
                    st.caption("未检索到相关引文")
            except Exception as e:
                st.caption("RAG 检索失败: " + str(e))

    with st.expander("完整 result dict (排错)"):
        st.json(result, expanded=False)

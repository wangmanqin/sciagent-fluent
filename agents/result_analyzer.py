"""
agents/result_analyzer.py
============================================================
仿真跑完后, 自动生成:
  (a) 温度云图 PNG -> reports/run_<时间戳>/temperature_contour.png
  (b) 文字总结 .txt -> reports/run_<时间戳>/report.txt
  (c) 结构化 result.json -> reports/run_<时间戳>/result.json
  并把这些路径回传给上层(LangGraph 用)。

为什么要这一层(写给以后的你/答辩用):
  run_simulation 只返回数字 dict, 数字本身不直观——
  你看到 "max_temp_K=313.15" 不会立刻反应这是 40 °C 还是危险温度。
  这一层把这些数字翻译成"人能看懂的报告":
    - 一张图(温度云图): 直观, 截图发给导师就能讨论
    - 一段中文(总结): 对照警戒线给"达标 / 超标"的判断
    - 一个 JSON: 给 LangGraph 当作下一节点的输入

设计原则(对应 设计思路.md §5.5 + 五大架构决策):
  1. 文字总结走"模板", 不调 LLM。架构决策 #1: LLM 只出 JSON, 不写自由文本——
     因为总结结构稳定(温度 + 警戒线判定), 模板能写就别让 LLM 编。
     加多轮迭代后, "建议下一轮怎么改" 那部分才需要 LLM。
  2. 云图生成多路 fallback。架构决策 #5: PyFluent graphics API 是版本最不稳的
     部分(每个小版本都有改动), 所以 modern API 失败 -> TUI 命令 -> 都失败就
     "PNG 跳过, 文字报告照出"——宁可少一张图, 也别把整个流程拖垮。
  3. 接受 session 而不是文件路径。架构决策 #4(状态机+检查点): LangGraph
     会把 session 在节点之间传递, 让 analyzer 共用 run 阶段的活会话, 不用再
     重启 Fluent 读 .dat(那要再花 30 秒+占一个 license 槽)。

验收:
  reports/ 下有 PNG + txt 报告。

调用接口(API):
  >>> from agents.result_analyzer import analyze, summarize
  >>>
  >>> # 形态 1: 有活的 session (run_simulation 调 analyzer hook 时走这路)
  >>> r = analyze(session, params, run_summary, output_dir="reports/run_xxx")
  PNG+txt+json
  >>>
  >>> # 形态 2: 只有数字 dict, 没 session (单测/事后补报告)
  >>> r = summarize(run_summary, params)
  只有文字总结，没有图片和json
"""
# ↑ 模块文档字符串。help(agents.result_analyzer) 能读到。

import argparse           # CLI 参数解析
import datetime           # 给报告打时间戳, 也用来生成 reports/run_<时间>/ 目录名
import json               # 写 result.json
import os                 # 路径处理 + makedirs
import sys                # sys.exit / sys.stderr
import traceback          # 出错打印调用栈
from typing import Any, Callable, Optional


# ============================================================
# 全局常量 — 改这里就能调"基线", 不动业务函数
# ============================================================

# 项目根 = 本文件所在目录的上一级 (agents/ 的上一级)
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# 默认报告目录: <项目根>/reports/
_DEFAULT_REPORTS_DIR = os.path.join(_PROJECT_ROOT, "reports")

# 默认警戒线: 85 °C — 芯片散热场景常见上限(消费电子结温多在 85~105 °C 之间)。
# W2 换共轭传热案例后, 加上 chip_power 才会真正逼近警戒线。
_DEFAULT_LIMIT_C = 85.0

# W2 manifold 警戒线: manifold 案例本身就是排气歧管 (热气 ~600 °C),
# 用消费电子的 85 °C 没意义. intent_parser 加 limit_C 字段允许 query 改,
# 但默认值需要按 case_mode 区分 — manifold 默认 1500 °C (高温合金钢上限).
_DEFAULT_LIMIT_C_MANIFOLD = 1500.0

# [W3] outlet 平均温度警戒线 — 给 iter_planner 用 (mixing_elbow 路径上
# max_temp 是 hot-inlet 边界温度钉死的, 永远 ~40°C 永远 ≤85°C, 迭代无法触发).
# mixing_elbow 默认 cold/hot inlet 混合后 outlet ~22°C, 设 21°C 让初始 query
# (chip_power=15W, v=0.4) 略超 1°C 触发迭代; 加风速会让 outlet 更接近 cold-inlet (293K=20°C).
# 用户可在 query 里写"出口低于 X°C"由 intent_parser 写到 params["outlet_limit_C"] 覆盖.
_DEFAULT_OUTLET_LIMIT_C = 21.0


def _default_limit_for_case(run_summary: dict) -> float:
    """根据 result 里的 case_mode 字段挑默认警戒线."""
    case_mode = (run_summary or {}).get("case_mode", "mixing_elbow")
    if case_mode == "manifold_cht":
        return _DEFAULT_LIMIT_C_MANIFOLD
    return _DEFAULT_LIMIT_C

# 云图渲染分辨率: 1024x768 是 PNG 截图常用尺寸, 在大多数答辩屏幕上够清晰。
_PNG_X_RES = 1024
_PNG_Y_RES = 768

# 云图想显示的 surface 名字(mixing-elbow 案例的命名)。
# 不同案例 surface 名不一样, 顺次试, 哪个 Fluent 不报"surface not found"就用哪个。
# "()" 是 Fluent TUI 里的"全选"通配符。
_CONTOUR_SURFACE_CANDIDATES = [
    ["symmetry-xyplane", "wall-elbow", "outlet"],   # mixing-elbow 标准命名
    ["symmetry", "wall", "outlet"],                  # 通用命名
    ["wall-fluid", "outlet"],                        # 一些案例的命名
    # W2 manifold case 的 face zone (inspect 验证):
    # outlet=出口, solid_up:1:830=solid-fluid 耦合面 (主要散热面), inlet=入口
    ["outlet", "solid_up:1:830", "inlet"],           # manifold 标准三件套
    ["outlet", "solid_up:1", "inlet"],               # solid_up:1 备选
    ["outlet"],                                      # 最少: 只 outlet
    ["()"],                                          # 全选, 兜底
]


# ============================================================
# 公开 API #1: summarize — 纯 Python, 不需要 session
# ============================================================

def summarize(
    run_summary: dict,
    params: dict,
    limit_C: Optional[float] = None,
) -> dict:
    """
    根据 run_simulation 返回的 result dict, 生成"分析后"的扩展 dict。
    这一步纯 Python: 拿数字 + 算判定 + 拼模板文字。不需要 Fluent session。

    用法 1: 单元测试 — 喂假数据进来, 验证模板/判定逻辑对不对, 不用启 Fluent。
    用法 2: 事后补报告 — Fluent 已经退出了, 但你存了 result.json, 可以重新出文字。
    用法 3: 被 analyze() 内部调用 — analyze() 是这个的"超集"(多了云图)。

    Args:
        run_summary: run_simulation() 的返回 dict, 至少要有 max_temp_K /
                     outlet_avg_temp_K / converged / iterations / elapsed_seconds。
                     W2 起多一个 case_mode 字段, 用来挑默认 limit_C 和报告语气。
        params:      原始用户层参数, 至少要有 chip_power_watts /
                     inlet_velocity_ms / max_iterations。
                     W2 起 params 可能含 limit_C 字段, 用户 query 显式覆盖.
        limit_C:     警戒线, 摄氏度。
                     None (默认) = 三层 fallback:
                       1. params["limit_C"] (用户 query 里指定)
                       2. _default_limit_for_case(run_summary) (按 case_mode 挑)
                       3. mixing_elbow 用 85, manifold 用 1500.

    Returns:
        dict, 在原 run_summary 基础上叠加:
            limit_K (float):           警戒线(K), = limit_C + 273.15
            limit_C (float):           警戒线(°C), 原样回传, 排错方便
            within_spec (bool):        最高温度是否未超警戒线
            margin_C (float):          离警戒线的余量, °C(正=未超, 负=超了)
            summary_text (str):        中文模板文字总结
            generated_at (str):        ISO 时间戳, 加进 JSON 里方便回溯
    """
    # ---------- (0) 决定 limit_C — 三层 fallback ----------
    if limit_C is None:
        limit_C = (params or {}).get("limit_C")
    if limit_C is None:
        limit_C = _default_limit_for_case(run_summary)

    # ---------- (1) 提关键数字, 容错处理 ----------
    # 用 .get() 而不是 [] — 如果 run_summary 是别人构造的 / 老版本字段不全,
    # 至少不会因为一个 KeyError 把整个分析流程崩掉。
    max_t_K = float(run_summary.get("max_temp_K", float("nan")))
    outlet_t_K = float(run_summary.get("outlet_avg_temp_K", float("nan")))
    converged = bool(run_summary.get("converged", False))
    iters = int(run_summary.get("iterations", 0))
    elapsed = float(run_summary.get("elapsed_seconds", 0.0))

    # ---------- (2) 警戒线判定 ----------
    limit_K = limit_C + 273.15
    # 余量(margin): 正数 = 安全, 负数 = 超标
    # 用 NaN 时 within_spec 设为 False(保守: 拿不到温度就当不达标)
    if max_t_K != max_t_K:  # NaN 检查 (NaN != NaN)
        within_spec = False
        margin_C = float("nan")
    else:
        margin_C = limit_C - (max_t_K - 273.15)
        within_spec = margin_C >= 0.0

    # ---------- (2.5) [W3] outlet 路径并行判定 ----------
    # W3 iter_planner 在 mixing_elbow 上不能用 max_temp_C (踩坑 #6 钉死),
    # 改用 outlet_avg_temp_C. 这里同时算两套, 让 graph 节点选用哪套.
    # 不影响 W1/W2 的 within_spec 字段语义 — 那个仍然按 max_temp 判.
    outlet_limit_C = (params or {}).get("outlet_limit_C")
    if outlet_limit_C is None:
        outlet_limit_C = _DEFAULT_OUTLET_LIMIT_C
    outlet_t_C = outlet_t_K - 273.15 if outlet_t_K == outlet_t_K else float("nan")
    if outlet_t_C != outlet_t_C:
        outlet_within_spec = False
        outlet_margin_C = float("nan")
    else:
        outlet_margin_C = outlet_limit_C - outlet_t_C
        outlet_within_spec = outlet_margin_C >= 0.0

    # ---------- (3) 拼模板文字 ----------
    summary_text = _render_summary_text(
        params=params,
        max_t_K=max_t_K,
        outlet_t_K=outlet_t_K,
        converged=converged,
        iters=iters,
        elapsed=elapsed,
        limit_C=limit_C,
        within_spec=within_spec,
        margin_C=margin_C,
        case_mode=run_summary.get("case_mode", "mixing_elbow"),
        chip_power_applied=run_summary.get("chip_power_applied"),
        chip_power_Q_volumetric=run_summary.get("chip_power_Q_volumetric"),
        hotspot=run_summary.get("hotspot"),
    )

    # ---------- (4) 组装返回 dict ----------
    # dict(run_summary) 浅拷贝, 不修改调用方的 dict (副作用安全)
    # [W4 多目标约束] all_within_spec = 所有 active 约束都中.
    # 用户没设 outlet_limit_C 时 outlet 约束就不算 active (默认 21°C, 但用户没主动要求).
    user_set_outlet_limit = (params or {}).get("outlet_limit_C") is not None
    user_set_max_limit = (params or {}).get("limit_C") is not None
    active_constraints = []
    failed_constraints = []
    if user_set_max_limit or run_summary.get("case_mode") == "manifold_cht":
        active_constraints.append("max_temp")
        if not within_spec:
            failed_constraints.append("max_temp")
    if user_set_outlet_limit or run_summary.get("case_mode") in (None, "mixing_elbow"):
        active_constraints.append("outlet")
        if not outlet_within_spec:
            failed_constraints.append("outlet")
    all_within_spec = (len(failed_constraints) == 0) and (len(active_constraints) > 0)

    out = dict(run_summary)
    out.update({
        "limit_K": round(limit_K, 2),
        "limit_C": round(limit_C, 2),
        "within_spec": within_spec,
        "margin_C": round(margin_C, 2) if margin_C == margin_C else None,
        # [W3] outlet 并行字段, 给 iter_planner 用
        "outlet_avg_temp_C": round(outlet_t_C, 2) if outlet_t_C == outlet_t_C else None,
        "outlet_limit_C": round(outlet_limit_C, 2),
        "outlet_within_spec": outlet_within_spec,
        "outlet_margin_C": round(outlet_margin_C, 2) if outlet_margin_C == outlet_margin_C else None,
        # [W4 多目标约束]
        "active_constraints": active_constraints,
        "failed_constraints": failed_constraints,
        "all_within_spec": all_within_spec,
        "summary_text": summary_text,
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
    })
    return out


def _render_summary_text(
    params: dict,
    max_t_K: float,
    outlet_t_K: float,
    converged: bool,
    iters: int,
    elapsed: float,
    limit_C: float,
    within_spec: bool,
    margin_C: float,
    case_mode: str = "mixing_elbow",
    chip_power_applied: Optional[bool] = None,
    chip_power_Q_volumetric: Optional[float] = None,
    hotspot: Optional[dict] = None,
) -> str:
    """
    拼一段中文报告。模板写死, 不经 LLM——架构决策 #1: 结构稳定的文字别让 LLM 编。
    分块写是为了让以后改格式时只动一处, 比一个大 f-string 好维护。

    W2 起新增字段:
      case_mode: "mixing_elbow" 或 "manifold_cht", 决定文末附录怎么写
      chip_power_applied / Q_volumetric: manifold 路径才有, 报告里多一段
      hotspot: {x, y, z, T_K} dict, manifold 路径有则多一段"热点位置"
    """
    # 防止 NaN 显示成 "nan"——分支里手动写 "未取到"
    def fmt_T(v: float) -> str:
        if v != v:  # NaN
            return "未取到"
        return f"{v:.2f} K  ({v - 273.15:.2f} °C)"

    # 收敛标志: ✓ / ✗ 比 True/False 直观, 但要避开 emoji(导师终端可能不支持)
    converged_label = "收敛" if converged else "未收敛 / 异常"

    # 警戒线判定一行: 优先告诉用户"达标了吗", 再给余量数字
    if within_spec:
        verdict_line = (
            f"判定: 最高温度未超过 {limit_C:.0f} °C 警戒线, "
            f"余量 {margin_C:+.2f} °C, 设计安全。"
        )
    elif margin_C != margin_C: #NaN
        verdict_line = (
            f"判定: 未取到最高温度, 无法判定是否达标 (limit={limit_C:.0f} °C)。"
        )
    else:
        # margin_C 已经是负数了, 直接打绝对值差更直观
        verdict_line = (
            f"判定: 最高温度超过 {limit_C:.0f} °C 警戒线 "
            f"{abs(margin_C):.2f} °C, 不达标, 需要重新设计。"
        )

    # mixing-elbow 上的特殊说明: 用户可能困惑"为啥 max_T 只有 40 °C"。
    # 写一行解释, 让答辩时不用临场组织语言。
    if case_mode == "manifold_cht":
        # W2 manifold 路径: 强调 chip_power 真生效 + 物理映射诚实声明
        elbow_note = (
            "\n说明: 当前是 W2 manifold 路径 (Ansys 官方 exhaust_manifold 案例), "
            "案例自带管内热气流和管壁固体共轭传热 (CHT). "
            f"chip_power_watts 已通过体积热源 source_terms.energy 真注入 solid_up zone, "
            f"max_temp 随 query 真实变化 (验证: 0W→830K, 5000W→1616K). "
            "\n物理映射 (known limit #2): Q = power / chip_volume_default(10 cm^3), "
            "实际加到 solid_up 全 691 cm^3, 等价于'manifold 管壁内嵌 10 cm^3 芯片, "
            "热量均匀摊到整个管壁'. 这是工程简化, W4 真做 PCB+chip 案例后切换 case 即生效."
        )
    elif 290.0 <= max_t_K <= 320.0:
        # W1 mixing-elbow 路径: 解释为什么 chip_power 没生效
        elbow_note = (
            "\n说明: 当前是 mixing-elbow 基准案例(冷热水混合管), 没有发热源, "
            "最高温度被 hot-inlet 边界条件 313.15 K 钉住, 不会超过警戒线。"
            "W2 换共轭传热案例后, chip_power 才会真正驱动 max_temp 上升。"
        )
    else:
        elbow_note = ""

    # W2 路径多打两行: chip_power_Q + hotspot
    extra_lines = []
    if case_mode == "manifold_cht":
        if chip_power_Q_volumetric is not None:
            extra_lines.append(
                f"  体积热源 Q     : {chip_power_Q_volumetric:.4e} W/m^3 "
                f"(= power / chip_volume_default)"
            )
        if chip_power_applied is False:
            extra_lines.append(
                f"  [WARN]         : chip_power 未成功施加到 solid_up, "
                f"max_temp 不反映 query"
            )
    if hotspot and isinstance(hotspot, dict):
        T = hotspot.get("T_K")
        zone = hotspot.get("zone", "")
        x = hotspot.get("x"); y = hotspot.get("y"); z = hotspot.get("z")
        if x is not None and y is not None and z is not None and T is not None:
            extra_lines.append(
                f"  热点坐标 (m)   : ({x:.4f}, {y:.4f}, {z:.4f}) @ {T:.2f} K"
            )
        elif T is not None and zone:
            # 没拿到精确坐标也写一行 — 至少告诉用户"热点在哪个 zone"
            extra_lines.append(
                f"  热点位置       : zone={zone}, T={T:.2f} K"
            )

    # 用 list + "\n".join 拼, 不再依赖 Python 的"相邻字符串字面量自动合并"规则。
    # 为什么改: 之前的 ( "=" * 58 + "\n" \n "..." \n ... ) 形式有个隐蔽陷阱——
    # 末尾 `f"生成时间...\n"` 紧跟一行 `"=" * 58 + "\n"` 时, 因为没用 + 显式拼,
    # lexer 先把 f-string 末尾的 \n 和 "=" 做相邻字面量合并, 再让 * 58 作用到
    # 整段合并后的字符串上, 结果"生成时间..."那段被乘了 58 倍, 一份报告变成
    # 24K 字符的重复噩梦 (踩坑记录 #8)。
    # 修复: 用 list 逐行 append + "\n".join, 拼接关系完全显式, 永远不会再踩。
    sep = "=" * 58
    lines = [
        sep,
        "SciAgent-Fluent 仿真结果报告",
        sep,
        "",
        "[输入参数]",
        f"  芯片功耗       : {params.get('chip_power_watts', '?')} W",
        f"  入口风速       : {params.get('inlet_velocity_ms', '?')} m/s",
        f"  最大迭代步数   : {params.get('max_iterations', '?')}",
        f"  芯片材料       : {params.get('chip_material', 'silicon')}",
        f"  网格档位       : {params.get('mesh_quality', 'medium')}",
        f"  case_mode      : {case_mode}",
        "",
        "[求解结果]",
        f"  状态           : {converged_label}",
        f"  实际迭代步数   : {iters}",
        f"  全域最高温度   : {fmt_T(max_t_K)}",
        f"  出口平均温度   : {fmt_T(outlet_t_K)}",
        f"  耗时           : {elapsed:.1f} 秒",
    ]
    # W2 多的几行 (Q_volumetric / hotspot / chip_power_applied 警告)
    lines.extend(extra_lines)
    lines.extend([
        "",
        "[警戒线判定]",
        f"  {verdict_line}{elbow_note}",
        "",
        f"生成时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        sep,
        "",  # 末尾空行 → join 后产生末尾换行
    ])
    return "\n".join(lines)


# ============================================================
# 公开 API #2: analyze — 需要 session, 出 PNG + txt + json
# ============================================================

def analyze(
    session: Any,
    params: dict,
    run_summary: dict,
    output_dir: Optional[str] = None,
    limit_C: Optional[float] = None,
) -> dict:
    """
    完整分析: 拿 session 抓温度云图 PNG, 拼文字总结, 把所有产物写到磁盘。

    Args:
        session: PyFluent 会话(必须还活着, 没 exit 过)。
        params: 用户层参数 dict (chip_power_watts/inlet_velocity_ms/max_iterations)。
        run_summary: run_simulation 已经返回的数字 dict(在 wrapper 内, exit 之前)。
        output_dir: 报告写到哪个目录。None = 自动新建 reports/run_<时间戳>/。
        limit_C: 警戒线 °C, 默认 85。

    Returns:
        dict, 在 summarize() 输出基础上再加:
            output_dir (str):           报告目录的绝对路径
            contour_image_path (str):   云图 PNG 路径; PNG 失败时为 None
            report_text_path (str):     txt 报告路径
            result_json_path (str):     result.json 路径
            contour_skipped_reason (str|None): PNG 没生成的话, 写为什么跳过
    """
    # ---------- (1) 算文字部分 (用 summarize 复用同一套模板) ----------
    enriched = summarize(run_summary, params, limit_C=limit_C)

    # ---------- (2) 准备报告目录 ----------
    if output_dir is None:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = os.path.join(_DEFAULT_REPORTS_DIR, f"run_{ts}")
    output_dir = os.path.abspath(output_dir)
    # exist_ok=True: 目录已存在不报错(支持调用方传现成目录)
    os.makedirs(output_dir, exist_ok=True)
    print(f"[analyzer] 报告目录: {output_dir}")
    #注意output_dir 是_DEFAULT_REPORTS_DIR/run_<时间戳>/ 这样的目录
 
    # ---------- (3) 抓温度云图 PNG ----------
    png_path = os.path.join(output_dir, "temperature_contour.png")
    contour_skipped_reason: Optional[str] = None
    contour_image_path: Optional[str] = None

    ok, err_msg = _save_temperature_contour(session, png_path)
    if ok:
        contour_image_path = png_path
        print(f"[analyzer] 温度云图已保存: {png_path}")
    else:
        contour_skipped_reason = err_msg
        print(
            f"[analyzer][WARN] 云图生成跳过: {err_msg} -- "
            "txt 报告仍会生成。",
            file=sys.stderr,
        )

    # ---------- (4) 写 txt 报告 ----------
    txt_path = os.path.join(output_dir, "report.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(enriched["summary_text"])
        #summary的结果已经写入了, 所以这里再写一次, 是为了方便调试
        # 如果云图没生成, 在报告末尾追加一段说明 — 用户/导师不看 stderr 也能知道
        if contour_skipped_reason:
            f.write(
                "\n[注] 温度云图生成失败, 已跳过:\n"
                f"     原因: {contour_skipped_reason}\n"
            )
        else:
            f.write(f"\n温度云图: {os.path.basename(png_path)}\n")
    print(f"[analyzer] 文字报告已保存: {txt_path}")

    # ---------- (5) 把扩展后的 dict 也存一份 JSON, 给下游读 ----------
    enriched.update({
        "output_dir": output_dir,
        "contour_image_path": contour_image_path,
        "contour_skipped_reason": contour_skipped_reason,
        "report_text_path": txt_path,
    })
    json_path = os.path.join(output_dir, "result.json")
    with open(json_path, "w", encoding="utf-8") as f:
        # ensure_ascii=False: 中文不要被转义成 \uXXXX, 直接读得懂
        # default=str: 兜底处理 numpy 数字之类不可序列化的类型
        json.dump(enriched, f, ensure_ascii=False, indent=2, default=str)
        #json.dump 把内存里的数据，倾倒进文件里，保存成 JSON 格式。
        #enciched是需要倾倒的东西，f是倒入的对象

    enriched["result_json_path"] = json_path
    print(f"[analyzer] 结构化 JSON 已保存: {json_path}")

    # ---------- (6) [W4] 自动 PDF 报告 (失败不阻断, 只 warn) ----------
    # PDF 是答辩台上最有价值的产物 — 但 reportlab 字体加载偶尔抽风, 失败别让
    # 整个仿真因为 PDF 没生成就显得失败. 跟云图同样的"软失败"哲学.
    try:
        from tools.pdf_reporter import generate_pdf_report
        pdf_path = generate_pdf_report(
            enriched,
            iter_history=None,  # analyze 是单轮 hook, 不知道 graph 的 iter_history
            out_dir=output_dir,
        )
        enriched["pdf_report_path"] = pdf_path
    except Exception as e:
        print(f"[analyzer][WARN] PDF 报告生成失败 (非致命): "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        enriched["pdf_report_path"] = None
        enriched["pdf_skipped_reason"] = f"{type(e).__name__}: {e}"

    return enriched


# ============================================================
# 云图生成 — 多路 fallback
# ============================================================

def _save_temperature_contour(session: Any, png_path: str) -> tuple:
    """
    试多种方式生成温度云图 PNG。返回 (success: bool, error_msg: str)。

    PyFluent 的 graphics API 是版本最飘忽的部分, 所以一律按"试 → 失败就换"
    的模式走。最坏情况返回 (False, "..."), 由调用方决定怎么处理。

    顺序:
      (a) modern settings API (PyFluent 0.30+ 的 dict-like graphics 接口)
      (b) TUI /display/ 命令 (古老但稳定)
      (c) 都失败 → 返回 False
    """
    errors = []

    # ---------- 路径 a: modern settings API ----------
    try:
        ok = _try_modern_contour(session, png_path)
        if ok and os.path.exists(png_path):
            return True, ""
        if ok:
            errors.append("modern API: 调用没抛错, 但 PNG 没生成")
        else:
            errors.append("modern API: 路径不可用(可能是属性名变了 / surface 不存在)")
    except Exception as e:
        errors.append(f"modern API: {type(e).__name__}: {e}")

    # ---------- 路径 b: TUI ----------
    try:
        ok = _try_tui_contour(session, png_path)
        if ok and os.path.exists(png_path):
            return True, ""
        if ok:
            errors.append("TUI: 调用没抛错, 但 PNG 没生成")
        else:
            errors.append("TUI: 命令路径不可用 / 所有 surface 候选都失败")
    except Exception as e:
        errors.append(f"TUI: {type(e).__name__}: {e}")

    # ---------- 都没成 ----------
    # 把所有路径的失败原因拼一起返回, 排错方便。
    return False, " | ".join(errors)


def _try_modern_contour(session: Any, png_path: str) -> bool:
    """
    用 modern settings API 创建温度 contour 并保存为 PNG。
    PyFluent 0.30+ 提供 dict-like 的 graphics 接口, 但具体属性名在版本间偶尔变,
    所以 surfaces_list 也走 candidates 多次试。

    返回 True = API 调用没抛错(但 PNG 是否真生成还要外层 os.path.exists 复核)。
    """
    # 先拿到 graphics root。不同版本路径不同, try 一下。
    graphics = None
    for path_attempts in [
        lambda s: s.results.graphics,
        lambda s: s.solution.graphics,  # 老版本可能在这
    ]:
        #lambda = 一行写完的迷你小函数, 用于获取 graphics root
        try:
            graphics = path_attempts(session)
            break
        except AttributeError:
            continue
    if graphics is None:
        return False

    # 创建 contour 对象。contour 就是你要生成的那张云图对象，PyFluent 的 graphics.contour 是 dict-like 容器,
    # 用 .create(name) 或赋值都能创建。
    contour_name = "temp_contour"
    try:
        # 写法 1: graphics.contour.create(name)
        contour = graphics.contour.create(contour_name)
    except Exception:
        try:
            # 写法 2: 直接索引, 像字典
            graphics.contour[contour_name] = {}
            contour = graphics.contour[contour_name]
        except Exception:
            return False

    # 设字段为温度。属性名常见两种: field / contour_field, 兼容写。
    set_ok = False
    for field_attr in ["field", "contour_field"]:
        try:
            setattr(contour, field_attr, "temperature")
            #setattr是给对象设置属性的万能方法
            set_ok = True
            break
        except Exception:
            continue
    if not set_ok:
        return False

    # 设 surfaces_list。多个候选名字, 哪个不报错就用哪个。
    # 告诉云图：你要画在哪些面上
    for surfs in _CONTOUR_SURFACE_CANDIDATES:
        if surfs == ["()"]:
            continue  # "()" 是 TUI 通配符, modern API 用不了, 跳过
        try:
            contour.surfaces_list = surfs
            break
        except Exception:
            continue

    # 渲染 + 保存
    try:
        contour.display()
    except Exception:
        # 没 display 不一定致命, 有些版本 save_picture 会自动渲染当前对象
        pass

    # 保存 PNG。picture 子模块路径在版本间也变过。
    saved = False
    for save_attempts in [
        lambda g: g.picture.save_picture(file_name=png_path),
        lambda g: g.views.save_picture(file_name=png_path),
        lambda g: g.picture(file_name=png_path),
    ]:
        try:
            save_attempts(graphics)
            saved = True
            break
        except Exception:
            continue
    return saved


def _try_tui_contour(session: Any, png_path: str) -> bool:
    """
    走 TUI 命令生成 + 保存温度云图。
    TUI 比 modern API 稳, 但语法对参数顺序和 prompt 数量敏感, 试两套常用序列。
    """
    # 先设图片驱动 + 分辨率。出错也不要紧 — 后面 save-picture 会用默认值。
    try:
        session.tui.display.set.picture.driver("png")
    except Exception:
        pass
    try:
        session.tui.display.set.picture.x_resolution(_PNG_X_RES)
        session.tui.display.set.picture.y_resolution(_PNG_Y_RES)
    except Exception:
        pass

    # 创建 contour 对象。TUI 路径: /display/objects/create contour <name>
    # 这条命令会进入 prompt 模式问 field / surfaces, PyFluent 把答案当位置参数。
    contour_name = "temp_tui"
    created = False
    for surfs in _CONTOUR_SURFACE_CANDIDATES:
        try:
            args = [
                "contour",         # 类型: contour
                contour_name,      # 对象名
                "field",           # 进入 set field 子模式
                "temperature",
                "surfaces-list",   # 进入 set surfaces-list 子模式
                *surfs,            # 候选 surfaces
                "()",              # 结束 surfaces 选择
                "quit",            # 退出 create 上下文
            ]
            session.tui.display.objects.create(*args)
            created = True
            break
        except Exception:
            continue
    if not created:
        return False

    # 显示 contour
    try:
        session.tui.display.objects.display(contour_name)
    except Exception:
        pass

    # 保存图片。Fluent TUI 是 /display/save-picture <path>。
    # PyFluent 把它映射成 session.tui.display.save_picture("path")。
    try:
        session.tui.display.save_picture(png_path.replace("\\", "/"))
        return True
    except Exception:
        # 试老的 /display/hardcopy 路径
        try:
            session.tui.display.hardcopy(png_path.replace("\\", "/"))
            return True
        except Exception:
            return False


# ============================================================
# 自测试: 纯 Python, 不需要 Fluent
# ============================================================

# 几个 fake run_summary, 覆盖不同场景。每个一组(标签, run_summary, params, 期望要点)。
_SELFTEST_CASES = [
    #(
    #"场景名字",    # 1. 这是啥情况
    #假的仿真结果,  # 2. 模拟 Fluent 跑出的数据
    #假的输入参数,  # 3. 模拟用户输入的功率、风速
    #期望的结果     # 4. 正确答案应该是什么）
    
    (
        "mixing-elbow 典型(40 °C, 远低于 85 警戒线)",
        {
            "max_temp_K": 313.15,
            "max_temp_C": 40.00,
            "outlet_avg_temp_K": 295.71,
            "outlet_avg_temp_C": 22.56,
            "converged": True,
            "iterations": 200,
            "elapsed_seconds": 78.3,
        },
        {"chip_power_watts": 15.0, "inlet_velocity_ms": 2.0, "max_iterations": 200},
        {"within_spec": True, "margin_positive": True},
    ),
    
    (
        "假想芯片超温(95 °C, 超过 85 警戒线)",
        {
            "max_temp_K": 368.15,   # = 95 °C
            "max_temp_C": 95.00,
            "outlet_avg_temp_K": 320.0,
            "outlet_avg_temp_C": 46.85,
            "converged": True,
            "iterations": 250,
            "elapsed_seconds": 95.7,
        },
        {"chip_power_watts": 50.0, "inlet_velocity_ms": 1.0, "max_iterations": 300},
        {"within_spec": False, "margin_positive": False},
    ),
    (
        "求解失败(NaN max_temp)",
        {
            "max_temp_K": float("nan"),
            "outlet_avg_temp_K": float("nan"),
            "converged": False,
            "iterations": 0,
            "elapsed_seconds": 12.5,
        },
        {"chip_power_watts": 10.0, "inlet_velocity_ms": 1.0, "max_iterations": 150},
        {"within_spec": False, "margin_positive": None},
    ),
]


def _selftest() -> int:
    """
    文字部分自测试: 跑 3 个 fake case, 验 within_spec 判定 + 模板渲染。
    返回 0 = PASS, 非 0 = FAIL。
    """
    print("=" * 60)
    print("自测试 (纯 Python, 不需要 Fluent)")
    print("=" * 60)

    n_pass = 0
    n_fail = 0
    for i, (label, run_summary, params, expected) in enumerate(_SELFTEST_CASES, 1):
        print(f"\n--- Case {i}/{len(_SELFTEST_CASES)}: {label} ---")
        try:
            r = summarize(run_summary, params)

            # 校验 within_spec 判定
            ok = True
            if r["within_spec"] != expected["within_spec"]:
                print(f"  [FAIL] within_spec={r['within_spec']}, "
                      f"期望 {expected['within_spec']}")
                ok = False

            # 校验 margin 符号
            mp = expected["margin_positive"]
            if mp is True and (r["margin_C"] is None or r["margin_C"] < 0):
                print(f"  [FAIL] margin_C={r['margin_C']}, 期望正")
                ok = False
            elif mp is False and (r["margin_C"] is None or r["margin_C"] >= 0):
                print(f"  [FAIL] margin_C={r['margin_C']}, 期望负")
                ok = False
            elif mp is None and r["margin_C"] is not None:
                print(f"  [FAIL] margin_C={r['margin_C']}, 期望 None(NaN 不可比)")
                ok = False

            # 校验文字非空且包含关键词
            if not r["summary_text"] or "SciAgent-Fluent" not in r["summary_text"]:
                print("  [FAIL] summary_text 缺失或不含项目名标题")
                ok = False

            # 把渲染出的报告片段打几行, 肉眼瞄一眼格式对不对
            preview = "\n      ".join(r["summary_text"].splitlines()[:6])
            print(f"  报告前几行预览:\n      {preview}")

            if ok:
                print("  [OK]")
                n_pass += 1
            else:
                n_fail += 1

        except Exception as e:
            print(f"  [FAIL] 抛异常: {type(e).__name__}: {e}")
            traceback.print_exc()
            n_fail += 1

    print("\n" + "=" * 60)
    print(f"Summary: {n_pass} PASS / {n_fail} FAIL "
          f"(共 {len(_SELFTEST_CASES)})")
    print("=" * 60)

    if n_fail == 0:
        print("文字部分 PASSED — summarize() 可用。")
        print("(云图部分要 Fluent session, 跑 _full_selftest 验证)")
        return 0
    return 1


def _full_selftest() -> int:
    """
    完整自测试: 真起一次 Fluent, 跑 mixing-elbow, 出云图 + txt + json。
    用时 2-5 分钟, 占一个 license 槽。要确认本机能跑通才用 --full。
    """
    print("=" * 60)
    print("完整自测试 (会真起 Fluent, 用时 2-5 分钟)")
    print("=" * 60)

    # 延迟 import — 避免单跑 _selftest 时也要装 PyFluent
    try:
        # 把项目根加进 sys.path, 才能 import tools.fluent_wrapper
        # 之所以不在文件顶部 import: agents/ 目录被独立用作单测时, 不一定有
        # tools/ 在 sys.path 里。
        if _PROJECT_ROOT not in sys.path:
            sys.path.insert(0, _PROJECT_ROOT)
        from tools.fluent_wrapper import run_simulation
    except ImportError as e:
        print(f"[FAIL] 无法导入 run_simulation: {e}", file=sys.stderr)
        return 2

    # 拿一组中等参数跑 mixing-elbow
    params = {
        "chip_power_watts": 15.0,
        "inlet_velocity_ms": 1.0,
        "max_iterations": 100,
    }
    print(f"参数: {params}")

    # 关键: 把 analyze 作为 analyzer hook 传给 run_simulation, 这样 wrapper 在
    # session.exit() 之前会调它, 让我们能拿到活的 session 抓云图。
    def hook(session, current_result):
        return analyze(session, params, current_result)

    try:
        result = run_simulation(params, analyzer=hook)
    except TypeError as e:
        # 如果 wrapper 还没加 analyzer 参数, 给一个友好提示
        if "analyzer" in str(e):
            print(
                "[FAIL] tools/fluent_wrapper.run_simulation 还没接 analyzer hook。\n"
                "       请把改动同步到 wrapper(见 result_analyzer.py 文档顶部)。",
                file=sys.stderr,
            )
            return 3
        raise
    except Exception as e:
        print(f"[FAIL] 仿真本身失败: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc()
        return 4

    # 校验文件落地
    print(f"\nresult keys: {sorted(result.keys())}")
    txt_path = result.get("report_text_path")
    json_path = result.get("result_json_path")
    png_path = result.get("contour_image_path")

    fail = False
    if not (txt_path and os.path.exists(txt_path)):
        print(f"[FAIL] txt 报告未生成: {txt_path}", file=sys.stderr)
        fail = True
    if not (json_path and os.path.exists(json_path)):
        print(f"[FAIL] result.json 未生成: {json_path}", file=sys.stderr)
        fail = True

    # PNG 是"尽力而为", 没生成只是 warn 不算 FAIL
    if png_path and os.path.exists(png_path):
        print(f"[OK] PNG: {png_path}")
    else:
        skip_reason = result.get("contour_skipped_reason", "unknown")
        print(f"[WARN] PNG 跳过: {skip_reason}")

    if fail:
        return 5

    print("\n完整自测试 PASSED — txt/json 已生成, "
          "PNG 取决于本机 Fluent 版本能否生成。")
    return 0


def _cli() -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        prog="python -m agents.result_analyzer",
        description="result analyzer: 仿真结果 → 云图 PNG + txt 报告 + json",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="跑完整自测试(会真起 Fluent, 2-5 分钟)。不加 = 只跑文字部分自测试。",
    )
    args = parser.parse_args()

    if args.full:
        return _full_selftest()
    return _selftest()


if __name__ == "__main__":
    # python -m agents.result_analyzer        → _selftest (1 秒, 无 Fluent)
    # python -m agents.result_analyzer --full → _full_selftest (~5 分钟, 跑 Fluent)
    sys.exit(_cli())

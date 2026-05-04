"""
tools/fluent_wrapper_w2.py
============================================================
manifold + chip_power 真生效版的 run_simulation。

跟 W1 tools/fluent_wrapper.py 的关系:
  W1 wrapper 走 mixing-elbow, chip_power_watts 只回传不施加 (架构决策 #2 留的钩子);
  W2 wrapper 走 manifold_solution (已 setup CHT case), chip_power_watts 真转成
  Q_volumetric 加到 solid_up cell zone 上, 让 max_temp 真随 query 变.

为什么不在 W1 wrapper 上加 if/else:
  W1 demo 路径和录屏脚本调用的是 W1 wrapper, 任何破坏性修改都会让那条
  路径"看起来还在但实际跑不了". 拆两份, W1 路径冻结不动, W2 走新文件.
  graph/pipeline.py 通过 case_mode 选哪个.

API 锚点 (baseline 验证过, 100% 在 PyFluent 0.38.1 + Fluent 2026 R1 工作):
  1. case + data 加载: settings.file.read_case + settings.file.read_data
  2. 体积/最高温度查询: settings.results.report.volume_integrals
       .get_maximum(cell_zones=[zone], cell_function="temperature")
       .get_volume(cell_zones=[zone], cell_function="temperature")
  3. 体积热源 (W/m³): settings.setup.cell_zone_conditions.solid[zone].sources
       .enable = True
       .terms.create(name="energy") + terms["energy"][0].option = "value" + .value = Q

物理映射 (重要 — 答辩 known limit):
  query 里 chip_power_watts 默认 15W. 如果直接 Q = power / V_solid_up (691 cm³),
  Q 太小, max_temp 涨不到 1 K, demo 看不动.
  采用约定: Q = chip_power_watts / chip_volume_default_m3 (默认 1e-5 m³ = 10 cm³).
  Q 加到 solid_up 全 691 cm³ 上 — 等价于"manifold 管壁里嵌了 10 cm³ 芯片,
  chip 功率均匀摊到 manifold 整个管壁". 这是工程简化, README known limit #2 主动声明.
  W4 / 后续真做 PCB+chip 案例时, 切换 case + 真在 chip-die cell zone 上加 Q,
  这一层封装就能复用.

返回的 result dict schema (跟 W1 wrapper 完全兼容):
  - max_temp_K / max_temp_C
  - outlet_avg_temp_K / outlet_avg_temp_C  (W2 仍提供, 取自 manifold outlet 面)
  - converged / iterations / elapsed_seconds
  - velocity_applied: 这里指 inlet_velocity 是否成功设到 manifold 的 inlet
  - chip_power_applied: 新增字段 — chip_power 是否成功设到 source_terms
  - params_echo
  + analyzer hook 的扩展字段
"""
import os
import sys
import time
import traceback
from typing import Callable, Optional


# ============================================================
# 全局常量 — 改这里就能调"基线", 不动函数体
# ============================================================

# [W2 vs W1 #1] 案例换了
#   W1: CASE_REMOTE = ("mixing_elbow.cas.h5", "pyfluent/mixing_elbow")  — 只有 cas, 冷启动
#   W2: cas + dat 两个文件, 走 hot-start (从已收敛态出发, 改完 source 跑 50 步即可)
CASE_FILENAME = "manifold_solution.cas.h5"
DAT_FILENAME = "manifold_solution.dat.h5"

# [W2 vs W1 #2] 全新常量 — W1 没有 chip zone 概念
# inspect 验证: solid_up 是 manifold 唯一的 solid zone, chip_power 加这里.
# [W4 升级] 不再 hardcoded — 走 tools/geometry_inspector.discover_zones 自动识别,
# 这里只作为 fallback 兜底, 让 manifold 案例之外的新 case 也能跑.
CHIP_ZONE_FALLBACK = "solid_up"
CHIP_ZONE = CHIP_ZONE_FALLBACK  # 运行时被 discover 覆盖, 保留全局常量给老代码引用

# [W2 vs W1 #3] 全新常量 — W1 chip_power 只 print 不施加, 不需要这个换算因子
# 物理映射: 用户层 W → Fluent 层 W/m³. 默认隐含 chip volume = 10 cm³.
# 这个虚拟体积是 demo 妥协 — 用 solid_up 真实 691 cm³ 会让 15W ΔT < 1 K 看不见.
DEFAULT_CHIP_VOLUME_M3 = 1.0e-5

# [W2 vs W1 #4] inlet 名变了
#   W1: target_inlet = "cold-inlet" (mixing-elbow 的冷水入口, 写在函数体里)
#   W2: PRIMARY_INLET = "inlet"     (manifold 主热气入口, 抽到常量便于改)
PRIMARY_INLET = "inlet"

# [W2 vs W1 #5] outlet 抽常量 (W1 写在 _query_outlet_avg_temperature 函数体里 hardcode "outlet")
OUTLET_FACE = "outlet"

# 启动 Fluent 用几个核. manifold 28万 cell, 给 2 核 (跟 W1 一样)
N_PROC = 2

# [W2 vs W1: 一致] 必填键复用 W1 schema, 用户接口稳定不动 — 这就是 W1 留接口的兑现
_REQUIRED_KEYS = ("chip_power_watts", "inlet_velocity_ms", "max_iterations")

# [W2 vs W1 #6] 全新常量 — W1 用 hybrid_initialize 冷启 + iter=200, W2 hot-start 50 步够
DEFAULT_W2_ITERATIONS_FROM_HOTSTART = 50


# ============================================================
# 公开 API: run_simulation (跟 W1 wrapper 同名, schema 兼容)
# ============================================================

def run_simulation(
    params: dict,
    analyzer: Optional[Callable[[object, dict], Optional[dict]]] = None,
    adapt_levels: int = 0,
) -> dict:
    """
    跑一次 manifold CHT 仿真, chip_power 真转成体积热源加到 solid_up.

    Args:
        params: 跟 W1 wrapper 完全相同的 schema (chip_power_watts / inlet_velocity_ms / max_iterations).
        analyzer: 同 W1 — fn(session, partial_result) -> Optional[dict].
        adapt_levels: [W4+ mesh study] 在 baseline mesh 上做几次 refine.
            0 = 原网格不动 (~290K cells, 默认行为, 跟 W2 完全兼容)
            1 = refine 一次 (~580K cells, ~2x baseline)
            2 = refine 两次 (~1.16M cells, ~4x baseline)
            >=3 不推荐 (Student license 4 核扛不住)
            注意: 加密在 read_data 之后立即做, 不动已收敛温度场拓扑.

    Returns:
        result dict (字段跟 W1 wrapper 兼容, agents/result_analyzer.py 能直接消费):
          - max_temp_K / max_temp_C: solid_up 的最高温度 (K, °C)
          - outlet_avg_temp_K / outlet_avg_temp_C: outlet 面质量加权平均温度
          - converged: 简化判定 = "走完没崩 + max_temp 物理合理"
          - iterations / elapsed_seconds
          - velocity_applied (bool): inlet 速度是否真改进去
          - chip_power_applied (bool): source_terms 是否真设进去 (W2 新增)
          - chip_power_Q_volumetric (float): 实际加的 W/m³ 值 (排错用)
          - params_echo: dict, 原样回传
          - case_mode: str = "manifold_cht" (W2 新增, 排错用)

    Raises:
        ImportError / TypeError / KeyError / ValueError / RuntimeError: 同 W1 wrapper.
    """
    # ---------- (1) 校验参数 (跟 W1 wrapper 共用同一份逻辑) ----------
    _validate_params(params)

    # ---------- (2) 延迟 import PyFluent ----------
    try:
        import ansys.fluent.core as pyfluent
    except ImportError as e:
        raise ImportError(
            "ansys-fluent-core 未安装. 激活 .venv 后 pip install -r requirements.txt"
        ) from e

    # ---------- (3) 定位 case + dat 文件 ----------
    cas_path = _resolve_case_file(CASE_FILENAME)
    dat_path = _resolve_case_file(DAT_FILENAME)

    # ---------- (4) 启 Fluent ----------
    t_total = time.time()
    print(f"[w2-wrapper] 启动 Fluent (no_gui, {N_PROC} 核)...")
    session = pyfluent.launch_fluent(
        mode="solver",
        ui_mode="no_gui",
        dimension=3,
        precision="double",
        processor_count=N_PROC,
        cleanup_on_exit=True,
    )

    # 先声明结果变量, finally 用 + NaN 防误读"成功"
    converged = False
    max_t = float("nan")
    outlet_avg_t = float("nan")
    velocity_applied = False
    chip_power_applied = False
    Q_volumetric = 0.0
    chip_zone_runtime = CHIP_ZONE_FALLBACK  # [W4] 几何感知运行时识别, try 失败时用 fallback
    pressure_drop_pa = None
    cell_count = None
    analyzer_extra: dict = {}

    try:
        # ---------- (5) read_case + read_data ----------
        # [W2 vs W1 #7] W1 只 read_case + hybrid_initialize 冷启动;
        # W2 多了 read_data 加载已收敛解, 之后跳过 initialize 直接 iterate.
        # 时间省 75% (W1 200 步 ≈ 4 分钟, W2 50 步 ≈ 1 分钟)
        print(f"[w2-wrapper] read_case {os.path.basename(cas_path)} ...")
        session.settings.file.read_case(file_name=cas_path)
        print(f"[w2-wrapper] read_data {os.path.basename(dat_path)} ...")
        session.settings.file.read_data(file_name=dat_path)
        print("[w2-wrapper] case + data 加载完成 (从已收敛态出发, hot-start)")

        # [W4+ mesh study] 可选 mesh adapt - refine 几次
        if adapt_levels and adapt_levels > 0:
            print(f"[w2-wrapper][mesh] 启动 mesh adapt: refine {adapt_levels} 次...")
            _apply_mesh_refine(session, n=adapt_levels)

        # [W4 几何感知] 自动识别 chip zone, 不再 hardcode "solid_up"
        # 老师答辩问题 "换案例还能跑吗" 的硬正面回答 — 这一段就是答案.
        try:
            from tools.geometry_inspector import find_chip_zone
            chip_zone_runtime = find_chip_zone(session, fallback=CHIP_ZONE_FALLBACK)
            print(f"[w2-wrapper][geo] chip_zone 自动识别: {chip_zone_runtime}")
        except Exception as e:
            print(f"[w2-wrapper][geo][WARN] 几何感知失败, 用 fallback: "
                  f"{CHIP_ZONE_FALLBACK} ({e})", file=sys.stderr)
            # chip_zone_runtime 保持 try 外预设的 fallback

        # [W2 vs W1 #8] W1 在这里有 _check_mesh(session) 跑网格检查;
        # W2 跳过, 因为 manifold 是 Ansys 官方已 setup case, 网格已被 Ansys 验证过.
        # 这也是 docs/网格策略.md 里答辩话术的依据.

        # ---------- (6) 应用 user 参数: 速度 + 体积热源 ----------
        velocity_applied = _apply_inlet_velocity(
            session, params["inlet_velocity_ms"]
        )

        # [W2 vs W1 #9] 核心差异 — W1 这里只调 _record_chip_power 打印一行就完了;
        # W2 真做 W → W/m³ 换算, 调 _apply_chip_power 把 source 设进 Fluent.
        # 这是整个 W2 wrapper 存在的唯一理由 — 关掉 W1 known limit.
        Q_volumetric = params["chip_power_watts"] / DEFAULT_CHIP_VOLUME_M3
        chip_power_applied = _apply_chip_power(
            session, chip_zone_runtime, Q_volumetric
        )

        # ---------- (7) 求解 ----------
        # [W2 vs W1 #10] W1 这里调 _initialize(session) 做 hybrid_initialize 冷启;
        # W2 不重新初始化 — read_data 已给我们已收敛场, 重 init 反而把已有解抹掉.
        n_iter = max(params["max_iterations"], 1)
        print(f"[w2-wrapper] iterate {n_iter} 步 (hot-start)...")
        try:
            session.settings.solution.run_calculation.iterate(iter_count=n_iter)
        except Exception as e:
            print(f"[w2-wrapper][WARN] settings iterate 失败 ({e}), 走 TUI...",
                  file=sys.stderr)
            session.tui.solve.iterate(n_iter)

        # ---------- (8) 查 max_temp + outlet_avg_temp ----------
        # [W2 vs W1 #11] 查询 API 完全换了
        #   W1: TUI 命令把结果写到临时文件, 再用正则抠数字 (老路, 兼容性最好)
        #   W2: modern Query API 直接返回数字 (baseline 验证过, 不需要写文件)
        #   _query_max_temp 内部用的是 settings.results.report.volume_integrals.get_maximum
        max_t = _query_max_temp(session, chip_zone_runtime)
        if max_t is None:
            print(f"[w2-wrapper][WARN] solid_up max_temp 查询失败, 用 NaN",
                  file=sys.stderr)
            max_t = float("nan")
        else:
            print(f"[w2-wrapper] solid_up max_temp = {max_t:.2f} K "
                  f"({max_t - 273.15:.2f} °C)")

        outlet_avg_t = _query_outlet_avg_temp(session)
        if outlet_avg_t is None:
            print(f"[w2-wrapper][WARN] outlet 平均温度查询失败, 用 NaN",
                  file=sys.stderr)
            outlet_avg_t = float("nan")
        else:
            print(f"[w2-wrapper] outlet 平均温度 = {outlet_avg_t:.2f} K "
                  f"({outlet_avg_t - 273.15:.2f} °C)")

        # [W4+ mesh study] 查压降 + cell 数 (失败不致命, 写 None)
        pressure_drop_pa = None
        cell_count = None
        try:
            from tools.pressure_drop_query import query_pressure_drop, query_cell_count
            pressure_drop_pa = query_pressure_drop(
                session, inlet_face=PRIMARY_INLET, outlet_face=OUTLET_FACE,
            )
            cell_count = query_cell_count(session)
        except Exception as e:
            print(f"[w2-wrapper][WARN] pressure_drop/cell_count 查询异常: {e}",
                  file=sys.stderr)

        # ---------- (8.5) 简化收敛判定 ----------
        # manifold 案例热气 ~ 850 K, solid_up 在加热源后到 1000-2000 K 也合理
        # (baseline 验证: 5000W → 1616 K). 给 200-3000 K 这个宽松范围.
        if 200.0 < max_t < 3000.0:
            converged = True

        # ---------- (8.6) analyzer hook ----------
        # 跟 W1 wrapper 完全一样的 hook 协议 — analyzer 不知道我们换了案例,
        # 它只看 result dict 的字段. 这就是"schema 兼容"的好处.
        if analyzer is not None:
            try:
                # hotspot: 写明最高温度在哪个 zone. 精确 XYZ PyFluent 0.38.1
                # 没暴露统一接口 (Reduction service 只有 maximum 没有 location),
                # 所以只填 zone+T_K 让 analyzer 模板出"热点位置"那行.
                # W3 真做"几何感知"时再考虑 services.field_data 取 cell location.
                hotspot = {"zone": chip_zone_runtime, "T_K": round(max_t, 2)} \
                    if max_t == max_t else None  # NaN check

                partial = {
                    "max_temp_K": round(max_t, 2),
                    "max_temp_C": round(max_t - 273.15, 2),
                    "outlet_avg_temp_K": round(outlet_avg_t, 2),
                    "outlet_avg_temp_C": round(outlet_avg_t - 273.15, 2),
                    "converged": converged,
                    "iterations": n_iter,
                    "elapsed_seconds": round(time.time() - t_total, 1),
                    "velocity_applied": velocity_applied,
                    "chip_power_applied": chip_power_applied,
                    "chip_power_Q_volumetric": round(Q_volumetric, 2),
                    "params_echo": dict(params),
                    "case_mode": "manifold_cht",
                    "hotspot": hotspot,
                    "chip_material_echo": params.get("chip_material", "silicon"),
                    "mesh_quality_echo": params.get("mesh_quality", "medium"),
                    "pressure_drop_pa": (round(pressure_drop_pa, 2)
                                         if pressure_drop_pa is not None else None),
                    "cell_count": cell_count,
                    "adapt_levels": adapt_levels,
                }
                ret = analyzer(session, partial)
                if isinstance(ret, dict):
                    analyzer_extra = ret
                elif ret is not None:
                    print(f"[w2-wrapper][WARN] analyzer 返回非 dict "
                          f"({type(ret).__name__}), 已忽略", file=sys.stderr)
            except Exception as e:
                print(f"[w2-wrapper][WARN] analyzer hook 抛异常 (非致命): "
                      f"{type(e).__name__}: {e}", file=sys.stderr)
                traceback.print_exc()

    except Exception as e:
        traceback.print_exc()
        raise RuntimeError(
            f"run_simulation (W2/manifold) 失败: {type(e).__name__}: {e}"
        ) from e

    finally:
        # 必关 Fluent — 学生版 license 紧张
        if session is not None:
            try:
                session.exit()
                print("[w2-wrapper] Fluent 干净退出")
            except Exception as e:
                print(f"[w2-wrapper][WARN] session.exit() 抛错(非致命): {e}",
                      file=sys.stderr)

    # ---------- (9) 组装 result dict 返回 ----------
    # [W2 vs W1 #12] result 字段对比
    #   完全相同: max_temp_K/C, outlet_avg_temp_K/C, converged, iterations,
    #             elapsed_seconds, velocity_applied, params_echo
    #   W2 新增: chip_power_applied (源是否真设上), chip_power_Q_volumetric (实际加的 W/m³),
    #           case_mode (= "manifold_cht"), hotspot ({zone, T_K}),
    #           chip_material_echo / mesh_quality_echo (schema 扩字段, 仅回显)
    # schema 兼容是关键 — agents/result_analyzer.py / graph 上层完全不用改
    hotspot = {"zone": chip_zone_runtime, "T_K": round(max_t, 2)} \
        if max_t == max_t else None
    result = {
        "max_temp_K": round(max_t, 2),
        "max_temp_C": round(max_t - 273.15, 2),
        "outlet_avg_temp_K": round(outlet_avg_t, 2),
        "outlet_avg_temp_C": round(outlet_avg_t - 273.15, 2),
        "converged": converged,
        "iterations": params["max_iterations"],
        "elapsed_seconds": round(time.time() - t_total, 1),
        "velocity_applied": velocity_applied,
        "chip_power_applied": chip_power_applied,
        "chip_power_Q_volumetric": round(Q_volumetric, 2),
        "params_echo": dict(params),
        "case_mode": "manifold_cht",
        "hotspot": hotspot,
        "chip_material_echo": params.get("chip_material", "silicon"),
        "mesh_quality_echo": params.get("mesh_quality", "medium"),
        "pressure_drop_pa": (round(pressure_drop_pa, 2)
                             if pressure_drop_pa is not None else None),
        "cell_count": cell_count,
        "adapt_levels": adapt_levels,
    }
    if analyzer_extra:
        result.update(analyzer_extra)
    return result


# ============================================================
# 参数校验 (跟 W1 wrapper 等价 — 复制而不是 import 因为 W1 wrapper 注释体积大)
# ============================================================

def _validate_params(params: dict) -> None:
    """简单 schema 校验. 跟 W1 wrapper 一致, 让 query schema 跨案例稳定."""
    if not isinstance(params, dict):
        raise TypeError(f"params 必须是 dict, 实际是 {type(params).__name__}")
    missing = [k for k in _REQUIRED_KEYS if k not in params]
    if missing:
        raise KeyError(f"params 缺少必要键: {missing}")
    if not (params["inlet_velocity_ms"] > 0):
        raise ValueError(
            f"inlet_velocity_ms 必须 > 0, 实际为 {params['inlet_velocity_ms']}"
        )
    if params["max_iterations"] < 10:
        raise ValueError(
            f"max_iterations 必须 >= 10, 实际为 {params['max_iterations']}"
        )
    if params["chip_power_watts"] < 0:
        raise ValueError(
            f"chip_power_watts 必须 >= 0, 实际为 {params['chip_power_watts']}"
        )


# ============================================================
# 文件定位 (跟 W1 wrapper 同款套路, 抄过来)
# ============================================================

def _resolve_case_file(filename: str) -> str:
    """
    返回项目本地 case/dat 文件的绝对路径.
    已经把 manifold_solution.cas.h5 / .dat.h5 下到项目根, 不再 fallback 远程.
    (远程下载踩过 GitHub 404 的坑, 走本地最稳.)
    """
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    candidates = [
        os.path.join(project_root, filename),
        os.path.join(os.getcwd(), filename),
    ]
    for p in candidates:
        if os.path.isfile(p):
            print(f"[w2-wrapper] 命中本地: {p}")
            return p
    raise RuntimeError(
        f"找不到 {filename}. 请确认已下载到项目根:\n"
        f"  curl -O https://github.com/ansys/example-data/raw/main/"
        f"pyfluent/exhaust_manifold/{filename}"
    )


# ============================================================
# 应用 user 参数: inlet 速度
# ============================================================

def _apply_inlet_velocity(session, vmag: float) -> bool:
    """
    把 manifold 主 inlet 的速度大小改成 vmag.
    其他 2 个 inlet (inlet1 / inlet2) 保留案例默认值 — 只改一个 inlet,
    跟 W1 mixing-elbow 同样的设计哲学 (踩坑 #6: 同时改多个会让流量比不变,
    看不出参数影响).
    """
    # 路径 1: modern API
    try:
        bc = session.settings.setup.boundary_conditions.velocity_inlet[PRIMARY_INLET]
        # PyFluent 0.30+ 不同案例下字段路径有差异, 多写法都试
        for setter in (
            lambda: setattr(bc.momentum.velocity, "value", vmag),
            lambda: setattr(bc.momentum, "velocity", vmag),
            lambda: setattr(bc, "vmag", vmag),
        ):
            try:
                setter()
                print(f"[w2-wrapper] {PRIMARY_INLET} 速度 = {vmag} m/s "
                      "(modern API)")
                return True
            except (AttributeError, TypeError, KeyError):
                continue
    except (AttributeError, KeyError):
        pass

    # 路径 2: TUI fallback
    try:
        session.tui.define.boundary_conditions.set.velocity_inlet(
            PRIMARY_INLET, "()", "vmag", "no", vmag, "quit",
        )
        print(f"[w2-wrapper] {PRIMARY_INLET} 速度 = {vmag} m/s (TUI)")
        return True
    except Exception as e:
        print(f"[w2-wrapper][WARN] {PRIMARY_INLET} 速度未改成 {vmag} m/s "
              f"({e}), 用案例默认值", file=sys.stderr)
        return False


# ============================================================
# 应用 user 参数: chip_power → 体积热源 (baseline 验证过的 API)
# ============================================================

def _apply_chip_power(session, zone: str, Q_volumetric: float) -> bool:
    """
    [W2 vs W1 #13] 这是 W2 wrapper 独有的函数 — W1 wrapper 里对应的是 _record_chip_power,
    那个函数只 print 一行不动 Fluent. 本函数是"生产化"版本:
      - 把多路径 fallback 收敛到唯一显式路径 (baseline 验证 path-explicit 最稳)
      - 加 Q==0 baseline 短路 (关 sources.enable 不动 terms)
      - 失败返回 False 而不是 raise — 跟 _apply_inlet_velocity 同样的"软失败"哲学

    """
    print(f"[w2-wrapper] chip_power 设置: zone={zone}, Q={Q_volumetric:.4e} W/m^3")

    try:
        czc = session.settings.setup.cell_zone_conditions.solid[zone]
    except Exception as e:
        print(f"[w2-wrapper][WARN] 找不到 solid[{zone}]: {e}", file=sys.stderr)
        return False

    # Q == 0: 关 source 即可 (baseline)
    if Q_volumetric == 0:
        try:
            czc.sources.enable = False
            print(f"[w2-wrapper] sources.enable = False (Q=0, baseline)")
            return True
        except Exception as e:
            print(f"[w2-wrapper][WARN] 关 sources.enable 失败: {e}",
                  file=sys.stderr)
            return False

    # Q > 0: 显式 schema 路径 (path-explicit 验证 OK)
    try:
        czc.sources.enable = True
        # terms 是 NamedObject, 不存在就 create
        existing = list(czc.sources.terms) if hasattr(
            czc.sources.terms, "__iter__"
        ) else []
        if "energy" not in existing:
            try:
                czc.sources.terms.create(name="energy")
            except TypeError:
                czc.sources.terms.create("energy")  # 老版本只接位置参
        term = czc.sources.terms["energy"]
        try:
            term.resize(1)
        except Exception:
            pass  # ListObject 已是长度 1 时 resize 可能 no-op
        term[0].option = "value"     # ← 关键: 不是 "constant"
        term[0].value = Q_volumetric
        print(f"[w2-wrapper] sources.terms[energy][0] = (value, "
              f"{Q_volumetric:.4e}) OK")
        return True
    except Exception as e:
        print(f"[w2-wrapper][WARN] sources.terms 设置失败: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return False


# ============================================================
# 查询 (baseline 验证过的 modern Query API)
# ============================================================

def _apply_mesh_refine(session, n: int = 1) -> None:
    """
    [W4+ mesh study] 把当前 mesh refine n 次 (每次大致 2x cell 数).
    用 PyFluent adapt API. 失败 raise — mesh study 流程靠 cell_count 字段判定是否成功.

    实现策略:
      用 region register 全域 mark 然后 refine. 这是 PyFluent 0.38.1 上跨案例最稳的写法.
      不用 isovalue / cell-zone register, 因为不同 case 的 zone 名字不一样.
    """
    for i in range(n):
        applied = False
        # 路径 1: modern API
        try:
            adapt = session.settings.solution.adapt
            try:
                # adapt-options 默认 anisotropic refine, 全域 register
                adapt.refine.refine()
                applied = True
            except Exception:
                pass
        except Exception:
            pass

        # 路径 2: TUI - mark 全域 + refine
        if not applied:
            try:
                # mark all cells -> /adapt/mark-inout-cells 不行, 用 adapt-cells-around-region
                # 最简单的: /adapt/refine-mesh 全网格 一次
                session.tui.adapt.refine_mesh("yes")
                applied = True
            except Exception as e:
                print(f"[w2-wrapper][mesh][WARN] refine_mesh TUI 失败: {e}",
                      file=sys.stderr)

        # 路径 3: TUI 老接口 adapt-set + refine-marked-cells
        if not applied:
            try:
                # 全网格 register: /adapt/set/min-cells-per-face / mark-all
                session.tui.adapt.mark_all_cells()
                session.tui.adapt.refine_marked_cells()
                applied = True
            except Exception as e:
                print(f"[w2-wrapper][mesh][WARN] mark_all+refine_marked 也失败: {e}",
                      file=sys.stderr)

        if not applied:
            raise RuntimeError(
                f"_apply_mesh_refine 第 {i+1} 次 refine 失败 — 所有路径都不通"
            )
        print(f"[w2-wrapper][mesh] refine #{i+1} 完成")


def _query_max_temp(session, zone: str) -> Optional[float]:
    """solid_up max_temp (K). Fluent 2026 R1 自己提示的方法."""
    try:
        r = session.settings.results.report.volume_integrals.get_maximum(
            cell_zones=[zone],
            cell_function="temperature",
        )
        v = _coerce_float(r)
        # 上限放宽到 5000 K — 200W/10cm³ = 2e7 W/m³ 真能把 solid_up 烧到 3000+ K,
        # 这不是 query 抽风, 是 W2 demo 故意拉到极端工况展示 chip_power 单调驱动.
        if v is not None and 200.0 <= v <= 5000.0:
            return v
        return None
    except Exception as e:
        print(f"[w2-wrapper][WARN] get_maximum 失败: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return None


def _query_outlet_avg_temp(session) -> Optional[float]:
    """
    outlet 面 mass-weighted 平均温度.
    surface_integrals 跟 volume_integrals 同款 modern API:
        settings.results.report.surface_integrals.get_mass_weighted_avg(...)
    多路径 fallback 防 PyFluent 版本字段漂移.
    """
    sr = None
    try:
        sr = session.settings.results.report.surface_integrals
    except Exception:
        return None

    # 几种常见命名都试一下 (跨版本)
    for method_name in ("get_mass_weighted_avg", "get_mass_weighted_average",
                        "get_facet_avg", "get_area_weighted_avg"):
        method = getattr(sr, method_name, None)
        if method is None:
            continue
        try:
            r = method(
                surface_names=[OUTLET_FACE],
                surface_id=[],
                report_of="temperature",
            )
            v = _coerce_float(r)
            if v is not None and 200.0 <= v <= 3000.0:
                return v
        except Exception:
            try:
                # 不同版本参数名: report_of vs cell_function vs scalar
                r = method(
                    surface_names=[OUTLET_FACE],
                    cell_function="temperature",
                )
                v = _coerce_float(r)
                if v is not None and 200.0 <= v <= 3000.0:
                    return v
            except Exception:
                continue
    return None


def _coerce_float(r) -> Optional[float]:
    """统一 query API 返回值: float / dict / list / np scalar 都能取出第一个数."""
    if r is None:
        return None
    if isinstance(r, (int, float)):
        return float(r)
    if isinstance(r, dict) and r:
        return float(next(iter(r.values())))
    if isinstance(r, (list, tuple)) and r:
        return float(r[0])
    try:
        return float(r)
    except (TypeError, ValueError):
        return None


# ============================================================
# 自测试: python -m tools.fluent_wrapper_w2
# 跑两组 chip_power (15W vs 100W), 验"不同 query → 不同 max_temp".
# 这是 W2 区别于 W1 的核心硬指标 — chip_power 真在动温度.
# ============================================================

def _selftest() -> int:
    """
    [W2 vs W1 #14] selftest 判定逻辑变了
      W1: 比 outlet_avg_temp_K — 因为 max_temp 在 mixing-elbow 是边界温度钉死的,
          只有 outlet 平均温度会随 cold-inlet 速度变 (踩坑 #6)
      W2: 比 max_temp_K — chip_power 真生效之后, max_temp 直接随功率单调递增,
          这才是 W2 兑现 W1 known limit 的唯一证据
    返回 0 = PASS, 非 0 = FAIL.
    """
    cases = [
        {"chip_power_watts":  15.0, "inlet_velocity_ms": 1.0, "max_iterations": 50},
        {"chip_power_watts": 100.0, "inlet_velocity_ms": 1.0, "max_iterations": 50},
    ]
    print("=" * 60)
    print("W2 wrapper 自测试: chip_power 15W vs 100W (manifold)")
    print("=" * 60)

    results = []
    for i, p in enumerate(cases, 1):
        print(f"\n--- Case {i}/{len(cases)}: {p} ---")
        try:
            r = run_simulation(p)
        except Exception as e:
            print(f"[FAIL] case {i} 抛异常: {e}")
            traceback.print_exc()
            return 1
        print(f"  max_temp_K = {r['max_temp_K']}")
        print(f"  chip_power_applied = {r['chip_power_applied']}")
        print(f"  Q_volumetric = {r['chip_power_Q_volumetric']} W/m^3")
        results.append(r)

    if len(results) == 2:
        delta = abs(results[1]["max_temp_K"] - results[0]["max_temp_K"])
        print(f"\nΔ max_temp (100W - 15W): {delta:.2f} K")
        if delta > 1.0:
            print("[PASS] chip_power 真驱动 max_temp")
            return 0
        else:
            print("[FAIL] Δ < 1 K — chip_power 没真生效, 看 [WARN] 行")
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
